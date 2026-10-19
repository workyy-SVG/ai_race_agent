"""Exercise the repaired app's real generation/state paths with offline model fixtures."""
import ast
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import data_source as ds
import rag_engine as rag
from test_report_repairs import SOURCE, state_for

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent


def fixture_source():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DEEPSEEK_API_KEY" for t in node.targets):
            node.value = ast.Constant("offline-key")
        if isinstance(node, ast.FunctionDef) and node.name == "llm_once":
            node.body = ast.parse('''
st.session_state["_eval_extraction_context"] = messages
if action == "metrics_extract":
    return "{}"
return json.dumps(st.session_state.get("_eval_analysis", {"company": "贵州茅台", "summary": "数据不足。", "metrics": [], "chart_data": []}), ensure_ascii=False)
''').body
        if isinstance(node, ast.FunctionDef) and node.name == "llm_stream":
            node.body = ast.parse('''
st.session_state["_eval_generation_context"] = messages
answer = st.session_state.get("_eval_answer", "公司面临风险。")
placeholder.markdown(answer)
return answer
''').body
    return "import sys\nsys.path.insert(0, " + repr(str(ROOT)) + ")\n" + ast.unparse(ast.fix_missing_locations(tree))


def clean(at):
    assert not at.exception, [e.value for e in at.exception]
    failures = [e.value for e in at.error if "生成失败" in e.value or "分析失败" in e.value or "调用失败" in e.value]
    assert not failures, failures


def button(at, label):
    return next(b for b in at.button if b.label == label)


def run():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ANALYSIS_HISTORY_CACHE": str(Path(tmp) / "history.json")}), \
         patch.object(ds, "_get", return_value=None), patch.object(ds, "_post", return_value=None), \
         patch.object(ds, "get_financial_indicators", return_value=[]), patch.object(ds, "build_annual_trend", return_value=[]), \
         patch.object(rag, "has_index", return_value=True), patch.object(rag, "retrieve") as retrieve:
        at = AppTest.from_string(fixture_source(), default_timeout=120)
        at.session_state["authenticated"] = True
        at.session_state["use_dual_model"] = False
        for k, v in state_for().items():
            at.session_state[k] = v
        at.session_state["single_online_name"] = "贵州茅台"
        at.session_state["single_online"] = SOURCE
        at.session_state["single_rag_status"] = {"ok": True, "pages": 143, "chunks": 143}
        at.run()
        clean(at)

        # Same actual generation path: a ready index must not certify a citation-free draft.
        retrieve.return_value = [{"page": 122, "text": "可能面对的风险：信用风险与流动性风险。", "chunk_idx": 121}]
        at.session_state["_eval_answer"] = "# 测试研报\n公司面临风险。"
        button(at, "📝 生成完整研报").click().run()
        clean(at)
        audit = at.session_state["report_evidence"]
        report = at.session_state["report"]
        assert audit["status"] == "limited" and audit["refs"] == 0
        assert "本稿未给出具体页码引用" in report
        assert "本轮检索片段" in report and "信用风险与流动性风险" in report
        assert report in [m.value for m in at.markdown]
        assert "均来自全文检索并标注页码" not in " ".join(c.value for c in at.caption)
        print("PASS: report generation audits final body, exposes versioned sources, and renders saved export text")

        at.session_state["_eval_answer"] = "2025一季报净利率49.92%。见年报第122页。"
        question = "净利率与风险怎么样？"
        at.session_state["messages"] = [{"role": "user", "content": question}]
        at.session_state["_answer_pending"] = question
        at.run()
        clean(at)
        reply = at.session_state["messages"][-1]
        assert "2025一季报净利率49.92%" not in reply["content"]
        assert "2026一季报净利率 49.92%" in reply["content"]
        assert reply["retrieval"]["pages"] == [122]
        assert len(reply["retrieval"]["queries"]) == 3
        assert reply["citation_audit"]["matched"] == 1
        context = json.dumps(at.session_state["_eval_generation_context"], ensure_ascii=False)
        assert "报告期事实表" in context and "2026中报" in context and "6.61" in context
        print("PASS: followup uses scoped risk retrieval, shared PB basis, period facts and output checks")

        # Switch to unnamed manual prose while a real online index and old PDF still exist.
        at.session_state["single_src"] = "✍️ 手动粘贴 / 上传"
        at.session_state["single_text"] = "这是一段普通的测试文字，没有公司和财务信息。"
        at.run()
        clean(at)
        retrieve.reset_mock()
        button(at, "🚀 开始结构化分析").click().run()
        clean(at)
        retrieve.assert_not_called()
        assert at.session_state["analysis"]["company"] == "目标公司（材料未明确名称）"
        assert at.session_state["analysis"]["total_score"] is None
        assert "贵州茅台" not in json.dumps(at.session_state["_eval_extraction_context"], ensure_ascii=False)
        assert not at.session_state["evidence_items_last"]
        at.run()
        clean(at)
        assert at.session_state["analysis"]["company"] == "目标公司（材料未明确名称）"
        print("PASS: unnamed manual prose never retrieves old PDF, inherits an online identity, or loses its fresh result on rerun")

        text = "QA测试公司2025年报：营业收入10亿元；净利率-5%。"
        at.session_state["single_text"] = text
        at.session_state["_eval_analysis"] = {"company": "QA测试公司", "summary": "测试数据。", "metrics": [{"item": "2025年报营业收入", "value": 10, "unit": "亿元"}], "chart_data": []}
        at.run()
        button(at, "🚀 开始结构化分析").click().run()
        clean(at)
        cards = at.session_state["evidence_items_last"]
        assert cards[0]["found"] and cards[0]["page"] is None
        retrieve.assert_not_called()
        at.run()
        clean(at)
        assert at.session_state["analysis"]["company"] == "QA测试公司"
        print("PASS: QA metrics can match their own material but cannot acquire an old real-company page")

        # History navigation from comparison goes directly to the restored single-company view.
        at.session_state["main_view"] = "⚖️"
        at.run()
        clean(at)
        history_button = next(b for b in at.button if str(b.key).startswith("hist_btn_") and "QA测试公司" in b.label)
        history_button.click().run()
        clean(at)
        assert at.session_state["main_view"] == "🏢"
        assert at.session_state["analysis"]["company"] == "QA测试公司"
        print("PASS: selecting a single-company history on the comparison page opens that history directly")

        # Invalid-code errors remain beside the preserved input on subsequent reruns.
        at.session_state["single_src"] = "🌐 在线自动取数（推荐）"
        at.run()
        with patch.object(ds, "resolve_stock", return_value={"ok": False, "error": "证券代码不存在"}):
            at.text_input(key="single_kw").set_value("000000").run()
            at.button(key="single_fetch").click().run()
            clean(at)
            assert at.text_input(key="single_kw").value == "000000"
            assert any("证券代码不存在" in e.value for e in at.error)
            at.run()
            assert any("证券代码不存在" in e.value for e in at.error)
        print("PASS: invalid code clears stale results while preserving input and an actionable error")


if __name__ == "__main__":
    run()

"""Offline AppTest of real single/multi-company followup flows; no model calls."""
import ast
import copy
import datetime
import json
import os
import re
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import altair as alt
import data_source as ds
from streamlit.testing.v1 import AppTest

from test_followup_charts import company
import followup_charts as charts
import scoring

ROOT = Path(__file__).resolve().parent
os.environ["ANALYSIS_HISTORY_CACHE"] = str(Path(tempfile.gettempdir()) / "followup-chart-eval-history.json")
source = ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig"))
for node in source.body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DEEPSEEK_API_KEY" for t in node.targets):
        node.value = ast.Constant("offline-test-key")
    if isinstance(node, ast.FunctionDef) and node.name == "llm_stream":
        node.body = ast.parse('''
st.session_state["_eval_followup_model_messages"] = messages
if st.session_state.get("_eval_fail_model"):
    raise RuntimeError("offline simulated model failure")
placeholder.markdown("固定离线文字解读；图表数据来自程序取数。")
return "固定离线文字解读；图表数据来自程序取数。"
''').body
source = "import sys\nsys.path.insert(0, " + repr(str(ROOT)) + ")\n" + ast.unparse(ast.fix_missing_locations(source))


def check_app(at):
    assert not at.exception, [e.value for e in at.exception]


def run():
    a, b = company("贵州茅台", "600519"), company("五粮液", "000858")
    data = {"600519": a["rows"], "000858": b["rows"]}
    with patch.object(ds, "get_financial_indicators", side_effect=lambda code, **kw: copy.deepcopy(data.get(code, []))), \
         patch.object(ds, "build_annual_trend", return_value=[]), \
         patch.object(ds, "_get", return_value=None), patch.object(ds, "_post", return_value=None):
        at = AppTest.from_string(source, default_timeout=120)
        at.session_state["authenticated"] = True
        at.session_state["single_online_code"] = "600519"
        at.session_state["single_online_name"] = "贵州茅台"
        at.session_state["single_online"] = "贵州茅台年报摘要；报告期2024年报。"
        at.run()
        check_app(at)
        q = "近五年营收趋势怎么样？"
        at.session_state["messages"] = [{"role": "user", "content": q}]
        at.session_state["_answer_pending"] = q
        at.run()
        check_app(at)
        bundle = at.session_state["messages"][-1]["chart_bundle"]
        assert bundle["charts"][0]["metric"] == "revenue"
        assert bundle["charts"][0]["kind"] == "line"
        assert len(at.get("vega_lite_chart")) >= 1
        assert all(set(m) == {"role", "content"} for m in at.session_state["_eval_followup_model_messages"])
        assert "本轮可视化的核验数据" in json.dumps(at.session_state["_eval_followup_model_messages"], ensure_ascii=False)
        before = copy.deepcopy(bundle)
        data["600519"][-1]["revenue"] = 99999
        at.run()
        check_app(at)
        assert at.session_state["messages"][-1]["chart_bundle"] == before
        assert len(at.get("vega_lite_chart")) >= 1
        print("PASS: single-company source data -> matching chart -> model context -> historical snapshot")

        q2 = "2022年营收是多少？"
        at.session_state["messages"] = at.session_state["messages"] + [{"role": "user", "content": q2}]
        at.session_state["_answer_pending"] = q2
        at.run()
        check_app(at)
        assert not at.session_state["messages"][-1]["chart_bundle"]["charts"]
        assert "2022" in " ".join(at.session_state["messages"][-1]["chart_bundle"]["notes"])
        print("PASS: explicit missing year produces a data warning, not invented values")

        q_fail = "2024年ROE是多少？"
        at.session_state["_eval_fail_model"] = True
        at.session_state["messages"] = at.session_state["messages"] + [{"role": "user", "content": q_fail}]
        at.session_state["_answer_pending"] = q_fail
        at.run()
        check_app(at)
        assert at.session_state["messages"][-1]["chart_bundle"]["charts"][0]["metric"] == "roe"
        at.session_state["_eval_fail_model"] = False
        print("PASS: model failure preserves verified charts independently of text generation")

        at.session_state["main_view"] = "⚖️"
        at.run()
        check_app(at)
        q3 = "对比贵州茅台和五粮液的毛利率"
        at.session_state["cmp_scores"] = {"items": [
            {"name": c["name"], "code": c["code"], "industry": "消费", "period": "2024年报",
             "raw": c["rows"][-1], "annual": c["rows"], "scores": scoring.compute_scores(c["rows"][-1], "消费")}
            for c in (a, b)]}
        at.session_state["cmp_messages"] = [{"role": "user", "content": q3}]
        at.session_state["_cmp_answer_pending"] = q3
        at.run()
        check_app(at)
        snapshot = at.session_state["cmp_messages"][-1]["chart_bundle"]
        assert snapshot["charts"][0]["metric"] == "gross_margin"
        assert {v["公司"] for v in snapshot["charts"][0]["data"]} == {"贵州茅台", "五粮液"}
        assert {v["报告期"] for v in snapshot["charts"][0]["data"]} == {"2024年报"}
        assert len(at.get("vega_lite_chart")) >= 1
        print("PASS: multi-company followup renders same-year charts for both companies")

    # Validate Vega-Lite specs independently of Streamlit's renderer.
    for q in ("营收趋势", "2024年营收", "评分", "近五年净利润柱状图"):
        for c in charts.build_bundle(q, [a, b])["charts"]:
            alt.Chart.from_dict(charts.chart_spec(c))
    print("PASS: line, bar, score and grouped-time chart specifications validate")

    helper = next(n for n in ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig")).body
                  if isinstance(n, ast.FunctionDef) and n.name == "_followup_chart_bundle")
    ns = {"datetime": datetime, "re": re, "ds": ds, "scoring": scoring, "fcharts": charts,
          "st": SimpleNamespace(session_state={}), "POPULAR_STOCKS": []}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "followup_helper", "exec"), ns)
    with patch.object(ds, "get_financial_indicators") as fetch:
        manual = ns["_followup_chart_bundle"]("ROE", {"company": "手动公司"},
                 {"name": "手动公司", "code": "600519", "source": "manual"},
                 raw_data="手动公司2024年报，净资产收益率15%。")
        fetch.assert_not_called()
        assert manual["charts"][0]["data"][0]["公司"] == "手动公司"
    print("PASS: manual material never borrows the previously selected online company")


if __name__ == "__main__":
    run()

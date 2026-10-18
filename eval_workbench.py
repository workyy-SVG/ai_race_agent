"""Offline integration checks for the approved workbench, with fixture credentials/data.

Run: python eval_workbench.py. Never calls a model or writes the real usage ledger.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from contextlib import ExitStack
from types import SimpleNamespace
import os

from streamlit.testing.v1 import AppTest
import budget
import data_source as ds
import pipeline
import workbench_ui as wb


def check_app(at):
    assert not at.exception, [e.value for e in at.exception]


def run():
    with TemporaryDirectory(prefix="workbench-test-") as tmp, ExitStack() as mocks:
        mocks.enter_context(patch.dict(os.environ, {"ANALYSIS_HISTORY_CACHE": f"{tmp}/history.json"}))
        counts = {budget.BUCKET_LLM: 64, budget.BUCKET_POLISH: 8, budget.BUCKET_EMBED: 1726}
        mocks.enter_context(patch.object(budget, "read", side_effect=lambda bucket: counts.get(bucket, 0)))
        bump = mocks.enter_context(patch.object(budget, "bump"))
        resolve = mocks.enter_context(patch.object(ds, "resolve_stock", side_effect=lambda value:
            {"ok": True, "code": value, "name": f"测试公司{value}", "orgId": "fixture"}))
        summary = mocks.enter_context(patch.object(ds, "build_summary_text", return_value="测试财报：营业收入 100 亿元，净利润 10 亿元。"))
        mocks.enter_context(patch.object(ds, "list_reports", return_value=[]))
        mocks.enter_context(patch.object(pipeline, "all_annual_rows", return_value=[]))
        at = AppTest.from_file(str(Path(__file__).with_name("app.py")), default_timeout=90)
        at.secrets = {"APP_USER": "workbench-fixture", "APP_PASSWORD": "fixture-only-password",
                      "DEMO_KEY": "fixture-only-token", "DAILY_LLM_LIMIT": "300",
                      "DEEPSEEK_API_KEY": "fixture-only-api-key"}
        at.run()
        at.text_input(key="login_user").set_value("workbench-fixture")
        at.text_input(key="login_pw").set_value("fixture-only-password")
        next(b for b in at.button if b.label == "安全登录").click().run()
        check_app(at)
        # AppTest 1.57 keeps the pre-rerun login widgets in this one tree.
        # Supply their removed values for serialization; authentication still
        # goes through the normal submitted form above.
        at.session_state["login_user"] = "workbench-fixture"
        at.session_state["login_pw"] = ""
        at.button(key="nav_off_⚖️").click().run()
        at.toggle(key="cmp_auto_fetch").set_value(False).run()
        at.selectbox(key="cmp0_use_online").select(False).run()
        at.text_input(key="cmp0_name").set_value("保留的公司名")
        at.text_area(key="cmp0_text").set_value("营业收入 100 亿元，净利润 10 亿元").run()
        for number in (3, 4, 5):
            at.button(key="cmp_add").click().run()
            check_app(at)
            assert at.session_state["cmp_n"] == number
            assert at.session_state["cmp_focus_request"][0] == number - 1
            assert at.text_input(key="cmp0_name").value == "保留的公司名"
            assert at.text_area(key="cmp0_text").value.startswith("营业收入 100")
        assert at.button(key="cmp_add").disabled
        cards = [e for e in at.expander if "　公司 " in e.label]
        assert [e.proto.expanded for e in cards] == [True, True, False, False, False]
        at.text_input(key="cmp4_name").set_value("即将移除").run()
        for number in (4, 3, 2):
            at.button(key="cmp_del").click().run()
            assert at.session_state["cmp_n"] == number
        assert "cmp4_name" not in at.session_state.filtered_state
        assert at.button(key="cmp_del").disabled
        print("PASS: 2–5 company limits, stable values, independent expanders, removed state cleanup")

        at.toggle(key="use_dual_model").set_value(False).run()
        for theme, page_color in (("☁️ 浅色雅蓝", "#EDF2F9"),
                                  ("🏖️ 暖白砂岩", "#F5F2ED"),
                                  ("🪻 淡雾紫", "#F3F0F7")):
            at.selectbox(key="ui_theme").select(theme).run()
            check_app(at)
            assert at.text_input(key="cmp0_name").value == "保留的公司名"
            assert at.text_area(key="cmp0_text").value.startswith("营业收入 100")
            assert f"--page-bg:{page_color}" in at.markdown[0].value
            at.button(key="nav_off_📖").click().run()
            assert f"--page-bg:{page_color}" in at.markdown[0].value
            at.button(key="nav_off_⚖️").click().run()
            assert at.selectbox(key="ui_theme").value == theme
            assert f"--page-bg:{page_color}" in at.markdown[0].value
            assert at.toggle(key="use_dual_model").value is False
        # A persisted legacy choice migrates before the widget is rendered.
        legacy = AppTest.from_file(str(Path(__file__).with_name("app.py")), default_timeout=90)
        legacy.secrets = dict(at.secrets)
        legacy.session_state["ui_theme"] = "🔮 星云紫"
        legacy.run()
        check_app(legacy)
        assert legacy.session_state["ui_theme"] == "🪻 淡雾紫"
        assert at.text_input(key="cmp0_name").value == "保留的公司名"
        assert wb.history_height(1) == wb.history_height(2) == "content"
        assert wb.history_height(3) == wb.history_height(30) == 250
        at.button(key="nav_off_📖").click().run()
        assert not at.tabs
        help_markup = "\n".join(m.value for m in at.markdown)
        assert 'aria-label="使用流程图"' in help_markup
        assert 'aria-label="系统架构图"' in help_markup
        assert help_markup.index("全流程一图看懂") < help_markup.index("系统架构图") < help_markup.index("快速上手")
        at.button(key="nav_off_⚖️").click().run()
        assert at.selectbox(key="cmp0_use_online").value is False
        assert at.text_area(key="cmp0_text").value.startswith("营业收入 100")
        assert [t.label for t in at.tabs] == ["评分对照", "图表", "对比报告", "追问"]
        print("PASS: theme/help navigation preserves company fields; original help diagrams/order and result tabs retained")

        markup = "\n".join(m.value for m in at.sidebar.markdown)
        assert '<strong>36</strong> / 100' in markup
        assert 'width:36.00%' in markup and '1,726 批' in markup and '8 次' in markup
        assert 'aria-valuenow="0"' in wb.quota_html(101, 100, 8, 1726)
        assert 'aria-valuenow="100"' in wb.quota_html(-1, 100, 0, 0)
        bump.assert_not_called()
        print("PASS: real ledger counts, remaining bar, daily cap 100; layout changes never consume quota")

        at.selectbox(key="cmp0_use_online").select(True).run()
        at.selectbox(key="cmp_periods").select(12).run()
        at.text_input(key="cmp0_kw").set_value("600519").run()
        summary.reset_mock()
        at.button(key="cmp0_fetch").click().run()
        check_app(at)
        assert summary.call_args.kwargs["periods"] == 12
        assert at.session_state["cmp0_online_code"] == "600519"
        assert any("原始数据 / 年报 PDF" in e.label for e in at.expander)
        assert any(b.key == "cmp0_cautobtn" for b in at.button)
        print("PASS: per-company fetch honors shared periods and retains raw data/PDF indexing controls")

        # Returning to manual mode must not refetch an old online target.
        at.selectbox(key="cmp0_use_online").select(False).run()
        at.text_input(key="cmp1_kw").set_value("000858").run()
        resolve.reset_mock()
        at.toggle(key="cmp_auto_fetch").set_value(True).run()
        resolve.assert_not_called()
        check_app(at)
        print("PASS: manual input cannot silently trigger stale online fetching")

        # Exercise the unchanged streaming answer path with a deterministic
        # provider fixture, not just an empty/quota-blocked chat component.
        stream = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: iter([
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="离线回归回答："))]),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="请结合报告期核对毛利率。"))]),
            ]))))
        mocks.enter_context(patch("openai.OpenAI", return_value=stream))
        at.button(key="nav_off_🏢").click().run()
        at.session_state["single_online_code"] = "600519"
        at.session_state["single_online_name"] = "回归样本公司"
        at.session_state["single_online"] = "回归样本财报：营业收入 100 亿元，净利润 10 亿元。"
        at.run()
        at.session_state["analysis"] = {"company": "回归样本公司", "summary": "离线样本",
            "scores": {"盈利能力": 8, "成长性": 7, "偿债能力": 6, "现金流质量": 8},
            "total_score": 7.25, "metrics": [], "chart_data": [], "risks": [], "highlights": []}
        at.run()
        at.session_state["messages"] = [{"role": "user", "content": "毛利率如何？"}]
        at.session_state["_answer_pending"] = "毛利率如何？"
        at.run()
        check_app(at)
        assert not at.error, [e.value for e in at.error]
        assert at.session_state["messages"][-1]["role"] == "assistant"
        assert at.session_state["messages"][-1]["content"].startswith("离线回归回答：")
        assert not at.session_state.filtered_state.get("_answer_pending")
        assert len(at.chat_input) == 1
        bump.assert_called_once_with(budget.BUCKET_LLM, 1)
        print("PASS: streaming follow-up answer, message order, pending cleanup, input retained, exactly one usage event")


if __name__ == "__main__":
    run()

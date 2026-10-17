# -*- coding: utf-8 -*-
"""「🤖 Agent 轨迹」页签回归测试（无浏览器，AppTest 直跑）

为什么要单独测这一页
--------------------
新增页签会牵动 `_view_single()` 里 tabs 的数量，而 `_tab5_agent_trace()`
定义在别处、引用了 get_tracer() / agent_trace / pd 等在外层才绑定的名字 ——
只要有一个名字没定义，页面就会抛异常。回归测试若只笼统断言"页面无异常"，
很难定位到是这一页出的问题，所以这里把「页签存在 + 位置正确 + 台账自洽」
拆开逐条断言。

跑法：python eval_agent_trace.py   （不需要 API Key，不联网）
"""
from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(_HERE)          # app.py 里有基于 cwd / __file__ 的相对资源依赖
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from streamlit.testing.v1 import AppTest  # noqa: E402

FAIL: list[str] = []


def ck(cond, msg, extra=""):
    print(("  OK   " if cond else "  FAIL ") + msg + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAIL.append(msg)


def main() -> int:
    at = AppTest.from_file("app.py", default_timeout=120)
    at.run()
    ck(not at.exception, "未登录时无异常", [e.value for e in at.exception])

    # ---- 登录 ----
    _ti = list(at.text_input)
    ck(len(_ti) >= 2, "登录页有账号/密码输入框", len(_ti))
    _ti[0].set_value(os.getenv("APP_USER", "zzy"))
    _ti[1].set_value(os.getenv("APP_PASSWORD", "zzy2026"))
    _btns = [b for b in at.button if "登录" in (b.label or "")]
    ck(bool(_btns), "找到登录按钮", [b.label for b in at.button])
    if not _btns:
        return 1
    _btns[0].click().run()
    ck(not at.exception, "登录后无异常", [e.value for e in at.exception])

    # ---- 注入一份最小分析结果，逼出结果区（含新增的第 5 个页签）----
    # ⚠️ v10 起这里必须换一个**全新的已认证 AppTest**：
    #    上面那个实例的树里还残留登录页的 text_input 节点，而登录成功后
    #    Streamlit 会清理已消失控件的状态 —— 下一次 at.run() 在
    #    get_widget_states() 阶段就会 KeyError（AppTest 伪问题，真实浏览器
    #    没有旧树残留）。直接用 session_state 置登录态开新实例即可绕开。
    at2 = AppTest.from_file("app.py", default_timeout=120)
    at2.session_state["authenticated"] = True
    at2.session_state["analysis"] = {
        "company": "验证用公司",
        "summary": "仅用于渲染验证",
        "scores": {"盈利能力": 8.0, "成长性": 6.5, "偿债能力": 9.0, "现金流质量": 7.5},
        "total_score": 7.8,
        "score_engine": "规则引擎 v3",
        "score_source": "在线财务指标（2025年报）",
        "score_coverage": 1.0,
        "score_confidence": "充分",
        "metrics": [{"item": "2025年报营业收入", "value": 100.0, "unit": "亿元"}],
        "chart_data": [{"item": "2025年报营业收入", "value": 100.0}],
        "risks": ["风险一"],
        "highlights": ["亮点一"],
        "trend": [],
    }
    at2.session_state["analysis_data"] = "营业收入 100 亿元"
    at2.run()
    at = at2
    ck(not at.exception, "注入分析结果后无异常", [e.value for e in at.exception])

    # AppTest 会把一组 tabs 展平成 N 个 tab 对象，每个 label 是单独的字符串
    _labels = ["".join(t.label) for t in at.tabs]
    print("  页签：", _labels)
    ck(len(_labels) == 5, "结果区共 5 个页签（原 4 个 + 新增轨迹页）", len(_labels))
    ck(any("Agent 轨迹" in s for s in _labels), "出现「🤖 Agent 轨迹」页签", _labels)
    ck(bool(_labels) and _labels[-1].endswith("Agent 轨迹"),
       "轨迹页排在最后一位", _labels[-1] if _labels else "")

    # ---- 台账模块自洽性 ----
    import agent_trace  # noqa: E402

    tr = agent_trace.Tracer()
    with tr.span("在线取数", "巨潮 + 东财", inp="600519 · 8 期") as s:
        s.out = "取到财务摘要 3120 字符"
    tr.hit("规则引擎", "四维评分", out="9.4 / 10", kind="check")
    tr.hit("口径控制", "锁定年报口径", out="采用 2025年报", kind="decision")
    try:
        with tr.span("年报 RAG", "检索年报章节", inp="风险因素") as s2:
            raise RuntimeError("未建索引")
    except RuntimeError:
        pass

    _rows = tr.rows()
    ck(len(_rows) == 4, "台账记录 4 步", len(_rows))
    _show_cols = [k for k in _rows[0].keys() if not str(k).startswith("_")]
    ck(_show_cols == ["#", "阶段", "动作", "输入", "产出", "耗时", "状态", "备注"],
       "展示列为 8 个中文表头（内部字段一律 _ 前缀）", _show_cols)
    _sum = tr.summary()
    ck(_sum["decisions"] == 1, "决策点计数正确", _sum)
    ck(_sum["failed"] == 1, "异常步骤被记为失败而非静默消失", _sum)
    ck(len(agent_trace.decisions_of(tr)) == 1, "决策点可单独抽出")
    ck(tr.rows()[-1]["状态"].find("失败") >= 0, "失败状态有中文标签",
       tr.rows()[-1]["状态"])

    # 空台账时页面要有提示而不是空白
    _empty = agent_trace.Tracer().rows()
    ck(_empty == [], "空台账返回空列表（页面走 st.info 分支）")

    print("")
    if FAIL:
        print(f"FAILED: {len(FAIL)} 项")
        for f in FAIL:
            print("  -", f)
        return 1
    print("ALL_AGENT_TRACE_TESTS_PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

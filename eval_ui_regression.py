# -*- coding: utf-8 -*-
"""P1-01 ~ P1-06 页面回归测试（无浏览器 · 不联网 · 固定样本）。

覆盖优化清单里的六条 P1：
  P1-01 年度时间轴：年份范围筛选 / 指标切换 / 选年份→卡片同步 / 缺失年份不补零 / 比率用百分点 / CSV 导出
  P1-02 多公司对比：2 → 3 → 5 家动态增删；移除后控件与派生结果同步清理；重复/无效/未完成拦截
  P1-03 对比口径：各公司独立行业选择器；N 家对照表 + 极差；CSV 导出；共同年报年份对齐
  P1-04 金融子行业：行业选「金融」→ 银行/保险/证券选择器
  P1-05 证据卡：pipeline + evidence_map 正向定位到页码与原文
  P1-06 统一业务流程：app.py 调用的就是 pipeline.analyze_core

运行（不联网、不需要 key）：
    python eval_ui_regression.py
退出码 0 = 全部通过；1 = 有失败项（失败项会打印在最后）。
"""
import sys, io, os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
os.chdir(_HERE)

from streamlit.testing.v1 import AppTest
import data_source as ds
import analysis_validate as _av
import pipeline as _pl

APP = os.path.join(_HERE, "app.py")
SRC = io.open(APP, encoding="utf-8").read()
FAILS = []


def _check(name, cond, extra=""):
    print(("  OK  " if cond else "  FAIL") + " " + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


def _boot(view: str = "🏢"):
    """启动 AppTest。

    view：三栏工作台的导航选中项（"🏢" 单公司 / "⚖️" 多公司对比 / "📖" 使用说明）。
    改成三栏之后一次只渲染一个视图，测试哪一栏就必须先切过去 ——
    否则断言的是"没渲染出来的控件"，会误报成功能坏了。
    """
    at = AppTest.from_file(APP, default_timeout=240)
    at.run()
    at.session_state["authenticated"] = True
    at.session_state["main_view"] = view
    at.run()
    return at


def _ss(at, k, default=None):
    """AppTest 的 session_state 是 SafeSessionState，没有 .get() -> 用安全读取。"""
    try:
        return at.session_state[k]
    except Exception:
        return default


def _btn(at, key):
    return next((b for b in at.get("button") if (b.key or "") == key), None)


def _md(at):
    return "\n".join(m.value for m in at.markdown)


def _cap(at):
    return "\n".join(c.value for c in at.caption) + "\n" + _md(at)


def _imgs(at):
    return _md(at).count("data:image/png;base64,")


# ---------------------------------------------------------------------------
print("=== P1-01 年度时间轴（固定样本 · 不联网）===")
# 直接替换取数函数 → 确定性数据；返回顺序刻意与东财一致（最新年报在最前）
ds.build_annual_trend = lambda code, years=8: [
    {"year": "2024", "period": "2024年报", "report_date": "2024-12-31", "report_type": "年报",
     "revenue": 160.0, "net_profit": 22.0, "roe": 16.0, "gross_margin": 44.0},
    {"year": "2023", "period": "2023年报", "report_date": "2023-12-31", "report_type": "年报",
     "revenue": 140.0, "net_profit": 18.0, "roe": 14.0, "gross_margin": 42.0},
    {"year": "2021", "period": "2021年报", "report_date": "2021-12-31", "report_type": "年报",
     "revenue": 100.0, "net_profit": 10.0, "roe": 12.0, "gross_margin": 40.0},
]

at = _boot()
# ⚠️ 必须先注入「取数结果」跑一轮，让 data_sig 指纹稳定下来，
#    再注入 analysis。否则 app 的"检测到分析对象变化 → 清空旧结果"逻辑
#    会把刚 set 进去的 analysis 立刻 pop 掉（真实防御逻辑，不是 bug）。
at.session_state["single_online_code"] = "600519"
at.session_state["single_online_name"] = "贵州茅台"
at.session_state["single_online"] = "贵州茅台（600519）在线取数摘要文本"
at.run()
at.session_state["analysis"] = {
    "company": "贵州茅台", "summary": "s",
    "scores": {"盈利能力": 9.0, "成长性": 8.0}, "total_score": 8.5,
    "metrics": [{"item": "2025年报营业收入", "value": 100.0, "unit": "亿元"}],
    "chart_data": [], "risks": [], "highlights": [], "score_engine": "规则引擎 v3",
}
at.run()
_check("无异常", not [str(e.value)[:200] for e in at.exception],
       str([str(e.value)[:200] for e in at.exception]))
_c = _cap(at)
_check("出现「年度时间轴」区块", "年度时间轴" in _c)
_check("年份范围筛选器存在", at.get("select_slider") is not None
       and len(at.get("select_slider")) > 0)
_rd = {r.key: r for r in at.radio}
_check("指标切换器存在（4 个指标）",
       "timeline_metric" in _rd and len(list(_rd["timeline_metric"].options)) == 4,
       str(list(_rd.get("timeline_metric").options)) if "timeline_metric" in _rd else "缺")
_sb = {s.key: s for s in at.selectbox}
_check("年份选择器存在", "timeline_year" in _sb,
       str([k for k in _sb if k.startswith("timeline")]))
if "timeline_year" in _sb:
    _check("年份选项只有有数据的年份 2024/2023/2021（2022 缺失不补零）",
           sorted(list(_sb["timeline_year"].options)) == ["2021", "2023", "2024"],
           str(list(_sb["timeline_year"].options)))
_check("缺失年份明确提示且声明不补零", ("2022" in _c) and ("不补零" in _c))
_check("年度柱状图已渲染（内联 PNG）", _imgs(at) >= 1, f"imgs={_imgs(at)}")
_dl = [str(getattr(d, "label", "")) for d in at.get("download_button")]
_check("提供年度时间轴 CSV 导出", any("年度时间轴" in x for x in _dl), str(_dl))

_ms = {m.label: m for m in at.metric}
_roe_m = next((m for k, m in _ms.items() if "ROE" in k), None)
_check("年份卡片含 ROE", _roe_m is not None, str(list(_ms))[:160])
if _roe_m is not None:
    _check("ROE 变化用百分点（+2.00 pp）而非百分比",
           "pp" in str(_roe_m.delta), f"delta={_roe_m.delta}")
_rev_m = next((m for k, m in _ms.items() if "营业收入" in k), None)
if _rev_m is not None:
    _check("金额类变化用百分比（+14.3%）", "%" in str(_rev_m.delta) and "+14.3" in str(_rev_m.delta),
           f"delta={_rev_m.delta}")

# 切换年份 → 卡片同步
if "timeline_year" in _sb:
    _sb["timeline_year"].set_value("2021").run()
    _ms2 = {m.label: m for m in at.metric}
    _r21 = next((m for k, m in _ms2.items() if "营业收入" in k), None)
    _check("切到 2021 年后卡片同步为 100", _r21 is not None and "100" in str(_r21.value),
           f"value={getattr(_r21, 'value', None)}")
    _check("切换年份后无异常", not [str(e.value)[:200] for e in at.exception])
# 切换指标 → 图重绘
if "timeline_metric" in _rd:
    _rd["timeline_metric"].set_value("net_profit").run()
    _check("切换指标为「归母净利润」后仍有图", _imgs(at) >= 1)
    _check("切换指标后无异常", not [str(e.value)[:200] for e in at.exception])

# ---------------------------------------------------------------------------
print("=== P1-02 多公司对比：2 → 3 → 5 家动态增删 ===")
at2 = _boot(view="⚖️")
_check("页面初始无异常", not [str(e.value)[:200] for e in at2.exception],
       str([str(e.value)[:200] for e in at2.exception]))
_check("默认对比 2 家", _ss(at2, "cmp_n") == 2,
       f"cmp_n={_ss(at2, 'cmp_n')}")
_cb = {c.key: c for c in at2.checkbox}
_check("公司 A/B 输入区域存在", ("cmp0_use_online" in _cb) and ("cmp1_use_online" in _cb))
_sb2 = {s.key: s for s in at2.selectbox}
_check("两家公司各有独立行业选择器", ("cmp0_ind" in _sb2) and ("cmp1_ind" in _sb2),
       str([k for k in _sb2 if k.startswith("cmp")]))
_add = _btn(at2, "cmp_add")
_check("找到「➕ 添加公司」", _add is not None)
if _add:
    _add.click().run()
    _check("添加后 3 家", _ss(at2, "cmp_n") == 3)
    _check("第 3 家输入区域出现", "cmp2_use_online" in {c.key for c in at2.checkbox})
    _btn(at2, "cmp_add").click().run()
    _btn(at2, "cmp_add").click().run()
    _check("连续添加后 5 家", _ss(at2, "cmp_n") == 5)
    _check("达到上限 5 后「添加」禁用", bool(_btn(at2, "cmp_add").disabled))
    for _ in range(3):
        _btn(at2, "cmp_del").click().run()
    _check("连续移除后回到 2 家", _ss(at2, "cmp_n") == 2)
    _check("第 3 家输入区域已消失（表格/图表随之下线）",
           "cmp2_use_online" not in {c.key for c in at2.checkbox})
    _check("下限 2 家时「移除」禁用", bool(_btn(at2, "cmp_del").disabled))

# 重复公司 / 无效代码 / 未完成输入
at2.session_state["cmp0_online_code"] = "600519"
at2.session_state["cmp1_online_code"] = "600519"
at2.session_state["cmp0_online_name"] = "贵州茅台"
at2.session_state["cmp1_online_name"] = "贵州茅台"
at2.session_state["cmp0_online"] = "x"
at2.session_state["cmp1_online"] = "x"
at2.run()
_err = "\n".join(e.value for e in at2.error)
_check("重复公司给出明确报错", "重复公司" in _err, _err[:100])
# ⚠️ 不能按 "⚖️" 前缀找按钮了：三栏改版后左侧图标菜单里也有一个 "⚖️" 导航按钮，
#    按前缀会先命中它（而它永远可点）。这里按完整文案精确匹配。
_b = next((x for x in at2.get("button") if "开始对比分析" in str(x.label or "")), None)
_check("重复公司时「开始对比分析」禁用", bool(_b) and bool(_b.disabled))
at2.session_state["cmp1_online_code"] = ""
at2.run()
_w = "\n".join(x.value for x in at2.warning)
_check("未取到数据 → 警告（无效代码 / 未完成输入）",
       ("尚未成功取到数据" in _w) or ("输入未完成" in _w), _w[:120])

# ---------------------------------------------------------------------------
print("=== P1-03 对比口径与导出（3 家）===")
at2.session_state["cmp_scores"] = {
    "items": [
        {"name": "甲公司", "code": "600519", "industry": "消费", "period": "2025年报",
         "scores": {"scores": {"盈利能力": 9.0, "成长性": 7.0, "偿债能力": 8.0, "现金流质量": 6.0},
                    "total_score": 7.5, "coverage": 1.0},
         "raw": {"roe": 34.0, "gross_margin": 91.5, "debt_ratio": 18.0}},
        {"name": "乙公司", "code": "000858", "industry": "消费", "period": "2025年报",
         "scores": {"scores": {"盈利能力": 8.0, "成长性": 6.0, "偿债能力": 7.0, "现金流质量": 5.0},
                    "total_score": 6.5, "coverage": 0.9},
         "raw": {"roe": 25.0, "gross_margin": 75.0, "debt_ratio": 33.0}},
        {"name": "丙公司", "code": "600887", "industry": "消费", "period": "2025年报",
         "scores": {"scores": {"盈利能力": 7.0, "成长性": 5.0, "偿债能力": 6.0, "现金流质量": 4.0},
                    "total_score": 5.5, "coverage": 0.8},
         "raw": {"roe": 20.0, "gross_margin": 38.0, "debt_ratio": 52.0}},
    ],
    "cross_industry": False, "base_year": "2025", "mode": "年报同年份对齐",
}
at2.run()
_dl2 = [str(getattr(d, "label", "")) for d in at2.get("download_button")]
_check("出现「导出评分对照表（CSV）」", any("评分对照表" in x for x in _dl2), str(_dl2))
_tbl = "".join(str(d.value) for d in at2.dataframe)
_check("3 家公司的列都在表里", all(n in _tbl for n in ("甲公司", "乙公司", "丙公司")))
_check("N>2 时用「极差」列替代两两差额", "极差" in _tbl)
_check("对比可视化已渲染（分组柱 + 评分柱）", _imgs(at2) >= 3, f"imgs={_imgs(at2)}")
_check("写明统一按同一年报取数（不同年度不混比）",
       "年报" in _cap(at2) and ("base_year" in str(_ss(at2, "cmp_scores"))
                                or "2025" in _cap(at2)))
_check("3 家对比无异常", not [str(e.value)[:200] for e in at2.exception],
       str([str(e.value)[:200] for e in at2.exception]))

# ---------------------------------------------------------------------------
print("=== P1-04 金融子行业口径 ===")
at3 = _boot()
_rd3 = {r.key: r for r in at3.radio}
_ind = _rd3.get("industry_widget")
_check("找到行业选择器", _ind is not None)
if _ind is not None:
    _check("选金融前没有子行业选择器",
           "single_finance_kind_widget" not in {r.key for r in at3.radio})
    _ind.set_value("金融").run()
    _rd3 = {r.key: r for r in at3.radio}
    _fk = _rd3.get("single_finance_kind_widget")
    _check("选「金融」后出现银行/保险/证券选择器", _fk is not None)
    if _fk is not None:
        _opts = " ".join(str(o) for o in _fk.options)
        _check("选项覆盖 银行 / 保险 / 证券", all(k in _opts for k in ("银行", "保险", "证券")),
               _opts[:120])
        _fk.set_value("📈 证券").run()
        _check("选择写入 session_state（供评分引擎使用）",
               _ss(at3, "single_finance_kind") == "证券",
               str(_ss(at3, "single_finance_kind")))
    _check("金融口径下无异常", not [str(e.value)[:150] for e in at3.exception])

# ---------------------------------------------------------------------------
print("=== P1-05 / P1-06 统一业务流程与证据卡 ===")
_check("app.py 调用 pipeline.analyze_core（页面与评测同一条链路）",
       "_pipeline.analyze_core(" in SRC)
_check("app.py 以健壮方式引入 pipeline（三级加载：常规 import → 绝对路径 → 兜底）",
       "def _load_pipeline()" in SRC and "_load_pipeline()" in SRC)
_check("app.py 不再静默吞掉 pipeline 的加载异常（防回归）",
       "except ImportError:\n    _pipeline = None" not in SRC
       and "_PIPELINE_ERROR" in SRC)
_check("app.py 渲染证据卡（正向定位页码/原文）", "证据卡" in SRC)
_check("app.py 有年度时间轴区块", "render_year_timeline(" in SRC)
_check("app.py 有 N 公司对比图函数调用", "fig_compare_group_n(" in SRC)
_check("app.py 有「无共同年报年份」明确提示", "无共同年报年份" in SRC)
_check("evidence_map 正向定位可用", callable(getattr(_av, "evidence_map", None)))
_res = _pl.analyze_core(
    {"metrics": [{"item": "2025年报营业收入", "value": 60.59}],
     "summary": "见年报第 12 页", "risks": [], "highlights": []},
    raw_data="营业收入 100.5 亿元", industry="消费", online=False,
    rag_hits=[{"page": 42, "text": "营业收入 6,058,908,000.00 元"}], rag_pages=220,
)
_check("pipeline 端到端：证据核对通过", _res["evidence_issues"] == [], str(_res["evidence_issues"]))
_check("pipeline 端到端：证据卡定位到第 42 页",
       _res["evidence_items"] and _res["evidence_items"][0].get("page") == 42)
_check("pipeline 端到端：引用正确率 100%", _res["citation"]["rate"] == 1.0)

# ---------------------------------------------------------------------------
print("=== P2-01 三栏工作台：图标菜单 / 分析记录 / 页签化 ===")

# 1) 左列图标菜单
at4 = _boot()
_navs = {b.key: b for b in at4.get("button") if (b.key or "").startswith("nav_btn_")}
_check("左列图标菜单三个入口都在（单公司/对比/说明）", len(_navs) == 3, str(sorted(_navs)))
_check("默认停在单公司视图", _ss(at4, "main_view", "🏢") == "🏢")
_check("单公司视图渲染了行业基准选择器",
       any((r.key or "") == "industry_widget" for r in at4.radio))

# 2) 切到对比视图（一次只渲染一个视图，切过去才该有对比控件）
at4.session_state["main_view"] = "⚖️"
at4.run()
_check("切到「多公司对比」后出现对比控件",
       any((c.key or "") == "cmp0_use_online" for c in at4.checkbox),
       str([c.key for c in at4.checkbox][:6]))

# 3) 切到使用说明
at4.session_state["main_view"] = "📖"
at4.run()
_check("切到「使用说明」后渲染说明正文", "使用说明" in _md(at4))

# 4) 中列分析记录：空态 + 有记录
at5 = _boot()
_check("无记录时中列给出占位说明", "还没有分析记录" in _cap(at5))
_check("中列明确声明不持久化（刷新即清空）",
       ("刷新页面即清空" in _cap(at5)) or ("刷新即清空" in _cap(at5)))
at5.session_state["analysis_history"] = [
    {"id": "t1", "code": "600519", "name": "贵州茅台", "industry": "消费",
     "total": 8.5, "ts": "12:00:00", "analysis": {}},
]
at5.run()
_check("有记录后中列出现可点击条目",
       any((b.key or "").startswith("hist_btn_") for b in at5.get("button")),
       str([b.key for b in at5.get("button") if "hist" in (b.key or "")]))

# 5) 页签化：源码层面确认结果区拆成四个子页签
_check("结果区已页签化（概览/时间轴/评分与依据/研报）",
       all(t in SRC for t in ("📊 分析概览", "🗓️ 年度时间轴", "🧮 评分与依据", "📝 研报")))
_check("页签内容由独立 _tab*_ 函数承载（避免为改布局批量改缩进）",
       SRC.count("def _tab1_") >= 1 and "def _tab4():" in SRC)

# 6) 大横幅已下线：hero 只剩登录页一处，主页面改用紧凑顶栏
_check("主页面不再渲染三行大横幅（hero 仅登录页保留）",
       SRC.count('<div class="hero"') == 1, str(SRC.count('<div class="hero"')))
_check("主页面改用紧凑顶栏（topbar）", 'class="topbar"' in SRC)

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL_P1_TESTS_PASSED")

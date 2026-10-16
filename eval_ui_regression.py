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
# 主题配置也纳入源码断言：v7 的"整体字号偏小"是从根字号（baseFontSize）治的，
# 这一条一旦被改回去，原生控件会整体缩回 16px 基准，页面看起来又"小一号"。
_CFG = io.open(os.path.join(_HERE, ".streamlit", "config.toml"), encoding="utf-8").read()
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
print("=== P2-01 侧边栏导航（图标+文字）/ 分析记录 / 结果页签化 ===")

# 1) 侧边栏导航菜单：图标 + 文字
# ⚠️ v7 起 key 变成 nav_on_<图标> / nav_off_<图标>：选中态靠**应用自己控制的 key
#    类名**（CSS 钩 st-key-nav_on_*）而不是 Streamlit 的 kind 属性 ——
#    styled-components v6 不保证把非标准属性写到 DOM 上。断言顺手守住这个命名约定。
at4 = _boot()
_navs = {b.key: b for b in at4.get("button")
         if (b.key or "").startswith(("nav_on_", "nav_off_"))}
_check("导航三个入口都在（单公司/对比/说明）", len(_navs) == 3, str(sorted(_navs)))
_check("导航按钮 key 带 on/off 状态位（CSS 靠它钩选中态，不依赖 kind 属性）",
       sum(1 for k in _navs if str(k).startswith("nav_on_")) == 1,
       str(sorted(_navs)))
_check("导航按钮全部落在侧边栏（主区不再占一列）",
       len([b for b in at4.sidebar.get("button")
            if (b.key or "").startswith(("nav_on_", "nav_off_"))]) == 3,
       str([b.key for b in at4.sidebar.get("button")]))
_check("导航按钮是「图标 + 文字」而不是纯图标",
       all(len(str(b.label or "").strip()) > 3 for b in _navs.values()),
       str([str(b.label) for b in _navs.values()]))
_check("默认停在单公司视图", _ss(at4, "main_view", "🏢") == "🏢")
_check("当前选中项高亮（type=primary，侧边栏窄放不下文字标记）",
       sum(1 for b in _navs.values() if b.proto.type == "primary") == 1,
       str([(str(b.label), b.proto.type) for b in _navs.values()]))
_check("单公司视图渲染了行业基准选择器",
       any((r.key or "") == "industry_widget" for r in at4.radio))
_check("侧边栏不再摆「快速上手 / 当前版本能力」静态卡片（让位给高频导航）",
       ("快速上手" not in _md(at4)) and ("当前版本能力" not in _md(at4)))

# 2) 切到对比视图（一次只渲染一个视图，切过去才该有对比控件）
at4.session_state["main_view"] = "⚖️"
at4.run()
_check("切到「多公司对比」后出现对比控件",
       any((c.key or "") == "cmp0_use_online" for c in at4.checkbox),
       str([c.key for c in at4.checkbox][:6]))
_check("对比视图**不再**出现单公司行业选择器（v4 把这块收进单公司视图）",
       not any((r.key or "") == "industry_widget" for r in at4.radio),
       str([r.key for r in at4.radio]))
_check("对比视图也不再出现单公司同业对标基准条",
       "同业对标基准（" not in _md(at4))

# 3) 切到使用说明：原侧边栏「快速上手」4 步并入这里，内容不丢
at4.session_state["main_view"] = "📖"
at4.run()
_check("切到「使用说明」后渲染说明正文", "使用说明" in _md(at4))
_check("「使用说明」承接了快速上手 4 步（内容没丢，只是换了地方）",
       "快速上手" in _md(at4))
_check("使用说明页同样不出现单公司取数/行业选择器",
       not any((r.key or "") == "industry_widget" for r in at4.radio))
_check("「使用说明」记录了侧边栏导航入口", "左侧边栏" in _md(at4))

# 4) 侧边栏分析记录：空态 + 有记录
at5 = _boot()
_check("无记录时给出占位说明", "还没有分析记录" in _cap(at5))
_check("明确声明不持久化（刷新即清空）",
       ("刷新页面即清空" in _cap(at5)) or ("刷新即清空" in _cap(at5)))
at5.session_state["analysis_history"] = [
    {"id": "t1", "code": "600519", "name": "贵州茅台", "industry": "消费",
     "total": 8.5, "ts": "12:00:00", "analysis": {}},
]
at5.run()
_hbs = [b for b in at5.sidebar.get("button") if (b.key or "").startswith("hist_btn_")]
_check("有记录后侧边栏出现可点击条目（不再占主区一列）", len(_hbs) == 1,
       str([b.key for b in at5.sidebar.get("button") if "hist" in (b.key or "")]))
_check("记录条目显示「公司名 · 代码」",
       bool(_hbs) and "贵州茅台" in str(_hbs[0].label) and "600519" in str(_hbs[0].label),
       str(_hbs[0].label) if _hbs else "—")


# 5) 页签化：源码层面确认结果区拆成四个子页签
_check("结果区已页签化（概览/时间轴/评分与依据/研报）",
       all(t in SRC for t in ("📊 分析概览", "🗓️ 年度时间轴", "🧮 评分与依据", "📝 研报")))
_check("页签内容由独立 _tab*_ 函数承载（避免为改布局批量改缩进）",
       SRC.count("def _tab1_") >= 1 and "def _tab4():" in SRC)

# 6) 大横幅已下线（P3-01 后连登录页也不再使用 hero）
_check("全站不再使用 100px 大横幅（hero 已下线）",
       SRC.count('class="hero"') == 0, str(SRC.count('class="hero"')))
_check("主页面改用紧凑顶栏（topbar）", 'class="topbar"' in SRC)

# 6b) 配色收敛 + 分区标题统一
# v6：底色从 v5 的"浅冷灰"再降一档到"浅蓝灰"——用户反馈 v5 跑起来"整体还是白色，
#     布局也没变"。底色必须明显带蓝，白卡才浮得起来（#334155 压在 #DAE6F6 上 8.3:1）。
_check("页面底色为浅蓝灰（v6 再降一档：白卡靠明度差浮起来，而不是靠描边堆）",
       "linear-gradient(180deg, #EDF3FC 0%, #E3EDF9 48%, #DAE6F6 100%)" in SRC)
_check("全站不再使用金色文字（金 #D9A441 压白底只有 2.25:1，等于隐形）",
       "#F5C563" not in SRC)
_check("统一分区标题组件存在（step_head：编号方块 + 短标题 + 灰色说明）",
       "def step_head(" in SRC and 'class="step-head"' in SRC)
_check("单公司流程 1~5 步齐备（先行业 → 再数据来源 → 取数 → 补充 → 运行）",
       all(k in SRC for k in ('card("kb", "1"', 'card("src", "2"', 'card("fetch", col=',
                              'card("extra", "4"', 'card("run", "5"')) and 'step_head("3"' in SRC,
       str([k for k in ('card("kb", "1"', 'card("src", "2"', 'card("fetch", col=',
                        'card("extra", "4"', 'card("run", "5"', 'step_head("3"')
            if k not in SRC]))

# 6c) v5 结构层：页面大标题 + 真正的卡片（用户：主界面还是很乱 → 要一个合理规划）
_check("页面大标题组件存在（page_head：25px 标题 + 品牌蓝短横 + 灰说明）",
       "def page_head(" in SRC and 'class="page-head"' in SRC)
_check("小节标题组件存在（sec_head：19px + 左侧蓝竖条，用于页面级分段）",
       "def sec_head(" in SRC and 'class="sec-head"' in SRC)
_check("卡片容器组件存在（card：st.container(key=kcard-*) → 一步一张白卡）",
       "def card(" in SRC and 'key=f"kcard-{key}"' in SRC)
_check("卡片样式挂在 st-key- 前缀类名上（Streamlit 1.57 唯一稳定的容器挂钩点）",
       '[class*="st-key-kcard-"]' in SRC)
_check("卡片不用已失效的 testid 挂钩（stVerticalBlockBorderWrapper 在 1.57 命中 0）",
       'data-testid="stVerticalBlockBorderWrapper"' not in SRC)
_check("step-head 降级成卡片内表头（去底色描边、只留一条分隔线 → 不盒子里套盒子）",
       "border-bottom: 1px solid #EDF2F8" in SRC)
_check("三个视图都有页面大标题（单公司 / 多公司 / 使用说明）",
       all(t in SRC for t in ('"单公司深度分析",', '"多公司对比",', '"使用说明",')))
_check("多公司对比做了页面级分段（1. 对比公司管理 / 2. 开始对比分析）",
       'sec_head("1", "对比公司管理"' in SRC and 'sec_head("2", "开始对比分析"' in SRC)
_check("对比页每家公司一张白卡（三列各一张，卡片边界即公司边界）",
       'with card(f"cmp{_i}")' in SRC and "st.columns(_n_cmp)" in SRC)
_check("结果区四个页签的面板也做成卡片（CSS 补底色，不多包一层容器）",
       '.stTabs [data-baseweb="tab-panel"]' in SRC)
_check("使用说明页卡片化（5 张分类卡：快速上手/主要能力/基准库/数据来源/技术说明）",
       all(k in SRC for k in ('card("help-quick"', 'card("help-cap"', 'card("help-kb"',
                              'card("help-src"', 'card("help-tech"')))
# 追问区的 chat_input 一旦被容器包住，就会从"钉在视口底部"退回"内联渲染"（用户明确要钉底）
_CHAT_ZONE = SRC.split("# ---- 追问对话（多轮，上下文实时更新） ----", 1)[-1]
_CHECK_ZONE = _CHAT_ZONE.split("_typed = st.chat_input", 1)[0]
_check("追问区没有被包进卡片容器（chat_input 的父容器一变就丢钉底）",
       "with card" not in _CHECK_ZONE)
_check("追问区也没有被塞进多栏网格（列也算容器，进列同样丢钉底）",
       "col=_single_grid" not in _CHECK_ZONE)
# 实测（streamlit/elements/widgets/chat.py）：判定条件是
# `root_container == MAIN and not _ancestor_block_types`。探针验证过"根层先建
# st.columns 多栏网格、卡片进列、之后再调 chat_input"时 ancestor_block_types 仍为空，
# 所以网格与钉底可以共存 —— 但这条前提一旦被改（比如把追问区包进 with col:）就会退化。

# 6d) v6：深蓝侧边栏 + 多行卡片网格（用户：背景还是白的、布局没变）
_check("侧边栏改深蓝渐变（模板里的深蓝导航条）",
       "linear-gradient(180deg, #0E2A5E 0%, #123A80 46%, #16489E 100%)" in SRC)
_check("侧栏内层容器透明（否则 Streamlit 主题底色会把渐变盖住）",
       'section[data-testid="stSidebar"] [data-testid="stSidebarContent"]' in SRC
       and 'section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"]' in SRC)
_check("深底文字全部显式上色且对比度写进注释（深底最怕漏一处 → 深字压深底）",
       all(c in SRC for c in ("#EAF4FF", "#AFD2F5", "#7FE9FF")) and "#EAF4FF 7.69:1" in SRC)
# ⚠️ 反向守卫：侧栏文字列表里**绝不能出现 span** —— 选中态导航按钮是"白底深蓝字"，
#    一旦通配到 span 就会被刷成浅色，变成白底白字（v3 那次"字看不见"的同型 bug）。
_check("侧栏文字选择器没有通配到 span（否则选中态白底白字）",
       'section[data-testid="stSidebar"] span' not in SRC)
_check("分析记录滚动槽有 key（深底要单独刷凹槽，1.57 没有旧 wrapper testid 可挂）",
       'st.container(height=210, key="khist-scroll")' in SRC
       and '[class*="st-key-khist-scroll"]' in SRC)
_check("card() 支持 col 参数（挂进某列 = 零缩进改动参与多栏）",
       "def card(" in SRC and "col=None" in SRC and "col.container(key=f\"kcard-{key}\")" in SRC)
# ---------------------------------------------------------------------------
# 6e) v7：字号 / 可读性 / 图形 / 侧栏选中态（用户的四条新反馈）
print("=== P2-04 v7：字体放大 / 可读性 / 图形装饰 / 侧栏选中态 ===")

# ① 字体偏小 → 根字号 + 自定义类 px 双管齐下
_check("config.toml 抬高了根字号 baseFontSize（原生组件整体放大）",
       "baseFontSize = 17" in _CFG,
       "（config.toml 里没找到 baseFontSize）")
_check("自定义类的 px 字号同步上调（px 不随根字号变，必须手改）",
       "font-size: 30px; font-weight: 800; color: #0B1F3A !important;" in SRC   # .ph-title
       and ".step-head .sh-title { font-size: 17px;" in SRC
       and "width: 23px; height: 23px; border-radius: 7px;" in SRC)              # .sh-num
_check("登录页字号同步放大（标题 36px / 按钮 17px）",
       ".login-brand .lb-title {" in SRC and "font-size: 36px" in SRC
       and "font-size: 17px !important;" in SRC)

# ② 有些字看不清 → 辅色从 #64748B（浅蓝灰底上仅 3.77:1）换成 #475569 / #3B4A5F
_check("主区辅色不再用 #64748B 当文字色（画布上只有 3.77:1，不合格）",
       "color: #64748B" not in SRC and "color:#64748B" not in SRC)
_check("说明类文字统一提到 #3B4A5F（白卡 9.0:1 / 画布 7.14:1）",
       "#3B4A5F" in SRC and "small, .stCaption, [data-testid=\"stCaptionContainer\"]" in SRC)
_check("深蓝侧栏文字提亮到 #EAF4FF / #AFD2F5（7.69:1 / 5.44:1）",
       "color: #EAF4FF !important;" in SRC and "color: #AFD2F5 !important;" in SRC)

# ③ 侧栏选中态：改用应用自己控制的 key 类名（不依赖 styled-components 是否转发 kind）
_check("导航 key 带 on/off 状态位（选中态的结构来源由应用自己掌握）",
       'key=f"nav_{\'on\' if _on else \'off\'}_{_icon}"' in SRC)
_check("选中态 CSS 钩 st-key-nav_on_*（1.57 里唯一稳定的容器/元素挂钩点）",
       '[class*="st-key-nav_on_"] button' in SRC)
_check("选中态「白底 + 深蓝字」写在同一条规则组里（不可能只生效一半 → 不会变全白）",
       'section[data-testid="stSidebar"] [class*="st-key-nav_on_"] button,' in SRC
       and 'section[data-testid="stSidebar"] [class*="st-key-nav_on_"] button *' in SRC
       and "color: #14377A !important;" in SRC)
_check("选中态含按钮内所有子元素（否则子元素会被侧栏浅色规则刷成浅色）",
       'button * {' in SRC)
_check("旧的 button[kind=primary] 仍保留为兼容兜底（同色，不冲突）",
       'section[data-testid="stSidebar"] .stButton > button[kind="primary"],' in SRC
       and '[data-testid="stBaseButton-primary"]' in SRC)

# ④ 界面太空旷 → 主区补图形（页面徽记 / 顶栏六边形 / 侧栏底纹 / 空状态插图）
_check("页面大标题右侧有科技徽记（同心环 + 六边形 + 迷你折线，纯内联 SVG）",
       'class="ph-deco"' in SRC and 'class="page-head"' in SRC
       and "M84 18 L106 31 L106 63 L84 76 L62 63 L62 31 Z" in SRC)
_check("顶栏左侧用纯 CSS 六边形徽标替代 emoji（.tb-logo）",
       'class="tb-logo"' in SRC and ".tb-logo {" in SRC
       and "clip-path: polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%)" in SRC)
_check("侧边栏底部加了同心圆 + 刻度环装饰（pointer-events:none，不挡点击）",
       'class="sb-deco"' in SRC and ".sb-deco i:nth-child(2)" in SRC
       and "pointer-events: none" in SRC)
_check("未取数时展示空状态插图，而不是一行 info（卡片不再空着）",
       'class="empty-art"' in SRC and 'class="ea-title"' in SRC and ".empty-art {" in SRC)
_check("卡片编号方块改成青蓝渐变 + 外发光（不再是一块死蓝）",
       "background: linear-gradient(145deg, #38BDF8 0%, #1D4ED8 100%);" in SRC
       and "box-shadow: 0 2px 9px rgba(29, 78, 216, 0.32);" in SRC)

# ⑤ 单公司工作栏排版：三栏 → 两行（用户：从左往右乱乱的）
_check("第 1 行网格按 ① ② ③ 排列（① 行业基准 / ② 数据来源 / ③ 数据获取）",
       '_single_grid = st.columns([1, 1, 1.15], gap="medium")' in SRC
       and "col=_single_grid[0]" in SRC and "col=_single_grid[1]" in SRC
       and "col=_single_grid[2]" in SRC)
_check("第 2 行网格独立创建，且落在 ③ 之后、④ 之前（创建顺序 = 视觉先后）",
       '_single_grid_r2 = st.columns([2, 1], gap="medium")' in SRC
       and SRC.index('with card("fetch", col=_single_grid[2])')
           < SRC.index("_single_grid_r2 = st.columns")
           < SRC.index('tag="可选", col=_single_grid_r2[0]'))
_check("④ 补充材料在第 2 行左栏（宽 2/3）",
       'tag="可选", col=_single_grid_r2[0]' in SRC)
_check("⑤ 运行分析按模式落栏（在线 → 右栏 / 手动 → 左栏，不留一大片空位）",
       "_run_col = _single_grid_r2[1] if src_mode.startswith(\"🌐\") else _single_grid_r2[0]" in SRC
       and 'tag="出结果", col=_run_col' in SRC)
_check("② 数据来源占用第 1 行第 2 栏（不再和 ① 挤同一栏）",
       'tag="二选一", col=_single_grid[1]' in SRC)
_check("手动模式的 ③ 也进了第 1 行第 3 栏（与在线模式同位置）",
       SRC.count("col=_single_grid[2]") == 2)

# 标题被第一张卡压在下面 → 整页看上去还是"一堆卡片摊着"（用户：布局没变）。
# 第 1 步「行业财务基准库」在模块级先渲染，所以标题必须也提到模块级、且在网格之前。
_check("单公司页面大标题移到模块级（否则渲染顺序变成「卡 1 → 标题 → 卡 2…」）",
       SRC.index('"单公司深度分析",') < SRC.index('_single_grid = st.columns([1, 1, 1.15]')
       and SRC.count('"单公司深度分析",') == 1)
_check("网格在标题之后创建（标题必须是最上面的元素）",
       SRC.index('page_head(\n        "单公司深度分析"')
       < SRC.index('_single_grid = st.columns([1, 1, 1.15]'))
_check("_view_single 内部不再重复渲染页面标题（避免出现两个大标题）",
       '"单公司深度分析",' not in SRC.split("def _view_single()", 1)[1].split("def _view_compare", 1)[0])

# 7) 布局搬迁的结构性守卫（这两条是最容易踩的坑）
_check("导航/记录函数定义在其调用点之前（模块级调用先于定义会 NameError）",
       SRC.index("def _render_icon_menu") < SRC.index("\n    _render_icon_menu()")
       and SRC.index("def _render_history_list") < SRC.index("\n    _render_history_list()"))
_check("侧边栏默认展开（导航在里面，默认折叠会让主区看起来像空白）",
       'initial_sidebar_state="expanded"' in SRC)
_check("主区不再用 st.columns 模拟「图标列 + 记录列」",
       "_nav_col, _hist_col, _work_col" not in SRC)
_check("旧的中列函数名已清理（避免留下两套实现）",
       "_render_history_column" not in SRC)

# ---------------------------------------------------------------------------
print("=== P3-01 登录页（v6 深蓝科技版）===")

# 1) 源码层：样式只在未登录分支注入
_check("登录页样式常量存在（LOGIN_CSS）", "LOGIN_CSS = " in SRC)
_check("LOGIN_CSS 只在未登录分支注入（登录后不得残留登录页样式）",
       SRC.count("st.markdown(LOGIN_CSS") == 1
       and SRC.index("def check_login") < SRC.index("st.markdown(LOGIN_CSS"))
_check("登录页深蓝科技底（按用户给的登录页模板：深蓝渐变 + 青蓝辉光）",
       "linear-gradient(160deg, #061428 0%, #0A2450 46%, #0F3272 100%)" in SRC
       and "radial-gradient(circle at 20% 26%" in SRC)
_check("登录页不再用 v4/v5 的浅底（浅蓝灰渐变不得同时出现）",
       "linear-gradient(180deg, #FFFFFF 0%, #F7F9FC" not in SRC)
# v6 换回深底的理由与 v4 放弃深底的理由是同一件事：深底要求每个文字元素都显式上色 +
# !important，漏一个就掉进全站 p/li/span/div{color:#334155} → 深字压深底 ≈1.0:1。
# 所以这条守的是"对比度必须**被算过**并写进注释"，而不是"必须用浅底"。
_check("深底文字全部显式上色 + 对比度量化写进注释（漏一处就是一片看不清）",
       all(c in SRC for c in ("#EAF2FF", "#CBDCF6", "#A9C0E6", "#8FA9D4"))
       and "L=0.036" in SRC and "5.1:1" in SRC)
_check("登录前隐藏侧边栏（导航/额度/退出登录都不该出现）",
       'section[data-testid="stSidebar"] { display: none !important; }' in SRC)
_check("深色玻璃卡 = 登录表单本体（半透明深底 + 青色顶条 + 大投影）",
       '[data-testid="stForm"]' in SRC and "rgba(8, 22, 48, 0.78) !important" in SRC
       and "border-top: 3px solid #22D3EE" in SRC)
_check("登录按钮 = 青色渐变 + 深蓝字（模板「安全登录」的形态，青底最亮端 8.8:1）",
       '[data-testid="stFormSubmitButton"] button' in SRC
       and "linear-gradient(90deg, #22D3EE 0%, #38BDF8 100%) !important" in SRC
       and "color: #052438 !important" in SRC)
_check("输入框在深底上不再是白底深字（改半透明底 + 浅字 + 浅占位符）",
       "background: rgba(255, 255, 255, 0.07) !important" in SRC
       and "color: #A9C0E6 !important" in SRC)
_check("卡内报错提示在深色卡上可见（否则红字压深底看不见）",
       '[data-testid="stForm"] [data-testid="stAlert"]' in SRC)
_check("账号/密码标签**外置**，框内放小号英文占位符",
       'st.text_input("账号", placeholder="Account")' in SRC
       and 'st.text_input("密码", type="password", placeholder="Password")' in SRC)
_check("占位符字号小于外置标签（13px < 15.5px，框内更轻）",
       "input::placeholder" in SRC
       and "font-size: 13px !important" in SRC
       and "font-size: 15.5px !important" in SRC)
_check("品牌区用 div 而非 h1（避开 Streamlit 锚点图标 + 全站标题样式覆盖）",
       'class="lb-title"' in SRC and 'class="login-brand"' in SRC)
_check("品牌区有发光六边形 AI 徽标（CSS 画的，不需要图片资源）",
       'class="lb-orb"' in SRC and "clip-path: polygon(50% 0%" in SRC)

# v7：登录页科技装饰层（用户："不要只给我一个登录的账号…要不然给人的感觉太空旷"）
_check("登录页加了装饰层容器（fixed + pointer-events:none，不挡点击）",
       'class="lb-deco"' in SRC and ".lb-deco { position: fixed; inset: 0;" in SRC
       and "pointer-events: none" in SRC)
_check("主内容抬到装饰层之上（fixed 元素默认会盖住静态内容 → 必须显式抬层）",
       '[data-testid="stMainBlockContainer"] { position: relative; z-index: 3; }' in SRC)
_check("两侧 6 块悬浮数据面板（走势曲线 / 财务指标 / 关系网络 / 指标摘要 / 财务评分 / 风险评分）",
       SRC.count('class="lb-panel"') == 6
       and all(k in SRC for k in ("■ 走势曲线", "■ 财务指标", "■ 公司关系网络",
                                  "■ 指标摘要", "■ 财务评分", "■ 风险评分")))
_check("品牌区背后有旋转光环 + 锥形扫描光（@keyframes 动画）",
       'class="lb-ring"' in SRC and 'class="lb-scan"' in SRC
       and "@keyframes lb-spin" in SRC and "animation: lb-spin 18s linear infinite" in SRC)
_check("底部透视网格（渐隐遮罩，做「地面」）+ 窄屏自动隐藏装饰",
       'class="lb-grid"' in SRC and "mask-image: linear-gradient(to top" in SRC
       and "@media (max-width: 1280px) { .lb-side { display: none; } }" in SRC)

# v5 登录页文案：数据源统一口径 + 去掉"系统"字样
# （品牌是 Agent，不是传统后台系统；"登录系统/进入系统"这类措辞与定位冲突）
_LOGIN_ZONE = SRC.split("def check_login", 1)[-1].split("if not check_login()", 1)[0]
_check("登录页来源文案改成「数据源类型式」（用户三选一里的第二档）",
       "交易所公开披露数据 · 上市公司年报 · 财务指标库" in _LOGIN_ZONE)
_check("登录页不再点名具体数据源接口（巨潮 / 东方财富）",
       not any(k in _LOGIN_ZONE for k in ("巨潮", "东方财富")),
       str([k for k in ("巨潮", "东方财富") if k in _LOGIN_ZONE]))
_check("卡头「账号登录」+ 按钮「安全登录」（不再出现「系统」二字）",
       ">账号登录<" in _LOGIN_ZONE and 'st.form_submit_button("安全登录"' in _LOGIN_ZONE)
_check("用户可见文案里彻底不再出现「系统」字样",
       not any(k in SRC for k in ("进入系统", "本系统仅供", "系统未配置", "系统识别到的行业")),
       str([k for k in ("进入系统", "本系统仅供", "系统未配置", "系统识别到的行业") if k in SRC]))

# 2) 运行时：未登录 → 浅色样式在、侧边栏为空、表单可交互
at6 = AppTest.from_file(APP, default_timeout=240)
at6.run()
_check("未登录时无异常", not [str(e.value)[:200] for e in at6.exception],
       str([str(e.value)[:200] for e in at6.exception]))
_check("未登录时注入了深蓝登录页样式", "linear-gradient(160deg, #061428" in _md(at6))
_check("未登录时侧边栏里没有任何元素（导航/记录都不该渲染）",
       len(at6.sidebar.get("button")) == 0,
       str([b.key for b in at6.sidebar.get("button")]))
_check("未登录时登录表单齐备（账号/密码/提交按钮）",
       len(at6.get("text_input")) >= 2
       and any("安全登录" in str(b.label) for b in at6.get("button")),
       str([str(b.label) for b in at6.get("button")]))
_check("账号/密码是**外置可见标签**（不再是张冠李戴的折叠标签）",
       [str(t.label) for t in at6.get("text_input")][:2] == ["账号", "密码"],
       str([str(t.label) for t in at6.get("text_input")]))
# v7：装饰层必须真的渲染出来（写过样式但没注入 HTML = 用户看到的还是"空旷的一张卡"）
_md6 = _md(at6)
_PANEL = 'class="lb-panel"'
_check("未登录时装饰层真的渲染了（6 块数据面板 + 光环 + 网格）",
       _md6.count(_PANEL) == 6 and 'class="lb-deco"' in _md6
       and 'class="lb-ring"' in _md6 and 'class="lb-grid"' in _md6,
       f"panels={_md6.count(_PANEL)}")
_check("未登录时六边形徽标在（品牌区视觉锚点）", 'class="lb-orb"' in _md6)

# 3) 错误凭据：给出提示且不放行
at6.get("text_input")[0].set_value("wrong_user")
at6.get("text_input")[1].set_value("wrong_pw")
next(b for b in at6.get("button") if "安全登录" in str(b.label)).click().run()
_check("密码错误时明确提示且不放行",
       bool(at6.error) and not _ss(at6, "authenticated", False),
       str([str(e.value)[:60] for e in at6.error]))

# 4) 演示链接（?key=）可进入，且进入后登录页样式不残留
at7 = AppTest.from_file(APP, default_timeout=240)
at7.query_params["key"] = "ai2026demo"
at7.run()
_check("演示令牌（?key=）可免登录进入", bool(_ss(at7, "authenticated", False)))
_check("进入工作台后不再注入登录页样式（不会重复叠加）",
       "linear-gradient(180deg, #FFFFFF 0%, #F7F9FC" not in _md(at7))
_check("进入工作台后侧边栏导航出现（3 个入口）",
       len([b for b in at7.sidebar.get("button")
            if (b.key or "").startswith(("nav_on_", "nav_off_"))]) == 3)

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL_P1_TESTS_PASSED")

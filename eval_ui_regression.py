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
# 【v18 · 用户第 1 条】工作台页签改成 radio 承载后，**一次只渲染选中的那一段**，
# 所以测时间轴必须先切过去 —— 顺带这也把「切出新段」本身验了一遍。
at.session_state["__wk_tab"] = "🗓️ 年度时间轴"
at.run()
_c = _cap(at)
_check("切到「年度时间轴」分段后无异常", not [str(e.value)[:200] for e in at.exception],
       str([str(e.value)[:200] for e in at.exception]))
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
# 切换指标 → 图重绘（【v18 · 用户第 1 条】原来这一步会一头跳回「分析概览」）
if "timeline_metric" in _rd:
    _rd["timeline_metric"].set_value("net_profit").run()
    _check("切换指标为「归母净利润」后仍有图", _imgs(at) >= 1)
    _check("切换指标后无异常", not [str(e.value)[:200] for e in at.exception])
    # —— 用户报的那个 bug：切完指标人还在「年度时间轴」里，没有被弹回概览 ——
    _rd2 = {r.key: r for r in at.radio}
    _check("v18①：切换指标后仍停在「年度时间轴」（不再跳回分析概览）",
           "timeline_metric" in _rd2 and str(_rd2["timeline_metric"].value) == "net_profit",
           str([k for k in _rd2 if k.startswith("timeline")]))
    _check("v18①：切换后的分段选中值仍落在 __wk_tab 会话键里（页签状态被持久化）",
           _ss(at, "__wk_tab") == "🗓️ 年度时间轴", str(_ss(at, "__wk_tab")))
    _sb2 = {s.key: s for s in at.selectbox}
    _check("v18①：切换指标后年份选择器也还在（整段时间轴都没丢）", "timeline_year" in _sb2,
           str([k for k in _sb2 if k.startswith("timeline")]))

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
# 【B9】这里原来是两条独立告警（"尚未成功取到数据" + "输入未完成"），
# 同一家公司被说两遍。v17 合并成一条、按公司分点列出原因与处理方式，
# 断言随之改为：只有**一条**相关告警，且写清了"缺什么 + 怎么补"。
_check("未取到数据 → 合并为一条告警（B9）", _w.count("暂无可分析的数据") == 1, _w[:160])
_check("告警里分点写明补全方式", "🔍 获取数据" in _w or "粘贴财报文本" in _w, _w[:160])

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
_check("app.py 有「没有共同披露的年报年份」明确提示（v11 起降级为 warning：可切各自最新口径）",
       "没有共同披露的年报年份" in SRC)
# v11（阶段三第 ① 条）：半透明白底压浅极光 = "点击输入全是白色看不清"
# → 改深墨蓝实底 + 白字 + 亮青光标 + autofill 修复 + 选中态高亮
_check("登录输入框改深色实底白字（聚焦/输入中/autofill 全程可读），光标亮青色",
       "background: #06142E !important" in SRC
       and "color: #FFFFFF !important" in SRC
       and "caret-color: #67E8F9" in SRC
       and "input:-webkit-autofill" in SRC
       and "-webkit-text-fill-color: #FFFFFF" in SRC
       and "input::selection" in SRC)
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
# v10-2（用户第 8a 条）：每家卡片必须有"取数进度/状态"块，且字体要大到窄卡里也看得清。
_wait_n = _md(at4).count('class="cmp-wait"')
_check("对比页每家卡片都有取数状态块（未取数时显示「尚未取数」占位，不再是一行浅灰小字）",
       _wait_n == 2, f"wait={_wait_n}")
_check("取数状态块：公司名 17.5px 深墨字 + 状态 14.5px 深绿字，白底浅绿 8:1 对比度",
       ".cmp-ok {" in SRC and "font-size: 17.5px; font-weight: 800" in SRC
       and "color: #0F2419 !important" in SRC
       and ".cmp-ok-sub {" in SRC and "color: #1F5138 !important" in SRC)
_check("紧凑模式用自定义状态块（不再用窄卡里会缩水的 st.success 小字），并做 HTML 转义",
       'if compact:' in SRC and 'class="cmp-ok-name"' in SRC
       and "_htm(name)" in SRC and "def _htm(" in SRC)

# 3) 切到使用说明：原侧边栏「快速上手」4 步并入这里，内容不丢
at4.session_state["main_view"] = "📖"
at4.run()
# v16：大标题删了，"使用说明" 这四个字不再出现在主区（它现在只在侧栏导航里）。
# 改断言"说明页自己的正文"——否则会误判成页面没渲染。
_check("切到「使用说明」后渲染说明正文", "🗺️ 全流程一图看懂" in _md(at4))
_check("「使用说明」承接了快速上手 4 步（内容没丢，只是换了地方）",
       "快速上手" in _md(at4))
_check("使用说明页同样不出现单公司取数/行业选择器",
       not any((r.key or "") == "industry_widget" for r in at4.radio))
_check("「使用说明」记录了侧边栏导航入口", "左侧边栏" in _md(at4))

# 4) 侧边栏分析记录：空态 + 有记录
# v10 起记录会写本地缓存（2 小时 TTL）：把缓存路径指到临时文件，
# 既不污染真实记录，也不被上一轮的真实记录污染空态断言
import tempfile as _tf
_HIST_TMP = os.path.join(_tf.gettempdir(), "_hist_ui_regression.json")
os.environ["ANALYSIS_HISTORY_CACHE"] = _HIST_TMP
try:
    os.remove(_HIST_TMP)
except OSError:
    pass
at5 = _boot()
_check("无记录时给出占位说明", "还没有分析记录" in _cap(at5))
_check("v10：明确声明本地持久化 2 小时（红色高亮，不再说『刷新即清空』）",
       ("保留 2 小时" in _md(at5)) and ("刷新页面即清空" not in _md(at5)))
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
# 【v16 · 用户第 ① 条】页面大标题（page_head）整块删除 —— 改成**反向断言**。
# 恢复时必须 CSS(.page-head) + 函数(def page_head) + 三处调用一起回，
# 只回一处会渲染出没有样式的裸 div，所以这里三处都要断言"不存在"。
# ⚠️ 删掉的东西仍然会在"说明为什么删"的注释里被提到，所以不能做纯子串判断。
#    必须剥掉两类注释：Python 的 # 注释 **和** CSS 的 /* */ 注释 ——
#    CUSTOM_CSS 里那段「⚠️ 连带删除：page_head() 函数…」就写在 /* */ 里，
#    只剥 # 注释照样误报（CSS 注释行以空格开头，不可能是 # 开头）。
_CODE = __import__("re").sub(
    r"/\*.*?\*/", "",
    "\n".join(l for l in SRC.split("\n") if not l.lstrip().startswith("#")),
    flags=__import__("re").S)
_check("v16 已删除页面大标题组件（page_head 函数 / .page-head CSS / 调用点 三处都不存在）",
       "def page_head(" not in _CODE
       and 'class="page-head"' not in _CODE
       and 'class="ph-deco"' not in _CODE
       and "page_head(" not in _CODE)
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
_check("v16：三个视图都不再渲染页面大标题（视图身份交给侧栏导航选中态）",
       SRC.count('"单公司深度分析",') == 0
       and SRC.count('"多公司对比",\n        "2~5') == 0
       and SRC.count('"使用说明",\n        "这套工作台') == 0)
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
# v16：.ph-title 30px 随 page_head 一起删了，字号断言改用仍在用的两处自定义类。
_check("自定义类的 px 字号同步上调（px 不随根字号变，必须手改）",
       ".step-head .sh-title { font-size: 17px;" in SRC
       and "width: 23px; height: 23px; border-radius: 7px;" in SRC)              # .sh-num
_check("登录页字号同步放大（标题 48px / 按钮 21px）",
       ".login-brand .lb-title {" in SRC and "font-size: 48px" in SRC
       and "font-size: 21px !important;" in SRC)
_check("品牌区（AI 六边形 + 大赛徽章 + 主标题）整体再加大并居中",
       "width: 118px; height: 133px; margin: 0 auto 22px auto;" in SRC
       and "font-size: 16.5px; letter-spacing: 2.4px;" in SRC
       and "margin:2.5vh auto 18px auto;" in SRC)

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

# ④ 界面太空旷 → 主区补图形（顶栏六边形 / 侧栏底纹 / 空状态插图）
# v16：页面徽记（.ph-deco）随 page_head 一起删除，改断言"不存在"。
_check("v16 已删除页面大标题右侧的科技徽记（.ph-deco 随 page_head 一起删）",
       'class="ph-deco"' not in SRC
       and "M84 18 L106 31 L106 63 L84 76 L62 63 L62 31 Z" not in SRC)
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

# ⑤ 单公司工作栏排版：两行网格 → 纵向逐行（v10，用户："直接一横一横来"）
_check("① ② ③ 改为纵向逐行容器（行业基准 / 数据来源 / 数据获取）",
       'st.container(key="sg-r1")' in SRC and "col=_single_grid[0]" in SRC
       and "col=_single_grid[1]" in SRC and "col=_single_grid[2]" in SRC)
_check("④ ⑤ 纵向容器在 ③ 之后创建（创建顺序 = 显示顺序）",
       '_single_grid_r2 = [st.container(key="sg-r4"), st.container(key="sg-r5")]'
       in SRC
       and SRC.index('with card("fetch", col=_single_grid[2])')
           < SRC.index("_single_grid_r2 = [st.container")
           < SRC.index('tag="可选", col=_single_grid_r2[0]'))
_check("④ 补充材料在第 2 行左栏（宽 2/3）",
       'tag="可选", col=_single_grid_r2[0]' in SRC)
_check("⑤ 运行分析按模式落栏（在线 → 右栏 / 手动 → 左栏，不留一大片空位）",
       "_run_col = _single_grid_r2[1] if src_mode.startswith(\"🌐\") else _single_grid_r2[0]" in SRC
       and 'tag="出结果", col=_run_col' in SRC)
_check("② 数据来源占用第 2 个纵向容器（顺序：① → ② → ③）",
       'tag="二选一", col=_single_grid[1]' in SRC)
_check("手动模式的 ③ 也进了第 3 个纵向容器（与在线模式同位置）",
       SRC.count("col=_single_grid[2]") == 2)

# 标题被第一张卡压在下面 → 整页看上去还是"一堆卡片摊着"（用户：布局没变）。
# 第 1 步「行业财务基准库」在模块级先渲染，所以标题必须也提到模块级、且在容器之前。
# 【v16】大标题删掉后不再有"标题 vs 卡 1"的顺序问题，改断言首屏就是操作区：
# 单公司视图第一个渲染出来的就是第 1 步的容器，不再有 page_head 夹在前面。
_check("v16：单公司视图首屏直接是操作区（sg-r1 之前没有 page_head；注释里的不算）",
       "page_head" not in _CODE[:_CODE.index('st.container(key="sg-r1")')])
_check("_view_single 内部不渲染页面标题（v16 已整块删除，避免出现裸 div）",
       'page_head' not in _CODE.split("def _view_single()", 1)[1].split("def _view_compare", 1)[0])

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

# v9：右上角工具栏（用户："share 什么什么那些根本就看不清，给我换一个颜色，并且字体变大"）
# 根因：原来写的是 [data-testid="stAppToolbar"] —— 但 stAppHeader / stAppToolbar
# 在 1.57 里是 **className**，真正的 testid 是 stHeader / stToolbar（已在前端包里核对），
# 那条规则从来没生效过 → 深色图标压在深蓝底上。
_check("工具栏按**真实 testid** 命中（stAppToolbar 是 className，写成 testid 永远选不中）",
       '[data-testid="stToolbar"]' in SRC and '[data-testid="stAppToolbar"]' not in SRC)
_check("工具栏文字换浅色并放大（15.5px + Share/星标按钮加浅色药丸底）",
       "font-size: 15.5px !important;" in SRC
       and '[data-testid="stToolbarActionButton"] button' in SRC
       and '[data-testid="stAppDeployButton"] button' in SRC)
_check("工具栏图标的 fill 也改成浅色（只改 color 的话 SVG 仍是深色）",
       '[data-testid="stToolbar"] svg,' in SRC
       and "fill: #EAF2FF !important; color: #EAF2FF !important;" in SRC)
_check("工具栏的浅色规则**不得污染**主菜单弹层（真 testid 是 stMainMenuPopover/List/MenuItem-*）",
       '[data-testid="stMainMenuPopover"]' in SRC
       and '[data-testid^="stMainMenuItem"]' in SRC
       and "color: #0B1F3A !important; fill: #334155 !important;" in SRC)
_check("深色玻璃卡 = 登录表单本体（半透明深底 + 青色顶条 + 毛玻璃 + 大投影）",
       '[data-testid="stForm"]' in SRC and "rgba(9, 24, 52, 0.72) !important" in SRC
       and "border-top: 4px solid #22D3EE" in SRC and "backdrop-filter: blur(8px)" in SRC)
_check("登录按钮 = 青色渐变 + 深蓝字（模板「安全登录」的形态，青底最亮端 8.8:1）",
       '[data-testid="stFormSubmitButton"] button' in SRC
       and "linear-gradient(90deg, #22D3EE 0%, #38BDF8 100%) !important" in SRC
       and "color: #052438 !important" in SRC)
# v11（阶段三第 ① 条）：半透明白底在浅色极光上"点击输入全是白色看不清"
# → 深墨蓝实底。旧断言（半透明白底）已由上面的"深色实底"断言取代。
_check("输入框占位符仍是浅色小字（深底上看得见但不抢正文）",
       "color: #A9C0E6 !important" in SRC)
_check("卡内报错提示在深色卡上可见（否则红字压深底看不见）",
       '[data-testid="stForm"] [data-testid="stAlert"]' in SRC)
_check("账号/密码标签**外置**，框内放小号英文占位符（v10 起带 key，避免控件状态串台）",
       'st.text_input("账号", key="login_user", placeholder="Account")' in SRC
       and 'st.text_input("密码", type="password", key="login_pw", placeholder="Password")' in SRC)
_check("占位符字号小于外置标签（15px < 18.5px，框内更轻）",
       "input::placeholder" in SRC
       and "font-size: 15px !important" in SRC
       and "font-size: 18.5px !important" in SRC)
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

# 先把注入 HTML 抽出来（都是"多行源码、单行字符串"）
# —— 后面的断言要用它们，尤其是"面板必须正好 4 块"：
#     直接数 SRC 会被 CSS 注释里的同名字面量污染，必须只数真正的 HTML 值。
# v9：面板从 LOGIN_DECO 拆到 LOGIN_PANELS_L / LOGIN_PANELS_R（各 3 块）。
# v15：用户点名删掉「公司关系网络」「指标摘要」两块 → 两侧各剩 2 块（2+2）。
_deco_blk = SRC.split("LOGIN_DECO = (", 1)[1].split("\n)\n", 1)[0]
_pl_blk = SRC.split("LOGIN_PANELS_L = (", 1)[1].split("\n)\n", 1)[0]
_pr_blk = SRC.split("LOGIN_PANELS_R = (", 1)[1].split("\n)\n", 1)[0]
_brand_blk = SRC.split("LOGIN_BRAND = (", 1)[1].split("\n)\n", 1)[0]
_deco_val = "".join(_deco_blk.split("'")[1::2])
_pl_val = "".join(_pl_blk.split("'")[1::2])
_pr_val = "".join(_pr_blk.split("'")[1::2])
_panels_val = _pl_val + _pr_val
_brand_val = "".join(_brand_blk.split("'")[1::2])
# v10-2（用户第 6 条）：面板表头原来是「■ + 中文」，■ 是纯装饰字符、图里也没有文字，
# 评审看不懂 → 改成「中文 + 英文」双行式，并在图里直接给关键节点打中文标签。
_check("两侧共 4 块数据面板、按 2+2 分列（走势曲线/财务指标 · 财务评分/风险评分；v15 删去另外两块）",
       _pl_val.count('class="lb-panel"') == 2 and _pr_val.count('class="lb-panel"') == 2
       and all(k in _panels_val for k in ("走势曲线 · K 线结构", "财务指标",
                                         "财务评分 · 四维", "风险评分"))
       and "公司关系网络" not in _panels_val and "指标摘要" not in _panels_val,
       f"L={_pl_val.count('lb-panel')} R={_pr_val.count('lb-panel')}")
_check("面板表头不再是纯图形「■」（改成中文 + 英文，看不懂图也能靠文字认）",
       "■" not in _panels_val
       and all(k in _panels_val for k in ('<span class="lp-en">Price Trend / K-Line</span>',
                                         '<span class="lp-en">Financial Indicators</span>',
                                         '<span class="lp-en">4-Dimension Score</span>',
                                         '<span class="lp-en">Risk Score</span>')),
       f"■ count={_panels_val.count('■')}")
_check("每张图下面都有「这图画的是什么」的文字说明（.lp-cap），图上关键节点也有文字标签",
       _panels_val.count('class="lp-cap"') == 4
       and _panels_val.count("<text ") >= 12
       and all(k in _panels_val for k in ("盈利", "成长", "财务风险", "经营稳健")))
_check("面板文字与深色玻璃底拉开对比度（白色 22px 标题 + 深色底片 + 青色竖条）",
       ".lb-panel .lp-head {" in SRC
       and "font-size: 22px; font-weight: 800" in SRC
       and "color: #FFFFFF !important;" in SRC
       and "background: rgba(5, 15, 34, 0.62);" in SRC,
       "旧版 #7FE9FF 青字压青色极光 = 字与背景融为一体")
_check("品牌区背后有旋转光环 + 锥形扫描光（@keyframes 动画）",
       'class="lb-ring"' in SRC and 'class="lb-scan"' in SRC
       and "@keyframes lb-spin" in SRC and "animation: lb-spin 18s linear infinite" in SRC)
_check("底部透视网格（渐隐遮罩，做「地面」）+ 中央柔和辉光把左中右熔成同一块控制台",
       'class="lb-grid"' in SRC and "mask-image: linear-gradient(to top" in SRC
       and 'class="lb-halo"' in SRC and ".lb-halo {" in SRC)
_check("窄屏撤掉两侧装饰（:has 命中则整列隐藏，并让登录卡那列占满宽度）",
       "@media (max-width: 1280px)" in SRC
       and '[data-testid="stColumn"]:has(.lb-side-col) { display: none !important; }' in SRC
       and '> [data-testid="stColumn"]:nth-child(2) {' in SRC)

# v8：三条修复 —— ① 代码块泄漏（真事故）② 横向变宽 ③ 面板放大提亮
# 事故复盘：v7 把装饰 HTML 写成"带缩进 + 中间夹空行"的多行字符串 →
# markdown-it 的 HTML 块遇空行就结束，剩下的行退回 markdown 解析；
# Streamlit 又先去公共缩进，剩 4 空格正好触发**缩进代码块**
# → 登录页直接打印出面板源码（6 块只渲染出 2 块）。
_check("注入的 HTML 一律单行（含缩进/空行就会掉进 markdown 缩进代码块，页面直接打印源码）",
       "\n" not in _deco_val and "\n" not in _brand_val
       and "\n" not in _pl_val and "\n" not in _pr_val
       and _deco_val.startswith('<div class="lb-aurora"')
       and '<div class="lb-deco"' in _deco_val
       and _pl_val.startswith('<div class="lb-side-col">')
       and _pr_val.startswith('<div class="lb-side-col">')
       and _brand_val.startswith('<div class="login-brand"'),
       f"deco_len={len(_deco_val)} brand_len={len(_brand_val)}")
_check("源码里不再出现「多行缩进的面板 HTML」（那正是事故的形态）",
       '\n        <div class="lb-panel">' not in SRC)
_check("登录页横向变宽：容器上限 1320→1560→**1800px**（治折行，不是拔高字号）",
       ".block-container { max-width: 1800px !important; }" in SRC)
_check("品牌区宽度再放宽到 1080px（说明文字一行铺开，不再折成两行）",
       "max-width:1080px;" in SRC)
_check("v15 面板搬进**真实三列**（1.4 : 2 : 1.4）→ 宽度由布局给，不再靠 clamp 硬撑、也不会压住输入框",
       'st.columns([1.4, 2, 1.4], gap="large")' in SRC
       and "st.markdown(LOGIN_PANELS_L, unsafe_allow_html=True)" in SRC
       and "st.markdown(LOGIN_PANELS_R, unsafe_allow_html=True)" in SRC
       and 'class="lb-side-col"' in SRC
       and ".lb-side { position: absolute" not in SRC)
_check("面板与登录卡同一套玻璃材质（同底色 / 同描边 / 同圆角 / 同毛玻璃）→ 视觉上「融合」",
       "background: rgba(9, 24, 52, 0.72) !important" in SRC      # 表单
       and "background: rgba(9, 24, 52, 0.72);" in SRC            # 面板
       and "border-radius: 18px !important" in SRC
       and "border-radius: 18px;" in SRC)
_check("面板再放大一档（内边距 19/22 + 圆角 18 + 顶条 4px + 进度条 13px）",
       "padding: 19px 22px 20px 22px;" in SRC
       and "height: 13px; border-radius: 999px; margin-top: 12px;" in SRC)
# v10-2（用户第 6 条）：青色标题压在青色极光上会"字与背景融为一体" → 改纯白 22px + 深色底片
_check("面板文字再放大提亮（标题 22px 纯白 + 深色底片 / 行文字 18.5px #EAF4FF / 数值 21px 白）",
       "font-size: 22px; font-weight: 800; letter-spacing: 1.1px;" in SRC
       and "color: #FFFFFF !important; margin-bottom: 13px;" in SRC
       and "font-size: 18.5px; color: #EAF4FF !important; line-height: 1.95;" in SRC
       and ".lb-panel .lp-row b { color: #FFFFFF !important; font-size: 21px; font-weight: 800; }" in SRC)
_check("面板组在列里竖直居中（列被拉高后不至于上重下轻）",
       '[data-testid="stColumn"]:has(.lb-side-col) > [data-testid="stVerticalBlock"]' in SRC
       and "flex: 1 1 auto; justify-content: center;" in SRC)
_check("面板标签列显式上色（全站 p/li/span/div{color:#334155} 是直接规则，会盖掉继承来的浅色）",
       ".lb-panel .lp-row span { color: #EFF6FF !important; font-size: 17px; }" in SRC)
_check("面板插画按比例放大（保留 viewBox，只改 width/height 等比缩放 + 铺满面板宽度）",
       'width="272"' in SRC and "width: 100%; height: auto;" in SRC)

# ⭐ 最强的一条：用 markdown-it（与 Streamlit 前端同源的 CommonMark 规则）真解析一遍。
# 只要输出里出现 <pre>，就说明这段 HTML 又被判成了"缩进代码块"→ 页面会直接打印源码。
# 这条断言能 100% 复现并守住 v7 那次事故（当时 6 块面板只有 2 块渲染、4 块变代码）。
try:
    from markdown_it import MarkdownIt
    _md_parser = MarkdownIt("commonmark", {"html": True})
    _dec_html = _md_parser.render(_deco_val)
    _pan_html = _md_parser.render(_panels_val)
    _check("装饰层 / 面板 HTML 经 markdown-it 解析：0 个代码块 + 4 块面板原样通过",
           "<pre>" not in _dec_html and "<pre>" not in _pan_html
           and _pan_html.count('class="lb-panel"') == 4,
           f"pre={_dec_html.count('<pre>')}/{_pan_html.count('<pre>')} "
           f"panels={_pan_html.count('lb-panel')}")
    _check("品牌区 HTML 经 markdown-it 解析：0 个代码块",
           "<pre>" not in _md_parser.render(_brand_val))
    # 反向验证：把 v7 的错误写法（多行 + 缩进 + 空行）喂进去，必须能复现出代码块
    _bad = _panels_val.replace("</div><div", "</div>\n\n          <div", 1).replace(
        '<div class="lb-side-col">', "\n        <div class=\"lb-side-col\">", 1)
    _bad_html = MarkdownIt("commonmark", {"html": True}).render(_bad)
    _check("反向验证：多行/缩进/空行的写法确实会被判成代码块（说明这条断言有效）",
           "<pre>" in _bad_html)
except ImportError:
    print("  SKIP  未安装 markdown-it（不影响其它断言）")

# 通用守卫：不只登录页，全文件扫一遍所有多行 markdown 注入体，
# 禁止"空行之后跟 4 空格以上缩进的 HTML 行"这种写法（那正是变代码块的形态）。
import re as _re
_bad_inject = []
for _m in _re.finditer(r'st\.markdown\(\s*"""(.*?)"""', SRC, _re.S):
    _lines = _m.group(1).split("\n")
    for _i, _ln in enumerate(_lines):
        if _re.match(r"^\s{4,}<", _ln) and any(
                not _lines[_j].strip() for _j in range(max(0, _i - 3), _i)):
            _bad_inject.append((_i, _ln.strip()[:40]))
_check("全文件多行注入里没有「空行 + 4 空格缩进 HTML」写法（会掉进缩进代码块）",
       not _bad_inject, str(_bad_inject[:3]))

# v5 登录页文案：数据源统一口径 + 去掉"系统"字样
# （品牌是 Agent，不是传统后台系统；"登录系统/进入系统"这类措辞与定位冲突）
# ⚠️ v8：品牌文案已抽成 LOGIN_BRAND 常量（放在 check_login 之前），
#    所以"登录页片区"要从常量定义处开始切，否则文案断言会扑空。
_LOGIN_ZONE = SRC[SRC.index("LOGIN_DECO = ("):SRC.index("if not check_login()")]
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
# v7/v9：装饰层必须真的渲染出来（写过样式但没注入 HTML = 用户看到的还是"空旷的一张卡"）
_md6 = _md(at6)
_PANEL = 'class="lb-panel"'
_check("未登录时装饰层真的渲染了（4 块数据面板 + 光环 + 网格 + 中央辉光）",
       _md6.count(_PANEL) == 4 and 'class="lb-deco"' in _md6
       and 'class="lb-ring"' in _md6 and 'class="lb-grid"' in _md6
       and 'class="lb-halo"' in _md6,
       f"panels={_md6.count(_PANEL)}")
_check("未登录时六边形徽标在（品牌区视觉锚点）", 'class="lb-orb"' in _md6)
# v9：面板住进真实列（不是 fixed 覆盖层）——运行时直接读列元素校验
_COLS6 = at6.get("column")
# v15：比例从 1.3:2:1.3 提到 1.4:2:1.4（删掉两块面板后侧列要更宽才协调）。
# 换算别写死倍数 —— 按实际总宽归一化到 4.8，以后再调比例也不会误报。
_W6 = [float(c.weight) for c in _COLS6]
_S6 = sum(_W6) or 1.0
_check("登录页确实是三列（左装饰 | 登录卡 | 右装饰 = 1.4 : 2 : 1.4，gap=large）",
       [round(w / _S6 * 4.8, 2) for w in _W6] == [1.4, 2.0, 1.4]
       and "large" in str(getattr(_COLS6[0], "gap", "")),
       str([(getattr(c, "weight", None), getattr(c, "gap", None)) for c in _COLS6]))

# =====================================================================
# 任务1：登录页六项指标（用户第 1 / 4 / 7 / 11 条反馈）
#   去提示 · 删简介 · 加深按钮 · 密码可见 · 大字体 · 动态背景（含图动效）
# =====================================================================
# ① 去提示：Streamlit 每个 form 底部都自带一行 "Press Enter to submit form"，
#    对中文评审来说是干扰项。全局补丁 + 表单内兜底，两条都要在。
# ⚠️ f-string 里不能出现反斜杠（Python 3.11 直接 SyntaxError，PEP 701 才放开）
_II_KEY = '[data-testid="InputInstructions"] { display: none !important; }'
_II_N = SRC.count(_II_KEY)
_check("登录页彻底去掉「Press Enter」英文提示（全局补丁 + 表单内兜底两条）",
       _II_N >= 2, f"n={_II_N}")
# ② 删简介：登录卡里塞功能介绍会挤占输入区，也拉低首屏质感（该内容在使用说明页）
_check("登录卡内只留登录本身（四条功能简介与演示路径已删除）",
       "单公司深度分析" not in _md6 and "演示路径" not in _md6
       and _md6.count("输入账号与密码进入投研工作台") == 1)
# ③ 加深按钮：旧版是青渐变 + 深蓝字 21px，字和青底糊在一起 → 深蓝底 + 纯白粗字
_check("「安全登录」按钮：深蓝渐变底 + 纯白 24px/900（不再是与青底融在一起的深蓝字）",
       "linear-gradient(92deg, #1D4ED8 0%, #2563EB 55%, #0EA5E9 100%)" in SRC
       and "font-size: 24px !important;" in SRC
       and "font-weight: 900 !important;" in SRC
       and "padding: 18px 22px !important;" in SRC)
# ④ 密码可见（任务1 新增项）：眼睛图标默认 #A9C0E6 淡蓝 + 18px，压在半透明框里
#    几乎看不见，用户根本不知道能点 → 提亮 + 放大 + 补一行明文说明。
_TIP_N = _md6.count('class="login-pw-tip"')
_check("密码可见：明文提示真的渲染出来了（告诉用户可以点眼睛核对）",
       _TIP_N == 1, f"tip={_TIP_N}")
_check("密码框右侧眼睛图标提到近白 #EAF2FF 并放大到 24px（否则没人发现能点）",
       "fill: #EAF2FF !important; color: #EAF2FF !important;" in SRC
       and "width: 24px; height: 24px;" in SRC
       and '[data-testid="stTextInputRootElement"] button:hover svg' in SRC)
# ⑤ 大字体：密码圆点「•」字形本身就小，跟账号同字号时看着像没输进去
_check("输入文字放大：账号 22px / 密码 26px + 6px 字距（圆点分得开、位数能数清）",
       "font-size: 22px !important;" in SRC
       and "font-size: 26px !important;" in SRC
       and "letter-spacing: 6px;" in SRC
       and "padding-right: 52px !important;" in SRC)
_check("卡内文字同步放大（卡标题 30px / 外置标签 20px / 占位符 16.5px）",
       "font-size:30px;font-weight:800" in SRC
       and "font-size: 20px !important;" in SRC
       and "font-size: 16.5px !important;" in SRC)
# ⑥ 动态：背景极光（v10 已有）+ **图也要动**（六张装饰图原来是死图）
_check("动态背景：三团极光色块缓慢漂移（17s / 21s / 25s 错开周期），且真的注入了 DOM",
       "@keyframes lbAurora1" in SRC and "@keyframes lbAurora3" in SRC
       and _md6.count('class="lb-aurora"') == 1)
_TR_N = _md6.count('class="tr-main"')
_NW_N = _md6.count('class="nw-node"')
_SC_N = _md6.count('class="sc-bar"')
_RK_N = _md6.count('class="rk-arc"')
_RKB_N = _md6.count('rk-arc-b"')
_check("四张装饰图也全部动起来（走势描线 / 评分柱生长 / 风险环扫描；v15 删去网络流光图）",
       _TR_N == 1 and _NW_N == 0 and _SC_N == 4 and _RK_N == 1 and _RKB_N == 1
       and "@keyframes lbDraw" in SRC and "@keyframes lbGrow" in SRC
       and "@keyframes lbRingSpin" in SRC,
       f"tr={_TR_N} nw={_NW_N} sc={_SC_N} rk={_RK_N}/{_RKB_N}")
# 风险环的 -90° 起始角必须写进 keyframes：CSS transform 会覆盖 SVG transform 属性
_check("风险环动画把 -90° 起始角写进 keyframes（否则弧线会跳回 3 点钟方向）",
       "from { transform: rotate(-90deg); }" in SRC
       and "transform-box: fill-box; transform-origin: center;" in SRC)
_check("系统开了「减少动态效果」时全部动画静止（无障碍）",
       ".lb-panel .tr-main, .lb-panel .tr-sub, .lb-panel .tr-dot," in SRC
       and ".lb-panel .sc-bar, .lb-panel .rk-arc { animation: none !important; }" in SRC)

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

# =====================================================================
# v11：阶段三 15 条反馈（①~15）静态断言
# =====================================================================
# ① 已在上面"深色实底"断言覆盖；②⑩a 侧栏文字颜色：
_check("侧栏历史按钮悬停改纯白 + help 浮层白字（用户第 ② 条：蓝色字看不清）",
       '[data-testid="stSidebar"] button:hover p' in SRC
       and "color: #FFFFFF !important;" in SRC
       and '[data-testid="stSidebar"] [data-testid="stTooltipContent"]' in SRC)
_check("2 小时警告改琥珀黄加粗 + 深色文字底片（用户第 ⑩ 条：红字看不清）",
       "#FFE066" in SRC and "border-left:4px solid #FFC53D" in SRC
       # 只查渲染字符串里的 color:#FF9D9D（源码注释里的旧色值不算）
       and "color:#FF9D9D" not in SRC)
# ③ 股票池 + 腾讯接口（接口在 data_source.py，单独读源）：
_DS_SRC = open("data_source.py", encoding="utf-8").read()
_STK_N = SRC.count('", "消费"),') + SRC.count('", "金融"),') + SRC.count('", "新能源"),') \
    + SRC.count('", "医药"),') + SRC.count('", "互联网"),') + SRC.count('", "通用"),')
_check("股票池扩容到 100 家以上（用户第 ③ 条：公司太少）",
       _STK_N >= 100, f"stocks={_STK_N}")
_check("接入腾讯免费行情接口（第四级兜底：东财→K线→新浪→腾讯）",
       "def tencent_quote" in _DS_SRC and "qt.gtimg.cn" in _DS_SRC
       and _DS_SRC.count("or tencent_quote(code)") >= 3)
# ④ 回看免重跑：
_check("历史记录升级为完整状态快照（回看=整页恢复，不再要求重新点运行分析）",
       '"snapshot": {' in SRC and '"online": (raw_data or "")[:60000]' in SRC
       and 'st.session_state["data_sig"] = None' in SRC)
# ⑤ 自动合并取数：
_check("多公司「自动合并取数」开关（选一家自动拉一家）",
       'key="cmp_auto_fetch"' in SRC and "_on_pick" in SRC
       and 'f"{key_prefix}_auto_fetch"' in SRC)
# ⑥/13b 多公司追问：
_check("多公司对比新增追问区（上下文携带全部对比公司 + 推荐问题 + 输入框）",
       'key="cmp_chat_q"' in SRC and "_cmp_answer_pending" in SRC
       and "🎯 推荐问题（按当前对比公司自动生成" in SRC)
# ⑦ 架构图：
_check("帮助页新增三层架构图（数据层/引擎层/展示层）",
       "🏛️ 系统架构图" in SRC and "_arch_layer" in SRC and "_arch_box" in SRC
       and "腾讯财经" in SRC and "逐级兜底" in SRC)
# ⑧/12/13 多年对比与估值图：
_check("对比新增「逐年年报对比」图（想看哪一年就看哪一年）",
       "def fig_compare_years_n" in SRC and "cmp-years-rev" in SRC
       and "cmp-years-np" in SRC and '"annual": list(_annual_rows' in SRC)
_check("对比新增「估值对比」图（PE/PB，行情走四级兜底）",
       "cmp-valuation" in SRC and "市盈率 PE-TTM" in SRC)
_check("对比口径可选：同年份对比 / 各自最新年报（无共同年份也能比）",
       "cmp_year_mode" in SRC and "_MODE_OWN" in SRC
       and "各自最新年报" in SRC)
# ⑨ 索引徽章：
_check("顶栏索引徽章改人话 + 悬停完整解释（不再是神秘的「未建年报索引」）",
       "页码引用未启用（非报错）" in SRC and "_tb_rag_tip" in SRC
       # 只查徽章字符串本身（源码注释/内部台账里的旧文案不算）
       and '"📚 未建年报索引"' not in SRC)
# 11 柱宽自适应：
_check("年度柱状图柱宽随柱数自适应（单柱 0.26 ≈ 减半，不再撑满全图）",
       "_bw = 0.26 if _nbar == 1" in SRC and "width=_bw" in SRC)
# 14/15 期数自选：
_check("单公司报告期数放开到 24 期（原来最多 8 期 ≈ 只有 2 个年报）",
       "list(range(1, 25))" in SRC)
_check("多公司对比新增全局报告期数选择器（3~24 期，作用全部公司）",
       'key="cmp_periods"' in SRC and "max(_cmp_periods, 8)" in SRC)

# =====================================================================
# v15 主题系统（用户：登录后背景单调丑陋 → 按两张参考图做主题，可切换；
#       主题变，字体颜色也跟着变）+ 三条点名修复（删面板 / 问号提示 / 井号提亮）
# =====================================================================
def _lum(hexs: str) -> float:
    """WCAG 相对亮度。只吃 #RRGGBB（主题变量值都是六位）。"""
    r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def _c(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * _c(r) + 0.7152 * _c(g) + 0.0722 * _c(b)


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# 直接从 app.py 里取出 THEMES / _apply_theme_vars，避免断言与实现各写一份
_ns = {}
_src = io.open(APP, encoding="utf-8").read()
_i0 = _src.index("DEFAULT_THEME = ")
_i1 = _src.index("# ============ 页面与登录 ============")
exec(compile(_src[_i0:_i1], "app_theme", "exec"), {"re": __import__("re")}, _ns)
_THEMES = _ns["THEMES"]
_apply = _ns["_apply_theme_vars"]
_BASE_RULES = _ns["_THEME_BASE_RULES"]

_check("v15 提供三套可切换主题（深空蓝 / 星云紫 / 浅色雅蓝）",
       list(_THEMES) == ["🌌 深空蓝", "🔮 星云紫", "☁️ 浅色雅蓝"],
       str(list(_THEMES)))
# 变量键必须完全一致：少一个键 → 该属性在某一主题下会继承上一主题的值（串色）
_keysets = {frozenset(v) for v in _THEMES.values()}
_check("三套主题的变量键完全一致（缺键会导致切主题时串色）",
       len(_keysets) == 1, f"keysets={len(_keysets)}")
_THEME_SETTINGS_SRC = io.open(os.path.join(_HERE, "theme_settings.py"), encoding="utf-8").read()
_check("主题设置挂在侧边栏，支持预览、应用与恢复默认",
       "render_theme_settings(THEMES, DEFAULT_THEME)" in SRC
       and "with st.sidebar:" in _THEME_SETTINGS_SRC
       and 'key="theme_apply"' in _THEME_SETTINGS_SRC
       and 'key="theme_reset"' in _THEME_SETTINGS_SRC
       and "_inject_page_css()" in SRC)
# ———【v18 · 用户第 6 条】页面上曾经凭空多出几屏 CSS 源码（font-size / config.toml /
#     选择器注释被当正文渲染），同时深主题下的原生层样式一直没生效 —— 同一个根因的两副面孔：
#     旧写法 `st.markdown(_theme_style() + _custom_css_themed())` 把**两个** <style>
#     拼在同一行，而 `st.markdown(unsafe_allow_html)` 走的是 CommonMark 解析：
#     type-1 原始 HTML 块的结束条件是「遇到**含有** </style> 的那一行」，于是第二个
#     <style> 之后的所有 CSS 被降级成普通段落 → 既印成可见正文、又永远不生效。
#     根治办法：全站合并成唯一一个 <style>，且 </style> 独占最后一行。
_INJL = [l for l in SRC.split("\n")
         if "st.markdown(" in l and "_inject_page_css()" in l]
_check("v18：整站只有一处 CSS 注入点（旧的『两个 style 拼一行』写法已删除）",
       len(_INJL) == 1
       and "def _theme_style(" not in SRC and "def _custom_css_themed(" not in SRC,
       str(_INJL))
_INJ_SRC = SRC.split("def _inject_page_css() -> str:", 1)[1]
_i_ret = _INJ_SRC.index('    return "<style>"')
_INJ_FN = _INJ_SRC[:_INJ_SRC.index("\n", _i_ret)]        # 含 return 那一行本身
# ⚠️ 必须先把 docstring 和行注释都抠掉：这两处为了讲清原理反复提到 `<style>…</style>`
#    字样，不抠的话"说明里的标签"会被算进 count，断言永久失真。
_INJ_BODY = __import__("re").sub(r'""".*?"""', "", _INJ_FN,
                                 flags=__import__("re").S)
_INJ_BODY = __import__("re").sub(r"(?m)^\s*#.*$", "", _INJ_BODY)
_check("v18：注入串只有一个 <style> / 一个 </style>，且 </style> 前有独占一行的换行",
       _INJ_BODY.count("<style>") == 1 and _INJ_BODY.count("</style>") == 1
       and __import__("re").search(r'return "<style>".*?\\n</style>"', _INJ_BODY,
                                   __import__("re").S),
       f"<style>x{_INJ_BODY.count('<style>')} </style>x{_INJ_BODY.count('</style>')}")
_check("v18：每段行内 CSS 先过 _strip_style_tags（漏一个就等于又造出一个收尾点）",
       all(f"_strip_style_tags({v})" in _INJ_BODY
           for v in ("_apply_theme_vars(CUSTOM_CSS)", "_THEME_BASE_RULES",
                     "_THEME_NATIVE_RULES", "_V18_RULES", "_V19_RULES")),
       str([v for v in ("_apply_theme_vars(CUSTOM_CSS)", "_THEME_BASE_RULES",
                        "_THEME_NATIVE_RULES", "_V18_RULES")
            if f"_strip_style_tags({v})" not in _INJ_BODY]))
# 功能性硬校验：真的把三套主题各生成一次 HTML，逐条验「只有一个 style 块、收尾干净」
# ⚠️ 上面 `_ns` 那次 exec 用的是「globals / locals 分离」写法，函数闭包捕获的是只装了
#    re 的 globals —— 直接调 _ns["_inject_page_css"]() 会 NameError: THEMES。
#    判和维护性都要求「能真的跑」，所以这里用同一个 dict 兼作 globals 再 exec 一次。
_ns_css = {"re": __import__("re")}
# ⚠️ 起点必须是 CUSTOM_CSS 而不是 DEFAULT_THEME：_inject_page_css 里要用到 CUSTOM_CSS，
#    而它定义在更靠前的位置（不在 THEMES 那一段里），少了它就 NameError。
exec(compile(_src[_src.index("CUSTOM_CSS = "):_i1], "app_css", "exec"), _ns_css)
_CHECKStype = type("StubST", (), {"session_state": {"ui_theme": "🌌 深空蓝"}})
_ns_css["st"] = _CHECKStype
_V18BAD = []
for _th18 in ("🌌 深空蓝", "🔮 星云紫", "☁️ 浅色雅蓝"):
    _CHECKStype.session_state = {"ui_theme": _th18}
    _h18 = _ns_css["_inject_page_css"]()
    _tail_ok = _h18.rstrip().endswith("\n</style>")
    _inside18 = (">" if _h18.count("<style>") == 1 and _h18.count("</style>") == 1
                 and _tail_ok else "✗#" + str((_h18.count("<style>"),
                                               _h18.count("</style>"), _tail_ok)))
    if _inside18 != ">":
        _V18BAD.append(f"{_th18}{_inside18}")
        continue
    # 抠掉唯一的 style 块后，剩下的东西必须为空（非空 = 又泄漏成正文了）
    _rest18 = __import__("re").sub(r"<style>.*?</style>", "", _h18,
                                   flags=__import__("re").S).strip()
    if _rest18:
        _V18BAD.append(f"{_th18}残留{len(_rest18)}字符:{_rest18[:60]}")
_check("v18：三套主题生成的 HTML 都只有 1 个 style 块、且块外没有任何残留文本",
       not _V18BAD, "|".join(_V18BAD))

# —— 对比度硬校验：文字 vs 卡片底，两套深色主题 + 浅色主题都要 ≥4.5:1 ——
# 深色主题的卡片是半透明 rgba(12,32,70,.82) 压在页面渐变上，这里取渐变首色作保守底。
_BAD_C = []
for _tn, _tv in _THEMES.items():
    _card = _tv["--bg-card"]
    if _card.startswith("rgba"):
        # 半透明卡片压在页面渐变上 → 按 alpha 与渐变首色合成出实际底色再算对比度。
        # 用正则取数，别按固定下标切：rgba(12,32,70,.82) 里通道位数不固定（还有 .82）。
        _n = [float(x) for x in
              __import__("re").findall(r"[\d.]+", _card)]  # 12,32,70,.82
        _cr, _a = _n[:3], _n[3]
        _base = _tv["--grad-page"]
        _base = _base[_base.index("#"):_base.index("#") + 7]      # 渐变首色
        _br = [int(_base[1 + i * 2:3 + i * 2], 16) for i in range(3)]
        _card = "#%02X%02X%02X" % tuple(round(c * _a + b * (1 - _a))
                                        for c, b in zip(_cr, _br))
    # --gold 是井号/安全提醒色，两套深色主题用亮黄 #FFD84D，浅色主题必须
    # 自动转深琥珀 #B45309（亮黄压白底 ≈1.1:1 等于隐形）—— 一起卡对比度。
    for _k in ("--tx-strong", "--tx-body", "--tx-dim", "--tx-cap", "--gold"):
        _r = _ratio(_tv[_k], _card)
        if _r < 4.5:
            _BAD_C.append(f"{_tn}{_k}={_r:.2f}")
_check("每套主题的正文/次要/说明文字压卡片底 ≥4.5:1（深色主题也按 WCAG 卡）",
       not _BAD_C, ",".join(_BAD_C))

# —— 用户点名①：删掉「公司关系网络」「指标摘要」两块装饰面板 ——
_PL_BLK = SRC.split("LOGIN_PANELS_L = (", 1)[1].split("\n)\n", 1)[0]
_PR_BLK = SRC.split("LOGIN_PANELS_R = (", 1)[1].split("\n)\n", 1)[0]
_check("登录页删掉「公司关系网络」「指标摘要」两块装饰面板（用户点名）",
       "公司关系网络" not in (_PL_BLK + _PR_BLK)
       and "指标摘要" not in (_PL_BLK + _PR_BLK)
       and (_PL_BLK + _PR_BLK).count('class="lb-panel"') == 4,
       f"panels={(_PL_BLK + _PR_BLK).count('lb-panel')}")
_check("删面板后侧列加宽（1.4 : 2 : 1.4，不再是 1.3）",
       "st.columns([1.4, 2, 1.4], gap=\"large\")" in SRC)

# —— 用户点名②：问号提示（help ⓘ tooltip）黑字混蓝底 → 深底亮黄字 ——
_check("问号提示改深底 + 亮黄字（不再黑字压蓝底看不清）",
       '[data-testid="stTooltipContent"]' in _BASE_RULES
       and "color: #FFE066 !important;" in _BASE_RULES
       and _ratio("#FFE066", "#060F24") >= 4.5,
       f"ratio={_ratio('#FFE066', '#060F24'):.1f}")
# —— 用户点名③：井号（标题 hover 锚点）→ 黄色 ——
# ⚠️ v15 这里写的是 stHeadingActionElements —— 该 testid 在 1.57 前端 bundle 里
#    命中 0，黄色一直没生效。正确名字是 stHeaderActionElements（它同时包着
#    help ⓘ 和 href="#xxx" 的锚点 a）。这条断言要连"错误名字不能出现"一起卡。
_check("井号（标题锚点 #）改黄色 --gold（用户点名）",
       'data-testid="stHeaderActionElements"' in _BASE_RULES
       and "color: var(--gold) !important;" in _BASE_RULES
       and 'data-testid="stHeadingActionElements"' not in SRC)
# —— 分析记录说明文字 ——
_check("分析记录说明文字改用 --tx-cap 变量（不再写死暗蓝 #475569）",
       'color:var(--tx-cap) !important;' in SRC and "color:#475569" not in SRC)

# —— 变量化覆盖率：CUSTOM_CSS 里不该再有"深色文字"硬编码残留 ——
# ⚠️ 必须先剥掉 CSS 注释：CUSTOM_CSS 顶部的配色说明里写满了 #64748B / #0B1F3A
#    这类"旧色值 + 对比度实测"的字面量，不剥注释会误判成"没变量化"。
_CSS0 = __import__("re").sub(
    r"/\*.*?\*/", "", SRC.split('CUSTOM_CSS = """', 1)[1].split('"""', 1)[0],
    flags=__import__("re").S)
_LEFTH = sorted({h.lower() for h in
                 __import__("re").findall(r"#[0-9A-Fa-f]{6}\b", _apply(_CSS0))})
# 允许残留的只有"本来就设计成白/近白"的值（压在蓝底或渐变上的白字白边）
_check("CUSTOM_CSS 的深色硬编码色已全部变量化（残留只允许 #FFFFFF）",
       set(_LEFTH) <= {"#ffffff"}, ",".join(_LEFTH))
_check("登录页固定深蓝底 → 无论哪套主题都兜底浅字（防止浅色主题把登录页文字变墨色）",
       ".stApp p, .stApp li," in SRC.split("LOGIN_CSS", 1)[-1]
       and "color: #EAF2FF !important;" in SRC)

# —— 运行时校验：静态断言只能证明"代码写了"，证明不了"切了真的变" ——
_at_t = _boot()
_ROOTS = {}
for _tn in _THEMES:
    _at_t.session_state["ui_theme"] = _tn
    _at_t.run()
    _v = [m.value for m in _at_t.markdown if m.value and ":root{" in m.value]
    _ROOTS[_tn] = _v[0] if len(_v) == 1 else ""
_check("运行时切主题：三套 :root 注入各不相同，且每次只注入一份（不重复叠加）",
       len(set(_ROOTS.values())) == 3 and all(_ROOTS.values()),
       str({k: len(v) for k, v in _ROOTS.items()}))
_check("运行时：深色主题井号走亮黄 #FFD84D，浅色主题自动转深琥珀 #B45309（白底上才看得清）",
       "--gold:#FFD84D" in _ROOTS["🌌 深空蓝"]
       and "--gold:#FFD84D" in _ROOTS["🔮 星云紫"]
       and "--gold:#B45309" in _ROOTS["☁️ 浅色雅蓝"])
_check("运行时：井号黄 + 问号 tooltip 兜底确实进了页面（不是只写在源码里）",
       all("stHeaderActionElements" in v and "stTooltipContent" in v
           for v in _ROOTS.values()))

# =====================================================================
# v16 ① 删除顶部大标题区（用户：「完全没有存在的必要，然后整体给我上移」）
# =====================================================================
_check("v16：单公司 / 多公司 / 使用说明三个视图都没有 page_head 调用",
       not __import__("re").search(r"^\s*page_head\(", _CODE, __import__("re").M),
       "还有 page_head( 调用")
_check("v16：.page-head 的 CSS 规则已全部删掉（只留说明性注释）",
       ".page-head .ph-title" not in _CODE and ".page-head .ph-deco" not in _CODE)
# 删掉 ~110px 的大标题后，首屏第一个元素应该是操作区容器而不是空白
_check("v16：单公司视图首屏第一个容器就是第 1 步（sg-r1），中间不再夹大标题",
       0 < _CODE.index('st.container(key="sg-r1")') < _CODE.index('card("fetch"'))

# =====================================================================
# v16 ②③④⑤ 原生组件层（用户：「白底字看不清 / 改主题它也不跟着变」）
# 根因：Streamlit 原生组件的配色来自 config.toml 的 [theme]，不吃 :root 变量。
# =====================================================================
_NATIVE = _ns["_THEME_NATIVE_RULES"]
# v18 起四层 CSS 都进 _inject_page_css 的 cache body，层序在这里卡（见上面 v18 断言）
_check("v16 原生层已接入注入（四层 CSS 全部拼进 _inject_page_css 的缓存体）",
       all(v in _INJ_FN for v in ("CUSTOM_CSS", "_THEME_BASE_RULES",
                                  "_THEME_NATIVE_RULES", "_V18_RULES", "_V19_RULES")))
_check("v16 原生层层序正确：兜底层在前、原生层在后（原生层要压 emotion 样式）",
       _INJ_FN.index("_THEME_BASE_RULES") < _INJ_FN.index("_THEME_NATIVE_RULES"))
_check("v16 原生层覆盖：输入框 / 展开面板 / 定高容器 / 上传区 / 表格 / 提示条",
       all(k in _NATIVE for k in (
           ".stApp input,", '[data-testid="stExpanderDetails"]',
           '[class*="st-key-raw-scroll"]', '[class*="st-key-khist-scroll"]',
           '[data-testid="stFileUploaderDropzone"]',
           '[data-testid="stDataFrame"]',
           '[data-testid="stAlertContainer"]')))
# ⚠️ stVerticalBlockBorderWrapper 在 1.57 的 bundle 里命中 0（老版本遗留名），
#    原生层里出现它就等于"写了个永远不生效的规则"（上面那条老断言会拦）。
_NATIVE_NC = __import__("re").sub(r"/\*.*?\*/", "", _NATIVE, flags=__import__("re").S)
_check("原生层不用已失效的 testid（stVerticalBlockBorderWrapper 在 1.57 命中 0）",
       "stVerticalBlockBorderWrapper" not in _NATIVE_NC)
_check("两个定高滚动容器都带了 key（否则生成不出 st-key-* 类名，CSS 点不到）",
       'st.container(height=220, key=f"raw-scroll-{key_prefix}")' in SRC
       and 'st.container(height=210, key="khist-scroll")' in SRC)
# ⚠️ raw-scroll 的 key 必须带 key_prefix：多公司页会为每家公司各渲染一份，
#    写死同名 key 会触发 DuplicateWidgetID，整个对比页直接崩（3 家公司必现）。
_check("raw-scroll 的 key 带 key_prefix（多公司页 3 份同面板，写死同名 key 会崩）",
       'key=f"raw-scroll-{key_prefix}"' in SRC
       and 'st.container(height=220, key="raw-scroll")' not in SRC)
# ⚠️ 表格变量自引用陷阱：--gdg-bg-cell: var(--gdg-bg-cell) 是死循环（值恒为空），
#    表格会退回默认白底。自己存的变量必须叫别的名字（这里是 --gd-*）。
_check("表格变量不自引用（--gdg-x 必须指向 --gd-x，不能指向自己）",
       not __import__("re").search(r"--gdg-[a-z-]+:\s*var\(--gdg-", _NATIVE)
       and "--gdg-bg-cell: var(--gd-cell);" in _NATIVE)
_check("v16 每套主题都给了原生层变量（少一个 → 该组件在切主题时串色）",
       all(all(k in t for k in ("--surface", "--on-surface",
                                "--fw-body", "--fw-label", "--fw-strong",
                                "--gd-cell", "--gd-tx"))
           for t in _THEMES.values()))
# —— 原生层对比度硬校验：--on-surface 压 --surface（合成后）≥4.5:1 ——
_BAD_N = []


def _solid(val: str, grad: str) -> str:
    """把半透明 surface 与页面渐变首色合成成实际底色。"""
    if not val.startswith("rgba"):
        return val
    _n = [float(x) for x in __import__("re").findall(r"[\d.]+", val)]
    _c, _a = _n[:3], _n[3]
    _b0 = grad[grad.index("#"):grad.index("#") + 7]
    _b = [int(_b0[1 + i * 2:3 + i * 2], 16) for i in range(3)]
    return "#%02X%02X%02X" % tuple(round(x * _a + y * (1 - _a))
                                   for x, y in zip(_c, _b))


for _tn, _tv in _THEMES.items():
    _sf = _solid(_tv["--surface"], _tv["--grad-page"])
    _r = _ratio(_tv["--on-surface"], _sf)
    if _r < 4.5:
        _BAD_N.append(f"{_tn}={_r:.2f}")
    # 表格正文 / 表头 压表格单元底 也要够
    for _k in ("--gd-tx", "--gd-tx-head"):
        _r2 = _ratio(_tv[_k], _tv["--gd-cell"])
        if _r2 < 4.5:
            _BAD_N.append(f"{_tn}{_k}={_r2:.2f}")
_check("v16 原生层对比度：on-surface / 表格正文 / 表头 压各自底色 ≥4.5:1（三套主题）",
       not _BAD_N, ",".join(_BAD_N))
# —— 字重随主题变（用户：「粗细那些并没有随着主题的改变而改变」）——
# 两套深色主题共用一组字重（都是"浅字压深底"），浅色主题单独一组
# （"深字压白底"，再粗就糊）。所以期望是 **2 组**，不是 3 组 —— 断言写成 ==3 会误报。
_check("v16 字重随主题变（深色主题更粗 500/700/800，浅色主题 400/600/700 不糊）",
       _THEMES["🌌 深空蓝"]["--fw-body"] == "500"
       and _THEMES["🔮 星云紫"]["--fw-body"] == "500"
       and _THEMES["☁️ 浅色雅蓝"]["--fw-body"] == "400"
       and int(_THEMES["☁️ 浅色雅蓝"]["--fw-body"])
       < int(_THEMES["🌌 深空蓝"]["--fw-body"])
       and len({(t["--fw-body"], t["--fw-label"], t["--fw-strong"])
                for t in _THEMES.values()}) == 2)
_check("v16 展开条标题按用户要求「换色之后再加粗加深」（--fw-strong）",
       '[data-testid="stExpander"] summary' in _NATIVE
       and "font-weight: var(--fw-strong) !important;" in _NATIVE)
# —— 运行时：原生层确实进了页面 ——
_at_n = _boot()
_at_n.session_state["ui_theme"] = "🔮 星云紫"
_at_n.run()
_INJ = "".join(m.value for m in _at_n.markdown if m.value and ":root{" in m.value)
# ⚠️ 别把 --surface 的具体色值写死在这里：v17 为了修「深主题白底白字」把
#    surface 压得更深、on-surface 提得更亮，一改就误报。改成跟 THEMES 对照。
_check("运行时：原生层随主题进入页面（stFileUploaderDropzone / stAlertContainer 都在）",
       "stFileUploaderDropzone" in _INJ and "stAlertContainer" in _INJ
       and "--surface:" in _INJ
       and _THEMES["🔮 星云紫"]["--surface"].replace(" ", "") in _INJ.replace(" ", ""))

# =====================================================================
# v17 · 外部测试报告 B1~B10 修复固化
# =====================================================================
print("\n=== v17 外部测试报告 B1~B10 ===")
import re as _re17

# —— B1：CSS 必须整段包在**一个** <style> 里 ——
# 根因实测：_THEME_BASE_RULES / _THEME_NATIVE_RULES 原来是拼在 CUSTOM_CSS 的
# </style> **之后** → 既没生效（深主题一直白底白字），又被浏览器当正文渲染出来。
# 【v18 重写】v17 只做到"拼进一个 style"；真正踩到的坑是 CommonMark type-1 HTML 块
# 在「含有 </style> 的那一行」就收尾，所以碎片还必须剥干净、收尾标签还得独占一行。
# 这里不再 match 拼接字符串（一改变量名就失效），而是直接调用真实函数产出 HTML 再验。
_check("B1：注入前先剥掉内层 <style>（避免两段并存的嵌套）",
       'return (s or "").replace("<style>", "").replace("</style>", "")' in SRC)
_check("B1：四层 CSS 由单点 _inject_page_css 统一包成一个 <style>",
       "def _inject_page_css() -> str:" in SRC
       and 'return "<style>" + _root + _CSS_CACHE["body"] + "\\n</style>"' in SRC
       and len([l for l in SRC.split("\n")
                if "st.markdown(" in l and "_inject_page_css()" in l]) == 1)

def _leak_of(atx) -> list:
    """把 <style>…</style> 抠掉后还剩下的可见文本 = 泄漏到正文的 CSS。"""
    out = []
    for m in atx.markdown:
        v = str(getattr(m, "value", "") or "")
        if "<style>" not in v:
            continue
        rest = _re17.sub(r"<[^>]+>", "", _re17.sub(r"<style>.*?</style>", "", v, flags=_re17.S))
        if rest.strip():
            out.append(rest.strip()[:120])
    return out

_at_l = AppTest.from_file(APP, default_timeout=240)
_at_l.run()
_check("B1：登录页正文里没有 CSS 泄漏", not _leak_of(_at_l), "|".join(_leak_of(_at_l)))
_B1BAD = []
for _th17 in ("🌌 深空蓝", "🔮 星云紫", "☁️ 浅色雅蓝"):
    _at_l.session_state["authenticated"] = True
    _at_l.session_state["main_view"] = "🏢"
    _at_l.session_state["ui_theme"] = _th17
    _at_l.run()
    if _leak_of(_at_l):
        _B1BAD.append(_th17)
_check("B1：三套主题下主界面都没有 CSS 泄漏", not _B1BAD, ",".join(_B1BAD))

# —— B2：按钮禁用必须自带原因 ——
_check("B2：未取数时给出禁用原因（不再静默灰按钮）",
       "两个按钮当前不可用" in SRC)
# —— B3：研报占位与正文互斥 ——
_check("B3：研报页签按 report 是否存在二选一渲染",
       'report = st.session_state.get("report")' in SRC
       and "还没有研报" in SRC
       and SRC.index('if not report:') < SRC.index('st.subheader("📝 完整投研报告")'))
# —— B4：下拉 + 手动输入合并成一个取数入参 ——
_check("B4：do_fetch 用「输入框 or 下拉代码」合并入参",
       '_kw = (kw or "").strip() or (_code or "").strip()' in SRC)
# —— B5：分析记录按唯一键覆盖更新 ——
_check("B5：写记录前按 _sig 去重（同股不再堆雷同记录）",
       '_hist = [h for h in _hist if (h.get("_sig") or "") != _sig]' in SRC)
# —— B6：共享账号会话隔离 ——
_check("B6：记录带会话 id + 读取按会话过滤 + 写回保留他人记录",
       '"_sid": _hist_sid(),' in SRC
       and "def _hist_load(only_sid" in SRC
       and "_others = [h for h in _all" in SRC)
# —— B7：禁止明文降级开关 ——
_DS17 = io.open(os.path.join(_HERE, "data_source.py"), encoding="utf-8").read()
_check("B7：明文降级改为开关控制，默认关闭（ALLOW_INSECURE_FALLBACK=0）",
       "ALLOW_INSECURE_FALLBACK" in _DS17
       and "ALLOW_INSECURE_FALLBACK and url.startswith" in _DS17
       and 'os.environ.get("ALLOW_INSECURE_FALLBACK", "0")' in _DS17)
# —— B8：登录页演示四维与引擎口径一致 ——
_check("B8：登录演示第四维改为「现金」并标注静态示例",
       ">现金</text>" in SRC and "静态示例，非真实评分" in SRC
       and "营运能力" not in SRC)
# —— B10：向量化只记成功批次 + 索引增量补齐 ——
_RG17 = io.open(os.path.join(_HERE, "rag_engine.py"), encoding="utf-8").read()
_check("B10：embed 只在成功后记账（不再开跑前预记 total 批）",
       "_budget.bump(_budget.BUCKET_EMBED, len(results))" in _RG17
       and "_budget.bump(_budget.BUCKET_EMBED, total)" not in _RG17)
_check("B10：指纹一致但块数不足时增量补齐，不整份重建",
       "索引增量补齐" in _RG17)

# —— v17 用户第 ⑤(a) 条：深主题侧栏与主区必须分得开 ——
_check("v17：三套主题都给了 --side-edge（侧栏分界色）",
       all("--side-edge" in t for t in _THEMES.values()))
_check("v17：侧栏有右边框 + 投影（不再与主区同色糊成一片）",
       'section[data-testid="stSidebar"]' in SRC
       and "var(--side-edge)" in SRC
       and "box-shadow: 6px 0 26px" in SRC)
# —— v17 用户第 ④ 条：多公司选齐后才一次性取数 ——
_check("v17：多公司有「选齐判断 + 批量取数」两个函数",
       "def _cmp_targets()" in SRC and "def _cmp_all_filled()" in SRC
       and "def _run_batch_fetch()" in SRC)
_check("v17：热门下拉在多公司页只登记目标、不再单独自动取数",
       "cmp" in SRC and "_prefix.startswith(\"cmp\")" in SRC.replace("'", '"')
       or 'startswith("cmp")' in SRC)
_check("v17：批量取数有目标指纹记账（选齐后只跑一次，rerun 不重复）",
       "cmp_batch_tried" in SRC and "_cmp_sig_targets" in SRC)
# —— v17 用户第 ⑥ 条：一键建索引不再自相矛盾 ——
_check("v17：索引状态文案统一（就绪 / 未建立 二选一）",
       "年报索引已就绪" in SRC and "当前尚未建立年报索引" in SRC)
_check("v17：紧凑模式不再重复渲染两套建索引按钮",
       "_compact_pdf_body" in SRC)

# =====================================================================
# v18 六条（本轮用户反馈的新一轮修复）
# =====================================================================
# —— 第 1 条：年度时间轴里换指标 → 回到了「分析概览」 ——
# 根因：st.tabs **不保存选中态**，tab 内任何控件交互都会 rerun，rerun 回来的
# tab 永远是第 1 个。改成用 st.radio 做「工作台分段」：选中值落在 session_state，
# rerun 后仍然停在原处。
_check("v18①：工作台不再用 st.tabs（它不保存选中态，换控件必跳回第一个页签）",
       "st.tabs(" not in SRC)
_check("v18①：工作台分段用 radio + session_state 持久化（key=__wk_tab）",
       'key="__wk_tab"' in SRC and '__wk_tab") not in _WK_TAB_LABELS' in SRC
       and 'with st.container(key="wkbody")' in SRC)
_check("v18①：五个分段都还在（概览/时间轴/评分/研报/轨迹），只是换了承载方式",
       all(k in SRC for k in ("📊 分析概览", "🗓️ 年度时间轴",
                              "🧮 评分与依据", "📝 研报", "🤖 Agent 轨迹")))
_check("v18①：分段条/内容区都带 key（CSS 要靠 st-key-* 命中才能排版成页签）",
       'st.container(key="wktabbar")' in SRC and "st-key-wktabbar" in SRC)

# —— 第 2 条：追问的「图表辅助」是一张碎图 ——
# 根因：st.image(bytes) 走 /media/<hash>.png 端点，被浏览器/代理/会话校验拦掉
# 就只剩占位符。改成内联 base64（show_png），不产生任何额外请求。
_VIZCALL = [l for l in SRC.split("\n") if "_viz_png" in l or "_vz_png" in l]
_check("v18②：追问图表走 show_png 内联 base64（不再用会碎图的 st.image）",
       not any("st.image(" in l for l in _VIZCALL)
       and 'show_png(_viz_png' in SRC and 'show_png(_vz_png' in SRC,
       str(_VIZCALL[:3]))

# —— 第 3 条：5 家公司的雷达图只剩一堆散点、没有轮廓 ——
# 根因两条：① 评分值可能不是 float（"7.5分" 这类串）→ 整家被判缺 → 什么都不画；
#           ② 旧代码要求"全部维度都有值"才画闭合轮廓，缺一个就整条线都没了。
_check("v18③：雷达图逐段连线（相邻两维都有值就连，缺口留白而不是整条不画）",
       "for _j in range(N):" in SRC and "_j2 = (_j + 1) % N" in SRC
       and "if vals[_j] is None or vals[_j2] is None:\n                continue" in SRC)
_check("v18③：缺失维度不再把整条轮廓抹掉（有 draw 兜底 + 散点补顶点）",
       "_drew = False" in SRC
       and "label=_lab if not _drew else None" in SRC
       and "if all(v is not None for v in vals):" in SRC)
_check("v18③：评分值统一过 _cmp_num 收敛成 float（串/NaN/inf 不再让线消失）",
       "def _cmp_num(" in SRC and "f != f or f in (float(\"inf\")" in SRC)
_check("v18③：缺数据的维度要在标题里点名（留白不能被读成 0 分）",
       "无数据 → 该段留白，不画 0" in SRC)

# —— 第 4 条：可视化子图太小（图被压成一条横带）——
# 根因不是字号而是**宽高比**：显示高度 = disp_w × (H / W)，W 随公司数涨到 11.5
# 英寸而 H 死守 3.3 ⇒ 570px 列里只剩 160px 高。现在：宽封顶 12 + 高度按 _fig_h 算。
_check("v18④：画布宽度封顶 12 英寸（旧值 15 / 甚至 28 会把字按 disp_w/W 压没）",
       SRC.count("min(max(6.2, 1.5 * n + 4.0), 12.0)") >= 3
       and "min(max(6.2, 0.62 * len(years) * n + 3.2), 12.0)" in SRC)
# v19 起高度改为「按目标显示高度反算」：H = target_px × W / disp_w ——
# 同一个 target_px 在半栏 / 整行下都成立，比固定宽高比更准（详见 app.py _fig_h）。
_check("v18④/v19：画布高度由 _fig_h 反推（目标显示高 330px，不再写死 3.3）",
       "def _fig_h(" in SRC and "_TARGET_FIG_PX = 330.0" in SRC
       and "float(target_px) * float(W) / max(float(disp_w), 1.0)" in SRC
       and SRC.count("H = _fig_h(W") >= 4)
_check("v18④：柱顶数字在放不下时竖排（90°），不再挤成一团黑",
       "def _cmp_bar_px(" in SRC and "rotation=_rot" in SRC)
_check("v18④：≥4 家公司时图表整行排（二分栏 ~480px 装不下 5 家）",
       "_vnc = 1 if len(_names) >= 4 else 2" in SRC
       and "_vdw = _ROW_W if _vnc == 1 else _COL_W" in SRC)
_check("v18④：每张图都显式传了 disp_w（漏传就会退回默认值，字号算错）",
       SRC.count("disp_w=_vdw") >= 4 and "disp_w=_COL_W" in SRC
       and "disp_w=_ROW_W" in SRC)
_check("v18④：图冗余出竖向余量给标注（margins(y=0.18) + tight_layout）",
       SRC.count("ax.margins(y=0.18)") >= 4)

# —— 第 5 条：推荐问题只有两家之间的（选 3~5 家时其他几家在推荐区消失）——
_check("v18⑤：推荐问题按公司数分流（2 家走两两口径，≥3 家走整体口径）",
       "_nv = len(_cnames)" in SRC and "if _nv == 2:" in SRC
       and "_cmp_sugs = [" in SRC
       and 0 < SRC.index("_nv = len(_cnames)") < SRC.index("if _nv == 2:"))
_check("v18⑤：≥3 家时推荐问题里出现全部公司名（不再写死前两家）",
       "_all = \"、\".join(_cnames)" in SRC
       and "f\"{_all} 这 {_nv} 家公司" in SRC)
_check("v18⑤：≥3 家追加一条「评分最高 vs 最低」的捉对问题（按真实评分动态取）",
       "_ranked = sorted(" in SRC
       and "_hi, _lo = _ranked[0][\"name\"], _ranked[-1][\"name\"]" in SRC)
_check("v18⑤：推荐区标注当前对比了几家（避免误以为还是两家）",
       "f\"当前对比 {_nv} 家：{_all}" in SRC)

# =====================================================================
# v19 十二条（本轮用户反馈）
# =====================================================================
# ② 基准条整行不再渲染（业务计算保留）
_check("v19②：规则优秀线/同业样本统计条不再渲染（peer_stat 计算保留）",
       "+ _peer_stat_html(" not in SRC
       and 'st.session_state["peer_stat"] = _peer_st' in SRC)
# ③ 数据来源副标题换成真实来源
_check("v19③：数据来源副标题写明真实来源（巨潮/东财/腾讯）",
       SRC.count("（巨潮资讯网 · 东方财富 · 腾讯财经）") >= 1
       and "在线取数已包含实时行情" not in SRC)
# ④⑤⑥ 三行说明文字删除
_check("v19④：报告期数说明行已删（口径说明挪进滑块 help）",
       "报告期数 = 抓取最近 N 期财务数据" not in SRC
       and "向东方财富拉取「最近 N 个报告期」" in SRC)
_check("v19⑤：年报索引已就绪提示行已删",
       "年报索引已就绪** —— 检索方式" not in SRC)
_check("v19⑥：先看分数提示行已删",
       "先看分数：四维评分" not in SRC)
# ⑦ 登录后页脚免责声明删除 + 追问输入框占位词去掉「例如：」示例
# ⚠️ 源码里留着注释解释"为什么删"，注释里会引用旧文案 —— 先剥掉 # 注释行再查。
_SRC_NOC = __import__("re").sub(r"(?m)^\s*#.*$", "", SRC)
_check("v19⑦：登录后的页脚免责声明不再渲染（登录页那条保留）",
       "第二届全国人工智能应用创新大赛参赛作品 · 分析结果仅供参考" not in _SRC_NOC
       and "本作品仅供大赛评审访问 · 分析结果仅供参考" in _SRC_NOC)
_check("v19⑦：追问输入框不再带「例如：…」示例占位",
       "例如：这家公司的毛利率" not in _SRC_NOC and "例如：这几家公司" not in _SRC_NOC
       and "输入你想追问的问题" in _SRC_NOC)
# ⑧⑨⑩⑪ CSS 层
_check("v19⑧⑩⑪：侧栏对比度/字号规则落在 _V19_RULES 并接入注入",
       "_strip_style_tags(_V19_RULES)" in SRC
       and 'section[data-testid="stSidebar"] div[role="radiogroup"] label' in SRC
       and "color: #FFD866 !important;" in SRC
       and "font-size: 15.5px !important;" in SRC)
_check("v19⑨：主区非主按钮按主题变量配色（深主题不再白底浅字）",
       '[data-testid="stMain"] .stButton > button:not([kind="primary"])' in SRC)
# ⑫ 登录页整体上移
_check("v19⑫：登录页顶部留白压缩（内容整体上移更对称）",
       'padding-top: 0.5rem !important;' in SRC.split("LOGIN_CSS = ", 1)[1]
       .split("\n</style>", 1)[0])

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL_P1_TESTS_PASSED")

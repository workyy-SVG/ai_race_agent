# -*- coding: utf-8 -*-
"""
O11 回归测试：行业基准可解释化
==============================

把「规则优秀线」与「同业样本统计」的口径分离固化成断言。

核心命题：
    规则优秀线 18%  ≠  行业平均 18%

前者是本项目设定的**评判标准**（用于评分与四象限），后者是东方财富 F10 公布的
**同业实际水平**（中值 / 均值）。B06 的根因就是把前者当后者说出去。
本文件保证二者在代码里永远是两个东西，且：
  · 没有真实样本时 → 显示"未取到"，**绝不**拿规则线顶替成行业平均；
  · 有真实样本时 → 中值与均值**分开**显示，且与统计值比较用百分点（pp）。

联网断言默认跳过，用 INDUSTRY_STAT_LIVE=1 开启（CI 里不依赖外部网络）。
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

# AppTest 里必须关掉取数：既保证不依赖网络，也让"未取到"分支被稳定覆盖
os.environ["INDUSTRY_STAT_OFF"] = "1"
_LIVE = os.environ.get("INDUSTRY_STAT_LIVE", "") == "1"

_TF = tempfile.mkdtemp(prefix="hist_o11_")
os.environ["ANALYSIS_HISTORY_CACHE"] = os.path.join(_TF, "_hist_cache.json")

FAIL = []


def ck(cond, label, detail=""):
    print(("  OK   " if cond else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAIL.append(label)


print("=" * 72)
print("第一层 · 纯函数（industry_stat / fincalc 守卫）")
print("=" * 72)

import fincalc as fc
import industry_stat as ist

# 一条可用的同业统计（与真实接口返回结构一致，值取自茅台实测）
_ST = {
    "ok": True,
    "source": "东方财富 F10 · 行业对比",
    "center": "贵州茅台",
    "center_code": "600519",
    "report_date": "2025-12-31",
    "period": "2025 年报",
    "n": 10,
    "roe": {"median": 13.24, "avg": 15.27, "self": 36.36},
    "rev": {"median": -20.13, "avg": -19.46, "self": -1.21},
    "peers": [
        {"code": "600809", "name": "山西汾酒", "roe": 38.17, "rev": 7.52},
        {"code": "000568", "name": "泸州老窖", "roe": 29.23, "rev": None},
    ],
}

print("\n[O11.1] 统计块的可用性判定")
ck(ist.usable(_ST) is True, "O11.1 样本充足时可用", ist.usable(_ST))
ck(ist.usable(None) is False, "O11.1 无数据 → 不可用")
ck(ist.usable({"ok": False, "n": 2, "roe": {"median": 10.0}, "rev": {}}) is False,
   "O11.1 样本不足 3 家 → 不可用（两家的中位数没有意义）")
ck(ist.usable({"ok": True, "n": 10, "roe": {}, "rev": {}}) is False,
   "O11.1 完全没有统计值 → 不可用")
ck(ist.MIN_PEERS >= 3, "O11.1 样本下限不小于 3 家", ist.MIN_PEERS)

print("\n[O11.2] 报告期与代码格式")
ck(ist._period_label("2025-12-31 00:00:00") == "2025 年报",
   "O11.2 12-31 → 年报", ist._period_label("2025-12-31 00:00:00"))
ck(ist._period_label("2026-06-30 00:00:00") == "2026 中报",
   "O11.2 06-30 → 中报", ist._period_label("2026-06-30 00:00:00"))
ck(ist._secu_code("600519") == "SH600519", "O11.2 沪市加 SH 前缀")
ck(ist._secu_code("000858") == "SZ000858", "O11.2 深市加 SZ 前缀")
ck(ist._secu_code("SH600519") == "SH600519", "O11.2 已带前缀不重复添加")

print("\n[O11.3] 与同业比较一律用百分点（pp）")
_p1 = ist.position_text(36.36, _ST, "roe")
ck("高于同业中值 23.12 个百分点" in _p1, "O11.3 ROE 高于中值用 pp", _p1)
ck("高于同业均值 21.09 个百分点" in _p1, "O11.3 ROE 高于均值用 pp", _p1)
ck("百分之" not in _p1, "O11.3 不出现『高出百分之多少』的说法", _p1)
_p2 = ist.position_text(-1.21, _ST, "rev")
ck("高于同业中值 18.92 个百分点" in _p2, "O11.3 营收同比对比用 pp", _p2)
ck(ist.position_text(None, _ST, "roe") == "", "O11.3 缺本股值 → 不生成对比")
ck(ist.position_text(36.36, None, "roe") == "", "O11.3 无统计 → 不生成对比")
# 中值与均值**同时**给出，缺一不可（只报一个会丢信息）
ck(_p1.count("个百分点") == 2, "O11.3 中值与均值并列，不只报一个", _p1)

print("\n[O11.4] 给大模型的文本块")
_tx = ist.stats_text(_ST)
ck("同业样本统计" in _tx, "O11.4 文本块标明是同业样本统计", _tx[:120])
ck("中值" in _tx and "均值" in _tx, "O11.4 中值与均值都给出", _tx[:300])
ck("样本 10 家" in _tx, "O11.4 标注样本数", _tx[:200])
ck("2025 年报" in _tx, "O11.4 标注报告期", _tx[:200])
ck("与「规则优秀线」不是一回事" in _tx, "O11.4 明确两条线不是一回事", _tx[-200:])
ck("不得笼统写作" in _tx, "O11.4 提示不得笼统写成行业平均", _tx[-200:])
ck(ist.stats_text({"ok": False, "n": 2, "roe": {}, "rev": {}}) == "",
   "O11.4 不可用时返回空串（模型拿不到任何『行业平均』）")

print("\n[O11.5] 允许列表（交给润色守卫放行）")
_av = ist.allowed_values(_ST)
ck("13.24" in _av and "15.27" in _av, "O11.5 放行中值与均值", _av)
ck("-20.13" in _av, "O11.5 负数也进入允许列表", _av)
ck(ist.allowed_values({"ok": False, "n": 1, "roe": {}, "rev": {}}) == [],
   "O11.5 不可用时允许列表为空")

print("\n[O11.6] 润色守卫：有真实来源才放行")
gd = "## 摘要\n茅台 ROE 为 36.36%。"
_, s1, _ = fc.polish_guard(gd, "## 摘要\n茅台 ROE 为 36.36%，高于行业中值 13.24%。",
                           allowed=["13.24"])
ck(s1 == "pass", "O11.6 引用真实注入的中值 → 通过", s1)
_, s2, i2 = fc.polish_guard(gd, "## 摘要\n茅台 ROE 为 36.36%，高于行业中值 20%。",
                            allowed=["13.24"])
ck(s2 == "keep_draft", "O11.6 未注入的中值 → 退回初稿", s2)
ck(any("中值" in x for x in i2), "O11.6 记录是哪个口径被拦下", i2)
_, s3, i3 = fc.polish_guard(gd, "## 摘要\n茅台 ROE 为 36.36%，高于行业平均水平。",
                            allowed=["13.24"])
ck(s3 == "keep_draft", "O11.6 说平均水平却不给数值 → 退回初稿", s3)
# 中文无空格：邻近取数不能把 13.24 切成 24
_, s4, i4 = fc.polish_guard("## 摘要\n营收 100 亿元。",
                            "## 摘要\n营收 100 亿元，高于行业中值13.24%。",
                            allowed=["13.24"])
ck(s4 == "pass", "O11.6 紧贴排版（中值13.24%）不被切串误伤", s4)
ck(not any("24" == x[-4:-1] for x in i4), "O11.6 不把 13.24 误切成 24", i4)
# B06 的老毛病仍然必须拦住：11% 是规则线，不是同业统计
_, s5, _ = fc.polish_guard(
    "## 摘要\n茅台 ROE 为 32.5%。白酒业 ROE 11% 即显落后。",
    "## 摘要\n茅台 ROE 为 32.5%。显著优于白酒行业约 11% 的平均水平。")
ck(s5 == "keep_draft", "O11.6 规则线 11% 被说成行业平均 → 仍然拦下", s5)

print("\n" + "=" * 72)
print("第二层 · 源码静态（app.py 关键写法）")
print("=" * 72)

_src = open("app.py", encoding="utf-8").read()

print("\n[O11.7] 两条线在代码里是两个东西")
ck("规则优秀线（{industry} · 本项目设定）" in _src,
   "O11.7 基准条明确标注规则线是『本项目设定』")
ck("这是「优秀标准」，不是行业实际水平" in _src,
   "O11.7 规则线旁注明它不是行业实际水平")
ck('📊 <b>同业样本统计</b>' in _src, "O11.7 有独立的同业统计栏")
ck("这是「同业实际水平」，与上方规则优秀线不是一回事" in _src,
   "O11.7 统计栏明确两条线不是一回事")
ck("拿规则优秀线顶替成「行业平均」" in _src,
   "O11.7 明示禁止拿规则线顶替行业平均")
ck("等于造一个未经支持的数字" in _src, "O11.7 说明顶替的后果是造假")

print("\n[O11.8] 取数与接入点")
ck("_peer_stat_for(_single_code, _single_name)" in _src,
   "O11.8 基准条按当前股票取同业统计")
ck("ist.stats_text(_pst" in _src, "O11.8 研报上下文注入同业统计块")
ck("report, _refined, allowed=_allow" in _src,
   "O11.8 润色守卫带上允许列表")
ck('st.session_state["peer_stat"] = _peer_st' in _src,
   "O11.8 统计块存业务键供后续使用")
ck('os.environ.get("INDUSTRY_STAT_OFF"' in _src,
   "O11.8 有测试开关，回归可不联网")

print("\n[O11.9] 中值与均值必须分开显示")
ck("营收同比中值" in _src and "均值 <span class=\"bb-num\">" in _src,
   "O11.9 页面上中值与均值是两个字段")

print("\n[O11.10] 系统提示词写清两条口径")
ck("【行业基准的两种口径必须分开】" in _src, "O11.10 提示词有专门章节")
# 只匹配核心片段：原文这句带 Markdown 加粗 **不得**，整句匹配太脆
ck("把\"落后规则优秀线\"表述成\"低于行业平均水平\"" in _src.replace("“", '"').replace("”", '"'),
   "O11.10 提示词禁止把规则线说成行业平均")
ck("必须说明是「中值」还是「均值」" in _src, "O11.10 提示词要求区分中值/均值")
ck("上下文中若**没有**同业统计区块，就不得杜撰任何" in _src,
   "O11.10 提示词禁止杜撰行业平均")

print("\n[O11.11] 样式两条线颜色不同（视觉上就不是一条）")
ck(".base-bar .bb-stat {" in _src, "O11.11 统计条有独立样式")
ck(".base-bar .bb-nodata {" in _src, "O11.11 未取到时有独立的灰色样式")

print("\n" + "=" * 72)
print("第三层 · AppTest 端到端（关掉联网，覆盖『未取到』分支）")
print("=" * 72)

from streamlit.testing.v1 import AppTest

# ⚠️ 与 eval_b_issues 同样的两个坑：
#   ① 历史记录必须在创建 AppTest **之前**写盘；
#   ② 绝不能直接 import app（会污染 st.form 上下文，导致所有 button 报错）。
import json as _json
import time as _time

_h = [{"id": "x", "name": "目标公司", "code": "", "industry": "通用",
       "total": None, "ts": "10:00:00", "_ts": _time.time(), "snapshot": {}}]
_hp = os.environ["ANALYSIS_HISTORY_CACHE"]
os.makedirs(os.path.dirname(_hp), exist_ok=True)
with open(_hp, "w", encoding="utf-8") as _fh:
    _json.dump(_h, _fh, ensure_ascii=False, default=str)

at = AppTest.from_file("app.py", default_timeout=120)
at.session_state["authenticated"] = True
at.session_state["main_view"] = "🏢"
at.run()
ck(not at.exception, "O11 单公司页无异常", at.exception)


def _md_all():
    return " ".join(str(getattr(m, "value", "")) for m in at.markdown)


print("\n[O11.12] 基准条同时存在两条线")
_all = _md_all()
ck("规则优秀线" in _all, "O11.12 页面显示规则优秀线", _all[:300])
ck("同业样本统计" in _all, "O11.12 页面显示同业样本统计", _all[:300])
ck("本项目设定" in _all, "O11.12 规则线标注了来源", _all[:300])

print("\n[O11.13] 关掉取数时只说『没拿到』，不伪造统计")
ck("没有证券代码，无从定位同业样本" in _all or "未取到东方财富 F10 的同业样本" in _all,
   "O11.13 明确说明没取到同业样本", _all[:400])
ck("此处不显示行业平均" in _all,
   "O11.13 明示此处不显示行业平均（关键：不拿规则线顶替）", _all[:400])
# 关键红线：没有真实样本时，页面不得冒出任何"行业平均/中值"的具体数值
ck("同业中值" not in _all, "O11.13 无样本时不出现同业中值数值", _all[:400])
ck("同业均值" not in _all, "O11.13 无样本时不出现同业均值数值", _all[:400])

print("\n[O11.14] 手动材料（无代码）时不显示同业统计")
import hashlib as _hl2

_mt = ("QA测试公司（虚构）2025年报：营业收入10亿元，同比下降20%；加权ROE为-5%。")
at.session_state["single_src"] = "✍️ 手动粘贴 / 上传"
at.session_state["single_text"] = _mt
at.session_state["ctx_name"] = "QA测试公司"
at.session_state["ctx_code"] = ""
at.session_state["ctx_source"] = "manual"
at.session_state["ctx_material"] = _hl2.md5(_mt.encode("utf-8")).hexdigest()[:10]
at.session_state["data_sig"] = None
at.run()
ck(not at.exception, "O11.14 手动材料页无异常", at.exception)
_m = _md_all()
ck("没有证券代码，无从定位同业样本" in _m,
   "O11.14 手动材料说明无代码所以无从定位同业", _m[:400])
ck("同业中值" not in _m, "O11.14 手动材料不展示同业中值", _m[:400])

# ---------------------------------------------------------------------------
# 联网层（默认跳过）
# ---------------------------------------------------------------------------
if _LIVE:
    print("\n" + "=" * 72)
    print("第四层 · 联网实测（INDUSTRY_STAT_LIVE=1）")
    print("=" * 72)
    _real = ist.industry_stats("600519", "贵州茅台")
    print("\n[O11.15] 真实取数")
    ck(_real is not None, "O11.15 能取到茅台的同业统计")
    if _real:
        ck(ist.usable(_real), "O11.15 取到的统计可用", _real)
        ck((_real.get("roe") or {}).get("median") is not None,
           "O11.15 有 ROE 中值", _real.get("roe"))
        ck(_real.get("n", 0) >= ist.MIN_PEERS,
           "O11.15 样本数达下限", _real.get("n"))
        # 中值与均值是**不同**的两个数（若相等说明接口口径变了）
        _r = _real.get("roe") or {}
        if _r.get("median") is not None and _r.get("avg") is not None:
            ck(_r["median"] != _r["avg"],
               "O11.15 中值与均值确实是两个不同的值", _r)

    # 真实链路的最后一步：用真数据渲染统计栏 HTML。
    # ⚠️ 放在 AppTest **之后**：这里的 import app 会把 st.form 上下文留在
    #    当前线程，放在 AppTest 之前会让所有 st.button 报错。
    print("\n[O11.16] 真实数据渲染统计栏")
    try:
        import app as _app

        _html = _app._peer_stat_html(_real, "贵州茅台")
        ck('class="bb-sub bb-stat"' in _html,
           "O11.16 真数据时用绿色统计条样式", _html[:120])
        ck("同业样本统计" in _html, "O11.16 渲染出同业统计标题", _html[:160])
        ck("中值" in _html and "均值" in _html,
           "O11.16 中值与均值都渲染出来", _html[:400])
        ck("个百分点" in _html, "O11.16 本股对比用百分点", _html[:600])
        ck("东方财富 F10" in _html, "O11.16 标注数据来源", _html[:200])
        ck("样本 10 家" in _html, "O11.16 标注样本数", _html[:200])
        ck("与上方规则优秀线不是一回事" in _html,
           "O11.16 提醒两条线不是一回事", _html[-200:])
        # 统计栏里绝不能出现"规则优秀线"的数值冒充统计
        ck("<b>规则优秀线" not in _html,
           "O11.16 统计栏内部不再混入规则线", _html[:200])
    except Exception as _e:
        ck(False, f"O11.16 渲染异常：{type(_e).__name__}: {_e}")

    # 拿不到数据时的渲染：必须走灰色"未取到"，且不伪造
    _h0 = _app._peer_stat_html({"ok": False, "n": 2, "roe": {}, "rev": {}}, "")
    ck('class="bb-sub bb-nodata"' in _h0, "O11.16 无数据时用灰色未取到样式", _h0[:120])
    ck("此处不显示行业平均" in _h0, "O11.16 明示不显示行业平均", _h0[:300])
    ck("中值" not in _h0 and "均值" not in _h0,
       "O11.16 无数据时不冒出任何中值/均值", _h0[:300])
else:
    print("\n[跳过] 联网断言（设 INDUSTRY_STAT_LIVE=1 开启）")

print("\n" + "=" * 72)
if FAIL:
    print(f"FAILED: {len(FAIL)} -> {FAIL}")
    sys.exit(1)
print("ALL_O11_TESTS_PASSED")

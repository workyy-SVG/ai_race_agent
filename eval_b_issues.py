# -*- coding: utf-8 -*-
"""
B01 ~ B12 回归测试（外部实测问题清单固化）
=========================================

把《智能体测试问题清单》里 12 项有页面证据的问题**固化成可重复执行的断言**，
对应优化方案 O12（自动回归与 RAG 评测）：12 项问题转成测试，防止缺陷回归。

分三层验证：
  1. 纯函数层：fincalc / analysis_context（不依赖 Streamlit，随时可跑）；
  2. 源码静态层：app.py 里的关键写法（防止重构时把已修的点改回去）；
  3. 端到端层：Streamlit AppTest 真实跑一遍应用，验证页面行为。
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

# 持久缓存必须重定向到临时目录（否则测试记录会污染真实分析记录）
_TF = tempfile.mkdtemp(prefix="hist_b_")
os.environ["ANALYSIS_HISTORY_CACHE"] = os.path.join(_TF, "_hist_cache.json")

FAIL = []


def ck(cond, label, detail=""):
    print(("  OK   " if cond else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAIL.append(label)


print("=" * 72)
print("第一层 · 纯函数（fincalc / analysis_context）")
print("=" * 72)

import fincalc as fc
import analysis_context as actx

# ---------------- B07：数值方向 ----------------
print("\n[B07] 语言方向校验")
_fx, _fs = fc.fix_direction("净利率由 50.53% 降至 50.75%，毛利率由 90.1% 升至 91.2%。")
ck("由 50.53% 升至 50.75%" in _fx, "B07 方向写反被改回（降至→升至）", _fx)
ck("由 90.1% 升至 91.2%" in _fx, "B07 方向正确的句子不动", _fx)
ck(len(_fs) == 1, "B07 只报一条修正记录", _fs)
ck(fc.describe_change("净利率", 50.53, 50.75).find("+0.22 个百分点") > 0,
   "B07 变化量用百分点 pp（+0.22 个百分点）",
   fc.describe_change("净利率", 50.53, 50.75))
ck("下降" in fc.describe_change("净利率", 50.75, 50.53),
   "B07 下降方向由数值决定", fc.describe_change("净利率", 50.75, 50.53))
ck(fc.direction_word(50.53, 50.53) == "基本持平", "B07 相等判为持平")
ck(fc.pp_delta(50.53, 50.75) == 0.22, "B07 百分点差值正确")
ck(fc.pp_delta(None, 50.75) is None, "B07 缺一端返回 None（不猜）")
_fx2, _fs2 = fc.fix_direction("营收由 100 亿元 降低到 80 亿元，成本由 12% 提高到 15%。")
ck(len(_fs2) == 0, "B07 原本正确的降/升都不动", _fx2)

# ---------------- B08：PB / PE 依赖分离 ----------------
print("\n[B08] 估值比率计算")
_pb, _pbmsg = fc.pb_from(1329.0, 200.99)
ck(abs(_pb - 6.6123) < 0.01, "B08 PB = 1329 / 200.99 = 6.61", _pb)
ck("1329" in _pbmsg and "200.99" in _pbmsg, "B08 PB 说明里给出输入值与算式", _pbmsg)
_v, _m = fc.pb_from(1329.0, None)
ck(_v is None and "每股净资产" in _m, "B08 缺每股净资产 → 说明缺什么", _m)
_v, _m = fc.pb_from(1329.0, -1.0)
ck(_v is None and "≤ 0" in _m, "B08 每股净资产为负 → PB 不适用", _m)
_v, _m = fc.pe_from(1329.0, None)
ck(_v is None and "EPS" in _m, "B08 缺 EPS 只影响 PE，不影响 PB", _m)
_inp = fc.parse_valuation_inputs(
    "- 最新价：1329.0 元\n- 市盈率(PE-TTM)：24.06\n- 每股净资产 200.99 元")
ck(_inp["price"] == 1329.0 and abs(_inp["bps"] - 200.99) < 1e-6,
   "B08 能从取数摘要中抽出股价/每股净资产", _inp)
ck(_inp["pe_ttm"] == 24.06, "B08 能抽出 PE-TTM", _inp)
_vb = fc.valuation_block("- 最新价：1329.0 元\n- 每股净资产 200.99 元")
ck("PB = 股价 1329 / 每股净资产 200.99 = 6.61" in _vb,
   "B08 估值块给出 PB 计算过程", _vb)
ck("无法判断是否便宜" in _vb or "不分位" in _vb or "分位" in _vb,
   "B08 估值块区分『算得出值』与『判不出高低』", _vb)
_vb2 = fc.valuation_block("没有任何行情数据")
ck("无法计算 PB" in _vb2, "B08 无输入时说明无法计算", _vb2)

# ---------------- B12：分布偏度 ----------------
print("\n[B12] 图表解读不再用最大/最小比冒充偏度")
_src = open("app.py", encoding="utf-8").read()
ck("分布偏度可通过最大/最小比" not in _src, "B12 已移除『分布偏度…最大/最小比』模板")
_neg = [{"item": "营业收入", "value": 10}, {"item": "归母净利润", "value": -1},
        {"item": "经营活动现金流净额", "value": -2}]
ins = _src[_src.find("def build_chart_insight"):]
ins = ins[:ins.find("\ndef build_trend_insight")]
ck("本期亏损" in ins and "现金净流出" in ins,
   "B12 负值科目按业务含义解读（亏损 / 净流出）")
ck("最大/最小比" not in ins, "B12 图解读里不再出现最大/最小比")

# ---------------- B06：润色不改事实 ----------------
print("\n[B06] 润色守卫")
_d = "## 摘要\n茅台 ROE 为 32.5%。白酒业 ROE 11% 即显落后。"
_p = ("以下是经过专业润色与一致性校验版本：\n润色原则如下：\n1. 保持原意\n\n"
      "## 摘要\n茅台 ROE 为 32.5%。显著优于白酒行业约 11% 的平均水平。")
_t, _st, _is = fc.polish_guard(_d, _p)
ck(_st == "keep_draft", "B06 润色引入『平均水平』→ 退回初稿", _st)
ck(any("平均水平" in i for i in _is), "B06 记录具体的口径变更", _is)
ck("平均水平" not in _t, "B06 采用的文本里没有新增口径")
_t2, _st2, _is2 = fc.polish_guard(_d, "## 摘要\n茅台 ROE 为 32.5%，即显落后于同业。")
ck(_st2 == "pass", "B06 只改表达的润色通过", _st2)
_t3, _st3, _is3 = fc.polish_guard(_d, "## 摘要\n茅台 ROE 为 99.9%，行业排名第 3。")
ck(_st3 == "keep_draft" and any("99.9" in i for i in _is3),
   "B06 新增数字 → 退回初稿", _is3)
_t4, _st4, _is4 = fc.polish_guard(_d, "")
ck(_st4 == "keep_draft", "B06 润色为空 → 保留初稿")

# ---- B06 的边界：数字有出处 ≠ 口径正确 ----
# B06 的本质是「数字没变、口径被偷换」：11% 在初稿里是**规则线**，
# 润色却把它说成行业平均。因此放行条件必须苛刻：
#   数值必须来自**本次真实注入的同业统计**（allowed），
#   来自初稿（尤其是规则线数值）的一律不算 —— 否则守卫形同虚设。
_t5, _st5, _is5 = fc.polish_guard(
    "## 摘要\n茅台 ROE 为 32.5%。",
    "## 摘要\n茅台 ROE 为 32.5%，高于行业中值 13.24%。",
    allowed=["13.24"])
ck(_st5 == "pass", "B06 有真实注入值时允许引用同业中值", _st5)
_t6, _st6, _is6 = fc.polish_guard(
    "## 摘要\n茅台 ROE 为 32.5%。",
    "## 摘要\n茅台 ROE 为 32.5%，高于行业中值 20%。",
    allowed=["13.24"])
ck(_st6 == "keep_draft", "B06 未注入的中值 → 退回初稿", _st6)
# 邻近窗口不得把 "13.24" 切成 "24"（中文无空格分隔，切串取数会凭空造数）
ck(not any(" 24）" in i or "24 " in i for i in _is5),
   "B06 邻近取数按 token 位置，不会把 13.24 切成 24", _is5)

# ---------------- B10：成稿格式 ----------------
print("\n[B10] 成稿清洗")
_c = fc.strip_polish_preface(
    "以下是这份报告的专业润色与一致性校验版本：\n润色原则如下：\n1. 不改数字\n\n"
    "## 一、公司概况\n正文内容")
ck(_c.startswith("## 一、公司概况"), "B10 正文从正式标题开始", _c[:30])
ck("润色原则如下" not in _c, "B10 加工前言已去除", _c[:30])
_f = fc.fix_markdown("成长性 **–1.2%**，毛利率 **–4.53% 不成对")
ck(_f.count("**") % 2 == 0, "B10 落单的 ** 被修掉", _f)
ck("–4.53%" in _f, "B10 修标记时不动数字本身", _f)

# ---------------- B01 / B02：上下文 ----------------
print("\n[B01/B02] 统一分析上下文")
ck(actx.detect_code("QA测试公司（虚构）2025年报：营业收入10亿元") == "",
   "B02 材料里没有代码 → 识别为空")
ck(actx.detect_code("贵州茅台（600519）2025年报") == "600519",
   "B01 材料里写了代码 → 识别出来")
ck(actx.detect_code("营业收入150000万元") == "",
   "B02 金额不是证券代码（150000万元 不会被当成代码）")
ck(actx.blank()["code"] == "", "B02 空上下文代码为空")

print("\n" + "=" * 72)
print("第二层 · app.py 源码静态断言")
print("=" * 72)

print("\n[B01] 缺指标不回退旧值")
ck("actx.code_for_source(actx.SOURCE_ONLINE)" in _src,
   "B01 年报趋势按当前分析上下文取代码")
ck("当前材料缺少定位所需指标（" in _src, "B01 缺指标时给出明确说明")
ck("缺失指标不会用" in _src and "顶替" in _src, "B01 明确不会用旧值顶替")

print("\n[B02] 身份同步")
ck("actx.identity_for_history(analysis)" in _src, "B02 历史记录身份来自统一上下文")
ck("st.session_state.pop(\"current_target\", None)" in _src,
   "B02 手动模式清理旧的 current_target")
ck("_ctx_now.get(\"name\")" in _src, "B02 推荐问题读统一上下文")
ck("手动材料 · 未识别到证券代码" in _src, "B02 手动模式顶栏明确说明未识别代码")
ck("actx.set_manual(raw_data, _mat_key)" in _src,
   "B02 手动模式按材料指纹重建身份")

print("\n[B03] 导航往返不重置")
ck('st.session_state["biz_industry"] = _default_industry' in _src,
   "B03 业务行业值存在非控件键 biz_industry")
ck('st.session_state["biz_industry"] = _v' in _src,
   "B03 手动切换行业也写回业务键")
ck("_biz_now = st.session_state.get(\"biz_industry\", \"\")" in _src,
   "B03 非单公司视图读业务键而不是控件键")
ck('st.session_state["biz_industry"] = _rs_ind' in _src,
   "B03 历史恢复时一并恢复行业")

print("\n[B04] 取数后保留入口")
_i = _src.find("def online_fetch_panel")
_pn = _src[_i:_src.find("\ndef _topbar_html")]
ck("紧凑模式**不再提前 return**" in _pn,
   "B04 紧凑模式取数成功后不再提前 return")
ck("return text, name, \"\"\n    elif compact" not in _pn,
   "B04 成功分支里不再有 return 挡住后续面板")
ck('"📄 原始数据 / 年报 PDF（点击展开）"' in _pn, "B04 紧凑模式给出 PDF / 原始数据入口")
ck('_compact_pdf_body()' in _pn, "B04 紧凑面板有实现")
ck("查看获取到的原始数据" in _pn, "B04 单公司保留原始数据入口")
ck("（尚未取到数据 —— 先在上方检索一家上市公司）" in _pn,
   "B04 未取数时也保留入口（内容随状态变化）")

print("\n[B05] 证据状态四种结果")
ck("— 未执行核验：本次材料没有可核验的数值指标" in _src,
   "B05 无可核验指标时显示『未执行核验』")
_i = _src.find("证据核对通过：指标与图表数值")
ck(_i > 0 and "if not _ev_num:" in _src[:_i],
   "B05 『核对通过』必须先经过可核验项判断")

print("\n[B09] 成功结果与失效提示不共存")
ck('st.session_state["_stale_pending"] = _sig_now' in _src,
   "B09 失效提示改为记账而非当场渲染")
ck('st.session_state.pop("_stale_pending", None)' in _src,
   "B09 分析成功后撤掉失效提示")
ck("【B09 统一失效提示出口】" in _src, "B09 有统一出口")

print("\n[B11] 缺失评分展示")
ck("—（数据不足未评分）" in _src, "B11 缺失综合评分显示『数据不足未评分』")
ck("_ht = _h.get(\"total\")" in _src, "B11 先取值再判断，不再直接拼 None")

print("\n[B06/B10] 润色守卫接入")
ck("fc.polish_guard(" in _src and "report, _refined, allowed=_allow)" in _src,
   "B06 研报润色结果过守卫（O11 起带 allowed 放行真实统计值）")
ck("fc.fix_markdown(fc.strip_polish_preface(report))" in _src,
   "B10 成稿统一清洗加工前言与落单标记")

print("\n[B07/B08] 接入点")
ck("fc.fix_direction(answer)" in _src, "B07 追问回答做方向校验")
ck("fc.valuation_block(raw_data)" in _src, "B08 研报上下文注入程序计算的估值")
ck("【指标计算的硬性口径】" in _src, "B08 系统提示词写清 PB/PE 依赖分离")
ck("连带把 PB 也说成无法计算" in _src, "B08 提示词禁止连带否定 PB")

print("\n" + "=" * 72)
print("第三层 · AppTest 端到端")
print("=" * 72)

from streamlit.testing.v1 import AppTest

# ⚠️ 两个坑都要避开：
#   ① 历史记录必须在创建 AppTest **之前**写盘（app 启动时才从文件加载）；
#   ② 这里**不能直接 import app** —— 导入即执行整份 Streamlit 脚本，
#      会把登录页的 st.form 上下文留在当前线程里，导致后续 AppTest 里
#      所有 st.button 都报 "can't be used in an st.form()"
#      （这是 AppTest 的环境特性，与被测代码无关）。
#      所以直接用 json 写盘，格式与 _hist_save 保持一致。
import json as _json
import time as _t
_h = [{"id": "x", "name": "目标公司", "code": "", "industry": "通用",
       "total": None, "ts": "10:00:00", "_ts": _t.time(), "snapshot": {}}]
_hist_path = os.environ["ANALYSIS_HISTORY_CACHE"]
os.makedirs(os.path.dirname(_hist_path), exist_ok=True)
with open(_hist_path, "w", encoding="utf-8") as _fh:
    _json.dump(_h, _fh, ensure_ascii=False, default=str)

print("\n[通用] 应用可正常加载")
at = AppTest.from_file("app.py", default_timeout=120)
at.session_state["authenticated"] = True
at.run()
ck(not at.exception, "登录态下应用无异常", at.exception)

print("\n[B11] 历史记录不显示 None")
_caps = " ".join(str(getattr(c, "value", "")) for c in at.caption)
ck("综合 None" not in _caps, "B11 侧栏记录不出现『综合 None』", _caps[:300])
ck("数据不足未评分" in _caps, "B11 缺失评分显示为『数据不足未评分』", _caps[:300])

print("\n[B03] 导航往返不重置行业、不清空结果")
# 直接模拟"用户选了消费行业 + 已有一条分析结果"
at.session_state["biz_industry"] = "消费"
at.session_state["industry_widget"] = "消费"
at.run()
_iv = None
for _r in at.radio:
    if _r.label == "行业":
        _iv = _r.value
ck(_iv == "消费", "B03 单公司页行业显示为消费", _iv)
# 切到多公司对比 → 再切回单公司
at.session_state["main_view"] = "⚖️"
at.run()
ck(not at.exception, "B03 切到对比页无异常", at.exception)
at.session_state["main_view"] = "🏢"
at.run()
_iv2 = None
for _r in at.radio:
    if _r.label == "行业":
        _iv2 = _r.value
ck(_iv2 == "消费", "B03 往返后行业仍是消费（旧版会变成通用）", _iv2)
ck(not at.exception, "B03 往返后无异常", at.exception)
# 再走一次「单公司 → 使用说明 → 单公司」
at.session_state["main_view"] = "📖"
at.run()
at.session_state["main_view"] = "🏢"
at.run()
_iv3 = None
for _r in at.radio:
    if _r.label == "行业":
        _iv3 = _r.value
ck(_iv3 == "消费", "B03 经使用说明往返后行业仍是消费", _iv3)

print("\n[B04] 取数成功/未取数都保留原始数据与 PDF 入口")
_iv_all = " ".join(
    str(getattr(_e, "label", "")) for _e in at.expander)
ck("查看获取到的原始数据" in _iv_all, "B04 单公司页有『查看获取到的原始数据』", _iv_all[:200])
ck("一键获取并解析年报 PDF" in _iv_all, "B04 单公司页有年报 PDF 解析面板", _iv_all[:200])
# 模拟取数成功
at.session_state["single_online"] = "## 一、市场表现\n- 最新价：1329.0 元"
at.session_state["single_online_code"] = "600519"
at.session_state["single_online_name"] = "贵州茅台"
at.run()
_iv_all2 = " ".join(str(getattr(_e, "label", "")) for _e in at.expander)
ck("查看获取到的原始数据" in _iv_all2, "B04 取数成功后原始数据入口仍在", _iv_all2[:200])
ck("一键获取并解析年报 PDF" in _iv_all2, "B04 取数成功后 PDF 入口仍在", _iv_all2[:200])
ck(not at.exception, "B04 取数成功后无异常", at.exception)
# 多公司对比页：每家公司都得有入口
at.session_state["main_view"] = "⚖️"
at.run()
ck(not at.exception, "B04 对比页无异常", at.exception)
_cmp_exp = " ".join(str(getattr(_e, "label", "")) for _e in at.expander)
ck(_cmp_exp.count("原始数据 / 年报 PDF") >= 2,
   "B04 对比页每家公司都有『原始数据 / 年报 PDF』入口", _cmp_exp[:200])

print("\n[B02] 端到端：手动材料绝不继承上一次在线公司的身份")
# 场景（对应实测 B02）：先在线分析过 600519，再切到手动粘贴虚构公司材料。
# 会话里 single_online_name/code 仍然是茅台 —— 页面必须**不**再拿它当当前对象。
_manual_text = ("QA测试公司（虚构，仅用于功能测试）2025年报：营业收入10亿元，"
                "同比下降20%；加权ROE为-5%。")
import hashlib as _hl
at.session_state["main_view"] = "🏢"
at.session_state["single_src"] = "✍️ 手动粘贴 / 上传"
at.session_state["single_online_name"] = "贵州茅台"
at.session_state["single_online_code"] = "600519"
at.session_state["single_online"] = "## 茅台数据"
at.session_state["single_text"] = _manual_text
at.session_state["ctx_name"] = "QA测试公司"
at.session_state["ctx_code"] = ""
at.session_state["ctx_source"] = "manual"
at.session_state["ctx_material"] = _hl.md5(
    _manual_text.encode("utf-8")).hexdigest()[:10]
# 数据指纹置空 = 跳过当轮的"数据版本变化 → 清空结果"（与历史恢复同一条路径），
# 否则刚放进去的 analysis 会被本轮清掉，测不到真正想测的渲染分支
at.session_state["data_sig"] = None
at.session_state["analysis"] = {
    "company": "QA测试公司", "scores": {"盈利能力": 1.7}, "total_score": 1.4,
    "score_reasons": {}, "score_missing": [], "metrics_raw": {}, "metrics": [],
}
at.run()
ck(not at.exception, "B02 手动模式端到端无异常", at.exception)
_tb = " ".join(str(getattr(m, "value", "")) for m in at.markdown)
ck("QA测试公司" in _tb, "B02 顶栏/页面显示本次材料识别出的公司", _tb[:200])
ck("手动材料 · 未识别到证券代码" in _tb,
   "B02 顶栏说明手动材料未识别到证券代码", _tb[:300])
_bs = [str(getattr(b, "label", "")) for b in at.button]
_sg = [x for x in _bs if "得分最高" in x or "毛利率与同行业" in x]
ck(bool(_sg), "B02 手动材料下仍生成推荐问题", _sg[:2])
ck(all("贵州茅台" not in x for x in _sg),
   "B02 推荐问题不再安在旧公司（贵州茅台）名下", _sg[:2])
ck(any("QA测试公司" in x for x in _sg),
   "B02 推荐问题用的是本次分析对象的名字", _sg[:2])

print("\n[B01] 端到端：无财务材料时不画象限定位点")
at.session_state["single_text"] = ("这是用于功能测试的普通文本，没有公司名称，"
                                   "没有财务数据，请不要补造指标。")
at.session_state["ctx_name"] = ""
at.session_state["ctx_code"] = ""
at.session_state["ctx_material"] = _hl.md5(
    "这是用于功能测试的普通文本，没有公司名称，没有财务数据，请不要补造指标。"
    .encode("utf-8")).hexdigest()[:10]
at.session_state["data_sig"] = None
at.session_state["analysis"] = {
    "company": "目标公司", "scores": {}, "total_score": None,
    "score_reasons": {}, "score_missing": [], "metrics_raw": {}, "metrics": [],
}
at.session_state["single_rag_status"] = {}
at.run()
ck(not at.exception, "B01 无财务材料端到端无异常", at.exception)
_txts = " ".join(str(getattr(m, "value", "")) for m in at.markdown)
_infos = " ".join(str(getattr(i, "value", "")) for i in at.info) if hasattr(at, "info") else ""
_all1 = _txts + " " + _infos
ck("当前材料缺少定位所需指标" in _all1,
   "B01 明确说明缺少定位所需指标", _all1[:300])
ck(not any(_q in _all1 for _q in ("现金牛型", "瘦狗型", "明星型", "问题型")),
   "B01 不再输出其他来源的象限结论")
ck("定位：<b>" not in _all1, "B01 不渲染公司定位点结论")

print("\n[B09] 分析成功后不再残留『旧结果已清空』提示")
at.session_state["main_view"] = "🏢"
at.session_state["analysis"] = {
    "company": "贵州茅台", "scores": {"盈利能力": 8.4}, "total_score": 8.4,
    "score_reasons": {}, "score_missing": [], "metrics_raw": {}, "metrics": [],
}
at.session_state["data_sig"] = "sig-A"
at.session_state["_stale_pending"] = "sig-A"
at.run()
_all_md = " ".join(str(getattr(m, "value", "")) for m in at.markdown)
_all_ex = " ".join(str(getattr(m, "value", "")) for m in at.exception) if at.exception else ""
ck("旧的分析结果与研报已清空" not in _all_md,
   "B09 有分析结果时不显示清空提示", _all_md[:200])

print()
if FAIL:
    print(f"FAILED: {len(FAIL)} -> {FAIL}")
    sys.exit(1)
print("ALL_B_ISSUES_TESTS_PASSED")

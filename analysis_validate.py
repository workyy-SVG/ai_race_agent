# -*- coding: utf-8 -*-
"""
结构化分析的输出校验层（自 app.py 抽出，供页面与离线评测共用）
==============================================================
为什么单独成模块：
  · 页面（app.py）与评测脚本（eval_cases.py）必须走【同一个】校验实现。
    否则会出现"评测脚本压根没调用校验、却对外宣称全流程都做了校验"的失真结论 ——
    这正是评审最容易一眼看穿的漏洞。
  · 校验逻辑是纯函数（不联网、不依赖 Streamlit），可以脱离运行时单独测试。

职责：
  · 字段 / 类型 / 范围兜底 —— 页面永不因模型返回格式异常而崩溃
  · 图表与指标明细的口径校准（期别感知：绝不跨报告期用明细值覆盖图表值）
  · 指标覆盖率门槛 —— 指标太少时撤下"综合评价"，不给低覆盖公司满分
  · 关键数值与页码引用的证据核对（verify_evidence）

自检: python analysis_validate.py
"""
from __future__ import annotations

import re
import json
import math
import hashlib
from bisect import bisect_left
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # 覆盖率门槛与评分引擎同源，避免两处阈值打架
    import scoring as _scoring
    MIN_SCORE_COVERAGE = float(getattr(_scoring, "MIN_COVERAGE", 0.5))
except Exception:  # pragma: no cover
    MIN_SCORE_COVERAGE = 0.5

# app.py 内部沿用的私有别名
_MIN_SCORE_COVERAGE = MIN_SCORE_COVERAGE


def parse_json_loose(text):
    """从模型输出里尽力抠出 JSON 对象（容忍 ```json 包裹与前后多余文字）。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


# ============ LLM 输出严格校验（页面永不因模型格式异常而崩溃） ============
def _safe_num(v, lo=None, hi=None):
    """转数值并夹范围；非法值返回 None。"""
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        if lo is not None:
            f = max(lo, f)
        if hi is not None:
            f = min(hi, f)
        return f
    except (TypeError, ValueError):
        return None


def validate_analysis(a):
    """
    校验并修复结构化分析结果。
    返回 (analysis, issues)。所有字段兜底到安全默认值，异常项被剔除/修复。
    """
    issues = []
    if not isinstance(a, dict):
        return {}, ["模型输出不是 JSON 对象，已丢弃"]

    out = {}

    # -- 基本字段 --
    out["company"] = str(a.get("company") or "目标公司").strip()[:50] or "目标公司"
    out["summary"] = str(a.get("summary") or "").strip()[:400]

    # -- metrics: list[dict]，逐项校验 --
    metrics = []
    raw_metrics = a.get("metrics")
    if isinstance(raw_metrics, list):
        for m in raw_metrics[:12]:
            if not isinstance(m, dict):
                continue
            item = str(m.get("item") or "").strip()[:30]
            if not item:
                continue
            val = m.get("value")
            val_out = _safe_num(val)
            if val_out is None and val not in (None, ""):
                val_out = str(val)[:20]  # 文本型数值（如 "91.5%"）保留原文
            elif val_out is not None:
                val_out = round(val_out, 4)
            metrics.append({
                "item": item,
                "value": val_out if val_out is not None else (str(val)[:20] if val not in (None, "") else "N/A"),
                "unit": str(m.get("unit") or "").strip()[:10],
                "comment": str(m.get("comment") or "").strip()[:120],
            })
    if not metrics:
        issues.append("metrics 缺失或格式异常，已置空")
    out["metrics"] = metrics

    # -- chart_data: 仅保留合法数值，最多 6 项 --
    chart = []
    raw_chart = a.get("chart_data")
    if isinstance(raw_chart, list):
        for c in raw_chart:
            if not isinstance(c, dict):
                continue
            v = _safe_num(c.get("value"), lo=-1e6, hi=1e7)
            if v is None:
                continue
            item = str(c.get("item") or "").strip()[:20]
            if item:
                chart.append({"item": item, "value": v})
    chart = chart[:6]
    if isinstance(raw_chart, list) and len(raw_chart) > 0 and not chart:
        issues.append("chart_data 全部非法（非数值），图表已隐藏")

    # -- chart_data 与 metrics 交叉校准：同名金额指标以指标明细为准 --
    # （模型分别提取两处时可能口径不一致，如图表取到中报、明细取到年报，
    #   导致柱状图与明细表对不上；此处强制对齐明细表的数值）
    # ⚠️ 期别感知：图表项常带「2026中报营业收入」这类期别前缀，而指标明细
    #    通常是最新一期的「营业收入」。二者报告期不同时数值本来就应该不同，
    #    绝不能用明细值覆盖历史期数据（否则柱状图每期柱子都变成同一个数）。
    if chart and metrics:
        m_lookup = {}
        for m in metrics:
            try:
                mv = float(m["value"])
            except (TypeError, ValueError):
                continue
            if m.get("unit", "") not in ("", "亿元", "亿"):
                continue  # 只校准金额类，比率(%)不参与
            m_lookup[m["item"].replace(" ", "")] = mv

        _PERIOD_RE = re.compile(r"20\d{2}\s*(?:年报|中报|一季报|三季报|半年报)")

        def _period_of(s: str) -> str:
            m_ = _PERIOD_RE.search(s)
            return m_.group(0).replace(" ", "") if m_ else ""

        def _match_key(name: str):
            if name in m_lookup:
                return name
            for k in m_lookup:
                if k and (k in name or name in k):
                    return k
            return None

        fixed_chart = []
        for c in chart:
            name = c["item"].replace(" ", "")
            k = _match_key(name)
            if k is not None:
                cp, kp = _period_of(name), _period_of(k)
                if cp == kp:  # 报告期一致才允许用明细值校准
                    mv = m_lookup[k]
                    if abs(mv - c["value"]) > max(0.5, abs(c["value"]) * 0.05):
                        issues.append(
                            f"图表项「{c['item']}」({c['value']}) 与指标明细 ({mv}) 口径不一致，已按明细校准"
                        )
                        c = {"item": c["item"], "value": round(mv, 4)}
                elif cp != kp and (cp or kp):
                    # 期别不同（如中报 vs 年报）：数值差异属正常，跳过校准
                    pass
            fixed_chart.append(c)
        chart = fixed_chart
    out["chart_data"] = chart

    # -- trend: 数值 + 期别标签，去重 --
    trend = []
    seen_periods = set()
    raw_trend = a.get("trend")
    if isinstance(raw_trend, list):
        for t in raw_trend:
            if not isinstance(t, dict):
                continue
            period = str(t.get("period") or "").strip()[:20]
            rev = _safe_num(t.get("revenue"), lo=-1e6, hi=1e7)
            np_ = _safe_num(t.get("net_profit"), lo=-1e6, hi=1e7)
            if rev is None and np_ is None:
                continue
            if not period or period in seen_periods:
                continue
            seen_periods.add(period)
            trend.append({"period": period, "revenue": rev, "net_profit": np_})
    out["trend"] = trend

    # -- scores / total_score: 夹在 [0,10]；总分非法时回退为维度均分 --
    raw_scores = a.get("scores")
    scores = {}
    if isinstance(raw_scores, dict):
        for k in ["盈利能力", "成长性", "偿债能力", "现金流质量"]:
            v = _safe_num(raw_scores.get(k), lo=0, hi=10)
            if v is not None:
                scores[k] = round(v, 1)
    out["scores"] = scores
    _svals = list(scores.values())
    _avg = round(sum(_svals) / len(_svals), 1) if _svals else None

    # 指标覆盖率：低覆盖时【绝不给综合评价】。
    # 历史问题：只输入 roe=20 时，盈利能力 10 分、其余维度为空，
    # 而总分取「非空维度均值」→ 综合分 10.0，等于用一个指标冒充全面优秀。
    _cov = _safe_num(a.get("score_coverage"))
    _cov_insufficient = _cov is not None and _cov < _MIN_SCORE_COVERAGE

    raw_total = a.get("total_score")
    _raw_total_num = _safe_num(raw_total)  # 不夹范围，用于判断是否非法
    total = _safe_num(raw_total, lo=0, hi=10)
    if _cov_insufficient:
        total = None
        issues.append(
            f"指标覆盖率仅 {_cov * 100:.0f}%，低于 {_MIN_SCORE_COVERAGE * 100:.0f}% 门槛 —— "
            "已撤下综合评价（仅保留可得维度分），避免用少量指标冒充全面结论。"
        )
    elif total is None or _raw_total_num is None or not (0 <= _raw_total_num <= 10):
        # 总分缺失或越界 → 用维度分数均分兜底
        total = _avg
        if _raw_total_num is not None and not (0 <= _raw_total_num <= 10):
            issues.append(f"total_score 非法（{raw_total}），已按维度均分回退为 {total}")
    if total is not None:
        total = round(total, 1)
    out["total_score"] = total
    if not scores:
        issues.append("评分缺失（数据不足或规则引擎未启用）")

    # -- risks / highlights: 字符串列表，截断 --
    def _str_list(x, maxn, name):
        if not isinstance(x, list):
            if x not in (None, ""):
                issues.append(f"{name} 格式异常，已置空")
            return []
        cleaned = []
        for it in x:
            s = str(it or "").strip()
            if s:
                cleaned.append(s[:150])
        return cleaned[:maxn]

    out["risks"] = _str_list(a.get("risks"), 4, "risks")
    out["highlights"] = _str_list(a.get("highlights"), 4, "highlights")

    # -- 评分依据（规则引擎生成，原样保留） --
    if isinstance(a.get("score_reasons"), dict):
        out["score_reasons"] = {
            str(k): [str(x)[:200] for x in (v if isinstance(v, list) else [])]
            for k, v in a["score_reasons"].items()
        }
    if a.get("score_engine"):
        out["score_engine"] = str(a["score_engine"])[:40]
    if isinstance(a.get("score_missing"), list):
        out["score_missing"] = [str(x)[:30] for x in a["score_missing"]][:12]
    # 覆盖率 / 置信标签 / 说明（评分可信度的载体，必须显式透传，否则被校验丢掉）
    if _cov is not None:
        out["score_coverage"] = round(_cov, 3)
    if a.get("score_confidence"):
        out["score_confidence"] = str(a["score_confidence"])[:20]
    if isinstance(a.get("score_notes"), list):
        out["score_notes"] = [str(x)[:300] for x in a["score_notes"]][:4]
    if isinstance(a.get("score_expected"), list):
        out["score_expected"] = [str(x)[:30] for x in a["score_expected"]][:20]
    if isinstance(a.get("score_provided"), list):
        out["score_provided"] = [str(x)[:30] for x in a["score_provided"]][:20]
    if a.get("score_industry"):
        out["score_industry"] = str(a["score_industry"])[:20]
    # 金融子行业（银行/保险/证券）同样属于"评分口径"，必须与 score_industry 一起透传：
    # 否则页面无从说明"这一版分数是按哪一版维度表算的"（优化清单 P1-04）。
    if a.get("score_finance_kind"):
        out["score_finance_kind"] = str(a["score_finance_kind"])[:20]

    # -- 标准化原始指标（ROE / 营收增速 等，供「象限定位」散点图取数）--
    #    注意：这里必须显式透传，否则 metrics_raw 会在校验环节被丢掉，
    #    导致散点图永远拿不到数据（历史 bug）。
    _mraw = a.get("metrics_raw")
    if isinstance(_mraw, dict):
        _mraw_clean = {}
        for _k, _v in list(_mraw.items())[:40]:
            _num = _safe_num(_v)
            if _num is not None:
                _mraw_clean[str(_k)[:30]] = _num
        if _mraw_clean:
            out["metrics_raw"] = _mraw_clean
    if a.get("score_source"):
        out["score_source"] = str(a["score_source"])[:60]
    if a.get("score_period"):
        out["score_period"] = str(a["score_period"])[:40]

    return out, issues


# ===========================================================================
# 证据核对：把"看起来专业"的数字与引用，与真正拿到的来源对一遍
# ===========================================================================
_NUM_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 单位换算链。模型按提示词统一输出「亿元」，而年报原表通常以「元 / 千元 / 万元」列示，
# 直接做字符串包含会 100% 误报 —— 实测：
#   「2025年报经营活动现金流净额 = 60.59」（亿元）
#   年报原文写作 6,058,908,000（元）
# 于是出现"每个指标都被判成模型推算"的满屏黄条。允许换算后比对才是正确的对账方式。
_UNIT_SCALES = (1.0, 1e2, 1e3, 1e4, 1e5, 1e8, 1e-2, 1e-3, 1e-4, 1e-5, 1e-8)
# 相对容差：大到能吸收"把 6,058,908,000 元写成 60.59 亿元"（1 位小数舍入 ≈ 8e-4），
# 小到不会把 99999 与 100.5 这种量级完全不同的数字撮合成匹配。
_REL_TOL = 1.5e-3
_NUM_CACHE: "OrderedDict[str, List[float]]" = OrderedDict()


def _extract_numbers(text: str) -> List[float]:
    """抽出文本中所有数字（自动去掉千分位），**返回升序的绝对值列表**（供二分查找）。

    年报全文几十万字，每次核对都重扫一遍会明显拖慢分析；按内容指纹缓存最近 4 份文本。
    """
    if not text:
        return []
    _key = hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()
    _hit = _NUM_CACHE.get(_key)
    if _hit is not None:
        _NUM_CACHE.move_to_end(_key)
        return _hit
    _out: List[float] = []
    for _m in _NUM_TOKEN_RE.finditer(text):
        try:
            _out.append(abs(float(_m.group(0).replace(",", ""))))
        except ValueError:
            continue
    _out.sort()
    _NUM_CACHE[_key] = _out
    while len(_NUM_CACHE) > 4:
        _NUM_CACHE.popitem(last=False)
    return _out


def number_has_source(
    value: Any, text: str, numbers: Optional[List[float]] = None
) -> bool:
    """判断某个数值能否在来源文本中找到出处。三道关，任一通过即算"有据可依"：

      ① 原文直配：`60.59` / `60.6` / `6,059` 这类写法直接出现在文本里；
      ② 单位链换算：`60.59`（亿元）能对上原文的 `6,058,908,000`（元）；
      ③ 舍入容差：模型把 `60.589` 写成 `60.59` 也算命中。

    目的只有一个 —— 既不放过真正凭空编造的数字（如 99999 亿元营收），
    也不因为"模型统一换算成亿元、原文是元"就对着年报原文狂报误警。
    """
    try:
        _v = abs(float(value))
    except (TypeError, ValueError):
        return True
    if _v == 0:
        return True  # 0 不做核对：原文里遍地是 0，比对没有意义
    # ① 原文直配：`60.59` / `60.6` / `6,059` 这类写法直接出现在文本里。
    #    ⚠️ 必须同时试**带千分位**的写法：取数摘要与年报里几乎都写 `1,741.00 亿元`，
    #    而模型输出的是 `1741`，只比无分隔符字符串会让明明有出处的数字被判成"推算"。
    for _s in (f"{_v:g}", f"{_v:.1f}", f"{_v:.2f}", f"{_v:,.2f}",
               f"{_v:,.0f}", f"{_v:,.1f}"):
        if _s and _s in text:
            return True
        _trim = _s.rstrip("0").rstrip(".")
        if _trim and _trim in text:
            return True
    _nums = numbers if numbers is not None else _extract_numbers(text)
    if not _nums:
        return False
    for _k in _UNIT_SCALES:
        _t = _v * _k
        _tol = max(_t * _REL_TOL, 0.005)
        _i = bisect_left(_nums, _t - _tol)
        if _i < len(_nums) and _nums[_i] <= _t + _tol:
            return True
    return False


def verify_evidence(
    analysis: Dict[str, Any],
    raw_data: str = "",
    page_range: Optional[Tuple[int, int]] = None,
    extra_sources: Optional[Sequence[str]] = None,
) -> List[str]:
    """核对分析里的关键数值与页码引用是否有据可依，返回问题清单（空 = 全部通过）。

    · 数值：指标明细 / 图表里的数值应能在**来源文本**中找到
      （容忍千分位、单位换算与两位小数舍入）
    · 页码：正文引用的「第 X 页」不应超出年报实际页数
    只报告、不改数 —— "改还是留"由分析者判断，程序只负责把疑点摆出来。

    ⚠️ ``extra_sources`` 是这次修复的关键：营业成本 / 管理费用 / 经营活动现金流净额
       这类数字往往**只出现在年报原文里**（RAG 检索片段或解析出的年报全文），
       并不在 raw_data（在线取数摘要）里。只拿 raw_data 当证据库，
       会把本来有明确出处的数字全部误报成"模型推算"。
    """
    issues: List[str] = []

    try:
        _sources = [raw_data or ""]
        for _s in (extra_sources or []):
            if _s:
                _sources.append(str(_s))
        _text = "\n".join(_sources)
        if _text.strip():
            _nums = _extract_numbers(_text)
            for m in (analysis.get("metrics") or [])[:20]:
                v = m.get("value")
                if isinstance(v, (int, float)) and not number_has_source(v, _text, _nums):
                    issues.append(
                        f"指标「{m.get('item')}」= {v} 未能在取数摘要 / 年报检索片段中找到对应数值，"
                        "请核对是否来自模型推算（建议以年报原文为准）"
                    )
            for c in (analysis.get("chart_data") or [])[:20]:
                v = c.get("value")
                if isinstance(v, (int, float)) and not number_has_source(v, _text, _nums):
                    issues.append(
                        f"图表项「{c.get('item')}」= {v} 未能在取数摘要 / 年报检索片段中找到对应数值"
                    )

        if page_range:
            _lo, _hi = int(page_range[0]), int(page_range[1])
            _blob = " ".join([
                str(analysis.get("summary") or ""),
                " ".join(str(x) for x in (analysis.get("risks") or [])),
                " ".join(str(x) for x in (analysis.get("highlights") or [])),
            ])
            for _pg in {int(x) for x in re.findall(r"第\s*(\d{1,6})\s*页", _blob)}:
                if _pg < _lo or _pg > _hi:
                    issues.append(
                        f"正文引用了「第 {_pg} 页」，超出年报实际页数范围（{_lo}-{_hi}），引用不可核验"
                    )
    except Exception as e:  # 核对失败不该影响主流程
        issues.append(f"证据核对过程出错（已跳过）：{type(e).__name__}")

    return issues


def _context_around(text: str, value: float, span: int = 70) -> str:
    """在文本中定位某个数值（含千分位/换算写法）并截取上下文，供"可定位到原文"展示。"""
    _v = abs(float(value))
    # 与 number_has_source 保持同一套写法（含千分位），否则会出现
    # "判定为有出处，却截不出上下文" 的自相矛盾展示。
    for _s in (f"{_v:g}", f"{_v:.1f}", f"{_v:,.2f}", f"{_v:,.0f}"):
        _i = text.find(_s)
        if _i >= 0:
            _lo = max(0, _i - span)
            _hi = min(len(text), _i + len(_s) + span)
            return re.sub(r"\s+", " ", text[_lo:_hi]).strip()
    return ""


def evidence_map(
    analysis: Dict[str, Any],
    hits: Optional[Sequence[Dict[str, Any]]] = None,
    raw_data: str = "",
    page_range: Optional[Tuple[int, int]] = None,
) -> List[Dict[str, Any]]:
    """把分析里的每个数值**正向**定位回它的出处（页码 + 原文片段）。

    与 `verify_evidence` 的关系：那个函数回答"哪些数字找不到出处"（异常清单），
    这个函数回答"每个数字出处在哪"（证据卡）。P1-05 要求「关键结论可定位到支持的原文」——
    只有正向映射才让"引用核验"变成可展示、可核查的东西，而不是只报错。

    返回 [{kind, item, value, unit, found, page, source, snippet}]，
    出处优先级：年报检索片段（带页码） > 在线取数摘要 > 未定位。
    """
    out: List[Dict[str, Any]] = []
    _hits = list(hits or [])
    _entries: List[Tuple[str, Dict[str, Any]]] = []
    for _m in (analysis.get("metrics") or [])[:24]:
        _entries.append(("指标", _m))
    for _c in (analysis.get("chart_data") or [])[:24]:
        _entries.append(("图表", _c))

    for _kind, _e in _entries:
        _v = _e.get("value")
        _item = str(_e.get("item") or "")
        _unit = str(_e.get("unit") or "")
        _rec = {
            "kind": _kind, "item": _item, "value": _v, "unit": _unit,
            "found": False, "page": None, "source": "", "snippet": "",
        }
        if not isinstance(_v, (int, float)) or isinstance(_v, bool):
            _rec["source"] = "非数值项，不做数值核对"
            out.append(_rec)
            continue
        for _h in _hits:
            _txt = str(_h.get("text") or "")
            if _txt and number_has_source(_v, _txt):
                _rec["found"] = True
                _pg = _h.get("page")
                _rec["page"] = int(_pg) if isinstance(_pg, (int, float)) and _pg else None
                _rec["source"] = "年报检索片段"
                _rec["snippet"] = _context_around(_txt, _v) or re.sub(r"\s+", " ", _txt[:120])
                break
        if not _rec["found"] and raw_data and number_has_source(_v, raw_data):
            _rec["found"] = True
            _rec["source"] = "在线取数摘要"
            _rec["snippet"] = _context_around(raw_data, _v)
        if not _rec["found"]:
            _rec["source"] = "未定位到出处（可能来自模型换算/推算，建议以年报原文为准）"
        # 页码越界 → 即便数值命中也标为可疑
        if _rec["found"] and _rec["page"] and page_range:
            if not (int(page_range[0]) <= _rec["page"] <= int(page_range[1])):
                _rec["found"] = False
                _rec["page"] = None
                _rec["source"] = "页码越界，引用不可核验"
        out.append(_rec)
    return out


def citation_accuracy(analysis: Dict[str, Any], page_range: Optional[Tuple[int, int]]) -> Dict[str, Any]:
    """正文里出现的页码引用有多少落在真实页数范围内（P1-06 的"引用正确率"）。"""
    _blob = " ".join([
        str(analysis.get("summary") or ""),
        " ".join(str(x) for x in (analysis.get("risks") or [])),
        " ".join(str(x) for x in (analysis.get("highlights") or [])),
    ])
    _refs = [int(x) for x in re.findall(r"第\s*(\d{1,6})\s*页", _blob)]
    if not _refs:
        return {"refs": 0, "ok": 0, "rate": None}
    if not page_range:
        return {"refs": len(_refs), "ok": 0, "rate": None}
    _lo, _hi = int(page_range[0]), int(page_range[1])
    _ok = sum(1 for p in _refs if _lo <= p <= _hi)
    return {"refs": len(_refs), "ok": _ok, "rate": round(_ok / len(_refs), 3)}


# ---------------------------------------------------------------------------
# CLI 自检：不联网、不依赖 Streamlit
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # 1) 覆盖率门槛：只给一个 ROE 时不得给出综合评价
    from scoring import compute_scores as _cs
    _thin = _cs({"roe": 20.0}, "通用")
    _a, _iss = validate_analysis({
        "company": "测试公司",
        "scores": _thin["scores"],
        "total_score": _thin["total_score"],
        "score_coverage": _thin["coverage"],
        "score_confidence": _thin["confidence"],
        "metrics": [{"item": "2025年报营业收入", "value": 99999, "unit": "亿元"}],
    })
    assert _a["total_score"] is None, "低覆盖率仍给出了综合评价"

    # 2) 期别感知：中报图表项不得被年报明细值覆盖
    _b, _ = validate_analysis({
        "metrics": [{"item": "营业收入", "value": 500.0, "unit": "亿元"}],
        "chart_data": [{"item": "2026中报营业收入", "value": 200.0}],
    })
    assert _b["chart_data"][0]["value"] == 200.0, "跨期覆盖回归！"

    # 3) 证据核对：越界页码与无来源数值都应被指出
    _ev = verify_evidence(
        {"metrics": [{"item": "营业收入", "value": 99999, "unit": "亿元"}],
         "summary": "详见年报第99999页"},
        raw_data="营业收入 100.5 亿元",
        page_range=(1, 220),
    )
    assert any("99999" in x for x in _ev), _ev

    # 4) 正常数据不应误报
    _ev2 = verify_evidence(
        {"metrics": [{"item": "营业收入", "value": 100.5, "unit": "亿元"}], "summary": "见年报第12页"},
        raw_data="营业收入 100.5 亿元",
        page_range=(1, 220),
    )
    assert _ev2 == [], _ev2

    # 5) 单位链换算：模型给「亿元」，年报原表给「元」，不得误报
    #    （实盘案例：经营活动现金流净额 60.59 亿元 vs 原文 6,058,908,000 元）
    _ev3 = verify_evidence(
        {"metrics": [{"item": "2025年报经营活动现金流净额", "value": 60.59, "unit": "亿元"}],
         "chart_data": [{"item": "2025年报营业成本", "value": 361.09}]},
        raw_data="## 二、主要财务指标\n- 营业收入 100.5 亿元\n",
        extra_sources=["经营活动现金流量净额 6,058,908,000.00 6,058,908,000.00\n营业成本 36,109,000,000.00"],
    )
    assert _ev3 == [], f"单位换算误报：{_ev3}"

    # 6) extra_sources 生效：数字只在年报全文里也应算"有出处"
    _ev4 = verify_evidence(
        {"chart_data": [{"item": "2025年报销售费用", "value": 86.84}]},
        raw_data="营业收入 100.5 亿元",
        extra_sources=["销售费用 8,684,000,000.00 元"],
    )
    assert _ev4 == [], f"extra_sources 未生效：{_ev4}"

    # 7) 真·编造的数字仍必须被揪出（即使给了年报全文）
    _ev5 = verify_evidence(
        {"metrics": [{"item": "营业收入", "value": 99999, "unit": "亿元"}]},
        raw_data="营业收入 100.5 亿元",
        extra_sources=["营业成本 36,109,000,000.00 元"],
    )
    assert any("99999" in x for x in _ev5), f"编造数值被放过：{_ev5}"

    # 8) 证据卡（正向定位）：数值应能定位回页码 + 原文片段
    _ana = {
        "metrics": [{"item": "2025年报营业收入", "value": 60.59, "unit": "亿元"}],
        "chart_data": [{"item": "2025年报未知科目", "value": 88888}],
    }
    _hits = [
        {"page": 42, "text": "报告期内公司实现营业收入 6,058,908,000.00 元，同比增长 5.2%。"},
        {"page": 88, "text": "公司主要会计政策说明。"},
    ]
    _em = evidence_map(_ana, _hits, raw_data="", page_range=(1, 220))
    assert _em[0]["found"] and _em[0]["page"] == 42, f"证据卡未定位到第 42 页: {_em[0]}"
    assert "6,058,908,000" in _em[0]["snippet"], f"证据卡未带回原文片段: {_em[0]['snippet']}"
    assert not _em[1]["found"], f"编造数值竟被定位到出处: {_em[1]}"

    # 9) 页码越界时，即便数值命中也不得算"可核验"
    _em2 = evidence_map(
        {"metrics": [{"item": "x", "value": 60.59}]},
        hits=[{"page": 9999, "text": "营业收入 6,058,908,000.00 元"}],
        page_range=(1, 220),
    )
    assert not _em2[0]["found"], f"越界页码未被拦截: {_em2[0]}"

    # 10) 引用正确率
    _ca = citation_accuracy({"summary": "见第 12 页与第 99999 页"}, (1, 220))
    assert _ca["refs"] == 2 and _ca["ok"] == 1 and _ca["rate"] == 0.5, _ca
    assert citation_accuracy({"summary": "无页码引用"}, (1, 220))["rate"] is None

    print(json.dumps({
        "module": "analysis_validate",
        "coverage_gate": "ok",
        "period_aware_calibration": "ok",
        "evidence_check": "ok",
        "unit_scale_match": "ok",
        "extra_sources": "ok",
        "evidence_map": "ok",
        "citation_accuracy": "ok",
    }, ensure_ascii=False, indent=2))

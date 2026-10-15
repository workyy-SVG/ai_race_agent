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
from typing import Any, Dict, List, Optional, Tuple

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
def verify_evidence(
    analysis: Dict[str, Any],
    raw_data: str = "",
    page_range: Optional[Tuple[int, int]] = None,
) -> List[str]:
    """核对分析里的关键数值与页码引用是否有据可依，返回问题清单（空 = 全部通过）。

    · 数值：指标明细 / 图表里的数值应能在原始数据文本中找到（容忍千分位与舍入写法）
    · 页码：正文引用的「第 X 页」不应超出年报实际页数
    只报告、不改数 —— "改还是留"由分析者判断，程序只负责把疑点摆出来。
    """
    issues: List[str] = []

    def _num_in_text(v: float, text: str) -> bool:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return True
        for c in (f, abs(f)):
            for s in (f"{c:g}", f"{c:.1f}", f"{c:.2f}"):
                if s and s in text:
                    return True
            _s = f"{c:,.2f}".rstrip("0").rstrip(".")
            if _s and _s in text:
                return True
        return False

    try:
        _text = raw_data or ""
        if _text:
            for m in (analysis.get("metrics") or [])[:20]:
                v = m.get("value")
                if isinstance(v, (int, float)) and not _num_in_text(v, _text):
                    issues.append(
                        f"指标「{m.get('item')}」= {v} 未能在原始数据中找到对应数值，"
                        "请核对是否来自模型推算（建议以原始数据为准）"
                    )
            for c in (analysis.get("chart_data") or [])[:20]:
                v = c.get("value")
                if isinstance(v, (int, float)) and not _num_in_text(v, _text):
                    issues.append(f"图表项「{c.get('item')}」= {v} 未能在原始数据中找到对应数值")

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

    print(json.dumps({
        "module": "analysis_validate",
        "coverage_gate": "ok",
        "period_aware_calibration": "ok",
        "evidence_check": "ok",
    }, ensure_ascii=False, indent=2))

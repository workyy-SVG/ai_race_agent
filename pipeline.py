# -*- coding: utf-8 -*-
"""
统一业务流程（页面 / 离线评测 **共用同一条链路**）
=================================================
优化清单 P1-06「页面和评测复用同一业务流程」的落地件。

为什么必须抽出来：
  之前「页面里怎么算分、怎么核对证据」写在 app.py 的事件回调里，
  「评测里怎么算分、怎么核对证据」写在 eval_cases.py 里。两份代码看起来一样，
  但没有任何机制保证它们真的一样 —— 页面改了阈值/口径，评测脚本不会跟着变，
  于是"评测报告"证明的其实是另一条流水线。这个模块是**唯一实现**：
  app.py 与 eval_regression.py / eval_cases.py 都调它。

链路（与页面完全一致）：
  1) annual_metrics()  取数并锁定【最近一个年报】口径（中期报告为年初至今累计值，与年报不可比）
  2) score_block()     规则引擎评分（同业对标 + 覆盖率门槛 + 金融子行业）
  3) verify()          证据核对（值可回溯 / 页码不越界）
  4) evidence_items()  证据卡（每个数值 → 页码 + 原文片段，正向可定位）
  5) citation_stat()   正文页码引用正确率

本模块是纯函数（不联网、不依赖 Streamlit），可脱离运行时单独测试；
取数与 LLM 抽取通过参数注入，便于评测脚本替换为固定样本。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import data_source as ds
import scoring
from analysis_validate import (
    citation_accuracy,
    evidence_map,
    validate_analysis,
    verify_evidence,
)

# 默认抓取的年报期数（≥5 才能支撑年度时间轴与多年趋势）
DEFAULT_PERIODS = 8


def page_range_of(rag_status: Optional[Dict[str, Any]]) -> Optional[Tuple[int, int]]:
    """从 RAG 建库状态里取「1 ~ 实际页数」，作为页码引用的合法区间。"""
    if not isinstance(rag_status, dict):
        return None
    _n = rag_status.get("pages")
    if isinstance(_n, int) and _n > 0:
        return (1, int(_n))
    return None


def annual_metrics(
    code: str, name: str = "", periods: int = DEFAULT_PERIODS
) -> Tuple[Dict[str, Any], str]:
    """取该公司【最近一个年报】的标准指标 → (metrics, 期别标签)。

    与页面同一个口径：先只请求年报（annual_only=True）；该公司确实没有年报
    （新上市/次新股）时才退回全部报告期，并在标签里明确标注口径已退化。
    """
    if not code:
        return {}, ""
    fina = ds.get_financial_indicators(code, periods=periods, annual_only=True)
    if not fina:
        fina = ds.get_financial_indicators(code, periods=periods)
    if not fina:
        return {}, ""
    _annual = [r for r in fina if (r.get("report_type") or "") == "年报"]
    _pick = _annual[0] if _annual else fina[0]
    _label = str(_pick.get("report_name") or "")
    if not _annual:
        _label += "（无年报记录，暂用中期报告口径）"
    return _pick, _label


def all_annual_rows(code: str, periods: int = DEFAULT_PERIODS) -> List[Dict[str, Any]]:
    """该公司全部年报行（用于多公司对齐「共同年报年份」）。"""
    if not code:
        return []
    try:
        rows = ds.get_financial_indicators(code, periods=periods, annual_only=True) or []
        return [r for r in rows if (r.get("report_type") or "") == "年报"]
    except Exception:
        return []


def year_of(row: Dict[str, Any]) -> str:
    """从一行财务指标里取出报告年份（4 位）。"""
    for _k in ("report_name", "report_date"):
        _v = str((row or {}).get(_k) or "")
        _m = _search_year(_v)
        if _m:
            return _m
    return ""


def _search_year(text: str) -> str:
    import re
    m = re.search(r"(20\d{2})", text or "")
    return m.group(1) if m else ""


def score_block(
    metrics: Dict[str, Any],
    industry: str = "通用",
    period: str = "",
    source: str = "",
    finance_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """规则评分块（纯函数，输入相同 → 输出必然相同）。"""
    sc = scoring.compute_scores(metrics or {}, industry, finance_kind=finance_kind)
    return {
        "scores": sc["scores"],
        "total_score": sc["total_score"],
        "score_reasons": sc["reasons"],
        "score_missing": sc["missing"],
        "score_engine": "规则引擎 v3（同业对标 + 覆盖率门槛 + 金融子行业）",
        "score_coverage": sc.get("coverage"),
        "score_confidence": sc.get("confidence"),
        "score_notes": sc.get("notes") or [],
        "score_expected": sc.get("expected_metrics"),
        "score_provided": sc.get("provided_metrics"),
        "score_industry": sc.get("industry"),
        "score_finance_kind": sc.get("finance_kind") or "",
        "metrics_raw": dict(metrics or {}),
        "score_period": period,
        "score_source": source or ("文本抽取" if not period else "在线财务指标"),
        "_scoring_result": sc,
    }


def verify(
    analysis: Dict[str, Any],
    raw_data: str = "",
    page_range: Optional[Tuple[int, int]] = None,
    extra_sources: Optional[Sequence[str]] = None,
) -> List[str]:
    """证据核对：值能不能在来源里找到 + 页码有没有越界。"""
    return verify_evidence(analysis, raw_data, page_range=page_range, extra_sources=extra_sources)


def evidence_items(
    analysis: Dict[str, Any],
    rag_hits: Optional[Sequence[Dict[str, Any]]] = None,
    raw_data: str = "",
    page_range: Optional[Tuple[int, int]] = None,
) -> List[Dict[str, Any]]:
    """证据卡：每个指标/图表数值 → 页码 + 原文片段（正向可定位）。"""
    return evidence_map(analysis, rag_hits, raw_data=raw_data, page_range=page_range)


def citation_stat(analysis: Dict[str, Any], page_range: Optional[Tuple[int, int]]) -> Dict[str, Any]:
    """正文页码引用正确率。"""
    return citation_accuracy(analysis, page_range)


def analyze_core(
    analysis: Dict[str, Any],
    raw_data: str,
    industry: str = "通用",
    *,
    code: str = "",
    periods: int = DEFAULT_PERIODS,
    online: bool = True,
    finance_kind: Optional[str] = None,
    pdftext: str = "",
    rag_hits: Optional[Sequence[Dict[str, Any]]] = None,
    rag_pages: Optional[int] = None,
    metrics_extractor: Optional[Callable[[str, List[str]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """**页面与评测共用的核心链路**：评分 → 校验 → 证据核对 → 证据卡 → 引用统计。

    就地补全 ``analysis`` 的 ``scores`` / ``score_*`` / ``evidence_*`` 字段并返回
    这份 analysis；同时返回汇总指标供评测记录。

    :param online: 是否允许使用在线财务指标评分（手动粘贴模式必须为 False，
        否则会出现"页面显示 B 公司文本、分数却来自 A 公司接口"的历史 bug）
    :param metrics_extractor: 手动模式下"正则抽不到指标"时的兜底抽取器
        ``fn(raw_data, need_keys) -> dict``（页面传 LLM 抽取，评测可传固定桩）
    """
    # ---- 1) 指标来源：在线优先（且只取年报口径）----
    metrics: Dict[str, Any] = {}
    period = ""
    source = ""
    _oc = code if online else ""
    if _oc:
        try:
            metrics, period = annual_metrics(_oc, periods=periods)
            if period:
                source = f"在线财务指标（{period}）"
            else:
                source = "在线财务指标（最新报告期）"
        except Exception:
            metrics, period, source = {}, "", ""
    if not metrics:
        metrics = scoring.extract_metrics_regex(raw_data)
        _need = [
            k for k in (
                "roe", "gross_margin", "net_margin", "revenue_yoy", "net_profit_yoy",
                "debt_ratio", "current_ratio", "ocf_to_revenue",
                "npl_ratio", "core_car", "provision_coverage",
                "nim", "loan_provision_ratio", "solvency_ar", "nbv_rate",
            ) if k not in metrics
        ]
        if _need and raw_data and metrics_extractor is not None:
            try:
                _m = metrics_extractor(raw_data, _need)
                if isinstance(_m, dict):
                    for k in _need:
                        if _m.get(k) is not None:
                            metrics[k] = _m[k]
            except Exception:
                pass
        # ⚠️ 掉到这里说明"在线财务指标没取到"，必须把来源如实改写。
        #    旧版沿用上面的 "在线财务指标（最新报告期）"，会出现"页面显示的是
        #    粘贴文本，却标注分数来自接口"的误导（与"在线看 A、手动粘 B"同一个坑）。
        if str(source).startswith("在线财务指标"):
            source = "文本抽取（在线财务指标不可用，已退回文本）"
        else:
            source = source or "文本抽取"

    # ---- 2) 规则评分 ----
    analysis.update(score_block(metrics, industry, period, source, finance_kind=finance_kind))

    # ---- 3) 输出校验（字段/类型/范围/口径）----
    analysis, v_issues = validate_analysis(analysis)

    # ---- 4) 证据核对 + 证据卡 + 引用统计 ----
    _pg_range = (1, int(rag_pages)) if isinstance(rag_pages, int) and rag_pages > 0 else None
    _sources: List[str] = []
    if pdftext and len(pdftext) <= 4_000_000:
        _sources.append(pdftext)
    for _h in (rag_hits or []):
        _sources.append(str((_h or {}).get("text") or ""))
    ev_issues = verify(analysis, raw_data, page_range=_pg_range, extra_sources=_sources)
    ev_items = evidence_items(analysis, rag_hits, raw_data=raw_data, page_range=_pg_range)
    cit = citation_stat(analysis, _pg_range)

    _found = [e for e in ev_items if e.get("value") is not None and e.get("found")]
    _checkable = [e for e in ev_items if isinstance(e.get("value"), (int, float))]
    _num_rate = round(len(_found) / len(_checkable), 3) if _checkable else None

    analysis["evidence_items_count"] = len(ev_items)
    analysis["evidence_issues_count"] = len(ev_issues)

    return {
        "analysis": analysis,
        "validation_issues": v_issues,
        "evidence_issues": ev_issues,
        "evidence_items": ev_items,
        "citation": cit,
        "num_trace_rate": _num_rate,
        "score_period": period,
        "score_source": source,
        "metrics": metrics,
    }


if __name__ == "__main__":  # pragma: no cover - 纯自检，不联网
    import json

    # 用一份固定样本跑链路（不联网：metrics 直接喂进去）
    _sample = {
        "roe": 34.0, "gross_margin": 91.5, "net_margin": 47.0,
        "revenue_yoy": 15.7, "net_profit_yoy": 15.4,
        "debt_ratio": 20.0, "current_ratio": 4.0, "ocf_to_revenue": 55.0,
    }
    _blk = score_block(_sample, "消费", period="2025年报", source="样本")
    assert _blk["total_score"] is not None and _blk["score_coverage"] == 1.0

    _ana = {
        "company": "示例",
        "metrics": [{"item": "2025年报营业收入", "value": 60.59, "unit": "亿元"}],
        "chart_data": [{"item": "2025年报营业成本", "value": 361.09}],
        "summary": "见年报第 12 页",
        "risks": ["风险一"], "highlights": ["亮点一"],
    }
    _hits = [{"page": 42, "text": "营业收入 6,058,908,000.00 元；营业成本 36,109,000,000.00 元"}]
    _res = analyze_core(
        _ana, raw_data="营业收入 100.5 亿元", industry="消费",
        online=False, pdftext="", rag_hits=_hits, rag_pages=220,
    )
    assert _res["evidence_issues"] == [], _res["evidence_issues"]
    assert all(e["found"] for e in _res["evidence_items"]), _res["evidence_items"]
    assert _res["citation"]["rate"] == 1.0, _res["citation"]

    print(json.dumps({
        "module": "pipeline",
        "shared_by": ["app.py", "eval_regression.py", "eval_cases.py"],
        "score_total": _blk["total_score"],
        "evidence_ok": True,
        "citation_rate": _res["citation"]["rate"],
        "num_trace_rate": _res["num_trace_rate"],
    }, ensure_ascii=False, indent=2))

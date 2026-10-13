# -*- coding: utf-8 -*-
"""
规则评分引擎 v1
================
设计目标（大赛评审要点）：
  · 分数由【固定规则】计算 —— 同一份数据永远得到同一个分数（可复现、可解释）
  · 大模型只负责【提取指标 + 解释结论】，不再直接打分
  · 规则阈值参考行业通行的财务分析经验法则，并内置金融（银行/保险）专用规则

输入 metrics 字段（均可缺失，缺失维度自动降权处理）：
  roe, gross_margin, net_margin, revenue_yoy, net_profit_yoy,
  debt_ratio, current_ratio, ocf_to_revenue,
  npl_ratio, core_car, provision_coverage   ← 银行/保险专用

CLI 自检: python scoring.py
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 分段线性打分表：(指标值, 分数)，按指标值升序；分数线性插值，夹在 [0, 10]
# "越低越好"的指标（资产负债率/不良率）同样用升序表，只是分数随值递减
# ---------------------------------------------------------------------------
_TABLES: Dict[str, List[Tuple[float, float]]] = {
    # ROE：长期 >15% 优秀，>20% 顶级
    "roe": [(0, 1), (5, 3), (8, 5), (10, 6.5), (12, 7.5), (15, 9), (20, 10), (40, 10)],
    # 毛利率：高端白酒 80%+，普通制造 15-25%
    "gross_margin": [(0, 1), (10, 3), (20, 4.5), (30, 6), (40, 8), (60, 10), (100, 10)],
    # 净利率
    "net_margin": [(-10, 0), (0, 1), (3, 3), (8, 6), (15, 8), (25, 10), (60, 10)],
    # 营收同比 %
    "revenue_yoy": [(-30, 0), (-10, 2), (0, 4), (5, 5.5), (10, 7), (20, 9), (30, 10), (200, 10)],
    # 归母净利润同比 %
    "net_profit_yoy": [(-50, 0), (-20, 2), (0, 4), (5, 5.5), (15, 7), (30, 9), (50, 10), (300, 10)],
    # 资产负债率 %（越低越好；金融行业不使用本表）
    "debt_ratio": [(20, 10), (30, 9), (40, 8), (50, 6.5), (60, 5), (70, 3.5), (80, 2), (95, 1)],
    # 流动比率（倍）
    "current_ratio": [(0.3, 1), (0.5, 2), (0.8, 4), (1.0, 5), (1.5, 7), (2.0, 9), (3.0, 10), (10, 10)],
    # 经营现金流/营收 %
    "ocf_to_revenue": [(-20, 0), (0, 2), (5, 4), (10, 6), (15, 7.5), (20, 8.5), (30, 10), (100, 10)],
    # ---- 银行/保险专用 ----
    # 不良贷款率 %（越低越好）
    "npl_ratio": [(0.3, 10), (0.5, 9.5), (1.0, 9), (1.5, 7.5), (2.0, 6), (3.0, 4), (5.0, 2), (10, 1)],
    # 核心一级资本充足率 %
    "core_car": [(8.0, 1), (8.5, 2), (9.0, 4), (10.5, 6), (12.0, 8), (14.0, 10), (30, 10)],
    # 拨备覆盖率 %
    "provision_coverage": [(80, 1), (100, 3), (120, 5), (150, 7), (200, 9), (300, 10), (600, 10)],
}

# 四个维度的指标构成：(指标名, 权重)
_DIM_GENERAL = {
    "盈利能力": [("roe", 0.50), ("gross_margin", 0.25), ("net_margin", 0.25)],
    "成长性": [("revenue_yoy", 0.50), ("net_profit_yoy", 0.50)],
    "偿债能力": [("debt_ratio", 0.60), ("current_ratio", 0.40)],
    "现金流质量": [("ocf_to_revenue", 1.0)],
}
_DIM_FINANCE = {
    "盈利能力": [("roe", 1.0)],
    "成长性": [("revenue_yoy", 0.50), ("net_profit_yoy", 0.50)],
    "偿债能力": [("npl_ratio", 0.40), ("core_car", 0.30), ("provision_coverage", 0.30)],
    "现金流质量": [("ocf_to_revenue", 1.0)],
}

# 阈值判读用语（用于生成评分依据文本）
def _verdict(key: str) -> str:
    _V = {
        "roe": "≥20% 顶级 / 15-20% 优秀 / 10-15% 良好 / <8% 偏弱",
        "gross_margin": "≥60% 强定价权 / 40-60% 良好 / <20% 偏薄",
        "net_margin": "≥25% 优秀 / 8-15% 中等 / <3% 偏弱",
        "revenue_yoy": "≥20% 高增长 / 10-20% 稳健 / <0 负增长",
        "net_profit_yoy": "≥30% 高增长 / 0-15% 稳健 / <-20% 大幅下滑",
        "debt_ratio": "≤40% 稳健 / 50-70% 中等 / >70% 需警惕（金融地产除外）",
        "current_ratio": "≥2 充裕 / 1-1.5 尚可 / <1 短期偿债承压",
        "ocf_to_revenue": "≥20% 利润质量高 / 10% 左右正常 / <0 现金流失血",
        "npl_ratio": "≤1% 健康 / 1.5-2% 关注 / >3% 风险较高",
        "core_car": "≥12% 充裕 / 10.5% 附近达标 / <9.5% 逼近监管红线",
        "provision_coverage": "≥200% 审慎 / 150-200% 达标 / <120% 偏薄",
    }
    return _V.get(key, "")


def _interp(key: str, v: float) -> float:
    """按分段线性表打分，结果夹在 [0, 10]。"""
    pts = _TABLES[key]
    if v <= pts[0][0]:
        return max(pts[0][1], 0.0)
    if v >= pts[-1][0]:
        return min(pts[-1][1], 10.0)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= v <= x1:
            if x1 == x0:
                return y1
            t = (v - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return 5.0


def _num(v) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):  # NaN / Inf
            return None
        return f
    except (TypeError, ValueError):
        return None


def compute_scores(metrics: Dict[str, Any], industry: str = "通用") -> Dict[str, Any]:
    """
    核心 API：固定规则计算四维评分。
    Returns:
        {
          "scores": {"盈利能力": x, "成长性": x, "偿债能力": x, "现金流质量": x},  # 保留1位小数
          "total_score": x,
          "reasons":  {"维度": ["依据1", ...]},
          "missing":  ["缺失的指标名"],
        }
    同样的 metrics + industry 输入，永远返回同样的分数。
    """
    m = {k: _num(v) for k, v in (metrics or {}).items() if k in _TABLES}

    # 金融行业自动切换：有银行专用指标或行业选了"金融"
    is_financial = (industry == "金融") or ("npl_ratio" in m and m["npl_ratio"] is not None)
    dims = _DIM_FINANCE if is_financial else _DIM_GENERAL

    scores: Dict[str, Optional[float]] = {}
    reasons: Dict[str, List[str]] = {}
    missing: List[str] = []

    for dim, items in dims.items():
        got = [(k, w, m[k]) for k, w in items if m.get(k) is not None]
        missing += [k for k, _ in items if m.get(k) is None]
        if not got:
            scores[dim] = None
            reasons[dim] = [f"缺少 {('、'.join(k for k, _ in items))} 数据，本维度不计分"]
            continue
        # 权重归一化（部分指标缺失时按剩余权重折算）
        wsum = sum(w for _, w, _ in got)
        s = sum(_interp(k, v) * (w / wsum) for k, w, v in got)
        scores[dim] = round(s, 1)
        reasons[dim] = []
        for k, w, v in got:
            _vi = _interp(k, v)
            reasons[dim].append(
                f"{k}={v:g}{_unit(k)} → {_vi:.1f} 分（权重 {w / wsum * 100:.0f}%；判读标准：{_verdict(k)}）"
            )

    avail = [v for v in scores.values() if v is not None]
    total = round(sum(avail) / len(avail), 1) if avail else None

    return {
        "scores": scores,
        "total_score": total,
        "reasons": reasons,
        "missing": sorted(set(missing)),
    }


def _unit(k: str) -> str:
    return "%" if k in (
        "roe", "gross_margin", "net_margin", "revenue_yoy", "net_profit_yoy",
        "debt_ratio", "ocf_to_revenue", "npl_ratio", "core_car", "provision_coverage",
    ) else ("倍" if k == "current_ratio" else "")


# 指标字段的中文名（生成评分依据时更友好）
CN_NAMES = {
    "roe": "ROE(加权)", "gross_margin": "毛利率", "net_margin": "净利率",
    "revenue_yoy": "营收同比", "net_profit_yoy": "归母净利润同比",
    "debt_ratio": "资产负债率", "current_ratio": "流动比率",
    "ocf_to_revenue": "经营现金流/营收",
    "npl_ratio": "不良贷款率", "core_car": "核心一级资本充足率", "provision_coverage": "拨备覆盖率",
}


# ---------------------------------------------------------------------------
# 手动粘贴模式的兜底：从文本中用正则抽取标准指标（无法抽取的交给 LLM 提取）
# ---------------------------------------------------------------------------
_METRIC_PATTERNS = {
    "roe": r"ROE[^0-9\-]{0,8}(-?\d+(?:\.\d+)?)\s*%",
    "gross_margin": r"毛利率[^0-9\-]{0,4}(-?\d+(?:\.\d+)?)\s*%",
    "net_margin": r"净利率[^0-9\-]{0,4}(-?\d+(?:\.\d+)?)\s*%",
    "revenue_yoy": r"营收同比[^0-9\-]{0,6}(-?\d+(?:\.\d+)?)\s*%",
    "net_profit_yoy": r"净利润同比[^0-9\-]{0,6}(-?\d+(?:\.\d+)?)\s*%",
    "debt_ratio": r"资产负债率[^0-9\-]{0,4}(-?\d+(?:\.\d+)?)\s*%",
    "current_ratio": r"流动比率[^0-9\-]{0,4}(-?\d+(?:\.\d+)?)",
    "ocf_to_revenue": r"经营现金流/营收[^0-9\-]{0,4}(-?\d+(?:\.\d+)?)\s*%",
    "npl_ratio": r"不良(?:贷款)?率[^0-9\-]{0,4}(-?\d+(?:\.\d+)?)\s*%",
    "core_car": r"核心一级资本充足率[^0-9\-]{0,4}(-?\d+(?:\.\d+)?)\s*%",
    "provision_coverage": r"拨备覆盖率[^0-9\-]{0,4}(-?\d+(?:\.\d+)?)\s*%",
}


def extract_metrics_regex(text: str) -> Dict[str, float]:
    """从摘要文本中正则抽取指标（抽取不到的字段直接缺省）。"""
    import re
    out: Dict[str, float] = {}
    for k, pat in _METRIC_PATTERNS.items():
        mm = re.search(pat, text or "")
        if mm:
            out[k] = float(mm.group(1))
    return out


# ---------------------------------------------------------------------------
# LLM 指标提取提示词（手动模式、正则抽取不到时使用；LLM 只提取数字，不打分）
# ---------------------------------------------------------------------------
METRICS_EXTRACTION_PROMPT = (
    "你是严谨的财务数据抽取器。请从下面的财务文本中抽取标准指标，"
    "只输出一个 JSON 对象，不要输出任何其他文字。格式：\n"
    "{\n"
    '  "roe": ROE加权(%),\n'
    '  "gross_margin": 毛利率(%),\n'
    '  "net_margin": 净利率(%),\n'
    '  "revenue_yoy": 营收同比(%),\n'
    '  "net_profit_yoy": 归母净利润同比(%),\n'
    '  "debt_ratio": 资产负债率(%),\n'
    '  "current_ratio": 流动比率(倍),\n'
    '  "ocf_to_revenue": 经营现金流占营收比(%),\n'
    '  "npl_ratio": 不良贷款率(%),\n'
    '  "core_car": 核心一级资本充足率(%),\n'
    '  "provision_coverage": 拨备覆盖率(%)\n'
    "}\n"
    "要求：文本中没有的指标填 null；数字只保留原文中的数值，禁止推算或编造。\n\n"
    "财务文本：\n"
)


# ---------------------------------------------------------------------------
# CLI 自检
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    # 茅台样例（2024 年报公开数据近似值，仅用于验证规则正确性）
    demo = {
        "roe": 34.0, "gross_margin": 91.5, "net_margin": 47.0,
        "revenue_yoy": 15.7, "net_profit_yoy": 15.4,
        "debt_ratio": 20.0, "current_ratio": 4.0, "ocf_to_revenue": 55.0,
    }
    r1 = compute_scores(demo, "消费")
    # 银行样例
    bank = {"roe": 14.0, "revenue_yoy": -1.0, "net_profit_yoy": 1.5,
            "npl_ratio": 0.95, "core_car": 13.0, "provision_coverage": 420.0}
    r2 = compute_scores(bank, "金融")
    # 一致性自检：同样输入两次，分数必须完全一致
    r1b = compute_scores(demo, "消费")
    assert r1 == r1b, "规则评分必须可复现！"

    print(json.dumps({
        "module": "scoring",
        "engine": "rule_v1",
        "reproducible": True,
        "demo_maotai": {"total": r1["total_score"], "scores": r1["scores"]},
        "demo_bank": {"total": r2["total_score"], "scores": r2["scores"]},
        "regex_test": extract_metrics_regex("毛利率 91.53%；ROE(加权) 34.2%；资产负债率 18.9%"),
    }, ensure_ascii=False, indent=2))

# -*- coding: utf-8 -*-
"""
规则评分引擎 v2
================
设计目标（大赛评审要点）：
  · 分数由【固定规则】计算 —— 同一份数据永远得到同一个分数（可复现、可解释）
  · 大模型只负责【提取指标 + 解释结论】，不再直接打分
  · 规则阈值参考行业通行的财务分析经验法则，并内置金融（银行/保险）专用规则
  · 【v2 同业对标】ROE 与营收/净利增速三张打分表随行业基准线缩放 ——
    银行 ROE 11% 与白酒 ROE 11% 得到完全不同的分数，这才是有效的财务判断
  · 【v2 覆盖率】同时输出指标覆盖率与置信标签；覆盖不足时不给综合评价
    （避免「只填了一个 ROE=20% 也拿 10.0 分」的失真结论）

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

# ---------------------------------------------------------------------------
# 行业基准（唯一真源）
#   (ROE 优秀线, 营收增速稳健线)
#   app.py 的「同业对标基准条」与四象限基准线都从这里读 ——
#   一处改动，评分阈值 / 散点基准线 / 页面文案三处口径自动一致。
# ---------------------------------------------------------------------------
INDUSTRY_PROFILE: Dict[str, Tuple[float, float]] = {
    "通用":   (15.0, 10.0),   # 通用制造/多元
    "消费":   (18.0, 8.0),    # 高端消费：ROE 高、增速稳
    "金融":   (11.0, 6.0),    # 银行/保险：高杠杆薄利差，ROE 11% 已属优秀
    "新能源": (12.0, 20.0),   # 高增长赛道：增速基准上调
    "医药":   (14.0, 12.0),   # 研发驱动：成长性要求更高
    "互联网": (14.0, 15.0),   # 平台型：增长与回报并重
}
_DEFAULT_PROFILE: Tuple[float, float] = INDUSTRY_PROFILE["通用"]

# 指标覆盖率低于该比例时，不提供「综合评价」（只给维度分 + 缺失清单）
MIN_COVERAGE = 0.5

# ---------------------------------------------------------------------------
# 以「行业基准线」为锚点的归一化打分表：横轴 = 指标值 ÷ 行业基准线 的倍数
#
# 锚点系数刻意取成「通用行业（ROE 15% / 增速 10%）代回后与 v1 固定表逐点一致」，
# 这样 v1 的历史结论仍可复现，而其他行业只需换一条基准线即可。
#   ROE 15%  → 9 分（优秀线）    20%  → 10 分
#   营收增速 10% → 7 分（稳健线） 20%  → 9 分
# ---------------------------------------------------------------------------
_ROE_ANCHOR: List[Tuple[float, float]] = [
    (0.0, 1), (1 / 3, 3), (8 / 15, 5), (2 / 3, 6.5), (0.8, 7.5), (1.0, 9), (4 / 3, 10), (8 / 3, 10),
]
_REV_ANCHOR: List[Tuple[float, float]] = [
    (-3.0, 0), (-1.0, 2), (0.0, 4), (0.5, 5.5), (1.0, 7), (2.0, 9), (3.0, 10), (20.0, 10),
]
_NP_ANCHOR: List[Tuple[float, float]] = [
    (-5.0, 0), (-2.0, 2), (0.0, 4), (0.5, 5.5), (1.5, 7), (3.0, 9), (5.0, 10), (30.0, 10),
]


def _scale(anchor: List[Tuple[float, float]], factor: float) -> List[Tuple[float, float]]:
    return [(round(x * factor, 4), y) for x, y in anchor]


def tables_for_industry(industry: str) -> Dict[str, List[Tuple[float, float]]]:
    """按行业基准线生成打分表。

    · ROE / 营收同比 / 归母净利同比 三张表随行业缩放（同业对标的核心）
    · 毛利率 / 净利率 / 资产负债率 / 流动比率 / 现金流 / 银行专用指标保持固定口径
      （这几项的合理区间差异已由维度权重与行业提示词承担，不再二次缩放，
        避免"消费"桶里乳制品与白酒被同一把尺子误伤）
    """
    line_roe, line_grow = INDUSTRY_PROFILE.get(industry or "通用", _DEFAULT_PROFILE)
    t = dict(_TABLES)  # 浅拷贝，不改动全局基线表
    t["roe"] = _scale(_ROE_ANCHOR, line_roe)
    t["revenue_yoy"] = _scale(_REV_ANCHOR, line_grow)
    t["net_profit_yoy"] = _scale(_NP_ANCHOR, line_grow)
    return t


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
#   roe / revenue_yoy / net_profit_yoy 三项随行业基准线动态生成 ——
#   否则「消费 18% 才优秀」的阈值配着「15-20% 优秀」的文案，前后自相矛盾
def _verdict(key: str, line_roe: float = 15.0, line_grow: float = 10.0) -> str:
    _V = {
        "roe": (
            f"≥{line_roe * 4 / 3:.0f}% 顶级 / {line_roe:.0f}-{line_roe * 4 / 3:.0f}% 优秀 / "
            f"{line_roe * 2 / 3:.0f}-{line_roe:.0f}% 良好 / <{line_roe * 8 / 15:.0f}% 偏弱"
        ),
        "gross_margin": "≥60% 强定价权 / 40-60% 良好 / <20% 偏薄",
        "net_margin": "≥25% 优秀 / 8-15% 中等 / <3% 偏弱",
        "revenue_yoy": (
            f"≥{line_grow * 2:.0f}% 高增长 / {line_grow:.0f}-{line_grow * 2:.0f}% 稳健 / <0 负增长"
        ),
        "net_profit_yoy": (
            f"≥{line_grow * 3:.0f}% 高增长 / 0-{line_grow * 1.5:.0f}% 稳健 / "
            f"<-{line_grow * 2:.0f}% 大幅下滑"
        ),
        "debt_ratio": "≤40% 稳健 / 50-70% 中等 / >70% 需警惕（金融地产除外）",
        "current_ratio": "≥2 充裕 / 1-1.5 尚可 / <1 短期偿债承压",
        "ocf_to_revenue": "≥20% 利润质量高 / 10% 左右正常 / <0 现金流失血",
        "npl_ratio": "≤1% 健康 / 1.5-2% 关注 / >3% 风险较高",
        "core_car": "≥12% 充裕 / 10.5% 附近达标 / <9.5% 逼近监管红线",
        "provision_coverage": "≥200% 审慎 / 150-200% 达标 / <120% 偏薄",
    }
    return _V.get(key, "")


def _interp(key: str, v: float, tables: Optional[Dict[str, List[Tuple[float, float]]]] = None) -> float:
    """按分段线性表打分，结果夹在 [0, 10]。tables 缺省时用通用基线表。"""
    pts = (tables or _TABLES)[key]
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


def compute_scores(
    metrics: Dict[str, Any],
    industry: str = "通用",
    min_coverage: float = MIN_COVERAGE,
) -> Dict[str, Any]:
    """
    核心 API：固定规则计算四维评分（同业对标版）。

    Returns:
        {
          "scores": {"盈利能力": x, "成长性": x, "偿债能力": x, "现金流质量": x},  # 保留1位小数
          "total_score": x | None,   # 覆盖率不足时给 None（不给综合评价）
          "reasons":  {"维度": ["依据1", ...]},
          "missing":  ["缺失的指标名"],
          "coverage": 0.0~1.0,       # 指标覆盖率
          "confidence": "充分" | "局部评价" | "信息不足",
          "expected_metrics": [...], "provided_metrics": [...],
          "industry": 实际使用的行业, "industry_base": (ROE线, 增速线),
        }
    同样的 metrics + industry 输入，永远返回同样的分数（可复现）。
    """
    line_roe, line_grow = INDUSTRY_PROFILE.get(industry or "通用", _DEFAULT_PROFILE)
    tables = tables_for_industry(industry)
    m = {k: _num(v) for k, v in (metrics or {}).items() if k in tables}

    # 金融行业自动切换：有银行专用指标或行业选了"金融"
    is_financial = (industry == "金融") or ("npl_ratio" in m and m["npl_ratio"] is not None)
    dims = _DIM_FINANCE if is_financial else _DIM_GENERAL
    expected = sorted({k for items in dims.values() for k, _ in items})

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
        s = sum(_interp(k, v, tables) * (w / wsum) for k, w, v in got)
        scores[dim] = round(s, 1)
        reasons[dim] = []
        for k, w, v in got:
            _vi = _interp(k, v, tables)
            _dim_note = "" if len(got) == len(items) else f"（本维度 {len(got)}/{len(items)} 项可得）"
            reasons[dim].append(
                f"{k}={v:g}{_unit(k)} → {_vi:.1f} 分（权重 {w / wsum * 100:.0f}%；"
                f"判读标准（{industry}）：{_verdict(k, line_roe, line_grow)}）{_dim_note}"
            )

    # ---- 指标覆盖率：缺失值绝不当 0 分用，也绝不让低覆盖公司拿到"满分综合评价" ----
    provided = [k for k in expected if m.get(k) is not None]
    coverage = round(len(provided) / max(len(expected), 1), 3)

    avail = [v for v in scores.values() if v is not None]
    notes: List[str] = []
    if coverage < min_coverage:
        total: Optional[float] = None
        confidence = "信息不足"
        notes.append(
            f"指标覆盖率仅 {coverage * 100:.0f}%（{len(provided)}/{len(expected)} 项），"
            f"低于 {min_coverage * 100:.0f}% 门槛 → 暂不提供综合评价，仅展示可得维度分。"
        )
    else:
        total = round(sum(avail) / len(avail), 1) if avail else None
        confidence = "局部评价" if coverage < 0.75 else "充分"
        if confidence == "局部评价":
            notes.append(
                f"指标覆盖率 {coverage * 100:.0f}%（{len(provided)}/{len(expected)} 项），"
                "部分维度缺指标 → 综合评价为局部口径，不宜与数据完整公司直接横向比较。"
            )

    return {
        "scores": scores,
        "total_score": total,
        "reasons": reasons,
        "missing": sorted(set(missing)),
        "coverage": coverage,
        "confidence": confidence,
        "expected_metrics": expected,
        "provided_metrics": provided,
        "notes": notes,
        "industry": industry or "通用",
        "industry_base": (line_roe, line_grow),
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

    # 同业对标自检：同一组数据在不同行业下必须得到不同分数（否则行业基准形同虚设）
    _same_roe = {"roe": 11.0, "gross_margin": 38.0, "net_margin": 12.0,
                 "revenue_yoy": 9.0, "net_profit_yoy": 8.0,
                 "debt_ratio": 45.0, "current_ratio": 1.4, "ocf_to_revenue": 12.0}
    _by_ind = {ind: compute_scores(_same_roe, ind)["scores"]["盈利能力"]
               for ind in INDUSTRY_PROFILE if ind != "金融"}
    assert len(set(_by_ind.values())) > 1, "行业基准未生效：不同行业得分相同！"

    # 覆盖率自检：只填一个 ROE 时不得给出综合评价
    _thin = compute_scores({"roe": 20.0}, "通用")
    assert _thin["total_score"] is None, "低覆盖率仍给出了综合评价！"
    assert _thin["coverage"] < MIN_COVERAGE

    print(json.dumps({
        "module": "scoring",
        "engine": "rule_v2（同业对标 + 覆盖率门槛）",
        "reproducible": True,
        "demo_maotai": {"total": r1["total_score"], "scores": r1["scores"],
                        "coverage": r1["coverage"], "confidence": r1["confidence"]},
        "demo_bank": {"total": r2["total_score"], "scores": r2["scores"],
                      "coverage": r2["coverage"], "confidence": r2["confidence"]},
        "roe_11pct_by_industry": _by_ind,
        "thin_input_total": _thin["total_score"],
        "regex_test": extract_metrics_regex("毛利率 91.53%；ROE(加权) 34.2%；资产负债率 18.9%"),
    }, ensure_ascii=False, indent=2))

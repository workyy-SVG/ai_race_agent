"""Explicit period facts and conservative checks for generated financial prose."""
from __future__ import annotations

import math
import re

from evidence_integrity import PERIOD, normalized_period

METRICS = {
    "净利率": ("销售净利率", "净利率"),
    "毛利率": ("毛利率",),
    "ROE": ("ROE(加权)", "加权ROE", "净资产收益率", "ROE"),
    "资产负债率": ("资产负债率",),
    "每股净资产": ("每股净资产",),
    "每股收益": ("每股收益",),
}
NUM = r"[+\-−]?\d[\d,]*(?:\.\d+)?"


def period_rank(period):
    p = normalized_period(period)
    return (int(p[:4]), {"一季报": 1, "中报": 2, "三季报": 3, "年报": 4}[p[4:]]) if p else (0, 0)


def extract_facts(text):
    """Read explicit period headings; never infer a year from output order."""
    text = text or ""
    headings = list(PERIOD.finditer(text))
    facts = []
    for i, heading in enumerate(headings):
        block = text[heading.end():headings[i + 1].start() if i + 1 < len(headings) else len(text)]
        period = normalized_period(heading[0])
        for key, labels in METRICS.items():
            pattern = "|".join(re.escape(label) for label in labels)
            for m in re.finditer(r"(?:" + pattern + r")\s*[:：]?\s*(" + NUM + r")\s*(%|％|元)", block, re.I):
                value = float(m[1].replace(",", "").replace("−", "-"))
                if math.isfinite(value):
                    facts.append({"metric": key, "period": period, "value": value, "unit": m[2].replace("％", "%")})
    return facts


def facts_prompt(text):
    facts = extract_facts(text)
    if not facts:
        return ""
    rows = [f"{f['period']}｜{f['metric']}｜{f['value']:g}{f['unit']}" for f in facts]
    return ("\n\n【报告期事实表 · 逐项保留期别，不得改写年份】\n" + "\n".join(rows)
            + "\n年报、半年报、单季/累计季报不可直接当作同比或环比趋势；比较必须先说明口径。")


def guard_periods(prose, source_text):
    """Withhold sentences contradicting known period facts, rather than guessing a correction."""
    facts = extract_facts(source_text)
    if not facts:
        return prose, []
    issues = []
    # Preserve original layout when every sentence passes.
    parts = re.split(r"(?<=[。！？])|(?<=\n)", prose or "")
    result = []
    for sentence in parts:
        periods = {normalized_period(m[0]) for m in PERIOD.finditer(sentence)}
        mentioned = [key for key, labels in METRICS.items() if any(label.lower() in sentence.lower() for label in labels)]
        reason = ""
        disclaimer = re.search(r"(?:不能|不可|不宜|不应|无法).{0,20}(?:同比|环比|直接比较|推断趋势)", sentence)
        if mentioned and len({p[4:] for p in periods}) > 1 and not disclaimer and re.search(r"同比|环比|上升|下降|提升|降低|改善|恶化|升至|降至|增至|减至", sentence):
            reason = "年报与季报/中报不能直接作为同口径趋势"
        if not reason and len(mentioned) == 1 and periods:
            known = [f for f in facts if f["metric"] == mentioned[0]]
            anchors = list(PERIOD.finditer(sentence))
            for token in re.finditer(r"(" + NUM + r")\s*(?:%|％|元)", sentence):
                value = float(token[1].replace(",", "").replace("−", "-"))
                before = [m for m in anchors if m.end() <= token.start()]
                after = [m for m in anchors if m.start() >= token.end()]
                anchor = before[-1] if before else after[0] if after else None
                if after and re.fullmatch(r"\s*[（(]\s*", sentence[token.end():after[0].start()]):
                    anchor = after[0]
                stated = normalized_period(anchor[0]) if anchor else ""
                matches = [f for f in known if math.isclose(value, f["value"], rel_tol=0, abs_tol=0.0051)]
                if matches and stated and not any(f["period"] == stated for f in matches):
                    reason = "该数值所属报告期与材料不一致"
                    break
        if reason:
            issues.append(reason + "：" + sentence.strip()[:180])
            verified = [f for f in facts if f["metric"] in mentioned]
            rows = "；".join(f"{f['period']}{f['metric']} {f['value']:g}{f['unit']}" for f in verified)
            suffix = re.search(r"\s*$", sentence)[0]
            result.append(f"【报告期核查】原句未通过校验，暂不采用。材料列示：{rows}。不同口径不能直接推断趋势。" + suffix)
        else:
            result.append(sentence)
    return "".join(result), issues

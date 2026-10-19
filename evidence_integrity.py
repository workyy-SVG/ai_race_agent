"""Conservative metric-level matching: names, signed values, units and periods."""
from __future__ import annotations
import math
import re

NUMBER = re.compile(r"(?<![\d.,])[+\-−–]?\d[\d,]*(?:\.\d+)?(?![\d.,])")
PERIOD = re.compile(r"(20\d{2})\s*年?\s*(年报|年度|一季报|第一季度|中报|半年报|半年度|三季报|第三季度)")
ALIASES = (
    ("营业收入", "营业总收入", "营收"), ("归母净利润", "归属于母公司股东的净利润", "归属于上市公司股东的净利润"),
    ("净利润",), ("营业成本",), ("毛利率",), ("净利率", "销售净利率"),
    ("ROE", "净资产收益率", "加权ROE", "ROE(加权)"), ("资产负债率",), ("流动比率",),
    ("经营现金流/营收", "经营现金流占营收比", "经营现金流/营业收入"),
    ("经营活动现金流净额", "经营活动产生的现金流量净额", "经营活动现金流量净额", "经营活动现金流净额"),
    ("每股净资产",), ("每股收益",), ("不良贷款率", "不良率"), ("拨备覆盖率",),
    ("核心一级资本充足率",), ("净息差",), ("管理费用",), ("销售费用",), ("研发费用",), ("存货",), ("应收账款",),
)
SCALES = {"元": 1., "千元": 1e3, "万元": 1e4, "百万元": 1e6, "亿元": 1e8, "%": 1., "倍": 1.}


def normalized_period(text):
    m = PERIOD.search(str(text or ""))
    if not m:
        return ""
    kind = {"年度": "年报", "第一季度": "一季报", "半年报": "中报", "半年度": "中报", "第三季度": "三季报"}.get(m[2], m[2])
    return m[1] + kind


def tokens(text):
    for m in NUMBER.finditer(str(text or "").replace("负", "-")):
        try:
            value = float(m[0].replace(",", "").replace("−", "-").replace("–", "-"))
            if math.isfinite(value):
                yield m, value
        except ValueError:
            continue


def _unit_after(text):
    m = re.match(r"\s*(亿元|百万元|万元|千元|元|%|％|倍)", text)
    return m[1].replace("％", "%") if m else ""


def match_metric(entry, text, *, source_year=""):
    value = entry.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return ""
    item = str(entry.get("item") or "")
    candidates = [(len(alias), g) for g in ALIASES for alias in g if alias.lower() in item.lower()]
    group = max(candidates, key=lambda x: x[0])[1] if candidates else ()
    if not group:
        return ""
    pattern = "|".join(re.escape(s) for s in sorted(group, key=len, reverse=True))
    wanted_period = normalized_period(item)
    wanted_unit = str(entry.get("unit") or "")
    if not wanted_unit:
        wanted_unit = "倍" if "流动比率" in item else "%" if any(s in item for s in ("率", "ROE", "增速")) else "亿元"
    t = str(text or "")
    for label in re.finditer(pattern, t, re.I):
        # Do not borrow a number from the next semicolon-delimited metric.
        end = re.search(r"[；;。]", t[label.end():label.end() + 220])
        tail = t[label.end():label.end() + (end.start() if end else 220)]
        other_labels = [re.escape(s) for g in ALIASES if g != group for s in g]
        next_label = re.search("|".join(other_labels), tail, re.I)
        if next_label:
            tail = tail[:next_label.start()]
        if re.match(r"\s*(同比|增长率|增速)", tail) and wanted_unit != "%":
            continue
        heading = t[max(0, label.start() - 500):label.end()]
        periods = list(PERIOD.finditer(heading))
        local_period = normalized_period(periods[-1][0]) if periods else ""
        if periods:
            period_line = heading[heading.rfind("\n", 0, periods[-1].start()) + 1:]
            if len(list(PERIOD.finditer(period_line))) > 1:
                # A multi-column table needs column alignment, not a guessed year.
                continue
        if wanted_period and local_period and wanted_period != local_period:
            continue
        if wanted_period and not local_period:
            # The document year does not identify a financial table's comparison column.
            continue
        table_unit = re.findall(r"单位\s*[:：]?\s*(亿元|百万元|万元|千元|元|%|倍)", heading)
        for m, actual in list(tokens(tail))[:1]:
            actual_unit = _unit_after(tail[m.end():]) or (table_unit[-1] if table_unit else "")
            if wanted_unit == "%" and actual_unit != "%":
                continue
            if wanted_unit in ("亿元", "万元", "元"):
                if actual_unit not in ("亿元", "百万元", "万元", "千元", "元"):
                    continue
                expected = value * SCALES[wanted_unit] / SCALES[actual_unit]
            else:
                if actual_unit and actual_unit != wanted_unit:
                    continue
                expected = value
            if math.isclose(actual, expected, rel_tol=1.5e-3, abs_tol=1e-8):
                start = max(0, label.start() - 60)
                stop = min(len(t), label.end() + m.end() + len(actual_unit) + 35)
                return re.sub(r"\s+", " ", t[start:stop]).strip()
    return ""

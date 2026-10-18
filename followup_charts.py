"""Question-driven charts made only from structured financial-source snapshots.

The planner is offline and JSON serializable. Model-generated answer text and
model-generated trend/chart_data fields are never used as numeric chart sources.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import re

METRICS = {
    "revenue": ("营业收入", "亿元"), "net_profit": ("归母净利润", "亿元"),
    "revenue_yoy": ("营收同比增速", "%"), "net_profit_yoy": ("归母净利润同比增速", "%"),
    "roe": ("净资产收益率 ROE", "%"), "gross_margin": ("毛利率", "%"),
    "net_margin": ("净利率", "%"), "debt_ratio": ("资产负债率", "%"),
    "current_ratio": ("流动比率", "倍"), "quick_ratio": ("速动比率", "倍"),
    "ocf_to_revenue": ("经营现金流 / 营收", "%"), "cash_flow_ratio": ("现金流量比率", "%"),
    "eps": ("每股收益 EPS", "元/股"), "bps": ("每股净资产 BPS", "元/股"),
    "total_assets": ("总资产", "亿元"), "total_equity": ("净资产", "亿元"),
    "npl_ratio": ("不良贷款率", "%"), "nim": ("净息差", "%"),
    "core_car": ("核心一级资本充足率", "%"), "provision_coverage": ("拨备覆盖率", "%"),
    "solvency_ar": ("综合偿付能力充足率", "%"), "rd_expend": ("研发费用", "亿元"),
}


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def year_of(row):
    text = str(row.get("report_name") or row.get("period") or row.get("report_date") or "")
    found = re.search(r"(?<!\d)(20\d{2})(?!\d)", text)
    return int(found.group(1)) if found else None


def annual_rows(rows):
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("report_type") or "")
        label = str(row.get("report_name") or row.get("period") or "")
        annual = kind == "年报" or (not kind and ("年报" in label or str(row.get("report_date") or "")[5:10] == "12-31"))
        year = year_of(row)
        if annual and year and year not in out:
            out[year] = copy.deepcopy(row)
    return out


def comparison_targets(question, stocks, current_code=""):
    if not re.search(r"对比|比较|相比|比一比|\bpk\b|\bvs\b|和.+比|与.+比", question, re.I):
        return []
    return mentioned_companies(question, stocks, current_code)


def mentioned_companies(question, stocks, current_code=""):
    candidates = [(code, "") for code in re.findall(r"(?<!\d)[03689]\d{5}(?!\d)", question)]
    candidates += [(s[1], s[0].split(" (")[0]) for s in stocks
                   if s[1] and len(s[0].split(" (")[0]) >= 3 and s[0].split(" (")[0] in question]
    unique = {}
    for code, name in candidates:
        if code != current_code:
            unique[code] = name or unique.get(code, "")
    return list(unique.items())[:2]


def requested_metrics(question):
    q = question.lower()
    keys = []
    growth = bool(re.search(r"增速|增长率|同比|增长.*跟|跟.*增长", q))
    for pattern, key in (
        (r"营收|营业收入|销售收入", "revenue_yoy" if growth else "revenue"),
        (r"净利润|归母|净利(?!率)", "net_profit_yoy" if growth else "net_profit"),
        (r"roe|净资产收益率", "roe"), (r"毛利率", "gross_margin"), (r"净利率", "net_margin"),
        (r"资产负债率|负债率", "debt_ratio"), (r"流动比率", "current_ratio"),
        (r"速动比率", "quick_ratio"), (r"经营现金流|现金流.*营收|收现", "ocf_to_revenue"),
        (r"现金流量比率", "cash_flow_ratio"), (r"eps|每股收益", "eps"),
        (r"bps|每股净资产", "bps"), (r"总资产", "total_assets"),
        (r"(?<!每股)净资产(?!收益)", "total_equity"), (r"不良贷款率|不良率", "npl_ratio"),
        (r"净息差", "nim"), (r"核心.*资本|资本充足率", "core_car"),
        (r"拨备覆盖", "provision_coverage"), (r"偿付能力", "solvency_ar"),
        (r"研发费用|研发投入|研发支出", "rd_expend"),
    ):
        if re.search(pattern, q):
            keys.append(key)
    if not keys:
        if re.search(r"盈利|利润率", q):
            keys = ["roe", "gross_margin", "net_margin"]
        elif re.search(r"成长|增长", q):
            keys = ["revenue_yoy", "net_profit_yoy"]
        elif re.search(r"偿债|负债|杠杆", q):
            keys = ["debt_ratio", "current_ratio", "quick_ratio"]
        elif "现金流" in q:
            keys = ["ocf_to_revenue", "cash_flow_ratio"]
    return list(dict.fromkeys(keys))


def _time_request(question):
    years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", question)]
    current_year = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).year
    for word, offset in (("今年", 0), ("去年", 1), ("前年", 2)):
        if word in question:
            years.append(current_year - offset)
    years = list(dict.fromkeys(years))
    if len(years) == 2 and re.search(r"至|到|[-—~～]", question):
        years = list(range(min(years), max(years) + 1))
    recent = re.search(r"近\s*([一二两三四五六七八九十\d]+)\s*年", question)
    count = None
    if recent:
        word = recent.group(1)
        count = int(word) if word.isdigit() else {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                                                   "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}.get(word)
    temporal = bool(re.search(r"趋势|历年|逐年|几年|多年|近.*年|走势|变化|变动|折线", question)) or len(years) > 1
    return years, count, temporal


def build_bundle(question, companies, *, previous_question=""):
    """Plan appropriately scaled charts; snapshot values never change on rerender."""
    result = {"version": 1, "question": question, "charts": [], "notes": [],
              "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    companies = copy.deepcopy(list(companies or []))
    if re.search(r"一季|二季|三季|四季|半年|中报|季报|季度|月度|本月|当月", question):
        result["notes"].append("当前图表数据采用年报口径；本问题需要季报、半年报或月度数据，暂不拿全年数值替代。")
        return result
    if re.search(r"经营现金流.*(?:净额|金额)|经营活动产生的现金流量净额", question):
        result["notes"].append("当前结构化数据未提供经营现金流净额，不能用现金流与营收的比率替代金额图。")
        return result
    named = [c for c in companies if (c.get("code") and c["code"] in question)
             or (c.get("name") and c["name"] in question)]
    if named and len(named) >= 2:
        companies = named
    keys = requested_metrics(question)
    referring = bool(previous_question and re.search(r"这[些两几]|上述|它们|换成|改成|画.*图|可视化", question))
    if not keys and referring:
        keys = requested_metrics(previous_question)
    score_request = bool(re.search(r"评分|得分|雷达|维度|优势|短板|哪家|值得投|强项|劣势|排序|排名", question)) and not keys
    if not keys and not score_request:
        if re.search(r"估值|市盈率|市净率|\bpe\b|\bpb\b|股价|行情|占比|构成|饼图", question, re.I):
            result["notes"].append("当前年报数据未提供该问题所需的行情或构成明细，暂不生成无依据的图表。")
        return result
    if not companies:
        result["notes"].append("当前没有可核验的结构化财务数据，暂不生成图表。")
        return result
    rows_by_company = [annual_rows(c.get("rows")) for c in companies]
    years, recent, temporal = _time_request(question)
    if referring and not years and recent is None:
        old_years, old_recent, old_temporal = _time_request(previous_question)
        years, recent, temporal = old_years, old_recent, temporal or old_temporal
    available = set.intersection(*(set(rows) for rows in rows_by_company))
    if len(companies) > 1 and not available:
        result["notes"].append("这些公司没有共同年报年份，暂不跨期绘制对比图；请补齐同年数据。")
        return result
    if len(companies) == 1:
        available = set(rows_by_company[0])
    selected = sorted(set(years) if years else available)
    if recent and available and not years:
        last = max(available)
        selected = list(range(last - min(recent, 10) + 1, last + 1))
    elif temporal and selected:
        selected = list(range(min(selected), max(selected) + 1))
    elif not years and selected:
        selected = [max(selected)]
    if years:
        missing = sorted(set(years) - available)
        if missing:
            result["notes"].append("缺少共同可核验的年报数据：" + "、".join(map(str, missing)) + "；缺失值不补零。")
    if temporal and len(selected) < 2:
        result["notes"].append("可用年报不足两期，暂不绘制趋势线；仅展示可核验的单期数据。")
    if not selected:
        # Unconfirmed-period manual snapshots may be shown alone, never as a trend or cross-company comparison.
        snapshots = [r for r in companies[0].get("rows", []) if r.get("_snapshot")]
        if len(companies) == 1 and snapshots and not temporal and not years:
            selected = [None]
            rows_by_company = [{None: snapshots[0]}]
        else:
            result["notes"].append("缺少可确认报告期的数据，无法可靠绘图。")
            return result
    if len(companies) > 1 and not any(year in available for year in selected):
        return result
    if score_request:
        keys = ["__scores"]
    missing_metrics = []
    for key in keys:
        label, unit = ("规则评分", "分（0–10）") if key == "__scores" else METRICS[key]
        values = []
        for company, rows in zip(companies, rows_by_company):
            for year in selected:
                row = rows.get(year, {})
                period = f"{year}年报" if year else str(row.get("report_name") or "材料未注明报告期")
                common = {"公司": str(company.get("name") or company.get("code") or "当前公司"),
                          "代码": str(company.get("code") or ""), "报告期": period,
                          "来源": str(company.get("source") or "来源未确认"),
                          "快照时间": str(company.get("fetched_at") or result["created_at"]),
                          "单位": unit}
                if key == "__scores":
                    import scoring
                    score = scoring.compute_scores(row, company.get("industry") or "通用",
                                                   finance_kind=company.get("finance_kind") or None)
                    for dimension, value in score["scores"].items():
                        values.append({**common, "指标": dimension, "数值": numeric(value)})
                else:
                    values.append({**common, "指标": label, "数值": numeric(row.get(key))})
        valid = [v for v in values if v["数值"] is not None]
        # Keep missing companies visible; do not turn a two-company comparison into one company silently.
        if not valid or (len(companies) > 1 and len({v["公司"] for v in valid}) < 2):
            missing_metrics.append(label)
            continue
        line = (temporal and len({v["报告期"] for v in valid}) > 1 and key != "__scores"
                and not re.search(r"柱状|条形", question))
        title = label + (" · 年报趋势" if line else " · 同期对比" if len(companies) > 1 else " · 指标快照")
        if key == "__scores":
            result["notes"].append("评分按各公司所属行业规则计算，仅用于本次指标解释；不同行业评分口径可能不同。")
        if any(v["数值"] is None for v in values):
            result["notes"].append(f"{label}存在缺失披露，图中留空，不补零或连成有效数据。")
        result["charts"].append({"title": title, "kind": "line" if line else "bar", "metric": key,
                                 "unit": unit, "data": values,
                                 "sources": [{k: c.get(k, "") for k in ("name", "code", "source", "source_url", "fetched_at")}
                                             for c in companies]})
    if missing_metrics:
        result["notes"].append("未取得足够的有效数据：" + "、".join(missing_metrics) + "；对应图表暂不生成。")
    if "饼图" in question and result["charts"]:
        result["notes"].append("这些指标不是互斥的组成部分，已采用对比图，避免用饼图误导。")
    if "ocf_to_revenue" in keys and not re.search(r"比|质量|收现|营收|收入", question):
        result["notes"].append("当前可核验的是经营现金流与营收的比率（%），不代表经营现金流净额（亿元）。")
    result["notes"] = list(dict.fromkeys(result["notes"]))
    return result


def context_for_model(bundle):
    if not bundle.get("charts") and not bundle.get("notes"):
        return ""
    # This exact table is shared with the charts: never ask the model to invent plot values.
    return ("\n\n【本轮可视化的核验数据；回答须与此保持一致，空值表示缺失，不得补造。"
            "图表采用年报口径；背景中的季报或不同年份不得混用于图中的同期对比】\n") + json.dumps(
        {"charts": [{"title": c["title"], "data": c["data"]} for c in bundle.get("charts", [])],
         "notes": bundle.get("notes", [])}, ensure_ascii=False, allow_nan=False)


def chart_spec(chart, *, foreground="#334155", grid="#DCE4EF"):
    """Vega-Lite spec with tooltips; one unit and one metric per axis."""
    line = chart["kind"] == "line"
    scores = chart.get("metric") == "__scores"
    periods = list(dict.fromkeys(v["报告期"] for v in chart["data"]))
    multiple_periods = len(periods) > 1
    finite = [v["数值"] for v in chart["data"] if numeric(v["数值"]) is not None]
    low, high = min([0.0] + finite), max([0.0] + finite)
    padding = max((high - low) * .08, .1)
    domain = [0, 10] if scores else [low - padding if low < 0 else 0, high + padding]
    time_axis = line or (multiple_periods and not scores)
    data = [dict(row, 系列=row["公司"] + (" · " + row["报告期"] if multiple_periods and scores else ""))
            for row in chart["data"]]
    encoding = {
        "x": {"field": "报告期" if time_axis else "指标" if scores else "公司", "type": "ordinal", "title": None,
              "axis": {"labelAngle": 0, "labelLimit": 140, "labelOverlap": "greedy"}},
        "y": {"field": "数值", "type": "quantitative", "title": chart["unit"], "scale": {"zero": True, "domain": domain}},
        "color": {"field": "系列", "type": "nominal", "legend": {"title": None, "orient": "bottom"}},
        "tooltip": [{"field": key, "type": "quantitative" if key == "数值" else "nominal"}
                    for key in ("公司", "指标", "报告期", "数值", "单位", "来源", "快照时间")],
    }
    if time_axis:
        encoding["x"]["sort"] = periods
    if not line:
        encoding["xOffset"] = {"field": "系列", "type": "nominal"}
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "data": {"values": data}, "height": 260,
        "mark": {"type": "line", "point": True, "invalid": "break-paths-show-domains"} if line else {"type": "bar", "tooltip": True},
        "encoding": encoding,
        "config": {"background": "transparent", "view": {"stroke": None},
                   "axis": {"labelColor": foreground, "titleColor": foreground, "gridColor": grid},
                   "legend": {"labelColor": foreground, "titleColor": foreground}},
    }


def render_bundle(bundle, *, foreground="#334155", grid="#DCE4EF"):
    import pandas as pd
    import streamlit as st
    for chart in bundle.get("charts", []):
        st.markdown("**📊 " + chart["title"] + "**")
        st.vega_lite_chart(spec=chart_spec(chart, foreground=foreground, grid=grid), width="stretch", theme=None)
        st.caption("报告期：" + "、".join(dict.fromkeys(v["报告期"] for v in chart["data"])) + " · 单位：" + chart["unit"])
        with st.expander("查看本图数据与来源", expanded=False):
            st.dataframe(pd.DataFrame(chart["data"]), hide_index=True, width="stretch")
            for source in chart.get("sources", []):
                label = str(source.get("name") or "当前公司") + "：" + str(source.get("source") or "")
                st.caption(label)
                url = str(source.get("source_url") or "")
                if url.startswith("https://data.eastmoney.com/"):
                    st.link_button("查看公开财务数据", url)
    for note in bundle.get("notes", []):
        st.caption("ℹ️ " + note)

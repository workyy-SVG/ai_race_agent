# -*- coding: utf-8 -*-
"""P1-06 离线效果评测：固定样本 + 三类场景 + 四项准确率 + 耗时 + 失败样例留存。

对应优化清单 P1-06：
  「抽出统一业务流程供页面与评测复用；固定样本覆盖 错误输入 / 切换公司 / 多公司；
    记录数值准确率、期间与单位准确率、引用正确率、耗时；保留失败样例。」

设计要点
--------
1. **固定样本**：所有数据来自本文件内的 fixtures，通过 monkeypatch 替换
   data_source 的取数函数 —— 不联网、不依赖 key、结果可复现（今天跑和明天跑一致）。
2. **同一条链路**：评测调用 ``pipeline.analyze_core``，与页面 Streamlit 调用的是
   同一个函数（不是"评测版实现"），所以评测通过 == 页面行为正确。
3. **失败样例留存**：任何一项不通过，都会把输入 + 期望 + 实际产出完整落盘到
   ``eval_results/regression_failures_<时间戳>.json``，便于事后复盘，而不是只留一行 FAIL。

四项指标
--------
  数值准确率      关键财务指标能否被链路正确取到且与期望一致（含年报口径）
  期间准确率      评分基准期是否为「最近一个年报」、是否会跨期混比
  单位准确率      金额类数值的单位换算链是否成立（模型的"亿元" vs 年报的"元"）
  引用正确率      正文「第 X 页」引用落在真实页数范围内的比例

运行： ``python eval_regression.py``   （退出码 0=全通过，1=存在失败项）
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
os.chdir(_HERE)

import data_source as ds  # noqa: E402
import pipeline as pl  # noqa: E402
from evidence_integrity import match_metric  # noqa: E402

OUT_DIR = os.path.join(_HERE, "eval_results")
try:
    os.makedirs(OUT_DIR, exist_ok=True)
except OSError:
    # 只读/受限环境（如沙箱）下无写权限时退到临时目录，评测本身照常跑完，
    # 不至于因为"建不了目录"就让整份评测作废。
    import tempfile
    OUT_DIR = os.path.join(tempfile.gettempdir(), "ai_race_eval_results")
    os.makedirs(OUT_DIR, exist_ok=True)

CAT_VALUE = "数值"
CAT_PERIOD = "期间"
CAT_UNIT = "单位"
CAT_CITE = "引用"

# ---------------------------------------------------------------------------
# 固定样本（fixtures）：刻意做成"年报 + 缺某年 + 银行/保险/消费混合"，
# 才能同时测到「口径对齐」「缺失不补零」「金融子行业」三件事。
# ---------------------------------------------------------------------------
FIXTURES: Dict[str, List[Dict[str, Any]]] = {
    # 消费：2024 / 2023 / 2021（缺 2022 —— 用于验证缺失年份不补零）
    "600519": [
        {"report_name": "2024年报", "report_date": "2024-12-31", "report_type": "年报",
         "roe": 34.0, "gross_margin": 91.5, "net_margin": 52.0, "revenue_yoy": 15.0,
         "net_profit_yoy": 15.4, "debt_ratio": 18.0, "ocf_to_revenue": 30.0,
         "current_ratio": 4.5, "revenue": 1741.0, "net_profit": 862.0},
        {"report_name": "2023年报", "report_date": "2023-12-31", "report_type": "年报",
         "roe": 32.0, "gross_margin": 91.0, "net_margin": 51.0, "revenue_yoy": 18.0,
         "net_profit_yoy": 19.0, "debt_ratio": 17.0, "ocf_to_revenue": 28.0,
         "current_ratio": 4.2, "revenue": 1476.0, "net_profit": 747.0},
        {"report_name": "2021年报", "report_date": "2021-12-31", "report_type": "年报",
         "roe": 29.0, "gross_margin": 90.0, "net_margin": 50.0, "revenue_yoy": 12.0,
         "net_profit_yoy": 12.5, "debt_ratio": 16.0, "ocf_to_revenue": 26.0,
         "current_ratio": 4.0, "revenue": 1094.0, "net_profit": 525.0},
    ],
    # 银行：同样 2024 / 2023 / 2021，但走银行专属维度（不良率/拨备/资本充足）
    "600036": [
        {"report_name": "2024年报", "report_date": "2024-12-31", "report_type": "年报",
         "roe": 15.0, "nim": 2.0, "npl_ratio": 0.95, "provision_coverage": 410.0,
         "core_car": 14.0, "revenue_yoy": 3.0, "net_profit_yoy": 6.0,
         "debt_ratio": 91.0, "ocf_to_revenue": 40.0},
        {"report_name": "2023年报", "report_date": "2023-12-31", "report_type": "年报",
         "roe": 14.5, "nim": 2.1, "npl_ratio": 1.05, "provision_coverage": 400.0,
         "core_car": 13.5, "revenue_yoy": 2.0, "net_profit_yoy": 5.0,
         "debt_ratio": 90.5, "ocf_to_revenue": 38.0},
        {"report_name": "2021年报", "report_date": "2021-12-31", "report_type": "年报",
         "roe": 14.0, "nim": 2.2, "npl_ratio": 1.10, "provision_coverage": 390.0,
         "core_car": 13.0, "revenue_yoy": 4.0, "net_profit_yoy": 7.0,
         "debt_ratio": 90.0, "ocf_to_revenue": 37.0},
    ],
    # 保险：只有 2024 / 2023 —— 与上面两家求交集时不应把 2021 混进来
    "601318": [
        {"report_name": "2024年报", "report_date": "2024-12-31", "report_type": "年报",
         "roe": 13.0, "solvency_ar": 205.0, "nbv_rate": 25.0, "net_margin": 10.0,
         "revenue_yoy": 8.0, "net_profit_yoy": 12.0, "debt_ratio": 89.0,
         "ocf_to_revenue": 35.0},
        {"report_name": "2023年报", "report_date": "2023-12-31", "report_type": "年报",
         "roe": 12.0, "solvency_ar": 200.0, "nbv_rate": 22.0, "net_margin": 9.0,
         "revenue_yoy": 6.0, "net_profit_yoy": 10.0, "debt_ratio": 88.5,
         "ocf_to_revenue": 33.0},
    ],
    # 只有 2020 年报 —— 与前面几家求交集必须为空（P1-03：无共同年份要明确提示）
    "000001": [
        {"report_name": "2020年报", "report_date": "2020-12-31", "report_type": "年报",
         "roe": 10.0, "npl_ratio": 1.5, "provision_coverage": 300.0, "core_car": 12.0,
         "nim": 2.3, "debt_ratio": 92.0, "ocf_to_revenue": 30.0},
    ],
}

# monkeypatch：评测期间所有"在线取数"都命中固定样本
ds.get_financial_indicators = lambda code, periods=8, annual_only=False: list(
    FIXTURES.get(str(code), [])
)
ds.build_summary_text = lambda code, name="", periods=8: (
    f"{name or code} 2024年报财务摘要：营业收入 1,741.00 亿元，同比增长 15.0%；"
    f"归母净利润 862.00 亿元；ROE 34.0%；毛利率 91.5%。"
)

# 模拟年报正文片段：单位刻意写成「元」，与模型常用的「亿元」不同 —— 考验单位换算链
PDF_TEXT = (
    "2024年报 第三节 主要会计数据和财务指标\n"
    "营业收入 6,058,908,000.00 元，同比增长 15.0%。\n"
    "归属于上市公司股东的净利润 3,400,000,000.00 元。\n"
)


# ---------------------------------------------------------------------------
# 断言收集器
# ---------------------------------------------------------------------------
class Case:
    """一个评测场景：跑同一条 pipeline 链路，按类别登记断言，并记录耗时。"""

    def __init__(self, cid: str, scenario: str, desc: str):
        self.id = cid
        self.scenario = scenario
        self.desc = desc
        self.checks: List[Dict[str, Any]] = []
        self.elapsed_ms = 0.0
        self.error: Optional[str] = None

    def check(self, category: str, name: str, cond: bool, detail: str = "") -> bool:
        ok = bool(cond)
        self.checks.append(
            {"category": category, "name": name, "ok": ok, "detail": str(detail)[:400]}
        )
        return ok

    def timed(self, func: Callable[[], None]) -> None:
        _t = time.perf_counter()
        func()
        self.elapsed_ms = round((time.perf_counter() - _t) * 1000, 1)

    @property
    def failed(self) -> List[Dict[str, Any]]:
        return [c for c in self.checks if not c["ok"]]


# ---------------------------------------------------------------------------
# 场景 1：单公司正常分析（消费）
# ---------------------------------------------------------------------------
def case_normal() -> Case:
    c = Case("S1", "正常分析", "消费股 600519 · 在线取数 · 带年报片段与页码引用")

    analysis: Dict[str, Any] = {
        "company": "贵州茅台",
        "summary": "盈利能力突出，详见年报第 42 页的经营情况讨论。",
        "risks": [], "highlights": [],
        "metrics": [
            {"item": "2024年报营业收入", "value": 1741.0, "unit": "亿元"},
            {"item": "2024年报毛利率", "value": 91.5, "unit": "%"},
        ],
        "chart_data": [], "scores": {}, "total_score": None,
    }
    # 年报检索片段：单位刻意写成「元」（174,100,000,000 = 1741 亿元），
    # 用来验证"模型写亿元、年报写元"时证据卡仍能正向定位到页码。
    hits = [{
        "page": 42,
        "text": "2024年报本期营业收入 174,100,000,000.00 元，同比增长 15.00%；毛利率 91.50%。",
    }]
    core: Dict[str, Any] = {}

    def _run():
        core.update(pl.analyze_core(
            analysis, ds.build_summary_text("600519", "贵州茅台"),
            industry="消费", code="600519", online=True,
            pdftext=PDF_TEXT, rag_hits=hits, rag_pages=220,
        ))

    c.timed(_run)
    m = core.get("metrics") or {}

    # —— 数值准确率：链路取到的指标必须与固定样本一致 ——
    for key, want in (("roe", 34.0), ("gross_margin", 91.5), ("net_margin", 52.0),
                      ("revenue_yoy", 15.0), ("debt_ratio", 18.0)):
        got = m.get(key)
        c.check(CAT_VALUE, f"{key} = {want}",
                isinstance(got, (int, float)) and abs(float(got) - want) < 1e-6,
                f"got={got}")

    # —— 期间准确率：必须锁在「最近一个年报」，不能落到季报/中报 ——
    c.check(CAT_PERIOD, "评分基准期为最近一个年报（2024年报）",
            "2024" in str(core.get("score_period") or ""), core.get("score_period"))
    c.check(CAT_PERIOD, "指标来源标注为在线财务指标",
            "在线财务指标" in str(core.get("score_source") or ""), core.get("score_source"))

    # —— 单位准确率：模型写「亿元」、年报写「元」，换算链必须能配上 ——
    c.check(CAT_UNIT, "亿元↔元 单位换算链命中（60.59 亿元 vs 6,058,908,000 元）",
            bool(match_metric({"item": "2024年报营业收入", "value": 60.59, "unit": "亿元"}, PDF_TEXT)), "")
    c.check(CAT_UNIT, "正常数值不误报为编造（证据核对无疑点）",
            not core.get("evidence_issues"), str(core.get("evidence_issues"))[:200])

    # —— 引用正确率：第 42 页在 1~220 的范围内 ——
    cite = core.get("citation") or {}
    c.check(CAT_CITE, "页码引用落在实际页数范围内", cite.get("rate") == 1.0,
            str(cite))
    evs = core.get("evidence_items") or []
    c.check(CAT_CITE, "证据卡可正向定位到页码 42",
            any(e.get("page") == 42 for e in evs),
            str([(e.get("item"), e.get("page")) for e in evs])[:200])
    return c


# ---------------------------------------------------------------------------
# 场景 2：切换公司（消费 → 银行）—— 防"行业/指标串用"
# ---------------------------------------------------------------------------
def case_switch() -> Case:
    c = Case("S2", "切换公司", "同一份输入文本，分析对象由 600519 切到 600036（银行）")

    base = {"company": "", "summary": "s", "risks": [], "highlights": [],
            "metrics": [], "chart_data": [], "scores": {}, "total_score": None}
    out: Dict[str, Any] = {}

    def _run():
        a1 = dict(base, company="贵州茅台")
        out["A"] = pl.analyze_core(a1, "", industry="消费", code="600519", online=True)
        a2 = dict(base, company="招商银行")
        out["B"] = pl.analyze_core(a2, "", industry="金融", code="600036", online=True)

    c.timed(_run)
    A, B = out.get("A", {}), out.get("B", {})

    c.check(CAT_VALUE, "切换后 ROE 变为银行样本值（15.0）",
            abs(float((B.get("metrics") or {}).get("roe", 0) or 0) - 15.0) < 1e-6,
            str((B.get("metrics") or {}).get("roe")))
    c.check(CAT_VALUE, "银行的专属指标被取到（不良率 / 拨备覆盖率）",
            (B.get("metrics") or {}).get("npl_ratio") is not None
            and (B.get("metrics") or {}).get("provision_coverage") is not None,
            str(sorted((B.get("metrics") or {}).keys())))
    c.check(CAT_VALUE, "切换到银行后不再沿用消费股指标（毛利率应为 None）",
            (B.get("metrics") or {}).get("gross_margin") is None,
            str((B.get("metrics") or {}).get("gross_margin")))
    c.check(CAT_PERIOD, "切换公司后评分基准期同步为 2024年报",
            "2024" in str(B.get("score_period") or ""), B.get("score_period"))
    c.check(CAT_PERIOD, "两次分析的维度不同——消费用通用维度、银行用银行维度",
            set((A["analysis"].get("scores") or {}).keys())
            != set((B["analysis"].get("scores") or {}).keys()),
            f"A={sorted((A['analysis'].get('scores') or {}).keys())} "
            f"B={sorted((B['analysis'].get('scores') or {}).keys())}")
    c.check(CAT_PERIOD, "银行金融子行业被识别为「银行」（随校验透传，未被字段重筛丢掉）",
            B["analysis"].get("score_finance_kind") == "银行",
            str(B["analysis"].get("score_finance_kind")))
    return c


# ---------------------------------------------------------------------------
# 场景 3：错误输入（无效代码 / 编造数值 / 空输入）
# ---------------------------------------------------------------------------
def case_error_input() -> Case:
    c = Case("S3", "错误输入", "无效代码 99999 / 编造营收 99999 亿元 / 空输入")

    res: Dict[str, Any] = {}

    def _run():
        # 3.1 无效代码：接口无此人 → 回落到「文本抽取」，且覆盖率不足不给综合评分
        res["bad"] = pl.analyze_core(
            {"company": "?", "summary": "s", "risks": [], "highlights": [],
             "metrics": [], "chart_data": [], "scores": {}, "total_score": None},
            "这是一段没有财务指标的文本，无法抽取任何数值。",
            industry="消费", code="999999", online=True,
        )
        # 3.2 编造数值：必须被证据核对点名（不能装作引用有效）
        res["fake"] = pl.analyze_core(
            {"summary": "s", "risks": [], "highlights": [],
             "metrics": [{"item": "2024年报营业收入", "value": 99999.0, "unit": "亿元"}],
             "chart_data": [], "scores": {}, "total_score": None},
            "营业收入 1,741.00 亿元", industry="消费", online=False,
        )
        # 3.3 空输入：链路必须不抛异常
        try:
            res["empty"] = pl.analyze_core({}, "", industry="通用", online=False)
            res["empty_ok"] = True
        except Exception as e:  # noqa: BLE001
            res["empty_ok"] = False
            res["empty_err"] = f"{type(e).__name__}: {e}"

    c.timed(_run)

    bad = res.get("bad", {})
    cov = (bad.get("analysis") or {}).get("score_coverage")
    c.check(CAT_VALUE, "无效代码下不借用任何旧指标（metrics 为空）",
            not bad.get("metrics"), str(bad.get("metrics"))[:200])
    c.check(CAT_PERIOD, "指标来源降级为「文本抽取」并如实标注",
            "文本抽取" in str(bad.get("score_source") or ""), bad.get("score_source"))
    c.check(CAT_PERIOD, "覆盖率 < 50% 时不给综合评分",
            cov is None or cov < 0.5, f"coverage={cov}")
    c.check(CAT_PERIOD, "评分置信度为「信息不足」",
            (bad.get("analysis") or {}).get("score_confidence") == "信息不足",
            str((bad.get("analysis") or {}).get("score_confidence")))

    fake = res.get("fake", {})
    c.check(CAT_UNIT, "编造的 99999 亿元被证据核对点名（不假装引用有效）",
            any("99999" in str(x) for x in (fake.get("evidence_issues") or [])),
            str(fake.get("evidence_issues"))[:240])

    c.check(CAT_VALUE, "空输入不崩溃", bool(res.get("empty_ok")), str(res.get("empty_err", "")))
    return c


# ---------------------------------------------------------------------------
# 场景 4：多公司（共同年报年份对齐 / 无共同年份提示）
# ---------------------------------------------------------------------------
def case_multi() -> Case:
    c = Case("S4", "多公司", "2~5 家对比：共同年报年份对齐 + 无共同年份明确提示")

    def years_of(code: str) -> List[str]:
        rows = pl.all_annual_rows(code, periods=8)
        return sorted({pl.year_of(r) for r in rows if pl.year_of(r)})

    def common_years(codes: List[str]) -> Tuple[List[str], Dict[str, List[str]]]:
        per = {k: years_of(k) for k in codes}
        sets = [set(v) for v in per.values() if v]
        common = sorted(set.intersection(*sets)) if len(sets) == len(per) and sets else []
        return common, per

    out: Dict[str, Any] = {}

    def _run():
        out["common3"], out["per3"] = common_years(["600519", "600036", "601318"])
        out["years_missing"], _ = common_years(["600519", "000001"])
        # 5 家（含一家不存在的代码）也不能崩，且该家的年份列表为空 → 交集为空
        out["common5"], out["per5"] = common_years(
            ["600519", "600036", "601318", "000001", "000002"]
        )

    c.timed(_run)

    c.check(CAT_PERIOD, "三家共同年报年份 = 2023、2024（保险缺 2021，不混进来）",
            out.get("common3") == ["2023", "2024"], str(out.get("common3")))
    c.check(CAT_PERIOD, "缺同一年份的公司不参与对齐（600519 有 2021，保险没有）",
            "2021" not in (out.get("common3") or []), str(out.get("per3")))
    c.check(CAT_PERIOD, "无共同年报年份时交集为空（可明确提示『无共同年报年份』）",
            out.get("years_missing") == [], str(out.get("years_missing")))
    c.check(CAT_PERIOD, "取不到数据的公司参与时整体交集也为空，不静默丢弃",
            out.get("common5") == [], str(out.get("per5")))
    return c


# ---------------------------------------------------------------------------
# 汇总与报告
# ---------------------------------------------------------------------------
def aggregate(cases: List[Case]) -> Dict[str, Any]:
    buckets: Dict[str, List[bool]] = {CAT_VALUE: [], CAT_PERIOD: [], CAT_UNIT: [], CAT_CITE: []}
    for c in cases:
        for chk in c.checks:
            buckets.setdefault(chk["category"], []).append(chk["ok"])

    def rate(key: str) -> Optional[float]:
        v = buckets.get(key) or []
        return round(sum(1 for x in v if x) / len(v), 4) if v else None

    pu = (buckets.get(CAT_PERIOD) or []) + (buckets.get(CAT_UNIT) or [])
    return {
        "数值准确率": rate(CAT_VALUE),
        "期间准确率": rate(CAT_PERIOD),
        "单位准确率": rate(CAT_UNIT),
        "期间与单位准确率": round(sum(1 for x in pu if x) / len(pu), 4) if pu else None,
        "引用正确率": rate(CAT_CITE),
    }


def main() -> int:
    cases = [case_normal(), case_switch(), case_error_input(), case_multi()]
    rates = aggregate(cases)
    total_ms = round(sum(c.elapsed_ms for c in cases), 1)

    print("=" * 68)
    print("P1-06 离线效果评测（固定样本 · 不联网 · 与页面共用 pipeline.analyze_core）")
    print("=" * 68)
    for c in cases:
        bad = c.failed
        flag = "PASS" if not bad else "FAIL"
        print(f"[{flag}] {c.id} {c.scenario}｜{c.desc}｜{c.elapsed_ms} ms｜{len(c.checks)} 项断言")
        for chk in c.checks:
            mark = "OK" if chk["ok"] else "×"
            line = f"      {mark} [{chk['category']}] {chk['name']}"
            if not chk["ok"] and chk["detail"]:
                line += f"  →  {chk['detail']}"
            print(line)

    print("-" * 68)
    for k, v in rates.items():
        print(f"{k:<16} {('%.1f%%' % (v * 100)) if isinstance(v, float) else '—'}")
    print(f"{'总耗时':<16} {total_ms} ms")
    print("-" * 68)

    failures = [
        {"case": c.id, "scenario": c.scenario, "desc": c.desc,
         "elapsed_ms": c.elapsed_ms, "check": chk}
        for c in cases for chk in c.failed
    ]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rates": rates, "total_ms": total_ms,
        "cases": [
            {"id": c.id, "scenario": c.scenario, "desc": c.desc,
             "elapsed_ms": c.elapsed_ms, "checks": c.checks}
            for c in cases
        ],
        "failure_count": len(failures),
    }
    import tempfile as _tempfile

    def _tempdir() -> str:
        return _tempfile.gettempdir()

    def _write_json(name: str, obj) -> str:
        """把报告落盘；目录不可写时**自动退到临时目录**并说明。

        只读文件系统 / 沙箱 / 只读容器下，写报告失败不应该让整份评测作废 ——
        控制台里的准确率才是评测的结论，报告只是留存。
        注意：这里不能只在建目录时 try，因为"目录建得出来、文件写不进去"
        （只读挂载、目录权限受限）同样常见，必须把**写动作本身**也包住。
        """
        for _d in (OUT_DIR, os.path.join(_tempdir(), "ai_race_eval_results")):
            try:
                os.makedirs(_d, exist_ok=True)
                _p = os.path.join(_d, name)
                with io.open(_p, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False, indent=2)
                return _p
            except OSError:
                continue
        return ""

    json_path = _write_json(f"regression_{ts}.json", report)
    if failures:
        fail_path = _write_json(f"regression_failures_{ts}.json", failures)
        print(f"失败样例已留存：{fail_path or '（写入失败，上面控制台输出即为完整记录）'}")
    print(f"评测报告：{json_path or '（写入失败，控制台输出即为完整记录）'}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

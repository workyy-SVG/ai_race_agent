"""Financial chart correctness tests, using fixed offline source rows."""
import copy
import json
import unittest

import followup_charts as charts


def company(name="甲公司", code="600001", years=(2021, 2023, 2024)):
    return {"name": name, "code": code, "industry": "消费", "source": "固定财务样本",
            "rows": [{"report_name": f"{y}年报", "report_type": "年报", "report_date": f"{y}-12-31",
                      "revenue": 100 + y - 2020, "net_profit": -2 if y == 2023 else 10,
                      "roe": 15, "gross_margin": 45, "net_margin": 10,
                      "revenue_yoy": 12, "net_profit_yoy": -5, "debt_ratio": 40,
                      "current_ratio": 2, "ocf_to_revenue": 22} for y in years]}


class FollowupChartTests(unittest.TestCase):
    def test_revenue_question_does_not_get_unrelated_profit(self):
        bundle = charts.build_bundle("近五年营收变化", [company()])
        self.assertEqual([c["metric"] for c in bundle["charts"]], ["revenue"])
        self.assertEqual(bundle["charts"][0]["kind"], "line")
        self.assertEqual(bundle["charts"][0]["unit"], "亿元")

    def test_missing_years_are_gaps_not_zero(self):
        c = charts.build_bundle("近五年营收趋势", [company()])["charts"][0]
        self.assertEqual([r["报告期"] for r in c["data"]], [f"{y}年报" for y in range(2020, 2025)])
        self.assertIsNone(next(r["数值"] for r in c["data"] if r["报告期"] == "2022年报"))
        self.assertEqual(charts.chart_spec(c)["mark"]["invalid"], "break-paths-show-domains")

    def test_growth_rates_not_amounts(self):
        bundle = charts.build_bundle("营收和净利润同比增速比较", [company()])
        self.assertEqual([c["metric"] for c in bundle["charts"]], ["revenue_yoy", "net_profit_yoy"])
        self.assertTrue(all(c["unit"] == "%" for c in bundle["charts"]))

    def test_negative_profit_is_preserved(self):
        c = charts.build_bundle("2023年净利润", [company()])["charts"][0]
        self.assertEqual(c["data"][0]["数值"], -2)

    def test_explicit_year_is_not_silently_replaced(self):
        bundle = charts.build_bundle("2022年营收是多少", [company()])
        self.assertFalse(bundle["charts"])
        self.assertIn("2022", " ".join(bundle["notes"]))

    def test_companies_align_to_common_year(self):
        a, b = company(), company("乙公司", "600002", (2021, 2023))
        c = charts.build_bundle("比较甲公司和乙公司的营收", [a, b])["charts"][0]
        self.assertEqual({r["报告期"] for r in c["data"]}, {"2023年报"})
        self.assertEqual({r["公司"] for r in c["data"]}, {"甲公司", "乙公司"})

    def test_no_common_year_refuses_mixed_period_comparison(self):
        bundle = charts.build_bundle("对比营收", [company(years=(2024,)), company("乙", "600002", (2023,))])
        self.assertFalse(bundle["charts"])
        self.assertIn("共同年报年份", " ".join(bundle["notes"]))

    def test_quarterly_rows_never_used_as_annual(self):
        a = company()
        a["rows"].append({"report_name": "2025三季报", "report_type": "三季报", "revenue": 99999})
        c = charts.build_bundle("营收", [a])["charts"][0]
        self.assertEqual(c["data"][0]["报告期"], "2024年报")
        self.assertNotEqual(c["data"][0]["数值"], 99999)

    def test_quarterly_question_is_not_answered_by_annual_chart(self):
        bundle = charts.build_bundle("2024年三季度营收", [company()])
        self.assertFalse(bundle["charts"])
        self.assertIn("年报口径", " ".join(bundle["notes"]))

    def test_cashflow_amount_not_replaced_with_percentage(self):
        self.assertFalse(charts.build_bundle("经营现金流净额是多少", [company()])["charts"])

    def test_relative_current_year_does_not_get_latest_old_year(self):
        self.assertFalse(charts.build_bundle("今年营收是多少", [company(years=(2021,))])["charts"])

    def test_metric_units_never_share_one_axis(self):
        cs = charts.build_bundle("营收、毛利率和流动比率", [company()])["charts"]
        self.assertEqual({c["unit"] for c in cs}, {"亿元", "%", "倍"})
        for c in cs:
            self.assertEqual({v["单位"] for v in c["data"]}, {c["unit"]})

    def test_missing_company_is_not_silently_dropped(self):
        a, b = company(), company("乙公司", "600002")
        for row in b["rows"]:
            row["revenue"] = None
        bundle = charts.build_bundle("对比营收", [a, b])
        self.assertFalse(bundle["charts"])

    def test_no_numbers_or_nonfinancial_questions_do_not_get_fake_chart(self):
        self.assertFalse(charts.build_bundle("介绍一下公司的主营业务", [company()])["charts"])
        self.assertFalse(charts.build_bundle("营收趋势", [])["charts"])
        self.assertFalse(charts.build_bundle("PE估值比较", [company()])["charts"])

    def test_snapshot_independent_of_current_data(self):
        a = company()
        bundle = charts.build_bundle("营收", [a])
        before = copy.deepcopy(bundle)
        a["rows"][-1]["revenue"] = 99999
        self.assertEqual(bundle, before)
        json.loads(json.dumps(bundle, ensure_ascii=False, allow_nan=False))

    def test_nan_inf_and_boolean_are_not_financial_values(self):
        for value in (float("nan"), float("inf"), True):
            a = company(years=(2024,))
            a["rows"][0]["revenue"] = value
            self.assertFalse(charts.build_bundle("营收", [a])["charts"])

    def test_scores_are_computed_from_raw_metrics(self):
        c = charts.build_bundle("评分优势和短板", [company()])["charts"][0]
        self.assertEqual(c["metric"], "__scores")
        self.assertTrue(all(0 <= r["数值"] <= 10 for r in c["data"] if r["数值"] is not None))

    def test_manual_single_period_snapshot_not_promoted_to_trend(self):
        manual = {"name": "手动公司", "rows": [{"_snapshot": True, "roe": 15}], "source": "输入材料"}
        self.assertTrue(charts.build_bundle("ROE", [manual])["charts"])
        self.assertFalse(charts.build_bundle("ROE趋势", [manual])["charts"])

    def test_reference_question_reuses_metric_not_old_values(self):
        bundle = charts.build_bundle("把这两个指标换成折线图", [company()], previous_question="营收和净利润的趋势")
        self.assertEqual([c["metric"] for c in bundle["charts"]], ["revenue", "net_profit"])
        self.assertTrue(all(c["kind"] == "line" for c in bundle["charts"]))

    def test_requested_bar_style_keeps_annual_time_axis(self):
        c = charts.build_bundle("近五年净利润柱状图", [company()])["charts"][0]
        self.assertEqual(c["kind"], "bar")
        self.assertEqual(charts.chart_spec(c)["encoding"]["x"]["field"], "报告期")

    def test_company_subset_is_respected_in_comparison(self):
        a, b, c = company(), company("乙公司", "600002"), company("丙公司", "600003")
        chart = charts.build_bundle("比较甲公司和乙公司的营收", [a, b, c])["charts"][0]
        self.assertEqual({row["公司"] for row in chart["data"]}, {"甲公司", "乙公司"})

    def test_model_context_uses_exact_chart_numbers(self):
        bundle = charts.build_bundle("营收", [company()])
        self.assertIn('"数值": 104.0', charts.context_for_model(bundle))

    def test_comparison_code_and_name_are_deduplicated(self):
        stocks = [("甲公司 (600001)", "600001", "消费"), ("乙公司 (600002)", "600002", "消费")]
        self.assertEqual(charts.comparison_targets("和乙公司600002对比", stocks, "600001"), [("600002", "乙公司")])


if __name__ == "__main__":
    unittest.main()

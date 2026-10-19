"""Regression cases derived from the two October 18 review reports; offline."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import analysis_validate as av
import evidence_integrity as ei
import fincalc as fc
import financial_facts as facts
import research_evidence as rev
import rag_lite


SOURCE = """最新价：1329 元
### 2026中报
每股净资产 200.99 元；每股收益 25 元；净利率 50.53%
### 2026一季报
每股净资产 216.32 元；净利率 49.92%
### 2025年报
每股净资产 195.36 元；净利率 36.44%
"""


def state_for(code="600519", body="2025年报材料", content=b"pdf-version-one"):
    sha = hashlib.sha256(content).hexdigest()
    return {"single_online_code": code, "single_pdf_code": code, "single_pdf_year": "2025",
            "single_pdftext": body, "single_pdf_text_hash": rev.digest(body), "single_pdf_hash": sha,
            "single_pdf_tag": "20260401_annual_" + sha[:16], "single_pdf_title": code + "年报"}


class MaterialScopeTests(unittest.TestCase):
    def test_manual_and_upload_do_not_borrow_online_document(self):
        for source in ("manual", "upload"):
            self.assertIsNone(rev.document_binding(state_for(), {"source": source, "code": "600519"}))

    def test_new_company_does_not_borrow_old_company(self):
        self.assertIsNone(rev.document_binding(state_for(), {"source": "online", "code": "000858"}))

    def test_hash_and_owner_required(self):
        state = state_for()
        self.assertIsNotNone(rev.document_binding(state, {"source": "online", "code": "600519"}))
        for key in ("single_pdf_code", "single_pdf_text_hash", "single_pdf_hash"):
            bad = dict(state)
            bad.pop(key)
            self.assertIsNone(rev.document_binding(bad, {"source": "online", "code": "600519"}))
        state["single_pdftext"] = "changed"
        self.assertIsNone(rev.document_binding(state, {"source": "online", "code": "600519"}))

    def test_unbound_never_calls_backend(self):
        backend = Mock()
        hits, diag = rev.retrieve_scoped(backend, None, "风险")
        self.assertEqual((hits, diag["status"]), ([], "unbound"))
        backend.retrieve.assert_not_called()

    def test_no_arbitrary_same_year_fallback(self):
        backend = Mock()
        backend.has_index.return_value = False
        binding = rev.document_binding(state_for(), {"source": "online", "code": "600519"})
        hits, diag = rev.retrieve_scoped(backend, binding, "风险")
        self.assertEqual(diag["status"], "no_index")
        backend.find_index_tag.assert_not_called()

    def test_failures_and_empty_hits_have_different_states(self):
        binding = rev.document_binding(state_for(), {"source": "online", "code": "600519"})
        backend = Mock()
        backend.retrieve.return_value = []
        self.assertEqual(rev.retrieve_scoped(backend, binding, "风险")[1]["status"], "empty")
        backend.retrieve.side_effect = RuntimeError("offline failure")
        self.assertEqual(rev.retrieve_scoped(backend, binding, "风险")[1]["status"], "error")
        self.assertEqual(rev.retrieve_scoped(None, binding, "风险")[1]["status"], "unavailable")

    def test_backend_wrong_owner_or_version_is_rejected(self):
        binding = rev.document_binding(state_for(), {"source": "online", "code": "600519"})
        backend = Mock()
        backend.retrieve.return_value = [{"code": "000858", "page": 122, "text": "风险"},
                                        {"code": "600519", "doc_tag": "old", "page": 122, "text": "风险"}]
        self.assertEqual(rev.retrieve_scoped(backend, binding, "风险")[0], [])

    def test_restore_exact_document_and_reject_changed_file(self):
        content = b"pdf-version-one"
        body = "2025年报材料"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.pdf"
            path.write_bytes(content)
            state = state_for(body=body, content=content)
            state["single_pdf_path"] = str(path)
            snapshot = {"document": rev.pdf_snapshot(state)}
            self.assertTrue(rev.restore_document(state, snapshot, "600519", lambda _: body))
            self.assertEqual(state["single_pdftext"], body)
            path.write_bytes(b"different")
            self.assertFalse(rev.restore_document(state, snapshot, "600519", lambda _: body))
            self.assertNotIn("single_pdftext", state)
            self.assertFalse(rev.restore_document(state, {}, "600519", lambda _: body))

    def test_risk_expansion_retrieves_body_beyond_intro(self):
        backend = Mock()
        binding = rev.document_binding(state_for(), {"source": "online", "code": "600519"})
        backend.retrieve.side_effect = lambda query, *a, **kw: (
            [{"page": 3, "text": "目录 风险因素........经营风险........其他风险........", "score": 2}]
            if query == "风险如何？" else [{"page": 122, "text": "可能面对的风险：流动性风险和信用风险，制定应对措施。", "score": 1}])
        hits, diag = rev.retrieve_scoped(backend, binding, "风险如何？", top_k=1)
        self.assertEqual(hits[0]["page"], 122)
        self.assertEqual(len(diag["queries"]), 3)
        self.assertEqual(hits[0]["source_id"], binding["source_id"])

    def test_real_lite_index_finds_risk_page_in_143_page_material(self):
        pages = [{"page": n, "text": f"一般业务介绍 公司简介 常规信息 {n}" * 5} for n in range(1, 144)]
        pages[121]["text"] = "可能面对的风险 风险因素 信用风险 流动性风险 市场风险 经营风险及应对措施。" * 8
        state = state_for()
        binding = rev.document_binding(state, {"source": "online", "code": "600519"})
        with tempfile.TemporaryDirectory() as tmp, patch.object(rag_lite, "INDEX_ROOT", Path(tmp)), patch.object(rag_lite, "_ROOT_READY", False), patch.object(rag_lite, "_CACHE", {}), patch.object(rag_lite, "pdf_to_pages", return_value=pages):
            built = rag_lite.build_index_from_pdf("synthetic.pdf", "600519", "2025", doc_tag=binding["doc_tag"])
            self.assertTrue(built["ok"])
            hits, diag = rev.retrieve_scoped(rag_lite, binding, "这家公司的风险是什么？")
            self.assertIn(122, diag["pages"])
            self.assertTrue(hits[0]["index_fingerprint"])


class EvidenceMatchingTests(unittest.TestCase):
    def test_whole_signed_tokens(self):
        self.assertFalse(av.number_has_source(20, "2024年度"))
        self.assertFalse(av.number_has_source(-5, "净利率 5%"))
        self.assertFalse(av.number_has_source(0, "没有任何数值"))
        self.assertFalse(av.number_has_source(float("nan"), "20"))
        self.assertTrue(av.number_has_source(-5, "净利率 -5%"))

    def test_same_number_in_different_account_is_not_evidence(self):
        row = {"item": "2025年报营业收入", "value": 10, "unit": "亿元"}
        self.assertFalse(ei.match_metric(row, "2025年报流动性风险敞口 10 亿元"))
        self.assertFalse(ei.match_metric(row, "2025年报营业收入 30 亿元；存货 10 亿元"))

    def test_unit_sign_and_period_all_must_match(self):
        row = {"item": "2025年报营业收入", "value": 10, "unit": "亿元"}
        self.assertTrue(ei.match_metric(row, "2025年报营业收入 1,000,000,000 元"))
        for text in ("2024年报营业收入 10 亿元", "2025年报营业收入 10 万元", "2025年报营业收入 -10 亿元", "营业收入 10 亿元"):
            self.assertFalse(ei.match_metric(row, text), text)
        self.assertFalse(ei.match_metric(row, "营业收入 10亿元", source_year="2025"))

    def test_empty_sources_cannot_pass_metric_verification(self):
        self.assertTrue(av.verify_evidence({"metrics": [{"item": "营业收入", "value": 10, "unit": "亿元"}]}))

    def test_negative_and_percentage_values(self):
        row = {"item": "2025年报净利率", "value": -5, "unit": "%"}
        self.assertTrue(ei.match_metric(row, "2025年报净利率 -5%"))
        self.assertFalse(ei.match_metric(row, "2025年报净利率 5%"))
        self.assertFalse(ei.match_metric(row, "2025年报净利率 -5 亿元"))

    def test_table_columns_are_not_guessed(self):
        row = {"item": "2024年报营业收入", "value": 20, "unit": "亿元"}
        self.assertFalse(ei.match_metric(row, "2025年报 2024年报\n营业收入 20亿元 10亿元"))

    def test_false_page_122_cannot_certify_qa_metrics(self):
        row = {"item": "2025年报营业收入", "value": 10, "unit": "亿元"}
        hits = [{"page": 122, "year": "2025", "text": "信用风险 10亿元，流动性风险 2025万元。"}]
        mapped = av.evidence_map({"metrics": [row]}, hits, "2025年报营业收入 10亿元", (1, 143))
        self.assertTrue(mapped[0]["found"])
        self.assertIsNone(mapped[0]["page"])
        self.assertNotEqual(mapped[0]["source"], "年报检索片段")

    def test_valid_page_match_carries_document_identity(self):
        hit = {"page": 42, "year": "2025", "text": "2025年报营业收入 10亿元", "source_id": "current", "document_hash": "abc"}
        mapped = av.evidence_map({"metrics": [{"item": "2025年报营业收入", "value": 10, "unit": "亿元"}]}, [hit])
        self.assertEqual((mapped[0]["page"], mapped[0]["source_id"]), (42, "current"))


class ValuationAndPeriodTests(unittest.TestCase):
    def test_pb_uses_latest_report_in_any_order(self):
        reversed_blocks = "最新价：1329 元\n" + "\n### ".join(reversed(SOURCE.split("### ")[1:]))
        for text in (SOURCE, reversed_blocks):
            inputs = fc.parse_valuation_inputs(text)
            self.assertEqual((inputs["bps"], inputs["bps_period"]), (200.99, "2026中报"))
            self.assertAlmostEqual(fc.pb_from(inputs["price"], inputs["bps"])[0], 6.6123, places=4)
            self.assertIn("2026中报", fc.valuation_block(text))
        self.assertIn("2026一季报", facts.facts_prompt(SOURCE))

    def test_ambiguous_undated_or_conflicting_bps_is_not_called_latest(self):
        inputs = fc.parse_valuation_inputs("每股净资产 195.36 元；每股净资产 200.99 元")
        self.assertIsNone(inputs["bps"])
        self.assertEqual(inputs["bps_period"], "未注明报告期")
        self.assertIsNone(fc.parse_valuation_inputs("2026中报 每股净资产 200元；每股净资产 210元")["bps"])

    def test_wrong_2025_quarter_is_withheld(self):
        answer, issues = facts.guard_periods("2025一季报净利率49.92%。", SOURCE)
        self.assertTrue(issues)
        self.assertNotIn("2025一季报净利率49.92%", answer)
        self.assertIn("2026一季报净利率 49.92%", answer)

    def test_annual_quarter_trend_is_withheld(self):
        answer, issues = facts.guard_periods("净利率从2025年报36.44%上升至2026一季报49.92%。", SOURCE)
        self.assertTrue(issues)
        self.assertIn("不同口径不能直接推断趋势", answer)

    def test_valid_text_layout_is_unchanged(self):
        text = "# 分析\n\n2026一季报净利率49.92%。\n- 同口径数据不足。"
        self.assertEqual(facts.guard_periods(text, SOURCE), (text, []))

    def test_swapped_period_values_are_withheld(self):
        answer, issues = facts.guard_periods("2025年报净利率49.92%，2026一季报净利率36.44%。", SOURCE)
        self.assertTrue(issues)

    def test_correct_comparison_limitation_is_preserved(self):
        text = "2025年报净利率36.44%，2026一季报净利率49.92%，不能直接比较同比。"
        self.assertEqual(facts.guard_periods(text, SOURCE), (text, []))


class CitationTests(unittest.TestCase):
    def setUp(self):
        self.binding = rev.document_binding(state_for(), {"source": "online", "code": "600519"})
        self.hits = [{**self.binding, "page": 122, "text": "信用风险与流动性风险。", "chunk_id": "12"}]

    def test_index_ready_does_not_mean_report_has_references(self):
        audit = rev.citation_audit("公司面临经营风险。", self.hits, self.binding)
        self.assertEqual((audit["refs"], audit["status"]), (0, "limited"))
        self.assertTrue(audit["issues"])

    def test_no_retrieved_evidence_cannot_certify_annual_report_claims(self):
        audit = rev.citation_audit("根据年报原文，公司具备优势。", [])
        self.assertEqual(audit["status"], "limited")

    def test_only_actual_current_hits_count(self):
        audit = rev.citation_audit("见第122页及第123页。", self.hits, self.binding)
        self.assertEqual((audit["refs"], audit["matched"]), (2, 1))
        self.assertTrue(audit["issues"])
        old = [{**self.hits[0], "source_id": "old"}]
        self.assertEqual(rev.citation_audit("见第122页", old, self.binding)["matched"], 0)

    def test_polishing_cannot_drop_references(self):
        self.assertFalse(rev.preserve_citations("见第122页与第42页。", "见第42页。"))
        self.assertFalse(rev.preserve_citations("见第122页。另见第122页。", "见第122页。"))
        self.assertTrue(rev.preserve_citations("见第122页。", "内容更清楚，见第122页。"))

    def test_source_appendix_is_traceable_and_honestly_labelled(self):
        exported = rev.source_appendix(rev.citation_audit("见第122页", self.hits, self.binding))
        self.assertIn("不代表正文结论已逐项验证", exported)
        self.assertIn(self.binding["document_hash"], exported)
        self.assertIn("信用风险与流动性风险", exported)


if __name__ == "__main__":
    unittest.main()

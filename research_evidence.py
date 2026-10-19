"""Current-material bindings, bounded retrieval, and honest citation diagnostics."""
from __future__ import annotations

import hashlib
import re
import uuid
from collections import Counter

INDEX_VERSION = "scoped-evidence-v1"


def digest(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def document_binding(state, context):
    """Manual/upload material never implicitly borrows an online document."""
    if context.get("source") != "online":
        return None
    code = str(context.get("code") or "")
    text = state.get("single_pdftext") or ""
    year = str(state.get("single_pdf_year") or "")
    owner = str(state.get("single_pdf_code") or "")
    if not code or code != owner or code != str(state.get("single_online_code") or "") or not text or not year:
        return None
    text_hash = digest(text)
    saved = state.get("single_pdf_text_hash")
    pdf_hash = state.get("single_pdf_hash") or ""
    tag = str(state.get("single_pdf_tag") or "")
    if saved != text_hash or not pdf_hash or not tag.endswith(pdf_hash[:16]):
        return None
    return {"entity_id": code, "code": code, "year": year,
            "document_hash": pdf_hash, "text_hash": text_hash, "index_version": INDEX_VERSION,
            "doc_tag": tag,
            "source_id": f"{code}:{year}:{pdf_hash[:20]}",
            "title": str(state.get("single_pdf_title") or "年报")}


def clear_document(state, *, keep_unbound_report=False):
    for key in list(state):
        if key == "report_evidence" and keep_unbound_report and not (state.get(key) or {}).get("source_id"):
            continue
        if key.startswith("single_pdf") or key in (
            "single_rag_status", "rag_build_status", "rag_hits_last", "rag_diagnostic", "report_evidence"):
            state.pop(key, None)


def pdf_snapshot(state):
    return {key: state.get(key) for key in (
        "single_pdf_code", "single_pdf_year", "single_pdf_tag", "single_pdf_path",
        "single_pdf_title", "single_pdf_hash", "single_pdf_text_hash", "single_rag_status") if key in state}


def restore_document(state, snapshot, code, extractor):
    """Restore only the exact local PDF; old histories without a binding stay unbound."""
    from pathlib import Path
    clear_document(state)
    saved = snapshot.get("document") or {}
    if not code or saved.get("single_pdf_code") != code or not saved.get("single_pdf_hash"):
        return False
    path = Path(saved.get("single_pdf_path") or "")
    if not path.is_file():
        return False
    try:
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != saved["single_pdf_hash"]:
            return False
        text = extractor(data) or ""
    except (OSError, ValueError):
        return False
    if digest(text) != saved.get("single_pdf_text_hash"):
        return False
    state.update(saved)
    state["single_pdftext"] = text
    return True


def _risk_body(hit):
    text = str(hit.get("text") or "")
    return bool(re.search(r"风险|不确定|流动性|信用|竞争|安全|汇率", text)) and not (
        len(re.findall(r"\.{3,}|…{2,}", text)) >= 3 or ("目录" in text and len(text) < 250))


def retrieve_scoped(rag, binding, query, top_k=8):
    """Return hits and a diagnosis; never search some other index as a fallback."""
    info = {"request_id": uuid.uuid4().hex[:16], "status": "unbound", "query": str(query),
            "source_id": (binding or {}).get("source_id", ""), "queries": [], "pages": [], "hits": 0,
            "document_hash": (binding or {}).get("document_hash", ""),
            "index_version": (binding or {}).get("index_version", "")}
    if not binding:
        info["reason"] = "本次材料未关联可验证版本的年报。在线材料请重新解析当前公司年报；手动/上传材料不沿用旧年报。"
        return [], info
    if rag is None:
        info.update(status="unavailable", reason="年报检索模块不可用。")
        return [], info
    code, year, tag = binding["code"], binding["year"], binding["doc_tag"]
    try:
        if not rag.has_index(code, year, tag):
            info.update(status="no_index", reason="当前文档版本尚未建立索引，请解析这份年报。")
            return [], info
        questions = [query]
        risk = bool(re.search(r"风险|不确定|risk", query, re.I))
        if risk:
            questions += ["可能面对的风险 风险因素 风险提示及应对措施", "信用风险 流动性风险 市场风险 经营风险"]
        hits = []
        for question in questions:
            info["queries"].append(question)
            found = rag.retrieve(question, code, year, top_k=max(top_k, 8) if risk else top_k,
                                 doc_tag=tag, strict_errors=True) or []
            for hit in found:
                # Check backend ownership even when a mock/legacy backend returns a mismatched record.
                if str(hit.get("code", code)) != code or str(hit.get("year", year)) != year:
                    continue
                if str(hit.get("doc_tag", tag) or "") != tag:
                    continue
                hits.append({**hit, **binding, "chunk_id": str(hit.get("chunk_idx", ""))})
        unique = {}
        for hit in hits:
            unique.setdefault((hit.get("page"), hit.get("text")), hit)
        hits = list(unique.values())
        if risk:
            hits.sort(key=lambda h: (_risk_body(h), float(h.get("score") or 0)), reverse=True)
        hits = hits[:top_k]
        info.update(status="ok" if hits else "empty", hits=len(hits),
                    pages=sorted({int(h.get("page") or 0) for h in hits if h.get("page")}))
        info["reason"] = "已检索当前文档版本。" if hits else "已检索当前文档，但未命中支持本问题的正文；开头摘录不代表全文范围。"
        return hits, info
    except Exception as exc:
        info.update(status="error", reason=f"当前文档检索调用失败（{type(exc).__name__}），本轮未取得年报证据。")
        return [], info


def retrieval_prompt(info):
    return (f"\n\n【本次年报检索状态】{info.get('status')}：{info.get('reason')}"
            "\n只把本轮实际命中的片段作为年报依据；不允许借用历史公司的年报。"
            "未命中时说明限制，不得把开头摘录说成全部已解析内容，也不得声称已经验证全文。")


def citation_audit(text, hits, binding=None):
    """Page existence and quote availability are diagnostics, never semantic certification."""
    hits = [h for h in (hits or []) if not binding or h.get("source_id") == binding.get("source_id")]
    pages = {int(h.get("page") or 0) for h in hits if h.get("page")}
    refs = [int(x) for x in re.findall(r"第\s*(\d+)\s*页", text or "")]
    supported = [page for page in refs if page in pages]
    issues = []
    if hits and not refs:
        issues.append("本稿未给出具体页码引用，年报相关论断尚未逐项核验。")
    if set(refs) - pages:
        issues.append("这些引用页不在本轮检索证据中：" + "、".join(map(str, sorted(set(refs) - pages))))
    if refs and not hits:
        issues.append("本稿出现页码，但本轮没有对应年报证据，不能认证引用。")
    if not hits and re.search(r"年报原文|根据年报|据年报|年报显示|年报披露|年报指出", text or ""):
        issues.append("本轮没有可定位的年报检索证据，文中涉及年报原文的表述仍需核对。")
    return {"refs": len(refs), "matched": len(supported), "issues": issues,
            "source_id": (binding or {}).get("source_id", ""),
            "sources": [{k: h.get(k) for k in ("source_id", "document_hash", "title", "page", "text", "chunk_id")} for h in hits],
            "status": "limited" if issues else "has_refs" if refs else "no_refs"}


def preserve_citations(draft, polished):
    original = Counter(re.findall(r"第\s*(\d+)\s*页", draft or ""))
    current = Counter(re.findall(r"第\s*(\d+)\s*页", polished or ""))
    return all(current[page] >= count for page, count in original.items())


def source_appendix(audit):
    """Export the actual retrieval excerpts separately from claims in the report."""
    if not audit.get("sources"):
        return ""
    lines = ["\n\n---\n**本轮检索片段（供核对，不代表正文结论已逐项验证）**"]
    for source in audit["sources"]:
        lines.append(f"\n- 《{source.get('title')}》第 {source.get('page')} 页；材料 ID：{source.get('source_id')}；"
                     f"文档 SHA256：{source.get('document_hash')}；块：{source.get('chunk_id')}")
        excerpt = str(source.get("text") or "")[:1200].replace("\n", "\n> ")
        lines.append("\n> " + excerpt)
    return "\n".join(lines)

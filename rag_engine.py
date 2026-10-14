"""
RAG 引擎 (Plan B)
=================
负责: PDF/年报解析 -> 文本切片 -> Embedding -> ChromaDB 持久化 -> Top-K 检索

设计要点:
1. 文本切片采用"段落优先"策略: 先按换行聚合出段落, 再把超长段落切成 ~500 字 chunk
2. Embedding 使用 DashScope text-embedding-v3 (中文效果优于 OpenAI text-3-small)
3. 持久化目录: ./chroma_store/<code>_<year>_v1/, Streamlit Cloud 重启会被清空,
   因此在 build_index() 入口加入 "磁盘不存在则重建" 的兜底逻辑
4. 检索返回 Top-K=6, 附带 source / page / score, 供 LLM 阶段做引用归因

CLI 自检: python rag_engine.py <股票代码>
"""
from __future__ import annotations

import os
import re
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import dashscope
from dashscope import TextEmbedding
import chromadb
from chromadb.config import Settings
from pypdf import PdfReader

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
CHROMA_ROOT = Path("./chroma_store")
CHROMA_ROOT.mkdir(parents=True, exist_ok=True)

EMBED_MODEL_DEFAULT = "text-embedding-v3"
EMBED_DIM = 1024  # text-embedding-v3 输出维度
CHUNK_SIZE = 500
CHUNK_OVERLAP = 80
TOP_K = 6


def _ensure_dashscope_key(api_key: Optional[str] = None) -> str:
    """从入参 / 环境变量 / st.secrets 三级获取 DashScope key."""
    key = api_key or os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")
    if not key:
        # Streamlit Cloud 场景 - 延迟到调用层捕获
        try:
            import streamlit as st
            key = st.secrets.get("QWEN_API_KEY")
        except Exception:
            key = None
    if not key:
        raise RuntimeError(
            "未找到 DashScope API Key. 请在环境变量 DASHSCOPE_API_KEY "
            "或 Streamlit Secrets QWEN_API_KEY 中配置。"
        )
    dashscope.api_key = key
    return key


def _collection_name(code: str, year: str) -> str:
    safe_code = re.sub(r"[^A-Za-z0-9]", "", code)
    safe_year = re.sub(r"[^0-9]", "", year)
    return f"report_{safe_code}_{safe_year}"


def _chroma_dir(code: str, year: str) -> Path:
    safe_code = re.sub(r"[^A-Za-z0-9]", "", code)
    safe_year = re.sub(r"[^0-9]", "", year)
    return CHROMA_ROOT / f"{safe_code}_{safe_year}_v1"


# ---------------------------------------------------------------------------
# 文本切片
# ---------------------------------------------------------------------------
def _split_paragraphs(text: str) -> List[str]:
    """按段落切, 去空行/单字符噪音段."""
    raw = re.split(r"\n\s*\n", text)
    cleaned = []
    for p in raw:
        p = p.strip()
        if len(p) >= 20:  # 过滤短碎屑
            cleaned.append(p)
    return cleaned


def _chunk_paragraph(para: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """把单个超长段落切成 size 字 + overlap 字的滑动窗口 (按字符)."""
    if len(para) <= size:
        return [para]
    chunks = []
    start = 0
    while start < len(para):
        end = min(start + size, len(para))
        chunk = para[start:end]
        chunks.append(chunk)
        if end >= len(para):
            break
        start = end - overlap
    return chunks


def split_text(text: str) -> List[str]:
    """对外暴露: 段落优先切片, 总 chunk 数可控, 适配 ~500 字/段."""
    paragraphs = _split_paragraphs(text)
    chunks: List[str] = []
    for p in paragraphs:
        chunks.extend(_chunk_paragraph(p))
    return chunks


def split_chunks_paged(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    v2 按页切片: 输入 pdf_to_pages() 的结果,
    返回 [{"text": chunk, "page": 页码}, ...]。
    财务报表/风险章节散落在几百页年报中, 页码是引用溯源的关键。
    """
    chunks: List[Dict[str, Any]] = []
    for pg in pages or []:
        page_no = int(pg.get("page") or 0)
        for para in _split_paragraphs(pg.get("text") or ""):
            for piece in _chunk_paragraph(para):
                chunks.append({"text": piece, "page": page_no})
    return chunks


# ---------------------------------------------------------------------------
# PDF 解析（v2：按页解析，保留页码，供引用归因）
# ---------------------------------------------------------------------------
def pdf_to_text(pdf_path: str | Path) -> str:
    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception as e:
            logger.warning("PDF 第 %s 页解析失败: %s", page, e)
    return "\n\n".join(parts)


def pdf_to_pages(pdf_path: str | Path) -> List[Dict[str, Any]]:
    """逐页解析，返回 [{"page": 页码(1起), "text": 文本}, ...]。"""
    reader = PdfReader(str(pdf_path))
    out: List[Dict[str, Any]] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            out.append({"page": i, "text": page.extract_text() or ""})
        except Exception as e:
            logger.warning("PDF 第 %s 页解析失败: %s", i, e)
            out.append({"page": i, "text": ""})
    return out


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------
def _embed_batch(
    texts: List[str],
    model: str = EMBED_MODEL_DEFAULT,
    progress_cb=None,
) -> List[List[float]]:
    """批量 embedding. 注意: DashScope embedding 接口单次最多 10 条
    (超过会报 InternalError.Algo.InvalidParameter: batch size ... not be larger than 10).
    v2: 6 路并发调用, 返回顺序与输入顺序严格一致.
    v3: 单批 25 秒超时 (future.result(timeout)), 防接口抽风时无限挂起.
    v4: 8 路并发 + as_completed 进度回调 progress_cb(done, total),
        40 万字年报 ~1000 chunks=100 批, 6 路约 50-70 秒 -> 8 路约 35-50 秒;
        配合进度条消除"看似卡死"的观感."""
    if not texts:
        return []
    _ensure_dashscope_key()
    BATCH = 10
    batches = [texts[i:i + BATCH] for i in range(0, len(texts), BATCH)]
    total = len(batches)
    if progress_cb:
        progress_cb(0, total)

    def _call_one(batch: List[str]) -> List[List[float]]:
        # 单批 25 秒超时; 注意必须 shutdown(wait=False), 否则 with 块退出
        # 仍会等挂起的 HTTP 调用跑完, 超时就失效了
        from concurrent.futures import ThreadPoolExecutor
        _single = ThreadPoolExecutor(max_workers=1)
        try:
            resp = _single.submit(
                TextEmbedding.call, model=model, input=batch, text_type="document"
            ).result(timeout=25)
        finally:
            _single.shutdown(wait=False)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Embedding 失败 [{resp.status_code}]: {resp.message}"
            )
        return [item["embedding"] for item in resp.output["embeddings"]]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    # 结果按提交索引归位, 保证返回顺序与输入严格一致; 进度按完成数即时回报
    results: Dict[int, List[List[float]]] = {}
    done_cnt = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futmap = {pool.submit(_call_one, b): i for i, b in enumerate(batches)}
        for fut in as_completed(futmap):
            results[futmap[fut]] = fut.result()
            done_cnt += 1
            if progress_cb:
                progress_cb(done_cnt, total)
    out: List[List[float]] = []
    for i in range(total):
        out.extend(results[i])
    return out


def _embed_query(q: str, model: str = EMBED_MODEL_DEFAULT) -> List[float]:
    """单条查询 embedding, 同样带 15 秒超时保护."""
    from concurrent.futures import ThreadPoolExecutor
    _ensure_dashscope_key()
    _single = ThreadPoolExecutor(max_workers=1)
    try:
        resp = _single.submit(
            TextEmbedding.call, model=model, input=[q], text_type="query"
        ).result(timeout=15)
    finally:
        _single.shutdown(wait=False)
    if resp.status_code != 200:
        raise RuntimeError(
            f"查询 embedding 失败 [{resp.status_code}]: {resp.message}"
        )
    return resp.output["embeddings"][0]["embedding"]


# ---------------------------------------------------------------------------
# ChromaDB
# ---------------------------------------------------------------------------
def _get_chroma_client() -> chromadb.PersistentClient:
    return chromadb.PersistentClient(
        path=str(CHROMA_ROOT),
        settings=Settings(anonymized_telemetry=False, allow_reset=False),
    )


def _build_index(
    code: str,
    year: str,
    texts: List,
    embedding_model: str = EMBED_MODEL_DEFAULT,
    progress_cb=None,
) -> Tuple[chromadb.Collection, int]:
    """构建索引并写入 Chroma, 同时持久化到磁盘.
    texts: List[str] 或 List[{"text", "page"}] (v2 按页切片).
    """
    client = _get_chroma_client()
    cname = _collection_name(code, year)
    # 如果磁盘目录不存在 → 删除老 collection, 重建 (Streamlit Cloud 兜底)
    if not _chroma_dir(code, year).exists():
        try:
            client.delete_collection(cname)
        except Exception:
            pass

    coll = client.get_or_create_collection(
        name=cname,
        metadata={"hnsw:space": "cosine", "code": code, "year": year},
    )

    # 兼容两种输入：纯字符串 或 {text, page} 字典
    norm: List[Dict[str, Any]] = []
    for i, t in enumerate(texts):
        if isinstance(t, dict):
            norm.append({"text": str(t.get("text") or ""), "page": int(t.get("page") or 0)})
        else:
            norm.append({"text": str(t), "page": 0})

    # 已有就跳过
    existing = coll.count()
    if existing >= len(norm):
        logger.info("索引已存在 (%s 条), 跳过构建", existing)
        return coll, existing

    vectors = _embed_batch([n["text"] for n in norm], model=embedding_model, progress_cb=progress_cb)
    ids = [f"{code}_{year}_{i:04d}" for i in range(len(norm))]
    metadatas = [
        {"code": code, "year": year, "chunk_idx": i, "page": n["page"]}
        for i, n in enumerate(norm)
    ]

    coll.upsert(ids=ids, embeddings=vectors, documents=[n["text"] for n in norm], metadatas=metadatas)
    # 触发持久化 (chroma 0.5+ PersistentClient 是惰性的, 这里强制 flush)
    coll.get(limit=1)
    return coll, len(norm)


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
def build_index_from_pdf(
    pdf_path: str | Path,
    code: str,
    year: str,
    embedding_model: str = EMBED_MODEL_DEFAULT,
    progress_cb=None,
) -> Dict[str, Any]:
    """从 PDF 解析 -> 按页切片 -> 索引.

    progress_cb(done, total): embedding 批次进度回调（可选，供 UI 进度条）。

    Returns: {"ok": bool, "chunks": int, "pages": int, "msg": str, "collection": str}
    """
    try:
        pages = pdf_to_pages(pdf_path)
        if not any(p["text"].strip() for p in pages):
            return {"ok": False, "chunks": 0, "pages": 0, "msg": "PDF 解析为空", "collection": ""}
        chunks = split_chunks_paged(pages)
        if not chunks:
            return {"ok": False, "chunks": 0, "pages": len(pages), "msg": "切片结果为空", "collection": ""}
        coll, cnt = _build_index(code, year, chunks, embedding_model, progress_cb=progress_cb)
        # 【扫描件检测】文本 <50 字的页占比 >30% 时提示（pypdf 读不到图片层文字）
        _low = sum(1 for p in pages if len((p.get("text") or "").strip()) < 50)
        _low_ratio = _low / max(len(pages), 1)
        _warn = ""
        if _low_ratio > 0.30:
            _warn = (
                f" ⚠️ 检测到 {int(_low_ratio * 100)}% 的页面几乎没有文本"
                "（疑似扫描件/图片型 PDF），这些页无法被检索，引用覆盖不完整。"
            )
        return {
            "ok": True,
            "chunks": cnt,
            "pages": len(pages),
            "msg": f"已构建/复用索引 ({cnt} chunks, 覆盖 {len(pages)} 页){_warn}",
            "collection": _collection_name(code, year),
        }
    except Exception as e:
        logger.exception("build_index_from_pdf 失败")
        return {"ok": False, "chunks": 0, "pages": 0, "msg": f"{type(e).__name__}: {e}", "collection": ""}


def retrieve(
    query: str,
    code: str,
    year: str,
    top_k: int = TOP_K,
    embedding_model: str = EMBED_MODEL_DEFAULT,
) -> List[Dict[str, Any]]:
    """检索 Top-K 片段, 返回 [{text, score, code, year, chunk_idx, page}, ...]."""
    try:
        client = _get_chroma_client()
        cname = _collection_name(code, year)
        if not _chroma_dir(code, year).exists():
            logger.warning("向量库目录不存在, 请先 build_index")
            return []
        coll = client.get_collection(cname)
        qvec = _embed_query(query, model=embedding_model)
        res = coll.query(
            query_embeddings=[qvec],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        out = []
        for d, m, dist in zip(docs, metas, dists):
            out.append({
                "text": d,
                "score": 1.0 - float(dist),  # cosine 距离 -> 相似度
                "code": m.get("code", code),
                "year": m.get("year", year),
                "chunk_idx": m.get("chunk_idx", -1),
                "page": m.get("page", 0),
            })
        return out
    except Exception as e:
        logger.exception("retrieve 失败")
        return []


def has_index(code: str, year: str) -> bool:
    return _chroma_dir(code, year).exists()


def format_citations(hits: List[Dict[str, Any]]) -> str:
    """把检索结果格式化成 LLM prompt 友好的引用块（带页码，可溯源）."""
    if not hits:
        return "（未检索到相关片段）"
    lines = []
    for i, h in enumerate(hits, 1):
        snippet = h["text"].replace("\n", " ").strip()
        if len(snippet) > 280:
            snippet = snippet[:280] + "..."
        _pg = h.get("page") or 0
        _loc = f"第{_pg}页" if _pg else f"chunk#{h.get('chunk_idx', -1)}"
        lines.append(
            f"[{i}] (年报{_loc}, 相似度={h['score']:.2f}) {snippet}"
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# CLI 自检
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import json

    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    pdf = sys.argv[2] if len(sys.argv) > 2 else None

    print(json.dumps({
        "module": "rag_engine",
        "version": "1.0",
        "EMBED_MODEL": EMBED_MODEL_DEFAULT,
        "CHUNK_SIZE": CHUNK_SIZE,
        "CHUNK_OVERLAP": CHUNK_OVERLAP,
        "TOP_K": TOP_K,
        "CHROMA_ROOT": str(CHROMA_ROOT),
        "code_arg": code,
        "pdf_arg": pdf,
    }, ensure_ascii=False, indent=2))

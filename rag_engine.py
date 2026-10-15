"""
RAG 引擎 (Plan B)
=================
负责: PDF/年报解析 -> 文本切片 -> Embedding -> ChromaDB 持久化 -> Top-K 检索

设计要点:
1. 文本切片采用"段落优先"策略: 先按换行聚合出段落, 再把超长段落切成 ~500 字 chunk
2. Embedding 使用 DashScope text-embedding-v3 (中文效果优于 OpenAI text-3-small)
3. 持久化目录: ./chroma_store/ (Chroma PersistentClient 目录, 内含 chroma.sqlite3
   与一个 UUID 命名的段目录)。Streamlit Cloud 重启会清空该目录, 此时
   get_or_create_collection 会天然返回空集合, count 判断即触发重建。
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

# 统一预算计数（可选依赖）：Embedding 批次要进账本，
# 否则"今天花了多少向量化资源"根本无从回答（对应评审意见 7.2）。
try:
    import budget as _budget
except Exception:  # pragma: no cover - 预算统计绝不拖垮 RAG
    class _budget:  # type: ignore
        BUCKET_EMBED = "embed"
        @staticmethod
        def bump(bucket, n=1): return None
        @staticmethod
        def exceeded(bucket, limit): return False

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

# 切片/清洗逻辑版本号：一旦改动切片策略（分段规则、清洗阈值、chunk 大小），
# 必须递增，否则旧索引会被当成新索引复用（旧切片 + 新策略 = 结果无法解释）。
CHUNK_VERSION = "v3-2026-10"

# 余弦相似度下限：低于该值的片段与问题基本无关。
# 宁可不给证据（页面明确说"未检索到"），也不要把无关段落当成结论依据塞给模型。
MIN_SCORE = 0.15

# 引用片段的最大长度（字符）。原先截到 280 字 —— chunk 本身约 500 字，
# 等于每次都砍掉后半段，落在后半段的关键结论（金额、风险、结论句）直接消失。
CITATION_MAX_CHARS = 1200


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


def _collection_name(code: str, year: str, doc_tag: str = "") -> str:
    """集合名 = report_<代码>_<年份>[_<文档标签>]。

    ⚠️ doc_tag 用来区分「同一年份下的不同文档」：年报、半年报、一季报、三季报、
    修订版报告都可能落在同一个年份里。只按 code+year 命名会让它们共用同一个集合，
    后建的报告直接复用旧索引 → 检索到的是**另一份报告**的内容，页码引用全错。
    """
    safe_code = re.sub(r"[^A-Za-z0-9]", "", code)
    safe_year = re.sub(r"[^0-9]", "", year)
    safe_tag = re.sub(r"[^A-Za-z0-9]", "", doc_tag or "")
    return f"report_{safe_code}_{safe_year}" + (f"_{safe_tag}" if safe_tag else "")


def doc_tag_for(announce_date: str, report_type: str) -> str:
    """把「公告日期 + 报告类型」压成一个安全的集合名后缀，如 20250425_年报。"""
    _d = re.sub(r"[^0-9]", "", announce_date or "")[:8]
    _t = (report_type or "").strip()[:8]
    _map = {"年报": "A", "半年报": "H", "一季报": "Q1", "三季报": "Q3"}
    return f"{_d}_{_map.get(_t, re.sub(r'[^A-Za-z0-9]', '', _t) or 'doc')}"


# ---------------------------------------------------------------------------
# 文本切片
# ---------------------------------------------------------------------------
def _split_paragraphs(text: str) -> List[str]:
    """按段落切, 过滤无意义短碎屑（但不丢含数字的短行）。

    ⚠️ 原实现「长度 <20 全部丢弃」会把年报表格整段吃掉：
    财报里的关键行恰恰是短行 —— 「营业收入 100.5 亿元」「资产负债率 42.3%」
    「归母净利润 -3.2 亿元」。这些行被过滤后，检索再准也检索不到答案。

    v2: 短且含数字的行保留；同时把紧邻在前的短标签行一起带上，
        避免出现"只留下 100亿元，却不知道是哪一项的 100亿元"。
    """
    raw = re.split(r"\n\s*\n", text)
    cleaned: List[str] = []
    pending: Optional[str] = None  # 暂存的"短且无数字"候选标签行（如「营业收入」）
    for p in raw:
        p = p.strip()
        if not p:
            continue
        if len(p) >= 20:
            cleaned.append(p)
            pending = None  # 长段落自己完整，不需要标签补位
        elif len(p) >= 4 and re.search(r"\d", p):
            if pending:  # 先把上一行的标签补回来，再放数值行
                cleaned.append(pending)
                pending = None
            cleaned.append(p)
        elif 2 <= len(p) < 20:
            # 短且无数字：可能是下一行的标签，暂存；若下一段不是数值行则丢弃
            pending = p
        # 其余（<2 字）视为碎屑直接丢
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
    v2: 并发调用, 返回顺序与输入顺序严格一致.
    v3: 单批超时 (future.result(timeout)), 防接口抽风时无限挂起.
    v4: as_completed 进度回调 progress_cb(done, total), 配合 UI 进度条.
    v5: 修正招行 40 万字年报实测问题——8 路并发会触发 DashScope 限流排队,
        25 秒超时太紧导致 TimeoutError 全场失败。改为:
        ① 并发回落到 6 路 (限流安全); ② 单批超时放宽到 60 秒;
        ③ 每批超时/失败自动重试 2 次 (退避 2s/4s); ④ 整体失败时
        cancel 未开始的任务并 shutdown(wait=False) 立即脱身,
        不让 with 块退出时傻等剩余 100 批队列."""
    if not texts:
        return []
    _ensure_dashscope_key()
    BATCH = 10
    WORKERS = 6
    TIMEOUT = 60
    RETRIES = 2  # 额外重试次数 (共 3 次尝试)
    batches = [texts[i:i + BATCH] for i in range(0, len(texts), BATCH)]
    total = len(batches)
    # 预算记账：按批次数计入 embed 桶（每批 ≤10 条，与 DashScope 调用口径一致）
    _budget.bump(_budget.BUCKET_EMBED, total)
    if progress_cb:
        progress_cb(0, total)

    def _call_one(batch: List[str]) -> List[List[float]]:
        import time as _time
        import random as _random
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout
        last_err: Exception = RuntimeError("embedding 未知错误")
        for attempt in range(RETRIES + 1):
            _single = ThreadPoolExecutor(max_workers=1)
            try:
                resp = _single.submit(
                    TextEmbedding.call, model=model, input=batch, text_type="document"
                ).result(timeout=TIMEOUT)
            except _FutTimeout as e:
                last_err = e
                if attempt < RETRIES:
                    # 退避 2s/4s + 随机扰动，避免多路并发同时重试再次撞上限流
                    _time.sleep(2.0 * (attempt + 1) + _random.uniform(0, 0.8))
                continue
            finally:
                _single.shutdown(wait=False)  # 绝不等挂起的 HTTP 调用
            if resp.status_code != 200:
                _sc = int(getattr(resp, "status_code", 0) or 0)
                # 429（限流）与 5xx（服务端临时故障）必须重试 ——
                # 原先非 200 直接抛错，等于"一遇限流整年报告索引全废"，
                # 与注释里写的"失败自动重试"并不一致。
                _retryable = _sc == 429 or 500 <= _sc < 600
                last_err = RuntimeError(f"Embedding 失败 [{resp.status_code}]: {resp.message}")
                if _retryable and attempt < RETRIES:
                    _time.sleep(2.0 * (attempt + 1) + _random.uniform(0, 0.8))
                    continue
                raise last_err
            return [item["embedding"] for item in resp.output["embeddings"]]
        raise RuntimeError(
            f"Embedding 批次在 {RETRIES + 1} 次尝试后仍失败 (单次上限 {TIMEOUT}s): {last_err}"
        )

    from concurrent.futures import ThreadPoolExecutor, as_completed
    # 结果按提交索引归位, 保证返回顺序与输入严格一致; 进度按完成数即时回报
    results: Dict[int, List[List[float]]] = {}
    done_cnt = 0
    futmap: dict = {}
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        futmap = {pool.submit(_call_one, b): i for i, b in enumerate(batches)}
        for fut in as_completed(futmap):
            results[futmap[fut]] = fut.result()
            done_cnt += 1
            if progress_cb:
                progress_cb(done_cnt, total)
    except Exception:
        # 某批最终失败: 取消所有未开始的任务并立即返回,
        # 否则 ThreadPoolExecutor.__exit__ 的 shutdown(wait=True)
        # 会把剩余几十批全部跑完才放行, UI 卡死数分钟
        for f in futmap:
            f.cancel()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    out: List[List[float]] = []
    for i in range(total):
        out.extend(results[i])
    return out


def _embed_query(q: str, model: str = EMBED_MODEL_DEFAULT) -> List[float]:
    """单条查询 embedding, 带 15 秒超时保护 + 限流重试."""
    import time as _time
    import random as _random
    from concurrent.futures import ThreadPoolExecutor
    _ensure_dashscope_key()
    # 预算记账：查询向量化按 1 次计入 embed 桶
    _budget.bump(_budget.BUCKET_EMBED, 1)
    last_err: Optional[Exception] = None
    for _attempt in range(3):
        _single = ThreadPoolExecutor(max_workers=1)
        try:
            resp = _single.submit(
                TextEmbedding.call, model=model, input=[q], text_type="query"
            ).result(timeout=15)
        finally:
            _single.shutdown(wait=False)
        if resp.status_code == 200:
            return resp.output["embeddings"][0]["embedding"]
        _sc = int(getattr(resp, "status_code", 0) or 0)
        last_err = RuntimeError(f"查询 embedding 失败 [{resp.status_code}]: {resp.message}")
        if (_sc == 429 or 500 <= _sc < 600) and _attempt < 2:
            _time.sleep(1.5 * (_attempt + 1) + _random.uniform(0, 0.6))
            continue
        raise last_err
    raise last_err if last_err else RuntimeError("查询 embedding 失败")


# ---------------------------------------------------------------------------
# ChromaDB
# ---------------------------------------------------------------------------
def _get_chroma_client() -> chromadb.PersistentClient:
    return chromadb.PersistentClient(
        path=str(CHROMA_ROOT),
        settings=Settings(anonymized_telemetry=False, allow_reset=False),
    )


def _doc_fingerprint(norm: List[Dict[str, Any]], embedding_model: str) -> str:
    """文档内容指纹：抽样首尾各若干块 + 总字数 + 切片策略版本 + Embedding 模型。

    用于回答一个关键问题：「我要找的这份报告，和索引里那份**是不是同一份**」。
    原先只比 chunk 条数（existing >= len(norm) 就直接复用），
    于是「今年年报 2 块 vs 去年年报 2 块」会被判定为同一份，新内容压根没入库。
    """
    h = hashlib.md5()
    h.update(CHUNK_VERSION.encode("utf-8"))
    h.update((embedding_model or "").encode("utf-8"))
    h.update(str(len(norm)).encode("utf-8"))
    h.update(str(sum(len(n["text"]) for n in norm)).encode("utf-8"))
    for n in (norm[:50] + norm[-20:]):
        h.update(n["text"][:200].encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


def _build_index(
    code: str,
    year: str,
    texts: List,
    embedding_model: str = EMBED_MODEL_DEFAULT,
    progress_cb=None,
    doc_tag: str = "",
) -> Tuple[chromadb.Collection, int]:
    """构建索引并写入 Chroma, 同时持久化到磁盘.
    texts: List[str] 或 List[{"text", "page"}] (v2 按页切片).
    doc_tag: 同一年份内的文档区分标签（见 doc_tag_for），避免年报/半年报互相顶掉。
    """
    client = _get_chroma_client()
    cname = _collection_name(code, year, doc_tag)
    # 注意: 不要再用 "磁盘目录是否存在" 判断索引状态 —— Chroma 的
    # PersistentClient(path=CHROMA_ROOT) 只会在根目录下生成 chroma.sqlite3
    # 和一个 UUID 命名的段目录, 永远不会生成 <code>_<year>_v1/ 这样的目录,
    # 所以以前那段 `if not _chroma_dir(...): delete_collection` 实际是
    # "每次构建前先删库重建"(白白多跑 40-80 秒 Embedding)。
    # 云端重启后 sqlite 本身消失 → get_or_create 天然返回空集合 →
    # 下面 existing >= len(norm) 的判断自然会触发重建, 无需手动删。
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

    fp = _doc_fingerprint(norm, embedding_model)
    base_meta = {
        "hnsw:space": "cosine", "code": code, "year": year, "doc_tag": doc_tag or "",
        "doc_fp": fp, "embed_model": embedding_model, "chunk_ver": CHUNK_VERSION,
        "chunks": len(norm),
    }

    # 已有索引：只有在「块数不减少 **且** 文档指纹一致」时才允许复用。
    # 指纹不一致 = 这是另一份文档（或切片策略/Embedding 模型换了）→ 必须重建。
    existing = coll.count()
    # 指纹优先读集合元数据；读不到时回退读 chunk 元数据（upsert 一定会写，
    # 避免"集合元数据写失败 → 每次打开页面都重建索引、白烧一遍 Embedding"）
    _fp_stored = (coll.metadata or {}).get("doc_fp")
    if not _fp_stored and existing > 0:
        try:
            _peek = coll.get(limit=1, include=["metadatas"])
            _mts = _peek.get("metadatas") or []
            _fp_stored = (_mts[0] or {}).get("doc_fp") if _mts else None
        except Exception:
            _fp_stored = None
    if existing >= len(norm) and _fp_stored == fp:
        logger.info("索引已存在且文档指纹一致 (%s 条), 跳过构建", existing)
        return coll, existing
    if existing > 0:
        logger.info("文档指纹不一致（旧 %s 条, 新 %s 条）, 重建集合 %s",
                    existing, len(norm), cname)
        try:
            client.delete_collection(cname)
        except Exception:
            try:  # 个别版本不支持 delete_collection 时，退化为清空该集合内容
                _old_ids = coll.get().get("ids") or []
                if _old_ids:
                    coll.delete(ids=_old_ids)
            except Exception:
                logger.warning("清理旧集合失败，改为 upsert 覆盖", exc_info=True)
        coll = client.get_or_create_collection(name=cname, metadata=base_meta)

    vectors = _embed_batch([n["text"] for n in norm], model=embedding_model, progress_cb=progress_cb)
    ids = [f"{code}_{year}_{doc_tag or 'd'}_{i:04d}" for i in range(len(norm))]
    metadatas = [
        {"code": code, "year": year, "chunk_idx": i, "page": n["page"],
         "doc_tag": doc_tag or "", "doc_fp": fp}
        for i, n in enumerate(norm)
    ]

    coll.upsert(ids=ids, embeddings=vectors, documents=[n["text"] for n in norm], metadatas=metadatas)
    # 指纹写入集合元数据 —— 下次复用判断靠它（写失败不致命，但会导致下次重建）
    try:
        coll.modify(metadata=base_meta)
    except Exception:
        logger.warning("写入集合元数据失败，下次将重建索引", exc_info=True)
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
    doc_tag: str = "",
) -> Dict[str, Any]:
    """从 PDF 解析 -> 按页切片 -> 索引.

    progress_cb(done, total): embedding 批次进度回调（可选，供 UI 进度条）。
    doc_tag: 同一年份内的文档标签（年报/半年报/修订版…），见 doc_tag_for()。

    Returns: {"ok": bool, "chunks": int, "pages": int, "msg": str, "collection": str}
    """
    try:
        pages = pdf_to_pages(pdf_path)
        if not any(p["text"].strip() for p in pages):
            return {"ok": False, "chunks": 0, "pages": 0, "msg": "PDF 解析为空", "collection": ""}
        chunks = split_chunks_paged(pages)
        if not chunks:
            return {"ok": False, "chunks": 0, "pages": len(pages), "msg": "切片结果为空", "collection": ""}
        coll, cnt = _build_index(code, year, chunks, embedding_model,
                                 progress_cb=progress_cb, doc_tag=doc_tag)
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
            "collection": _collection_name(code, year, doc_tag),
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
    doc_tag: str = "",
    min_score: float = MIN_SCORE,
) -> List[Dict[str, Any]]:
    """检索 Top-K 片段, 返回 [{text, score, code, year, chunk_idx, page}, ...].

    min_score: 余弦相似度下限。低于该值的片段与问题基本无关，直接丢弃 ——
    宁可返回空（调用方会明确说"未检索到相关片段"），也不要把无关段落当依据。
    相似度**只表示排序依据**，不是"结论正确的概率"，页面也照此措辞。
    """
    try:
        client = _get_chroma_client()
        cname = _collection_name(code, year, doc_tag)
        coll = client.get_collection(cname)
        qvec = _embed_query(query, model=embedding_model)
        res = coll.query(
            query_embeddings=[qvec],
            n_results=max(int(top_k or TOP_K), 1),
            include=["documents", "metadatas", "distances"],
        )
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        out, seen = [], set()
        for d, m, dist in zip(docs, metas, dists):
            _score = 1.0 - float(dist)  # cosine 距离 -> 相似度
            if min_score and _score < float(min_score):
                continue
            _key = (m.get("page", 0), (d or "")[:60])
            if _key in seen:      # 相邻块高度重复时去重，别把同一段话说三遍
                continue
            seen.add(_key)
            out.append({
                "text": d,
                "score": _score,
                "code": m.get("code", code),
                "year": m.get("year", year),
                "chunk_idx": m.get("chunk_idx", -1),
                "page": m.get("page", 0),
                "doc_tag": m.get("doc_tag", doc_tag),
            })
        return out
    except Exception as e:
        logger.exception("retrieve 失败")
        return []


def find_index_tag(code: str, year: str) -> Optional[str]:
    """找出该 代码+年份 下**已存在**索引的 doc_tag（找不到返回 None）。

    用途：兼容两种情形 ——
      ① 老会话/老索引是按 "report_<code>_<year>" 建的（没有 doc_tag）；
      ② 调用方拿不准当初是哪份报告的索引。
    只返回真的装了向量的集合，避免"查到的集合是空的"。
    """
    try:
        client = _get_chroma_client()
        prefix = f"report_{re.sub(r'[^A-Za-z0-9]', '', code)}_{re.sub(r'[^0-9]', '', year)}"
        for c in client.list_collections():
            name = getattr(c, "name", None) or str(c)
            if name == prefix or name.startswith(prefix + "_"):
                try:
                    if client.get_collection(name).count() > 0:
                        return name[len(prefix):].lstrip("_")
                except Exception:
                    continue
    except Exception:
        logger.warning("find_index_tag 失败", exc_info=True)
    return None


def has_index(code: str, year: str, doc_tag: str = "") -> bool:
    """索引是否可用 —— 直接问 Chroma 集合里有多少条向量。

    历史 bug: 原先用 `_chroma_dir(code, year).exists()`(即
    ./chroma_store/<code>_<year>_v1/)判断, 但 Chroma 的 PersistentClient
    只生成 chroma.sqlite3 + UUID 段目录, 从不生成该目录 ⇒ 判断恒为 False
    ⇒ retrieve() 每次都在入口直接 return [] ⇒ **RAG 检索从未真正生效**
    (应用照样会显示"向量库已就绪", 因为那只取决于 build 返回的 chunks 数)。
    """
    try:
        client = _get_chroma_client()
        coll = client.get_collection(_collection_name(code, year, doc_tag))
        return coll.count() > 0
    except Exception:
        return False


def format_citations(hits: List[Dict[str, Any]]) -> str:
    """把检索结果格式化成 LLM prompt 友好的引用块（带页码，可溯源）."""
    if not hits:
        return "（未检索到相关片段）"
    lines = []
    for i, h in enumerate(hits, 1):
        snippet = h["text"].replace("\n", " ").strip()
        # 截到 1200 字（chunk 约 500 字，等于不再截断）：
        # 原 280 字会把 chunk 砍掉一半，落在后半段的金额/结论句直接消失，
        # 模型拿到的"证据"其实是被腰斩的。
        if len(snippet) > CITATION_MAX_CHARS:
            snippet = snippet[:CITATION_MAX_CHARS] + "…（片段过长已截断）"
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

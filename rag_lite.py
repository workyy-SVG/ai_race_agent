"""零依赖年报页级索引后端（RAG 降级方案）。

为什么需要它
------------
`rag_engine.py` 顶层 `import chromadb` / `import dashscope`，这两个包一旦缺失：

    app.py: try: import rag_engine as rag / except ImportError: rag = None

`rag` 变成 None → 面板里 `if rag is not None` 永远不成立 → **点「下载并解析」后
索引永远建不起来**，页面上就一直挂着「当前未建立年报向量索引」。用户看到的现象是
"功能摆在那儿但点了没用"，而代码层面连一条报错都没有（被 except 吞了）。

ChromaDB 依赖树极重（含 onnxruntime / grpcio / kubernetes 等上百个包），
在受限环境（公司电脑、离线赛区、Anaconda 目录不可写）里经常装不上。
但「见年报第X页」这个能力**本质上不需要向量库**：

    解析 PDF → 按页切片 → 存页号 → 按问题检索相关页 → 返回页码

纯 Python + pypdf 就能做到。本模块就是这条**无条件可用的兜底链路**，
保证任何环境下"解析了年报就一定能拿到页码引用"。

设计要点
--------
1. **只依赖 stdlib + pypdf**（pypdf 已在 requirements.txt，且是解析 PDF 的刚需）。
2. 索引落盘为**单个 JSON**：`./rag_index/<code>_<year>[_<tag>].json`，
   不依赖任何数据库进程/目录语义，`has_index` 判断 100% 可靠
   （ChromaDB 那一版曾因 "PersistentClient 不生成预期目录" 导致 has_index 恒 False，
   RAG 检索从未真正生效——这里不再重蹈覆辙）。
3. 检索用 **词项覆盖 + IDF 加权**（中文按 2-gram，无需分词器），
   再加轻量关键词重排；没有 Embedding 也能量出相关性并给出页码。
4. 与 rag_engine 的返回结构**完全一致**（text/score/cos/overlap/page/code/year），
   调用方无需区分后端。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pypdf import PdfReader

logger = logging.getLogger(__name__)

# ⚠️ 不要在模块导入期 mkdir：受限环境（只读目录 / 沙箱 / 权限不足）会让
# `import rag_lite` 直接抛 FileNotFoundError，进而把整个 RAG 链路拖垮
# （实测踩到过）。目录首次**使用时**再建，且建失败要能降级到临时目录。
INDEX_ROOT = Path(os.environ.get("RAG_LITE_DIR", "./rag_index"))
_ROOT_READY = False


def _ensure_root() -> Path:
    """确保索引目录**真的可写**（目录存在 ≠ 有写权限，实测踩到过：
    沙箱/只读挂载下 mkdir 成功但 write_text 抛 PermissionError）。
    写探针验证，失败就退到系统临时目录 —— 宁可用临时目录，也不能建不出索引。"""
    global _ROOT_READY, INDEX_ROOT
    if _ROOT_READY:
        return INDEX_ROOT
    for _cand in (INDEX_ROOT, Path(tempfile.gettempdir()) / "rag_lite_index"):
        try:
            _cand.mkdir(parents=True, exist_ok=True)
            _probe = _cand / ".write_probe"
            _probe.write_text("1", encoding="utf-8")
            try:
                _probe.unlink()
            except Exception:
                pass
            INDEX_ROOT = _cand
            break
        except Exception:
            continue
    else:
        logger.warning("rag_index 与临时目录均不可写，页码索引将无法持久化")
    _ROOT_READY = True
    return INDEX_ROOT

CHUNK_SIZE = 500
CHUNK_OVERLAP = 80
TOP_K = 6
# 相关度下限。语义向量那版用 0.15（余弦，0~1 量纲）；
# 这里是 BM25 量纲（0~20+），且真正的过滤交给下面的「词项覆盖门槛」，
# 所以默认不给分数门槛，避免误杀。
MIN_SCORE = 0.0

# 词项覆盖门槛：查询切成 N 个词项，至少命中几个才算相关。
# 没有它时，"现金分红政策"会命中只含"政策"二字的员工培训页（实测踩到过）——
# 中文 2-gram 里"政策""现金"这类通用词到处都是，必须要求覆盖一定比例。
MIN_HIT_RATIO = 0.34

# BM25 参数（标准取值）
BM25_K1 = 1.5
BM25_B = 0.75

# 解析后的索引缓存：年报索引 JSON 可能上 MB，每次检索都 json.loads 太浪费。
# 只缓存指纹级（mtime+size），内容变了自动失效。
_CACHE: Dict[str, Any] = {}
CITATION_MAX_CHARS = 1200

_BACKEND = "lite"


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def doc_tag_for(announce_date: str, report_type: str) -> str:
    """与 rag_engine.doc_tag_for 同签名同结果，保证两个后端的集合名一致。"""
    _d = re.sub(r"[^0-9]", "", announce_date or "")[:8]
    _t = (report_type or "").strip()[:8]
    _map = {"年报": "A", "半年报": "H", "一季报": "Q1", "三季报": "Q3"}
    return f"{_d}_{_map.get(_t, re.sub(r'[^A-Za-z0-9]', '', _t) or 'doc')}"


def _key(code: str, year: str, doc_tag: str = "") -> str:
    raw = f"{code}_{year}_{doc_tag or ''}"
    # 用哈希避免公司名/标签里的非法文件名字符
    return re.sub(r"[^0-9A-Za-z._-]", "_", raw)[:80]


def _path(code: str, year: str, doc_tag: str = "") -> Path:
    return _ensure_root() / f"{_key(code, year, doc_tag)}.json"


def _fingerprint(pages: List[Dict[str, Any]]) -> str:
    h = hashlib.md5()
    for p in pages:
        h.update(str(p.get("page", 0)).encode())
        h.update((p.get("text") or "").encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# PDF 解析 + 按页切片（与 rag_engine.split_chunks_paged 同策略）
# ---------------------------------------------------------------------------
def pdf_to_pages(pdf_path) -> List[Dict[str, Any]]:
    reader = PdfReader(str(pdf_path))
    out: List[Dict[str, Any]] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            out.append({"page": i, "text": page.extract_text() or ""})
        except Exception as e:
            logger.warning("PDF 第 %s 页解析失败: %s", i, e)
            out.append({"page": i, "text": ""})
    return out


def split_chunks_paged(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按页切成带页码的 chunk（页内再按 CHUNK_SIZE 滑窗，保留页码归因）。"""
    chunks: List[Dict[str, Any]] = []
    for p in pages:
        txt = re.sub(r"[ \t]+", " ", (p.get("text") or "")).strip()
        if not txt:
            continue
        page_no = int(p.get("page", 0) or 0)
        if len(txt) <= CHUNK_SIZE:
            chunks.append({"text": txt, "page": page_no})
            continue
        step = max(CHUNK_SIZE - CHUNK_OVERLAP, 1)
        for s in range(0, len(txt), step):
            seg = txt[s:s + CHUNK_SIZE].strip()
            if seg:
                chunks.append({"text": seg, "page": page_no})
            if s + CHUNK_SIZE >= len(txt):
                break
    return chunks


# ---------------------------------------------------------------------------
# 词项化：中文 2-gram + 英文/数字 token（无分词器依赖）
# ---------------------------------------------------------------------------
_CJK = r"\u4e00-\u9fff"


def _terms(text: str) -> set:
    if not text:
        return set()
    t = set()
    for w in re.findall(r"[A-Za-z]{2,}|\d{2,}", text):
        t.add(w.lower())
    zh = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for i in range(len(zh) - 1):
        t.add(zh[i:i + 2])
    # 单字兜底：查询很短（如"商誉"）时 2-gram 已覆盖；单字问题（如"债"）靠它
    if len(zh) == 1:
        t.add(zh)
    return t


def _term_freq(text: str) -> Dict[str, int]:
    if not text:
        return {}
    tf: Dict[str, int] = {}
    for w in re.findall(r"[A-Za-z]{2,}|\d{2,}", text):
        k = w.lower()
        tf[k] = tf.get(k, 0) + 1
    zh = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for i in range(len(zh) - 1):
        k = zh[i:i + 2]
        tf[k] = tf.get(k, 0) + 1
    if len(zh) == 1:
        tf[zh] = tf.get(zh, 0) + 1
    return tf


# ---------------------------------------------------------------------------
# 建索引
# ---------------------------------------------------------------------------
def build_index_from_pdf(
    pdf_path,
    code: str,
    year: str,
    doc_tag: str = "",
    progress_cb=None,
    **_ignored,          # 兼容 rag_engine 的 embedding_model 等命名参数
) -> Dict[str, Any]:
    """按页切片 → 存 JSON。返回结构与 rag_engine.build_index_from_pdf 一致。"""
    try:
        pages = pdf_to_pages(pdf_path)
        if not any((p.get("text") or "").strip() for p in pages):
            return {"ok": False, "chunks": 0, "pages": 0,
                    "msg": "PDF 解析为空（可能是扫描件/图片型 PDF）",
                    "collection": "", "backend": _BACKEND}
        chunks = split_chunks_paged(pages)
        if not chunks:
            return {"ok": False, "chunks": 0, "pages": len(pages),
                    "msg": "切片结果为空", "collection": "", "backend": _BACKEND}

        # 统计 df（文档频率）用于 IDF
        df: Dict[str, int] = {}
        chunk_terms: List[Dict[str, int]] = []
        for c in chunks:
            tf = _term_freq(c["text"])
            chunk_terms.append(tf)
            for k in tf.keys():
                df[k] = df.get(k, 0) + 1
        # 存 df（文档频率）而非 idf：BM25 的 IDF 依赖 N，建索引时算死会让
        # 后续无法调参；且 df 是整数，JSON 体积更小。
        data = {
            "code": code,
            "year": year,
            "doc_tag": doc_tag,
            "backend": _BACKEND,
            "fingerprint": _fingerprint(pages),
            "pages": len(pages),
            "chunks": [
                {"text": c["text"], "page": c["page"], "dl": len(tf)}
                for c, tf in zip(chunks, chunk_terms)
            ],
            # 检索期不想重算全量 TF，但存全量 TF 会让 JSON 膨胀数倍；
            # 改为存 df 表（体积小得多），检索时对候选 chunk 现算 TF。
            "df": df,
            "n_chunks": len(chunks),
            "avgdl": (sum(len(t) for t in chunk_terms) / max(len(chunk_terms), 1)),
        }
        p = _path(code, year, doc_tag)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        _low = sum(1 for pg in pages if len((pg.get("text") or "").strip()) < 50)
        _warn = ""
        if _low / max(len(pages), 1) > 0.30:
            _warn = (
                f" ⚠️ 检测到 {int(_low / max(len(pages), 1) * 100)}% 的页面几乎没有文本"
                "（疑似扫描件/图片型 PDF），这些页无法被检索，引用覆盖不完整。"
            )
        return {
            "ok": True,
            "chunks": len(chunks),
            "pages": len(pages),
            "msg": f"已构建页级索引（{len(chunks)} 块 · 覆盖 {len(pages)} 页）{_warn}",
            "collection": _key(code, year, doc_tag),
            "backend": _BACKEND,
        }
    except Exception as e:
        logger.exception("rag_lite.build_index_from_pdf 失败")
        return {"ok": False, "chunks": 0, "pages": 0,
                "msg": f"{type(e).__name__}: {e}", "collection": "", "backend": _BACKEND}


def has_index(code: str, year: str, doc_tag: str = "") -> bool:
    p = _path(code, year, doc_tag)
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return int(d.get("n_chunks", 0)) > 0
    except Exception:
        return False


def stats(code: str, year: str, doc_tag: str = "") -> Dict[str, Any]:
    p = _path(code, year, doc_tag)
    if not p.exists():
        return {"ok": False, "chunks": 0, "pages": 0}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {"ok": True, "chunks": int(d.get("n_chunks", 0)),
                "pages": int(d.get("pages", 0)), "backend": _BACKEND}
    except Exception:
        return {"ok": False, "chunks": 0, "pages": 0}


def find_index_tag(code: str, year: str) -> Optional[str]:
    """该 code+year 下已存在索引的 doc_tag（兼容"当初没带 tag 建的"老索引）。"""
    root = _ensure_root()
    for p in root.glob(f"{_key(code, year, '')}.json"):
        return ""
    pre = re.sub(r"[^0-9A-Za-z._-]", "_", f"{code}_{year}_")
    for p in sorted(root.glob(f"{pre}*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if int(d.get("n_chunks", 0)) > 0:
                return d.get("doc_tag", "")
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def retrieve(
    query: str,
    code: str,
    year: str,
    top_k: int = TOP_K,
    doc_tag: str = "",
    min_score: float = MIN_SCORE,
    **_ignored,
) -> List[Dict[str, Any]]:
    """词项覆盖 + IDF 加权检索，返回带 page 的 hits（结构与 rag_engine 一致）。"""
    p = _path(code, year, doc_tag)
    if not p.exists():
        return []
    # 缓存：key 用 (路径, mtime, size)，索引重建后自动失效
    try:
        _st = p.stat()
        _ck = f"{p}|{int(_st.st_mtime)}|{_st.st_size}"
    except Exception:
        _ck = str(p)
    d = _CACHE.get(_ck)
    if d is None:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
        if len(_CACHE) > 8:
            _CACHE.clear()
        _CACHE[_ck] = d

    q_terms = _terms(query)
    if not q_terms:
        return []
    chunks = d.get("chunks") or []
    N = max(len(chunks), 1)
    df = d.get("df") or {}
    avgdl = float(d.get("avgdl") or 1.0) or 1.0
    # 至少要命中几个词项。用 round 而非 ceil + max(2,…)：
    # 强抬下限会把"风险因素"这类 3 词项查询（年报里只说"可能面对的风险"）全砍光，
    # 而"现金分红政策"（5 词项）仍要求命中 2 个，足以滤掉只含"政策"的无关页。
    _min_hit = max(1, int(round(len(q_terms) * MIN_HIT_RATIO)))

    out: List[Dict[str, Any]] = []
    seen = set()
    for i, c in enumerate(chunks):
        txt = c.get("text") or ""
        tf = _term_freq(txt)
        dl = int(c.get("dl") or len(tf)) or 1
        bm25 = 0.0
        hit_n = 0
        for t in q_terms:
            f = tf.get(t, 0)
            if not f:
                continue
            hit_n += 1
            # BM25：IDF 用平滑式（避免 df=N 时出现负数），并对长 chunk 做长度惩罚
            _df = int(df.get(t, 0) or 0)
            idf = math.log(1.0 + (N - _df + 0.5) / (_df + 0.5))
            bm25 += idf * (f * (BM25_K1 + 1.0)) / (f + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avgdl))
        if hit_n < _min_hit:
            continue
        if bm25 < float(min_score or 0.0):
            continue
        _k = (c.get("page", 0), txt[:60])
        if _k in seen:
            continue
        seen.add(_k)
        out.append({
            "text": txt,
            "cos": round(hit_n / max(len(q_terms), 1), 4),  # 语义位：这里放词项覆盖率
            "overlap": round(hit_n / max(len(q_terms), 1), 3),
            "score": round(bm25, 4),
            "code": d.get("code", code),
            "year": d.get("year", year),
            "chunk_idx": i,
            "page": int(c.get("page", 0) or 0),
            "doc_tag": d.get("doc_tag", doc_tag),
        })
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:max(int(top_k or TOP_K), 1)]


def format_citations(hits: List[Dict[str, Any]]) -> str:
    if not hits:
        return "（未检索到相关片段）"
    lines = []
    for i, h in enumerate(hits, 1):
        snippet = (h.get("text") or "").replace("\n", " ").strip()
        if len(snippet) > CITATION_MAX_CHARS:
            snippet = snippet[:CITATION_MAX_CHARS] + "…（片段过长已截断）"
        _pg = h.get("page") or 0
        _loc = f"第{_pg}页" if _pg else f"chunk#{h.get('chunk_idx', -1)}"
        _sc = h.get("score")
        _sc_txt = f"，相关度={float(_sc):.2f}" if isinstance(_sc, (int, float)) else ""
        lines.append(f"[{i}] (年报{_loc}{_sc_txt}) {snippet}")
    return "\n\n".join(lines)

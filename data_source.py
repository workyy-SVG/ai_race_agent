# -*- coding: utf-8 -*-
"""
数据源模块 —— 在线自动取数，无需手动找财报。

数据来源（全部为公开免费接口）：
  1. 巨潮资讯网（cninfo.com.cn）—— 官方指定信息披露平台
     · 股票搜索
     · 定期报告（年报/半年报/季报）公告列表
     · 公告 PDF 直链下载
  2. 东方财富（eastmoney.com）—— 行情与财务数据
     · 实时行情（价格/涨跌/市值/PE/PB）
     · 日 K 线（近 N 个交易日）
     · 主要财务指标（含银行/保险专用字段：不良率、净息差、资本充足率等）
     · 多期年报趋势（近 N 年营收/净利润/毛利率/ROE）

设计原则：
  · 纯 requests，无额外重依赖
  · 全部函数均为「失败返回空/None」的容错风格，绝不让页面崩溃
  · 内置内存缓存，避免重复请求
"""

from __future__ import annotations

import datetime
import os
import re
import time
import json
import logging
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

logger = logging.getLogger(__name__)


# ============================================================
# 基础配置
# ============================================================
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_TIMEOUT = 20

# 传输安全标记：本次进程内真正发生过 https → http 降级的 URL（评审 7.3 第 1 条）。
# 降级只是"HTTPS 被网络阻断"时的兜底，正常不应触发；一旦触发就在页面上说明，
# 避免"数据其实走了明文连接却无人知晓"。
_INSECURE_FALLBACKS: List[str] = []


def insecure_fallbacks() -> List[str]:
    """返回本次运行内发生过明文降级的接口 URL（供 UI 提示数据来源完整性）。"""
    return list(_INSECURE_FALLBACKS)


# 【B7】明文降级开关（外部测试报告 B7：取数存在 HTTPS 降级为 HTTP 明文连接）。
# 默认 **禁止** 明文降级：HTTPS 拿不到就直接判失败，绝不留明文传输金融数据。
# 只有在确认网络环境确实拦 HTTPS、且能接受明文风险时，才用环境变量打开：
#     ALLOW_INSECURE_FALLBACK=1
# 打开后行为与旧版一致（先 https 后 http），但一旦真降级，仍会记录到
# _INSECURE_FALLBACKS 并在页面上如实提示 —— 降级永远是"可观测"的。
ALLOW_INSECURE_FALLBACK = str(
    os.environ.get("ALLOW_INSECURE_FALLBACK", "0") or "0"
).strip().lower() in ("1", "true", "yes", "on")

# BUG-05（外部测试报告）：巨潮三个接口原来硬编码 http://，取数成功后 UI 会出现
# 「HTTPS 降级为 HTTP（明文连接）」警告 —— 评委会关注传输安全。实测三个域名
# 均支持 HTTPS（2026-10-17 验证 200），全部升级，警告从此不再出现。
CNINFO_SEARCH = "https://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_ANNOUNCE = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC = "https://static.cninfo.com.cn/"

EM_QUOTE = "https://push2.eastmoney.com/api/qt/stock/get"
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"

_CACHE: Dict[str, Any] = {}
_CACHE_TTL = 600  # 10 分钟


def _cached(key: str, fn):
    now = time.time()
    if key in _CACHE:
        ts, val = _CACHE[key]
        if now - ts < _CACHE_TTL:
            return val
    val = fn()
    _CACHE[key] = (now, val)
    return val


def _headers(referer: str = "https://data.eastmoney.com/") -> Dict[str, str]:
    return {"User-Agent": _UA, "Referer": referer, "Accept": "*/*"}


def _session() -> "requests.Session":
    """
    独立 Session，忽略系统代理环境变量。
    原因：部分用户开着本地代理（VPN），会让部分财经接口连接失败；
    而 Streamlit Cloud 上是直连，不需要代理。统一忽略环境代理最稳。
    """
    s = requests.Session()
    s.trust_env = False
    return s


_SESS = None


def _sess():
    global _SESS
    if _SESS is None:
        _SESS = _session()
    return _SESS


def _get(url: str, params=None, referer="https://data.eastmoney.com/", tries: int = 2):
    """GET，带重试与 http/https 自动降级。失败返回 None。

    ⚠️ 传输安全（评审 7.3 第 1 条）：https → http 降级会削弱传输安全，
       因此这里把它从"静默行为"改成"可观测行为"：
         · 每次都先试 https，只有 https 失败才试 http（降级是最后手段）；
         · 一旦真的发生降级，记录到 _INSECURE_FALLBACKS 并写 warning 日志，
           页面/日志据此可以说明"本次数据来自降级连接"。
       注：巨潮 cninfo 的公开接口本身是 http-only，不属于"降级"。
    """
    if requests is None:
        return None
    # 【B7】默认不生成 http 备选地址（禁止明文降级）；开关打开时才退回旧行为。
    alt = (url.replace("https://", "http://", 1)
           if (ALLOW_INSECURE_FALLBACK and url.startswith("https://")) else None)
    for attempt in range(tries):
        for u in ([url] if not alt else [url, alt]):
            try:
                r = _sess().get(u, params=params, headers=_headers(referer), timeout=_TIMEOUT)
                if r.status_code == 200 and r.text.strip():
                    if alt and u == alt:
                        if url not in _INSECURE_FALLBACKS:
                            _INSECURE_FALLBACKS.append(url)
                        logger.warning("数据接口发生 http 降级（明文连接）: %s", url)
                    return r
            except Exception:
                continue
        time.sleep(0.5)
    return None


def _post(url: str, data=None, referer="https://www.cninfo.com.cn/"):
    if requests is None:
        return None
    for _ in range(2):
        try:
            r = _sess().post(
                url,
                data=data,
                headers={
                    "User-Agent": _UA,
                    "Referer": referer,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=_TIMEOUT,
            )
            if r.status_code == 200 and r.text.strip():
                return r
        except Exception:
            time.sleep(0.5)
            continue
    return None


def _num(v):
    """安全转数值，失败返回 None。"""
    if v is None or v == "" or v == "--":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _yi(v):
    """元 -> 亿元，保留 2 位。"""
    n = _num(v)
    if n is None:
        return None
    return round(n / 1e8, 2)


def _pct(v):
    """东财字段里「本身就是百分数」的比率（ROE/资产负债率/毛利率/增速…）。"""
    n = _num(v)
    if n is None:
        return None
    return round(n, 2)


def _ratio_pct(v):
    """东财字段里「小数比率」的比率 → 百分数（×100）。

    ⚠️ 同一张表里并非所有比率字段口径一致：实测（2026-10，多只个股交叉验证）
      · ROEJQ / ZCFZL / XSMLL / XSJLL / TOTALOPERATEREVETZ … 已是百分数（19.59 / 15.99 / 41.04）
      · JYXJLYYSR / XJLLB / YSZKYYSR … 是小数比率（0.0758 = 7.58% / 1.24 = 124%）
    后者若直接当百分数用，会把一家现金流健康的公司算成「现金流失血」，
    是评分出现严重偏差的根因，必须在此处统一换算。
    """
    n = _num(v)
    if n is None:
        return None
    return round(n * 100, 2)


def _rn(v, nd: int = 2):
    """转数值并按 nd 位四舍五入（返回 float，供计算与展示）。

    ⚠️ 东财原始值是完整浮点（如流动比率 3.85488210986），若直接透传，
    前端会显示一长串脏数（实测海天味业「流动比率 3.85488 倍」出现在
    评分依据里）。所有非金额数值都必须过这里。
    """
    n = _num(v)
    if n is None:
        return None
    return round(n, nd)


def _r(v, nd: int = 2):
    """通用四舍五入显示，去尾零。"""
    n = _num(v)
    if n is None:
        return ""
    s = f"{n:.{nd}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _provision_coverage(provision_ratio, npl_ratio):
    """
    拨备覆盖率 = 贷款拨备率 / 不良贷款率 × 100%（近似）。
    东财原始字段 LOAN_PROVISION_RATIO 是「贷款拨备率」（拨备/总贷款），
    需除以不良率才能得到常用的「拨备覆盖率」。
    """
    pr = _num(provision_ratio)
    npl = _num(npl_ratio)
    if pr is None or npl is None or npl <= 0:
        return None
    return round(pr / npl * 100, 2)


# ============================================================
# 一、股票搜索（巨潮）
# ============================================================
def search_stock(keyword: str) -> List[Dict[str, str]]:
    """
    按名称/代码搜索股票。
    返回 [{"code": "600519", "name": "贵州茅台", "orgId": "gssh0600519", "market": "sse"}, ...]
    """
    if not keyword or not keyword.strip():
        return []

    def _run():
        r = _post(CNINFO_SEARCH, data={"keyWord": keyword.strip(), "maxSecNum": 10, "maxListNum": 10})
        if r is None:
            return []
        try:
            data = r.json()
        except Exception:
            return []
        out = []
        if isinstance(data, dict):
            data = data.get("data") or data.get("keyBoardList") or []
        if isinstance(data, list):
            for it in data:
                if not isinstance(it, dict):
                    continue
                code = str(it.get("code") or "").strip()
                name = str(it.get("zwjc") or it.get("name") or "").strip()
                org = str(it.get("orgId") or "").strip()
                if code and len(code) == 6 and code.isdigit():
                    out.append(
                        {
                            "code": code,
                            "name": name,
                            "orgId": org,
                            "market": _guess_market(code),
                        }
                    )
        return out[:10]

    return _cached(f"search:{keyword.strip()}", _run)


def _guess_market(code: str) -> str:
    """按代码前缀推断市场，用于构造东方财富 secid。"""
    if code.startswith("6"):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    if code.startswith(("4", "8", "9")):
        return "bj"
    return "sh"


def _secucode(code: str) -> str:
    """东方财富 datacenter 使用的 SECUCODE，如 600519.SH。"""
    m = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
    return f"{code}.{m.get(_guess_market(code), 'SH')}"


def _secid(code: str) -> str:
    """行情接口使用的 secid，如 1.600519（1=沪, 0=深）。"""
    return f"{'1' if _guess_market(code) == 'sh' else '0'}.{code}"


# ============================================================
# 一(补)、证券解析：把用户输入变成一个「确定」的目标公司
# ============================================================
# 为什么必须单独做这一步（对应验收清单 P0-01 / 关键验收场景「错误代码」）：
#   巨潮 topSearch 是【模糊 / 前缀匹配】接口，实测（2026-10）：
#     '00000'（5 位，不完整） → 10 条模糊结果，第一条是 000001 平安银行
#     '60051'（5 位，不完整） → 9 条模糊结果，第一条是 600510 黑牡丹
#     'abc'                   → 1 条完全无关的结果（180306 华夏安博仓储REIT）
#     '999999'（不存在的代码）→ 0 条
#   所以「取 hits[0]」会把用户没输入过的公司当成分析对象 —— 这是最严重的一类
#   可信度缺陷（研究对象错误，下游所有结论都失效）。
#   本函数的原则：**代码必须精确命中；名称多义时交给用户确认；绝不猜。**
_CODE_RE = re.compile(r"^\d{6}$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")   # A 股简称必含中文，用于拦掉 'abc' 这类噪声输入


def resolve_stock(keyword: str) -> Dict[str, Any]:
    """把用户输入解析为唯一目标公司。返回结构固定，便于 UI 分支与单测。

    Returns:
        {
          "ok": bool,                 # 是否已确定唯一目标
          "code": str, "name": str, "orgId": str,
          "candidates": [ {code,name,orgId}, ... ],  # 供用户确认的候选
          "ambiguous": bool,          # True = 需要用户从候选中确认
          "error": str,               # ok=False 且非 ambiguous 时的原因
        }
    """
    kw = (keyword or "").strip()
    blank = {"ok": False, "code": "", "name": "", "orgId": "", "candidates": [], "ambiguous": False}

    # BUG-03（外部测试报告）：错误提示把 5000 字符原文塞进 alert 撑破布局。
    # 回显一律截断到 16 字符（UI 侧另有 max_chars=20 的输入上限做双保险）。
    echo = kw if len(kw) <= 16 else kw[:16] + "…"

    if not kw:
        return {**blank, "error": "请输入股票代码（6 位数字）或公司名称。"}

    # ---- 1) 形态校验：全数字就必须是 6 位，否则直接拒绝，不去请求接口 ----
    if kw.isdigit() and not _CODE_RE.match(kw):
        return {**blank, "error": f"「{echo}」不是有效的股票代码：A 股代码应为 6 位数字（如 600519）。"}
    if len(kw) < 2:
        return {**blank, "error": f"「{echo}」太短，请输入完整的 6 位代码或公司名称。"}
    # A 股简称一定含中文字符。纯英数关键词（如 'abc'）走搜索接口会被模糊匹配到
    # 毫不相干的证券（实测 'abc' → 180306 华夏安博仓储REIT），因此直接拦掉。
    # 注意：合法的 6 位代码本身不含中文，必须排除在本条之外。
    if not _CODE_RE.match(kw) and not _CJK_RE.search(kw):
        return {**blank, "error": f"「{echo}」既不是 6 位股票代码，也不像 A 股公司名称，请重新输入。"}

    hits = search_stock(kw)
    if not hits:
        return {**blank, "error": f"未找到与「{echo}」对应的证券，请检查代码或名称是否正确。"}

    # ---- 2) 6 位代码：必须精确命中，否则一律拒绝（不允许模糊降级） ----
    if _CODE_RE.match(kw):
        exact = [h for h in hits if h.get("code") == kw]
        if exact:
            h = exact[0]
            return {"ok": True, "code": h["code"], "name": h.get("name", ""),
                    "orgId": h.get("orgId", ""), "candidates": [], "ambiguous": False, "error": ""}
        return {
            **blank,
            "candidates": hits[:8],
            "error": (f"未找到代码为 {echo} 的证券。搜索接口返回的 {len(hits)} 条都是「相近匹配」"
                      f"（如 {hits[0].get('name','')} {hits[0].get('code','')}），"
                      f"已拒绝自动选择，以免分析错公司。"),
        }

    # ---- 3) 名称：完全同名优先；唯一命中直接采用；多条命中交给用户确认 ----
    same = [h for h in hits if h.get("name") == kw]
    if len(same) == 1:
        h = same[0]
        return {"ok": True, "code": h["code"], "name": h.get("name", ""),
                "orgId": h.get("orgId", ""), "candidates": [], "ambiguous": False, "error": ""}

    pool = same or hits
    if len(pool) == 1:
        h = pool[0]
        return {"ok": True, "code": h["code"], "name": h.get("name", ""),
                "orgId": h.get("orgId", ""), "candidates": [], "ambiguous": False, "error": ""}

    return {
        **blank,
        "candidates": pool[:8],
        "ambiguous": True,
        "error": f"「{echo}」匹配到 {len(pool)} 家公司，需要你确认具体是哪一家。",
    }


# 数值合理性校验：接口口径变更 / 单位换算写错时，宁可给出可疑提示，也不要静默产出
# 一个"看起来正常"的错误分数（对应验收清单里"东财字段口径靠硬编码摸出来"的风险）。
_SANITY_RULES = {
    "gross_margin":  (-100.0, 100.0, "毛利率"),
    "net_margin":    (-200.0, 100.0, "净利率"),
    "roe":           (-200.0, 100.0, "ROE"),
    "debt_ratio":    (0.0, 200.0, "资产负债率"),
    "ocf_to_revenue": (-1000.0, 1000.0, "经营现金流/营收"),
    "npl_ratio":     (0.0, 30.0, "不良贷款率"),
    "provision_coverage": (0.0, 1500.0, "拨备覆盖率"),
    "core_car":      (0.0, 80.0, "核心一级资本充足率"),
    "revenue_yoy":   (-100.0, 2000.0, "营收同比"),
    "net_profit_yoy": (-2000.0, 5000.0, "净利润同比"),
}


def sanity_check_metrics(metrics: Dict[str, Any]) -> List[str]:
    """对标准化后的指标做「数值合理性」兜底校验，返回可疑项说明（可能为空）。"""
    out: List[str] = []
    for k, (lo, hi, label) in _SANITY_RULES.items():
        v = (metrics or {}).get(k)
        if isinstance(v, (int, float)) and not (lo <= float(v) <= hi):
            out.append(f"{label} {float(v):g}% 超出常见区间 [{lo:g}, {hi:g}]，疑似接口口径变更，请人工确认")
    return out


# ============================================================
# 二、巨潮：定期报告列表 + PDF 直链
# ============================================================
_CNINFO_PLATE = {
    "sse": ("sse", "sh", "category_ndbg_szsh"),
    "szse": ("szse", "sz", "category_ndbg_szsh"),
}


def list_reports(code: str, org_id: str = "", report_type: str = "年报", limit: int = 20) -> List[Dict[str, str]]:
    """
    获取定期报告列表。
    report_type: 年报 / 半年报 / 一季报 / 三季报
    返回 [{"title","date","url","type"}]
    """
    cat_map = {
        "年报": "category_ndbg_szsh",
        "半年报": "category_bndbg_szsh",
        "一季报": "category_yjdbg_szsh",
        "三季报": "category_sjdbg_szsh",
    }
    category = cat_map.get(report_type, "category_ndbg_szsh")
    market = _guess_market(code)
    column = "sse" if market == "sh" else "szse"
    plate = "sh" if market == "sh" else "sz"
    stock = f"{code},{org_id}" if org_id else code

    def _run():
        payload = {
            "pageNum": 1,
            "pageSize": max(limit, 30),
            "column": column,
            "tabName": "fulltext",
            "stock": stock,
            "searchkey": "",
            "secid": "",
            "plate": plate,
            "category": category,
            "trade": "",
            "sortName": "",
            "sortType": "",
            "limit": "",
            "showTitle": "",
            "seDate": "",
        }
        r = _post(CNINFO_ANNOUNCE, data=payload)
        if r is None:
            return []
        try:
            anns = (r.json() or {}).get("announcements") or []
        except Exception:
            return []
        out = []
        for a in anns:
            title = a.get("announcementTitle") or ""
            adj = a.get("adjunctUrl") or ""
            ts = a.get("announcementTime")
            date_str = ""
            if ts:
                try:
                    date_str = time.strftime("%Y-%m-%d", time.localtime(ts / 1000))
                except Exception:
                    date_str = str(ts)[:10]
            if adj:
                # 过滤英文版与摘要版（摘要版仅 2 万字左右、无财报明细与风险章节，
                # 不适合 RAG 按页检索引用；完整年报才是演示与建库的正确口径）
                if "英文" in title or "English" in title or "摘要" in title:
                    continue
                out.append(
                    {
                        "title": title,
                        "date": date_str,
                        "url": CNINFO_STATIC + adj,
                        "type": report_type,
                    }
                )
        return out[:limit]

    return _cached(f"reports:{code}:{report_type}:{limit}", _run)


def download_pdf(url: str, max_mb: int = 40) -> Optional[bytes]:
    """下载 PDF 内容（限制大小），失败返回 None。

    ⚠️ 超过大小上限时必须**明确拒绝**（返回 None），不能返回"已读到的片段"：
    被截断的 PDF 交给 pypdf 解析，得到的文本是残缺的，甚至会被当成年报原文
    写进向量库 —— 之后检索到的"年报原文"和页码其实来自一个坏文件。
    """
    if requests is None or not url:
        return None
    try:
        r = _sess().get(url, headers={"User-Agent": _UA}, timeout=60, stream=True)
        if r.status_code != 200:
            return None
        buf = bytearray()
        limit = max_mb * 1024 * 1024
        overflow = False
        for chunk in r.iter_content(65536):
            buf.extend(chunk)
            if len(buf) > limit:
                overflow = True
                break
        try:
            r.close()
        except Exception:
            pass
        if overflow:
            # 明确失败，而不是把半截文件送去解析
            return None
        if not buf.startswith(b"%PDF"):
            return None
        return bytes(buf)
    except Exception:
        return None


# ============================================================
# 三、东方财富：实时行情
# ============================================================
def get_quote(code: str) -> Optional[Dict[str, Any]]:
    """
    实时行情。返回 dict 或 None。
    兜底链：东方财富实时接口 → 东财日 K → 新浪财经 hq 接口（v10 接入）
    → 腾讯财经 qt.gtimg.cn（v11 接入，用户阶段三第 ③ 条）。
    若全部不可用，则返回 None（行情缺失不阻断财务分析）。
    """
    fields = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f107,f116,f117,f162,f167,f168,f169,f170,f171"

    def _run():
        r = _get(
            EM_QUOTE,
            params={"secid": _secid(code), "fields": fields, "invt": 2, "fltt": 2},
            referer="https://quote.eastmoney.com/",
        )
        if r is None:
            q = _quote_from_kline(code)
            return q or sina_quote(code) or tencent_quote(code)
        try:
            d = (r.json() or {}).get("data")
        except Exception:
            d = None
        if not d:
            q = _quote_from_kline(code)
            return q or sina_quote(code) or tencent_quote(code)
        q = {
            "code": d.get("f57"),
            "name": d.get("f58"),
            "price": _num(d.get("f43")),
            "change_pct": _num(d.get("f170")),
            "high": _num(d.get("f44")),
            "low": _num(d.get("f45")),
            "open": _num(d.get("f46")),
            "prev_close": _num(d.get("f60")),
            "volume": _num(d.get("f47")),
            "amount": _num(d.get("f48")),
            "turnover_rate": _num(d.get("f168")),
            "pe_ttm": _num(d.get("f162")),
            "pb": _num(d.get("f167")),
            "total_mktcap": _num(d.get("f116")),
            "float_mktcap": _num(d.get("f117")),
            "source": "实时行情",
        }
        if q.get("price") is None:
            q = _quote_from_kline(code)
            return q or sina_quote(code) or tencent_quote(code)
        return q

    return _cached(f"quote:{code}", _run)


# ============================================================
# 六（c）、腾讯财经兜底源（v11 接入 · 用户阶段三第 ③ 条）
# ============================================================
# qt.gtimg.cn 是腾讯自选股的公开行情接口，多年稳定、无需鉴权；
# 返回 GBK 文本，字段以 ~ 分隔（38=换手率 39=PE-TTM 46=PB 45=总市值）。
# 作为行情兜底链的第四条腿：东财 → 东财K线 → 新浪 → 腾讯。
TENCENT_HQ = "https://qt.gtimg.cn/q={pfx}{code}"


def tencent_quote(code: str) -> Optional[Dict[str, Any]]:
    """腾讯财经实时行情（GBK 解码；失败返回 None，绝不抛异常）。"""
    try:
        pfx = "sh" if str(code).startswith(("6", "9")) else (
            "bj" if str(code).startswith(("4", "8")) else "sz")
        url = TENCENT_HQ.format(pfx=pfx, code=code)
        r = _session().get(url, headers={"Referer": "https://gu.qq.com/",
                                         "User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200 or not r.content:
            return None
        txt = r.content.decode("gbk", errors="ignore")
        # 形如 v_sh600519="1~贵州茅台~600519~1688.00~...";
        seg = txt.split('"')
        if len(seg) < 2 or "~" not in seg[1]:
            return None
        f = seg[1].split("~")
        if len(f) < 47 or not f[3]:
            return None
        return {
            "code": code,
            "name": f[1],
            "price": _num(f[3]),
            "change_pct": _num(f[32]),
            "high": _num(f[33]),
            "low": _num(f[34]),
            "open": _num(f[5]),
            "prev_close": _num(f[4]),
            "volume": _num(f[36]),            # 手
            "amount": None,
            "turnover_rate": _num(f[38]),
            "pe_ttm": _num(f[39]),
            "pb": _num(f[46]),
            "total_mktcap": _num(f[45]),      # 亿元
            "float_mktcap": _num(f[44]),
            "source": "腾讯行情",
        }
    except Exception:
        return None


# ============================================================
# 六（b）、新浪财经兜底源（v10 接入 · 免费公开接口）
# ============================================================
# 为什么接新浪：全站目前只有巨潮 + 东方财富两个源，任何一个被限流/改版，
# 取数就直接失败。新浪财经的 hq 行情接口与 vFD_FinancialGuideLine 财务指标页
# 都是多年稳定的公开接口，作为第三条腿成本极低。
# ⚠️ hq.sinajs.cn 必须带 Referer，否则返回空串（新浪 2022 起的反爬策略）。
SINA_HQ = "https://hq.sinajs.cn/list={pfx}{code}"
SINA_GUIDELINE = ("https://vip.stock.finance.sina.com.cn/corp/go.php/"
                  "vFD_FinancialGuideLine/stockid/{code}/ctrl/{year}/displaytype/4.phtml")
_SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}


def _sina_get(url: str) -> str:
    """新浪页面统一取回（GBK 解码，失败返回空串；不抛异常）。"""
    try:
        r = _session().get(url, headers=_SINA_HEADERS, timeout=10)
        if r.status_code != 200 or not r.content:
            return ""
        return r.content.decode("gbk", errors="ignore")
    except Exception:
        return ""


def sina_quote(code: str) -> Optional[Dict[str, Any]]:
    """新浪实时行情兜底。字段序（A 股）：名称,今开,昨收,现价,最高,最低,…,日期,时间。"""
    pfx = "sh" if str(code).startswith(("6", "9", "5")) else "sz"
    txt = _sina_get(SINA_HQ.format(pfx=pfx, code=code))
    m = re.search(r'"(.*)"', txt)
    if not m or "," not in m.group(1):
        return None
    f = m.group(1).split(",")
    if len(f) < 32 or not f[3]:
        return None
    try:
        price = float(f[3])
        prev = float(f[2]) if f[2] else None
        chg = round((price - prev) / prev * 100, 2) if prev else None
        return {
            "code": code, "name": f[0], "price": price, "change_pct": chg,
            "open": _num(f[1]), "prev_close": prev,
            "high": _num(f[4]), "low": _num(f[5]),
            "source": "实时行情（新浪财经兜底）",
        }
    except Exception:
        return None


# 财务指标页里**直接可用**的行名 → 标准指标映射（只挑评分用得到的子集，
# 整页几十行全塞给模型只会稀释重点）。
_SINA_METRIC_MAP = [
    ("摊薄每股收益(元)", "eps"),
    ("每股净资产_调整前(元)", "bps"),
    ("每股经营性现金流(元)", "ocf_per_share"),
    ("净资产收益率(%)", "roe"),
    ("加权净资产收益率(%)", "roe_weighted"),
    ("销售净利率(%)", "net_margin"),
    ("主营业务利润率(%)", "gross_margin"),
    ("资产负债率(%)", "debt_ratio"),
    ("流动比率", "current_ratio"),
    ("主营业务收入增长率(%)", "revenue_yoy"),
    ("净利润增长率(%)", "net_profit_yoy"),
    ("总资产(元)", "total_assets"),
]


def _sina_parse_guideline(html: str) -> List[Dict[str, Any]]:
    """解析 vFD_FinancialGuideLine 页：报告日期行 + 指标行 → 按报告期的 dict 列表。"""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
    dates: List[str] = []
    out: List[Dict[str, Any]] = []
    for raw in rows:
        cells = [re.sub(r"<[^>]+>", "", c).strip().replace("&nbsp;", "")
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", raw, re.S)]
        cells = [c for c in cells if c != ""]
        if not cells:
            continue
        if cells[0].startswith("报告日期"):
            dates = [d for d in cells[1:] if re.match(r"20\d{2}-\d{2}-\d{2}", d)]
            out = [{"report_date": d, "source": "新浪财经"} for d in dates]
            continue
        if not dates or len(cells) < 2:
            continue
        label = cells[0]
        key = next((k for name, k in _SINA_METRIC_MAP if label.startswith(name)), None)
        if key is None:
            continue
        for i, d in enumerate(dates):
            if i + 1 < len(cells):
                v = _num(cells[i + 1])
                if v is not None:
                    out[i][key] = v
    return [o for o in out if len(o) > 2]


def sina_financial_indicators(code: str, years: int = 2) -> List[Dict[str, Any]]:
    """新浪财务指标兜底（东财 get_financial_indicators 失败时用）。

    按年份抓 vFD_FinancialGuideLine（每页含该年 4 个报告期），返回最新在前。
    ⚠️ 新浪没有「年报/中报」的显式标注，这里按报告日期后缀推断：
       12-31 → 年报；06-30 → 半年报；03-31 → 一季报；09-30 → 三季报。
    """
    this_year = datetime.date.today().year
    rows: List[Dict[str, Any]] = []
    for y in (this_year - 1, this_year - 2, this_year)[:max(1, years)]:
        html = _sina_get(SINA_GUIDELINE.format(code=code, year=y))
        if html:
            rows.extend(_sina_parse_guideline(html))
    for r in rows:
        d = str(r.get("report_date") or "")
        r["report_name"] = f"{d[:4]}年报" if d.endswith("12-31") else d
        if d.endswith("06-30"):
            r["report_type"] = "半年报"
        elif d.endswith("03-31"):
            r["report_type"] = "一季报"
        elif d.endswith("09-30"):
            r["report_type"] = "三季报"
        else:
            r["report_type"] = "年报"
    return rows


def _quote_from_kline(code: str) -> Optional[Dict[str, Any]]:
    """用最近两根日 K 线兜底计算价格与涨跌幅。"""
    kl = get_kline(code, days=5)
    if not kl:
        return None
    last = kl[-1]
    prev = kl[-2] if len(kl) >= 2 else None
    price = last.get("close")
    chg = None
    if prev and prev.get("close"):
        chg = round((price - prev["close"]) / prev["close"] * 100, 2)
    return {
        "code": code,
        "name": "",
        "price": price,
        "change_pct": chg,
        "high": last.get("high"),
        "low": last.get("low"),
        "open": last.get("open"),
        "prev_close": prev.get("close") if prev else None,
        "volume": last.get("volume"),
        "amount": last.get("amount"),
        "turnover_rate": None,
        "pe_ttm": None,
        "pb": None,
        "total_mktcap": None,
        "float_mktcap": None,
        "source": f"日线收盘（{last.get('date')}）",
    }


# ============================================================
# 四、东方财富：K 线
# ============================================================
def get_kline(code: str, days: int = 60) -> List[Dict[str, Any]]:
    """
    日 K 线（前复权）。返回 [{"date","open","close","high","low","volume","amount"}]
    """
    def _run():
        params = {
            "secid": _secid(code),
            "fields1": "f1,f2,f3",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": "101",
            "fqt": "1",
            "end": "20500101",
            "lmt": str(max(days, 10)),
        }
        r = _get(EM_KLINE, params=params, referer="https://quote.eastmoney.com/")
        if r is None:
            return []
        try:
            d = (r.json() or {}).get("data") or {}
        except Exception:
            return []
        out = []
        for line in d.get("klines") or []:
            parts = str(line).split(",")
            if len(parts) < 7:
                continue
            out.append(
                {
                    "date": parts[0],
                    "open": _num(parts[1]),
                    "close": _num(parts[2]),
                    "high": _num(parts[3]),
                    "low": _num(parts[4]),
                    "volume": _num(parts[5]),
                    "amount": _num(parts[6]),
                }
            )
        return out

    return _cached(f"kline:{code}:{days}", _run)


# ============================================================
# 五、东方财富：主要财务指标（按报告期）
# ============================================================
_MAIN_COLS = (
    "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE,REPORT_TYPE,REPORT_DATE_NAME,"
    "EPSJB,BPS,MGJYXJJE,TOTALOPERATEREVE,PARENTNETPROFIT,KCFJCXSYJLR,"
    "TOTALOPERATEREVETZ,PARENTNETPROFITTZ,KCFJCXSYJLRTZ,"
    "ROEJQ,ROEKCJQ,XSMLL,XSJLL,ZCFZL,LD,SD,XJLLB,"
    "YSZKYYSR,JYXJLYYSR,ZZCZZTS,CHZZTS,"
    "NON_PERFORMING_LOAN,NONPERLOAN,NET_INTEREST_MARGIN,NET_INTEREST_SPREAD,"
    "FIRST_ADEQUACY_RATIO,LOAN_PROVISION_RATIO,LIQUIDITY_COVERAGE_RATIO,"
    "TOTALDEPOSITS,GROSSLOANS,"
    "NBV_LIFE,NBV_RATE,SOLVENCY_AR,"
    "TOTAL_ASSETS_PK,TOTAL_EQUITY_PK,TOTAL_SHARE,"
    "STAFF_NUM,RDEXPEND,RDPERSONNEL"
)


def get_financial_indicators(code: str, periods: int = 4, annual_only: bool = False) -> List[Dict[str, Any]]:
    """
    主要财务指标（多期，最新在前）。
    annual_only=True 时只取年报（适合做多年趋势图）。
    """
    filt = f'(SECUCODE="{_secucode(code)}")'
    if annual_only:
        filt += '(REPORT_TYPE="年报")'

    def _run():
        params = {
            "reportName": "RPT_F10_FINANCE_MAINFINADATA",
            "columns": _MAIN_COLS,
            "filter": filt,
            "pageNumber": "1",
            "pageSize": str(max(periods, 1)),
            "sortTypes": "-1",
            "sortColumns": "REPORT_DATE",
            "source": "WEB",
            "client": "WEB",
        }
        r = _get(EM_DATACENTER, params=params)
        if r is None:
            return []
        try:
            res = ((r.json() or {}).get("result") or {}).get("data") or []
        except Exception:
            return []
        out = []
        for row in res:
            out.append(_normalize_fina_row(row))
        return out

    return _cached(f"fina:{code}:{periods}:{annual_only}", _run)


def _normalize_fina_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """把东财原始行标准化为业务字段（金额统一为亿元，比率为 %）。"""

    def g(k):
        return row.get(k)

    d = {
        "name": g("SECURITY_NAME_ABBR") or "",
        "report_name": g("REPORT_DATE_NAME") or "",
        "report_date": str(g("REPORT_DATE") or "")[:10],
        "report_type": g("REPORT_TYPE") or "",
        # 每股指标
        "eps": _rn(g("EPSJB")),
        "bps": _rn(g("BPS")),
        "cps": _rn(g("MGJYXJJE")),
        # 规模（亿元）
        "revenue": _yi(g("TOTALOPERATEREVE")),
        "net_profit": _yi(g("PARENTNETPROFIT")),
        "net_profit_deduct": _yi(g("KCFJCXSYJLR")),
        # 增速（%）
        "revenue_yoy": _pct(g("TOTALOPERATEREVETZ")),
        "net_profit_yoy": _pct(g("PARENTNETPROFITTZ")),
        "net_profit_deduct_yoy": _pct(g("KCFJCXSYJLRTZ")),
        # 盈利能力（%）
        "roe": _pct(g("ROEJQ")),
        "roe_deduct": _pct(g("ROEKCJQ")),
        "gross_margin": _pct(g("XSMLL")),
        "net_margin": _pct(g("XSJLL")),
        # 偿债（%）
        "debt_ratio": _pct(g("ZCFZL")),
        "current_ratio": _rn(g("LD")),
        "quick_ratio": _rn(g("SD")),
        "cash_flow_ratio": _ratio_pct(g("XJLLB")),
        # 经营质量
        "ar_to_revenue": _ratio_pct(g("YSZKYYSR")),
        "ocf_to_revenue": _ratio_pct(g("JYXJLYYSR")),
        "asset_turnover_days": _rn(g("ZZCZZTS"), 1),
        "inventory_turnover_days": _rn(g("CHZZTS"), 1),
        # 银行/保险专用
        "npl_amount": _yi(g("NON_PERFORMING_LOAN")),
        "npl_ratio": _rn(g("NONPERLOAN")),
        "nim": _rn(g("NET_INTEREST_MARGIN")),
        "nis": _rn(g("NET_INTEREST_SPREAD")),
        "core_car": _rn(g("FIRST_ADEQUACY_RATIO")),
        "loan_provision_ratio": _rn(g("LOAN_PROVISION_RATIO")),
        "provision_coverage": _provision_coverage(g("LOAN_PROVISION_RATIO"), g("NONPERLOAN")),
        "liquidity_coverage": _rn(g("LIQUIDITY_COVERAGE_RATIO")),
        "total_deposits": _yi(g("TOTALDEPOSITS")),
        "gross_loans": _yi(g("GROSSLOANS")),
        "nbv": _yi(g("NBV_LIFE")),
        "nbv_rate": _pct(g("NBV_RATE")),
        "solvency_ar": _pct(g("SOLVENCY_AR")),
        # 其他
        "total_assets": _yi(g("TOTAL_ASSETS_PK")),
        "total_equity": _yi(g("TOTAL_EQUITY_PK")),
        "total_share": _yi(g("TOTAL_SHARE")),
        "staff_num": _num(g("STAFF_NUM")),
        "rd_expend": _yi(g("RDEXPEND")),
        "rd_personnel": _num(g("RDPERSONNEL")),
    }
    return d


# ============================================================
# 六、行业自动识别（用于自动匹配知识库）
# ============================================================
_INDUSTRY_KEYWORDS = [
    # 金融类排第一：优先匹配具体银行/保险/证券简称，避免被「中国」之类通用词误命中
    ("金融", ["银行", "证券", "保险", "信托", "基金", "期货", "金融", "租赁", "投资", "资产管理", "信用", "支付", "招商", "兴业", "工商", "建设", "农业", "平安", "中国人寿", "中信", "民生", "交通银行", "光大", "浦发", "华夏", "广发", "宁波", "国泰君安", "海通证券", "中信证券", "华泰证券", "中国平安", "中国人保", "新华保险", "中国太保", "招商证券", "国信证券", "银河证券", "申万宏源", "东方财富", "宁波银行", "成都银行", "杭州银行", "上海银行", "南京银行", "江苏银行", "北京银行", "齐鲁银行", "重庆银行"]),
    ("消费", ["白酒", "食品", "饮料", "酒", "乳业", "调味", "家电", "服装", "纺织", "零售", "商贸", "百货", "免税", "旅游", "酒店", "餐饮", "消费", "茅台", "五粮液", "伊利", "海天", "格力", "美的", "泸州老窖", "洋河", "双汇", "蒙牛", "光明", "古井贡", "山西汾酒", "青岛啤酒", "海大", "茶", "酒馆", "中免", "中国中免", "王府井", "永辉", "百联", "重庆百货", "家家悦", "红旗连锁", "良品铺子", "三只松鼠"]),
    ("新能源", ["电池", "锂", "锂电", "光伏", "风电", "储能", "新能源", "电力", "能源", "汽车", "电网", "电气", "宁德", "比亚迪", "隆基", "阳光电源", "通威", "长江电力", "三峡", "国电", "华能", "国投", "换电", "充电", "风能", "太阳能", "核电", "水电", "燃气", "中国核电"]),
    ("医药", ["药", "医", "生物", "疫苗", "医疗", "器械", "健康", "诊断", "恒瑞", "迈瑞", "药明", "医院", "诊所", "血液", "基因", "检测", "康希诺", "复星", "上海莱士", "智飞", "通化东宝", "丽珠", "云南白药", "同仁堂", "白云山", "片仔癀", "长春高新", "沃森", "国药", "九州通"]),
    ("互联网", ["科技", "软件", "信息", "互联网", "电子", "通信", "半导体", "芯片", "计算机", "网络", "数据", "传媒", "游戏", "海康", "中兴", "立讯", "京东方", "腾讯", "阿里", "百度", "京东", "美团", "字节", "小米", "华为", "紫光", "浪潮", "联想", "用友", "科大讯飞"]),
    ("制造", ["机械", "装备", "工程", "重工", "建机", "钢构", "建材", "钢铁", "化工", "煤", "矿", "神华", "陕煤", "陕西煤业", "煤炭", "中煤", "三一", "徐工", "中联", "柳工", "振华", "中国一重", "二重", "东方电气", "上海电气", "哈电", "金风", "明阳"]),
]


def guess_industry(name: str) -> Optional[str]:
    """按公司名称猜测所属行业（用于匹配行业财务基准库），无法判断返回 None。"""
    if not name:
        return None
    for industry, kws in _INDUSTRY_KEYWORDS:
        for kw in kws:
            if kw in name:
                return industry
    return None


# ============================================================
# 七、组装：把取到的数据转成"看得懂的财报摘要文本"
# ============================================================
def build_summary_text(code: str, name: str = "", periods: int = 4) -> str:
    """
    调用各接口，拼装成一段结构化的中文摘要文本，可直接喂给大模型分析。
    返回 markdown 文本；全部失败则返回空串。
    报告期口径：年报/半年报/季报会显式标注，防止中期数据被误读为全年数据。
    """
    lines: List[str] = []
    quote = get_quote(code)
    fina = get_financial_indicators(code, periods=periods)
    _sina_fallback = False
    if not fina:
        # 【v10 第三数据源】东财财务接口拿不到时，改用新浪财经财务指标页兜底。
        # 兜底来源必须显式标注，不能混在东财数据里假装同一个来源。
        fina = sina_financial_indicators(code)
        _sina_fallback = bool(fina)

    real_name = name or (quote or {}).get("name") or (fina[0].get("name") if fina else "") or code

    lines.append(f"# {real_name}（{code}）财务与市场数据摘要")
    lines.append("")
    lines.append("> 数据来源：巨潮资讯网 / 东方财富公开接口自动获取"
                 + ("（财务指标由新浪财经兜底提供）" if _sina_fallback else ""))
    lines.append("")

    # —— 行情 ——
    if quote:
        lines.append(f"## 一、市场表现（{quote.get('source', '最新')}）")
        lines.append(f"- 最新价：{quote.get('price')} 元")
        if quote.get("change_pct") is not None:
            lines.append(f"- 当日涨跌幅：{quote.get('change_pct')}%")
        if quote.get("total_mktcap"):
            lines.append(f"- 总市值：{_yi(quote.get('total_mktcap'))} 亿元")
        if quote.get("pe_ttm"):
            lines.append(f"- 市盈率(PE-TTM)：{quote.get('pe_ttm')}")
        if quote.get("pb"):
            lines.append(f"- 市净率(PB)：{quote.get('pb')}")
        if quote.get("turnover_rate") is not None:
            lines.append(f"- 换手率：{quote.get('turnover_rate')}%")
        lines.append("")

    # —— 财务指标（多期） ——
    if fina:
        lines.append("## 二、主要财务指标（按报告期）")
        is_bank = any((p.get("npl_ratio") is not None or p.get("nim") is not None) for p in fina)
        # 口径提示：年报 / 半年报 / 季报不能混为一谈
        has_interim = any((p.get("report_type") or "") != "年报" for p in fina[:periods])
        if has_interim:
            lines.append("> ⚠️ 口径说明：下列报告期中可能包含一季报/半年报/三季报等**中期报告**，"
                         "其金额为**年初至今累计值**，不能与年报全年数据直接比较；"
                         "同比增速字段为可比口径。分析多年趋势时请只使用年报数据。")
            lines.append("")

        for p in fina[:periods]:
            rt = p.get("report_type") or ""
            rt_label = f"（{rt}）" if rt and rt != "年报" else "（年报）"
            lines.append(f"### {p.get('report_name') or p.get('report_date')}{rt_label}")
            rows = []
            if p.get("revenue") is not None:
                rows.append(f"营业收入 {p['revenue']} 亿元" + (f"（同比 {p['revenue_yoy']}%）" if p.get("revenue_yoy") is not None else ""))
            if p.get("net_profit") is not None:
                rows.append(f"归母净利润 {p['net_profit']} 亿元" + (f"（同比 {p['net_profit_yoy']}%）" if p.get("net_profit_yoy") is not None else ""))
            if p.get("gross_margin") is not None:
                rows.append(f"毛利率 {p['gross_margin']}%")
            if p.get("net_margin") is not None:
                rows.append(f"净利率 {p['net_margin']}%")
            if p.get("roe") is not None:
                rows.append(f"ROE(加权) {p['roe']}%")
            if p.get("eps") is not None:
                rows.append(f"每股收益 {_r(p['eps'])} 元")
            if p.get("bps") is not None:
                rows.append(f"每股净资产 {_r(p['bps'])} 元")
            if p.get("debt_ratio") is not None:
                rows.append(f"资产负债率 {p['debt_ratio']}%")
            if p.get("current_ratio") is not None:
                rows.append(f"流动比率 {_r(p['current_ratio'])}")
            if p.get("ocf_to_revenue") is not None:
                rows.append(f"经营现金流/营收 {p['ocf_to_revenue']}%")
            if is_bank:
                if p.get("npl_ratio") is not None:
                    rows.append(f"不良贷款率 {p['npl_ratio']}%")
                if p.get("provision_coverage") is not None:
                    rows.append(f"拨备覆盖率 {p['provision_coverage']}%")
                if p.get("nim") is not None:
                    rows.append(f"净息差 {p['nim']}%")
                if p.get("core_car") is not None:
                    rows.append(f"核心一级资本充足率 {p['core_car']}%")
            if rows:
                lines.append("- " + "；".join(rows))
            lines.append("")

    text = "\n".join(lines).strip()
    # 全部为空则返回空串
    if len(text) < 80:
        return ""
    return text


def build_annual_trend(code: str, years: int = 5) -> List[Dict[str, Any]]:
    """近 N 年年报关键指标，用于画趋势图 / 年度时间轴。

    ⚠️ 返回顺序是东财原序（**最新年报在最前**）。绘图与文案必须先过
    `app._trend_chrono()` 升序，否则 X 轴时间倒置、"最新一期"算成最早一期。
    """
    data = get_financial_indicators(code, periods=years, annual_only=True)
    out = []
    for p in data:
        _rd = str(p.get("report_date") or "")
        out.append(
            {
                "period": p.get("report_name") or _rd,
                "year": (_rd[:4] if len(_rd) >= 4 else ""),
                "report_date": _rd,
                "report_type": p.get("report_type") or "",
                "revenue": p.get("revenue"),
                "net_profit": p.get("net_profit"),
                "revenue_yoy": p.get("revenue_yoy"),
                "net_profit_yoy": p.get("net_profit_yoy"),
                "gross_margin": p.get("gross_margin"),
                "net_margin": p.get("net_margin"),
                "roe": p.get("roe"),
                "debt_ratio": p.get("debt_ratio"),
                "ocf_to_revenue": p.get("ocf_to_revenue"),
                "eps": p.get("eps"),
                "bps": p.get("bps"),
            }
        )
    return out

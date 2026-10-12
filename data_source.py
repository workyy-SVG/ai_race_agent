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

import time
import json
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


# ============================================================
# 基础配置
# ============================================================
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_TIMEOUT = 20

CNINFO_SEARCH = "http://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_ANNOUNCE = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC = "http://static.cninfo.com.cn/"

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
    """GET，带重试与 http/https 自动降级。失败返回 None。"""
    if requests is None:
        return None
    alt = url.replace("https://", "http://") if url.startswith("https://") else None
    for attempt in range(tries):
        for u in ([url] if not alt else [url, alt]):
            try:
                r = _sess().get(u, params=params, headers=_headers(referer), timeout=_TIMEOUT)
                if r.status_code == 200 and r.text.strip():
                    return r
            except Exception:
                continue
        time.sleep(0.5)
    return None


def _post(url: str, data=None, referer="http://www.cninfo.com.cn/"):
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
    n = _num(v)
    if n is None:
        return None
    return round(n, 2)


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
                # 过滤英文版
                if "英文" in title or "English" in title:
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
    """下载 PDF 内容（限制大小），失败返回 None。"""
    if requests is None or not url:
        return None
    try:
        r = _sess().get(url, headers={"User-Agent": _UA}, timeout=60, stream=True)
        if r.status_code != 200:
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "pdf" not in ctype and not url.lower().endswith(".pdf"):
            # 有些接口 Content-Type 不准，仍尝试读取
            pass
        buf = bytearray()
        limit = max_mb * 1024 * 1024
        for chunk in r.iter_content(65536):
            buf.extend(chunk)
            if len(buf) > limit:
                break
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
    若行情接口不可用，则用最近一根日 K 线兜底（价格/涨跌幅仍可用）。
    """
    fields = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f107,f116,f117,f162,f167,f168,f169,f170,f171"

    def _run():
        r = _get(
            EM_QUOTE,
            params={"secid": _secid(code), "fields": fields, "invt": 2, "fltt": 2},
            referer="https://quote.eastmoney.com/",
        )
        if r is None:
            return _quote_from_kline(code)
        try:
            d = (r.json() or {}).get("data")
        except Exception:
            d = None
        if not d:
            return _quote_from_kline(code)
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
            return _quote_from_kline(code)
        return q

    return _cached(f"quote:{code}", _run)


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
        "eps": _num(g("EPSJB")),
        "bps": _num(g("BPS")),
        "cps": _num(g("MGJYXJJE")),
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
        "current_ratio": _num(g("LD")),
        "quick_ratio": _num(g("SD")),
        "cash_flow_ratio": _pct(g("XJLLB")),
        # 经营质量
        "ar_to_revenue": _pct(g("YSZKYYSR")),
        "ocf_to_revenue": _pct(g("JYXJLYYSR")),
        "asset_turnover_days": _num(g("ZZCZZTS")),
        "inventory_turnover_days": _num(g("CHZZTS")),
        # 银行/保险专用
        "npl_amount": _num(g("NON_PERFORMING_LOAN")),
        "npl_ratio": _num(g("NONPERLOAN")),
        "nim": _num(g("NET_INTEREST_MARGIN")),
        "nis": _num(g("NET_INTEREST_SPREAD")),
        "core_car": _num(g("FIRST_ADEQUACY_RATIO")),
        "loan_provision_ratio": _num(g("LOAN_PROVISION_RATIO")),
        "provision_coverage": _provision_coverage(g("LOAN_PROVISION_RATIO"), g("NONPERLOAN")),
        "liquidity_coverage": _num(g("LIQUIDITY_COVERAGE_RATIO")),
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
    """按公司名称猜测行业知识库分类，无法判断返回 None。"""
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
    """
    lines: List[str] = []
    quote = get_quote(code)
    fina = get_financial_indicators(code, periods=periods)

    real_name = name or (quote or {}).get("name") or (fina[0].get("name") if fina else "") or code

    lines.append(f"# {real_name}（{code}）财务与市场数据摘要")
    lines.append("")
    lines.append("> 数据来源：巨潮资讯网 / 东方财富公开接口自动获取")
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

        for p in fina[:periods]:
            lines.append(f"### {p.get('report_name') or p.get('report_date')}")
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
    """近 N 年年报关键指标，用于画趋势图。"""
    data = get_financial_indicators(code, periods=years, annual_only=True)
    out = []
    for p in data:
        out.append(
            {
                "period": p.get("report_name") or p.get("report_date"),
                "revenue": p.get("revenue"),
                "net_profit": p.get("net_profit"),
                "gross_margin": p.get("gross_margin"),
                "roe": p.get("roe"),
            }
        )
    return out

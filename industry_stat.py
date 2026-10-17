# -*- coding: utf-8 -*-
"""行业基准可解释化（优化清单 O11）—— 把「规则优秀线」与「同业样本统计」分开。

# 为什么要分
---------------------------------------------------------------------------
「规则优秀线」是本项目为各行业设定的**评判标准**（如消费 ROE 18% 是"优秀线"），
它决定评分与四象限定位；「同业样本统计」是东方财富 F10 公布的该细分行业的
**实际水平**（中位数 / 平均数）。两者含义完全不同：

    规则线 18%  ≠  行业平均 18%

把它们混为一谈，就会造出 B06 那种错误——润色时把"ROE 落后 18% 优秀线"
写成"低于行业平均水平"。本模块的作用，就是让页面上同时存在这两条线，
且各自标出来源，互不冒充。

# 数据来自哪里
---------------------------------------------------------------------------
东方财富 F10「行业对比」接口，返回的是**东财自己算好的**行业中值 / 行业平均，
不是本项目对样本二次统计的结果 —— 因此可以在页面上直接标注来源为东方财富，
可追溯到官方口径。取不到就是取不到，绝不自己拿两三家凑一个"行业平均"。

# 三条红线（与 fincalc / analysis_context 一致）
---------------------------------------------------------------------------
1. 样本数 < 3 家 → **不展示统计**（两家的"中位数"没有意义）；
2. 中值与均值**必须分开写**，不得只说"行业平均"含糊带过；
3. 与统计值对比时用**百分点（pp）**，不说"高出 X%"。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

try:
    import requests
except Exception:  # pragma: no cover - 缺 requests 时优雅降级
    requests = None

# ---------------------------------------------------------------------------
# 接口配置
# ---------------------------------------------------------------------------
_ENDPOINT = "https://emweb.securities.eastmoney.com/PC_HSF10/IndustryAnalysis/PageAjax"
_REFERER = "https://emweb.securities.eastmoney.com/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 行业对比里「统计行」的名字（这些行不是公司，是东财算好的统计量）
_STAT_ROW_AVG = "行业平均"
_STAT_ROW_MED = "行业中值"

# 样本数下限：低于这个数不展示统计
MIN_PEERS = 3

_CACHE: Dict[str, Any] = {}
_TTL = 1800.0  # 30 分钟：财报数据不会分钟级变化，别反复打接口


def _sess():
    """复用 data_source 的 session（忽略系统代理，云端直连最稳）。"""
    try:
        import data_source as ds

        return ds._sess()
    except Exception:
        if requests is None:
            return None
        s = requests.Session()
        s.trust_env = False  # 忽略本地 VPN 代理，与 data_source 保持一致
        return s


def _f(v: Any) -> Optional[float]:
    """转 float，失败返回 None（东财大量返回 None / "-" / ""）。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            f = float(v)
        except Exception:
            return None
        # 浮点误差不消除，留待展示层格式化
        return f if f == f else None  # 排除 NaN
    try:
        s = str(v).strip().replace(",", "")
        if not s or s in ("-", "--", "null", "None"):
            return None
        return float(s)
    except Exception:
        return None


def _pick_row(rows: List[Dict[str, Any]], key: str, name: str) -> Optional[Dict[str, Any]]:
    """在分组里按 CORRE_SECURITY_NAME 精确取行。"""
    for r in rows or []:
        if (r.get(key) or "").strip() == name:
            return r
    return None


def _fmt_num(v: Optional[float], nd: int = 2) -> Optional[float]:
    if v is None:
        return None
    return round(v, nd)


def _period_label(date_str: Optional[str]) -> str:
    """'2025-12-31 00:00:00' → '2025 年报'。"""
    if not date_str:
        return ""
    s = str(date_str)[:10]
    parts = s.split("-")
    if len(parts) < 3:
        return s
    y, m = parts[0], parts[1]
    if m == "12":
        return f"{y} 年报"
    if m == "06":
        return f"{y} 中报"
    if m == "03":
        return f"{y} 一季报"
    if m == "09":
        return f"{y} 三季报"
    return f"{y}-{m}"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def industry_stats(code: str, name: str = "", timeout: int = 12) -> Optional[Dict[str, Any]]:
    """取「以该股为中心」的同业财务统计。

    返回结构（失败 / 样本不足 / 无代码 → None）：
        {
          "ok": True,
          "source": "东方财富 F10 · 行业对比",
          "center": "贵州茅台", "center_code": "600519",
          "report_date": "2025-12-31", "period": "2025 年报",
          "n": 6,
          "roe": {"median": 13.24, "avg": 15.27, "self": 36.36},
          "rev": {"median": -20.13, "avg": -19.47, "self": -1.21},
          "peers": [{"name": "山西汾酒", "roe": 38.17, "rev": 7.52}, ...],
        }

    注意：这里的"行业"是**东方财富的细分行业**（白酒 / 银行 / 电池…），
    与本项目 6 个大行业桶（消费 / 金融 / 医药…）不是同一层口径 ——
    展示时必须说明"以某股为中心"，不能拿它代表整个大桶。
    """
    code = (code or "").strip()
    if not code or requests is None:
        return None

    ck = f"istat:{code}"
    hit = _CACHE.get(ck)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]

    s = _sess()
    if s is None:
        return None
    try:
        r = s.get(
            _ENDPOINT,
            params={"code": _secu_code(code)},
            headers={"User-Agent": _UA, "Referer": _REFERER, "Accept": "*/*"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        j = r.json()
    except Exception:
        return None
    if not isinstance(j, dict):
        return None

    roe_rows = j.get("dbfxbj") or []   # 盈利比较：ROE_AVG / ROEPJ_Ln
    rev_rows = j.get("czxbj") or []    # 成长比较：YYSRTB / JLRTB

    if not roe_rows and not rev_rows:
        return None

    # --- 统计行（东财算好的中值 / 均值） ---
    roe_avg_row = _pick_row(roe_rows, "CORRE_SECURITY_NAME", _STAT_ROW_AVG)
    roe_med_row = _pick_row(roe_rows, "CORRE_SECURITY_NAME", _STAT_ROW_MED)
    rev_avg_row = _pick_row(rev_rows, "CORRE_SECURITY_NAME", _STAT_ROW_AVG)
    rev_med_row = _pick_row(rev_rows, "CORRE_SECURITY_NAME", _STAT_ROW_MED)

    # --- 自己的值：czxbj / dbfxbj 里本股那一行 ---
    _sroe = _self_row(roe_rows, code)
    _srev = _self_row(rev_rows, code)
    self_roe = _f(_sroe.get("ROE_AVG")) if _sroe else None
    self_rev = _f(_srev.get("YYSRTB")) if _srev else None

    # --- 同业公司清单（剔除统计行与自己） ---
    peers: List[Dict[str, Any]] = []
    for rows, rk, vk in ((roe_rows, "roe", "ROE_AVG"), (rev_rows, "rev", "YYSRTB")):
        for row in rows:
            pcode = (row.get("CORRE_SECURITY_CODE") or "").strip()
            pname = (row.get("CORRE_SECURITY_NAME") or "").strip()
            if not pcode or pname in (_STAT_ROW_AVG, _STAT_ROW_MED):
                continue
            if pcode == code:
                continue
            item = None
            for p in peers:
                if p["code"] == pcode:
                    item = p
                    break
            if item is None:
                item = {"code": pcode, "name": pname, "roe": None, "rev": None}
                peers.append(item)
            if item.get(rk) is None:
                item[rk] = _f(row.get(vk))

    # 样本数 = 同业公司数 + 自己
    n = len(peers) + 1

    out: Dict[str, Any] = {
        "ok": True,
        "source": "东方财富 F10 · 行业对比",
        "center": name or "",
        "center_code": code,
        "report_date": str(roe_med_row.get("REPORT_DATE") or "")[:10] if roe_med_row else "",
        "n": n,
        "roe": {
            "median": _fmt_num(_f(roe_med_row.get("ROE_AVG"))) if roe_med_row else None,
            "avg": _fmt_num(_f(roe_avg_row.get("ROE_AVG"))) if roe_avg_row else None,
            "self": _fmt_num(self_roe),
        },
        "rev": {
            "median": _fmt_num(_f(rev_med_row.get("YYSRTB"))) if rev_med_row else None,
            "avg": _fmt_num(_f(rev_avg_row.get("YYSRTB"))) if rev_avg_row else None,
            "self": _fmt_num(self_rev),
        },
        "peers": peers[:12],
    }
    out["period"] = _period_label(out.get("report_date"))

    # 样本不足：统计没有意义，直接判为不可用（页面只显示规则线）
    if n < MIN_PEERS:
        out["ok"] = False
        out["reason"] = f"同业样本仅 {n} 家，不足 {MIN_PEERS} 家，不展示统计"

    _CACHE[ck] = (time.time(), out)
    return out


def _self_row(rows: List[Dict[str, Any]], code: str) -> Optional[Dict[str, Any]]:
    """取本股那一行（东财把本股也放在 CORRE_* 里）。"""
    for r in rows or []:
        if (r.get("CORRE_SECURITY_CODE") or "").strip() == code:
            return r
    return None


def _secu_code(code: str) -> str:
    """600519 → SH600519（F10 接口要带市场前缀）。"""
    c = (code or "").strip()
    if c.startswith(("SH", "SZ", "BJ")):
        return c
    if c.startswith(("6", "9")):
        return "SH" + c
    if c.startswith(("0", "2", "3")):
        return "SZ" + c
    if c.startswith(("4", "8")):
        return "BJ" + c
    return c


def usable(st: Optional[Dict[str, Any]]) -> bool:
    """统计块是否可展示：拿到数据 + 样本够 + ROE 或营收增速至少一项有值。"""
    if not st or not st.get("ok"):
        return False
    roe, rev = st.get("roe") or {}, st.get("rev") or {}
    return (roe.get("median") is not None or roe.get("avg") is not None
            or rev.get("median") is not None or rev.get("avg") is not None)


# ---------------------------------------------------------------------------
# 对比描述：方向由程序按数值判定，比率差一律用百分点（pp）
# ---------------------------------------------------------------------------
def _cmp_word(d: Optional[float]) -> str:
    if d is None:
        return ""
    if abs(d) < 0.005:
        return "基本持平"
    return "高于" if d > 0 else "低于"


def compare_pp(value: Optional[float], base: Optional[float]) -> Optional[float]:
    """两个百分比量的差，单位百分点。任一缺失返回 None。"""
    if value is None or base is None:
        return None
    return round(value - base, 2)


def position_text(value: Optional[float], st: Optional[Dict[str, Any]], kind: str) -> str:
    """生成「相对同业」的描述文案。

    kind: "roe" | "rev"
    例："高于同业中值 22.76 个百分点、高于同业均值 21.09 个百分点"
    """
    if value is None or not usable(st):
        return ""
    blk = st.get(kind) or {}
    segs: List[str] = []
    for base_key, label in (("median", "同业中值"), ("avg", "同业均值")):
        d = compare_pp(value, blk.get(base_key))
        if d is None:
            continue
        w = _cmp_word(d)
        if w == "基本持平":
            segs.append(f"与{label}基本持平")
        else:
            segs.append(f"{w}{label} {abs(d):g} 个百分点")
    return "、".join(segs)


def stats_text(st: Optional[Dict[str, Any]], center: str = "") -> str:
    """给大模型用的文本块。

    只在 usable() 为真时返回，否则返回空串 —— 拿不到就不给模型任何"行业平均"。
    """
    if not usable(st):
        return ""
    c = center or st.get("center") or st.get("center_code") or "本公司"
    period = st.get("period") or st.get("report_date") or "未标注报告期"
    lines = [
        f"【同业样本统计 · 来源：{st.get('source')} · 以 {c} 为中心的同行业公司 · "
        f"{period} · 样本 {st.get('n')} 家】",
    ]
    roe, rev = st.get("roe") or {}, st.get("rev") or {}
    segs = []
    if roe.get("median") is not None:
        segs.append(f"ROE 中值 {roe['median']:g}%")
    if roe.get("avg") is not None:
        segs.append(f"ROE 均值 {roe['avg']:g}%")
    if rev.get("median") is not None:
        segs.append(f"营收同比中值 {rev['median']:g}%")
    if rev.get("avg") is not None:
        segs.append(f"营收同比均值 {rev['avg']:g}%")
    if segs:
        lines.append("· " + "；".join(segs))
    lines.append(
        "· 引用口径：这是**同业实际水平**（东财官方统计），与「规则优秀线」不是一回事；"
        "引用时必须说明是「中值」还是「均值」，不得笼统写作「行业平均」。"
    )
    lines.append("· 比率与中值/均值的比较用**百分点（pp）**，不说「高出百分之多少」。")
    if st.get("peers"):
        names = "、".join([p.get("name", "") for p in st["peers"][:6] if p.get("name")])
        if names:
            lines.append(f"· 同行业可比公司（部分）：{names}")
    return "\n".join(lines)


def allowed_values(st: Optional[Dict[str, Any]]) -> List[str]:
    """允许在润色稿里出现的统计值字面量（交给 fincalc.polish_guard 放行）。

    作用：润色稿引用「行业中值 13.24%」时，polish_guard 不再把它判成
    "润色新增的统计口径"而退回初稿 —— 因为这些值是本次真实注入的。
    """
    if not usable(st):
        return []
    vals: List[str] = []
    for kind in ("roe", "rev"):
        blk = st.get(kind) or {}
        for k in ("median", "avg"):
            v = blk.get(k)
            if v is None:
                continue
            vals.append(f"{v:g}")
            vals.append(f"{v:.2f}")
    return vals

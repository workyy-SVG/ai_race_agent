"""
fincalc —— 确定性财务计算、语言方向校验与润色守卫
==================================================

对应优化方案 **O05（财务计算与语言方向校验）** 与 **O10（约束抽取、润色与成稿）**，
修复实测问题 **B07 / B08 / B12 / B06 / B10**。

设计原则（与 FinRobot 的「程序计算 vs 模型叙述」分工一致）：

1. **公式、比率、涨跌方向由程序算**，大模型只负责围绕计算结果解释；
2. **缺输入就返回空值 + 原因**，绝不臆造、绝不拿别的口径顶替；
3. **润色阶段锁定数值 / 口径 / 方向**，差异超阈值则退回初稿，绝不允许润色引入新事实。

本模块**零 Streamlit 依赖**，可被 app / pipeline / 离线评测脚本共用，
保证「评测与页面走同一条链路」。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple, List

__all__ = [
    "to_f", "pp_delta", "direction_word", "describe_change",
    "pb_from", "pe_from", "parse_valuation_inputs", "valuation_block",
    "scan_direction_errors", "fix_direction",
    "strip_polish_preface", "fix_markdown", "polish_guard",
    "NONE_LABEL",
]

# 缺失值的统一展示口径：页面与研报一律显示「数据不足」，绝不把 None 拼进句子
NONE_LABEL = "数据不足"

_NUM = r"[+-]?\d+(?:\.\d+)?"


# ---------------------------------------------------------------------------
# 0. 基础：安全转数值
# ---------------------------------------------------------------------------
def to_f(x) -> Optional[float]:
    """把任意输入安全转成 float；失败返回 None（绝不抛异常、绝不返回 0 顶替）。"""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        s = str(x).replace(",", "").replace("%", "").replace("　", "").strip()
        if not s:
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _fmt(v: float, nd: int = 2) -> str:
    return f"{v:.{nd}f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# 1. 变化方向与百分点（B07）
# ---------------------------------------------------------------------------
def pp_delta(old, new) -> Optional[float]:
    """比率类指标的变化量，单位 = 百分点（pp）。

    50.53% → 50.75% 得到 +0.22，而不是 -0.22、也不是 0.44%（那是变化率）。
    任何一端缺失 → 返回 None（调用方必须说明"无法比较"）。
    """
    a, b = to_f(old), to_f(new)
    if a is None or b is None:
        return None
    return round(b - a, 4)


def direction_word(old, new, tol: float = 1e-9) -> str:
    """返回 上升 / 下降 / 基本持平（缺失时返回空串，由调用方改成"无法比较"）。"""
    a, b = to_f(old), to_f(new)
    if a is None or b is None:
        return ""
    if abs(b - a) <= tol:
        return "基本持平"
    return "上升" if b > a else "下降"


def describe_change(label: str, old, new, kind: str = "rate") -> str:
    """生成一句**程序算出来**的变化描述。

    kind = "rate" → 比率类，变化量用「百分点」：
        describe_change("净利率", 50.53, 50.75) →
        "净利率由 50.53% 上升至 50.75%（+0.22 个百分点）"
    kind = "amount" → 金额类，变化量用同比百分比。
    任一端缺失 → 返回"……缺少比较基准，无法计算变化方向"（绝不猜）。
    """
    a, b = to_f(old), to_f(new)
    if a is None or b is None:
        return f"{label}缺少{'基期' if a is None else '本期'}数据，无法计算变化方向"
    d = b - a
    if abs(d) <= 1e-9:
        return f"{label}由 {_fmt(a)} 到 {_fmt(b)}（基本持平）"
    if kind == "rate":
        return (f"{label}由 {_fmt(a)}% 上升至 {_fmt(b)}%（+{_fmt(d)} 个百分点）"
                if d > 0 else
                f"{label}由 {_fmt(a)}% 下降至 {_fmt(b)}%（{_fmt(d)} 个百分点）")
    if a == 0:
        return f"{label}由 0 变为 {_fmt(b)}（基期为 0，同比变化率不适用）"
    pct = d / abs(a) * 100
    return (f"{label}由 {_fmt(a)} 增至 {_fmt(b)}（同比 +{_fmt(pct, 1)}%）"
            if d > 0 else
            f"{label}由 {_fmt(a)} 降至 {_fmt(b)}（同比 {_fmt(pct, 1)}%）")


# ---------------------------------------------------------------------------
# 2. 估值比率：PB / PE 的依赖关系必须分开（B08）
# ---------------------------------------------------------------------------
def pb_from(price, bps) -> Tuple[Optional[float], str]:
    """PB = 每股股价 / 每股净资产。

    只要股价与每股净资产齐备即可计算 —— **与有没有 EPS 预测无关**。
    旧实现的错误：把"无法评价估值高低"和"无法计算 PB"混为一谈，
    明明已有 1329.0 元股价与 200.99 元每股净资产，却说"缺少一致预期无法计算动态 PE、PB"。

    返回 (值, 说明)；不可算时 (None, 原因)。
    """
    p, b = to_f(price), to_f(bps)
    if p is None or b is None:
        miss = []
        if p is None:
            miss.append("股价")
        if b is None:
            miss.append("每股净资产")
        return None, f"缺少{'与'.join(miss)}，无法计算 PB"
    if b <= 0:
        return None, "每股净资产 ≤ 0（净资产为负或数据口径异常），PB 不适用"
    return round(p / b, 4), f"PB = 股价 {_fmt(p)} / 每股净资产 {_fmt(b)} = {_fmt(p / b)}"


def pe_from(price, eps) -> Tuple[Optional[float], str]:
    """PE = 股价 / 每股收益。缺 EPS → 明确说"缺 EPS"，不影响 PB 的判断。"""
    p, e = to_f(price), to_f(eps)
    if p is None or e is None:
        miss = []
        if p is None:
            miss.append("股价")
        if e is None:
            miss.append("每股收益 EPS")
        return None, f"缺少{'与'.join(miss)}，无法计算 PE"
    if e <= 0:
        return None, "EPS ≤ 0（亏损），PE 不适用"
    return round(p / e, 4), f"PE = 股价 {_fmt(p)} / EPS {_fmt(e)} = {_fmt(p / e)}"


_PRICE_RE = re.compile(r"最新价[：: ]*\s*(" + _NUM + r")\s*元")
_PB_RE = re.compile(r"市净率\(PB\)[：:]\s*(" + _NUM + r")")
_PE_RE = re.compile(r"市盈率\(PE-TTM\)[：:]\s*(" + _NUM + r")")
_BPS_RE = re.compile(r"每股净资产\s*(" + r"[+-]?[\d,]+(?:\.\d+)?" + r")\s*元")
_EPS_RE = re.compile(r"每股收益\s*(" + r"[+-]?[\d,]+(?:\.\d+)?" + r")\s*元")


def parse_valuation_inputs(text: str) -> dict:
    """从在线取数摘要文本里**确定性**抽出估值所需的输入值。

    返回 dict(price, bps, eps, pe_ttm, pb_quote)，抽不到的字段一律为 None，
    由调用方决定如何说明"缺什么"（不做任何补零或回退）。
    """
    t = text or ""
    out = {"price": None, "bps": None, "eps": None, "pe_ttm": None, "pb_quote": None}
    m = _PRICE_RE.search(t)
    if m:
        out["price"] = to_f(m.group(1))
    m = _PB_RE.search(t)
    if m:
        out["pb_quote"] = to_f(m.group(1))
    m = _PE_RE.search(t)
    if m:
        out["pe_ttm"] = to_f(m.group(1))
    # 每股净资产 / 每股收益取**最近一次**出现（摘要按报告期倒序排列时最近期在前，
    # 正序时最近期在后；估值口径统一取"最新一期财报"，故这里取最后一处匹配）
    for m in _BPS_RE.finditer(t):
        out["bps"] = to_f(m.group(1))
    for m in _EPS_RE.finditer(t):
        out["eps"] = to_f(m.group(1))
    return out


def valuation_block(text: str) -> str:
    """生成「🧮 程序计算的估值参考」文本块（供研报上下文与页面共用）。

    口径说明与缺失原因都在块内写清：
      · PB 只要有股价 + 每股净资产就算，不依赖任何预测数据；
      · 动态 PE 需要全年 EPS 预测，本次材料没有就**单独说明 PE 不算**，
        绝不再连带把 PB 也判成"无法计算"。
    """
    inp = parse_valuation_inputs(text or "")
    price, bps = inp["price"], inp["bps"]
    lines = ["【程序已计算的估值指标 · 不得改写，直接采用】"]
    pb_v, pb_msg = pb_from(price, bps)
    if pb_v is None:
        lines.append(f"· PB：{pb_msg}")
    else:
        lines.append(
            f"· PB = {pb_msg}（口径：股价取行情接口最新价，每股净资产取最新一期财报）"
        )
    if inp["pe_ttm"] is not None:
        lines.append(
            f"· PE-TTM = {_fmt(inp['pe_ttm'])}（行情接口直接给出，滚动 12 个月口径）"
        )
    else:
        pe_v, pe_msg = pe_from(price, inp["eps"])
        if pe_v is None:
            lines.append(f"· PE：{pe_msg}（其中动态 PE 还需全年 EPS 预测，本次材料未提供）")
        else:
            lines.append(f"· PE = {pe_msg}")
    lines.append(
        "· 口径限制：以上只完成**指标计算**；是否便宜还需历史分位与同业对比，"
        "本次材料未提供分位数据时不得给出高低判断。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. 语言方向校验（B07）
# ---------------------------------------------------------------------------
# 「由 A 升至/降至 B」这类模板句：数值与自然语言方向必须一致。
# 50.53% → 50.75% 写成"降至"，就是方向反了 —— 程序直接按数值改回来。
_DIR_RE = re.compile(
    r"由\s*(" + _NUM + r")\s*(%|个百分点)?\s*"
    r"(上升至|上升到|升至|提高至|提高到|提升至|增长至|增至|上涨至|涨至"
    r"|下降至|下降到|降至|降低至|降低到|回落至|减少至|减至|下跌至|跌至)"
    r"\s*(" + _NUM + r")\s*(%|个百分点)?"
)

_FLIP = {
    "上升至": "下降至", "上升到": "下降到", "升至": "降至",
    "提高至": "降低至", "提高到": "降低到", "提升至": "降低至",
    "增长至": "减少至", "增至": "减至", "上涨至": "下跌至", "涨至": "跌至",
    "下降至": "上升至", "下降到": "上升到", "降至": "升至",
    "降低至": "提高至", "降低到": "提高到", "回落至": "回升至",
    "减少至": "增长至", "减至": "增至", "下跌至": "上涨至", "跌至": "涨至",
}


def _word_dir(w: str) -> int:
    """1 = 上升类，-1 = 下降类，0 = 无法归类。"""
    if any(k in w for k in ("升", "涨", "增", "提高", "提升", "回升")):
        return 1
    if any(k in w for k in ("降", "跌", "减少", "降低", "回落", "下滑")):
        return -1
    return 0


def scan_direction_errors(text: str) -> List[Tuple[str, str]]:
    """扫出「数值方向与用词相反」的句子，返回 [(原句, 修正句)]。"""
    if not text:
        return []
    out = []
    for m in _DIR_RE.finditer(text):
        a, b = to_f(m.group(1)), to_f(m.group(4))
        w = m.group(3)
        if a is None or b is None:
            continue
        if abs(b - a) <= 1e-9:
            continue
        wd = _word_dir(w)
        if wd == 0:
            continue
        real = 1 if b > a else -1
        if wd == real:
            continue
        fixed_w = _FLIP.get(w)
        if not fixed_w:
            continue
        old = m.group(0)
        u1, u2 = m.group(2) or "", m.group(5) or ""
        new = f"由 {m.group(1)}{u1} {fixed_w} {m.group(4)}{u2}"
        out.append((old, new))
    return out


def fix_direction(text: str) -> Tuple[str, List[str]]:
    """把方向写反的句子改回来，返回 (修正后文本, 修正记录)。

    只改**方向词**，不动任何数字 —— 数字由取数决定，方向由数字决定。
    """
    if not text:
        return text, []
    fixes = []
    result = text
    for old, new in scan_direction_errors(text):
        result = result.replace(old, new)
        fixes.append(f"方向修正：{old} → {new}")
    return result, fixes


# ---------------------------------------------------------------------------
# 4. 成稿清洗：去除加工前言 + 修复不成对的 Markdown 标记（B10）
# ---------------------------------------------------------------------------
_META_PAT = re.compile(
    r"(以下是|以下为|这是)[^。\n]{0,40}(润色|一致性校验|加工|修订|调整)[^。\n]{0,20}(版本|文稿|内容|结果)?"
    r"|^润色(原则|说明|要点|思路)(如下|如下：|:)?"
    r"|^(修改|调整|优化)(说明|要点)(如下|:|：)?"
    r"|^好的[，,].{0,30}$"
    r"|^已(经)?(完成|根据)[^。\n]{0,20}(润色|校验|整理)[^。\n]{0,10}$"
    r"|^注：?[^。\n]{0,20}(润色|加工|机器|模型)[^。\n]{0,20}$",
)


def _is_meta_line(s: str) -> bool:
    if not s:
        return True
    if s.lstrip().startswith("#"):
        return False          # 只要是标题，一律保留
    return bool(_META_PAT.search(s.strip()))


def strip_polish_preface(text: str) -> str:
    """去掉正文前面的模型加工说明（"以下是……润色版本""润色原则如下……"）。

    验收标准（B10）：**正文从正式标题（# / ##）开始**，读者看到的第一行就是报告内容。
    """
    if not text:
        return text
    lines = text.split("\n")
    i = 0
    while i < len(lines) and _is_meta_line(lines[i].strip()):
        i += 1
    # 开头若仍是"前言段落"（未到第一个标题），直接截到第一个标题
    rest = lines[i:]
    for j, s in enumerate(rest[:15]):
        if s.lstrip().startswith("#"):
            rest = rest[j:]
            break
    out = "\n".join(rest).strip()
    return out or text


def fix_markdown(text: str) -> str:
    """修复不成对的 ** 标记。

    Streamlit 对不成对的 `**` 会**原样显示**成 `**–4.53%`（实测 B10 的现象），
    读者看到的是一串星号而不是数字。处理办法：逐行去掉那个落单的 `**`，
    宁可不加粗，也不能把标记漏到读者眼前。
    """
    if not text:
        return text
    out = []
    for line in text.split("\n"):
        if line.count("**") % 2 == 1:
            idx = line.rfind("**")
            line = line[:idx] + line[idx + 2:]
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 5. 润色守卫：润色只能改表达，不能改事实（B06）
# ---------------------------------------------------------------------------
# 这些词一出现就意味着**引入了新的统计口径**：
# 初稿"白酒业 ROE 11% 即显落后"是规则判断线，润色成"行业约 11% 的平均水平"
# 就凭空造出一个未经本轮数据支持的行业均值。
_LOCKED_STATS = ("平均水平", "行业平均", "平均值", "平均数", "均值",
                 "中位数", "分位数", "均值水平", "平均水平约为")

_LIST_MARK = re.compile(r"(?m)^\s*\d{1,2}[\.、)]\s*")
_NUM_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")


def _numbers(text: str) -> set:
    """取出文本里的全部数字（先剔除列表编号 1. 2. 3. 这类非事实数字）。"""
    t = _LIST_MARK.sub(" ", text or "")
    return {round(float(x), 4) for x in _NUM_RE.findall(t)}


def polish_guard(draft: str, polished: str) -> Tuple[str, str, List[str]]:
    """润色结果守卫。返回 (最终采用的文本, 状态, 说明列表)。

    状态：
      · "pass"        —— 润色稿通过，采用润色稿；
      · "keep_draft"  —— 润色引入了新事实/新口径，**退回初稿**（初稿也做同样的清洗）；
      · "no_polish"   —— 没有润色结果（外部调用前判断），这里不会返回。

    检查项：
      1. 新增**统计口径词**（平均水平 / 中位数 / 分位数 …）；
      2. 新增**数字**（初稿里从未出现过的数值 = 新事实）；
      3. 数值方向与用词相反（交由 fix_direction 修正，修正即视为通过）。
    """
    issues: List[str] = []
    if not polished:
        return draft, "keep_draft", ["润色结果为空，保留初稿"]

    clean = fix_markdown(strip_polish_preface(polished))
    if not clean.strip():
        return draft, "keep_draft", ["润色结果清洗后为空，保留初稿"]

    # ① 方向写反 → 程序按数值改回来（这是可修的，修完不算"新事实"）
    clean, fixes = fix_direction(clean)
    for f in fixes:
        issues.append(f"已修正：{f}")

    # ② 新统计口径
    d0, p0 = draft or "", clean
    for w in _LOCKED_STATS:
        if w in p0 and w not in d0:
            issues.append(f"润色新增了未经本次数据支持的统计口径「{w}」")

    # ③ 新数字
    dn, pn = _numbers(d0), _numbers(p0)
    new_nums = sorted(pn - dn)
    if new_nums:
        _show = "、".join(_fmt(x) for x in new_nums[:6])
        issues.append(f"润色新增了初稿中不存在的数值：{_show}")

    if any(i.startswith("润色新增") for i in issues):
        # 只有"新事实/新口径"才退回初稿；方向修正属于可修复项，用润色稿
        keep = fix_markdown(strip_polish_preface(draft))
        return (keep or draft), "keep_draft", issues
    return clean, "pass", issues

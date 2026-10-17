"""
analysis_context —— 统一的「当前分析对象」上下文
================================================

对应优化方案 **O01（统一当前分析上下文）**，修复实测问题 **B01 / B02**。

实测现象：
  · 先在线分析贵州茅台（600519），再切到「手动粘贴」输入一段不含任何财务数据的
    普通文本，四象限图与解读仍显示"ROE 32.5% · 营收增速 -1.2%" ——
    这两个数只存在于**上一次在线取数**里（B01）；
  · 手动输入虚构公司"QA测试公司"后，顶部对象仍是"贵州茅台（600519）"，
    推荐问题写成"贵州茅台的『盈利能力』得分最高"，历史记录变成
    "QA测试公司 · 600519"（B02）。

根因：公司身份散落在 `single_online_code / current_target / analysis['company']`
多处，手动模式从不清理，`single_online_code` 于是被**跨来源继承**。

本模块的约定（违反即视为缺陷）：

1. 一次分析只认**一个**当前对象：name / code / source / period；
2. 手动与上传来源的证券代码一律为**空**——未知公司不继承上一次在线公司的代码；
   只有当本次材料里真的写了 6 位证券代码，才用它；
3. 指标缺失就返回空值，**禁止**回退到其它数据来源的旧值；
4. 评分、图表、推荐问题、历史记录、顶栏一律读这个上下文，绝不各读各的。
"""
from __future__ import annotations

import re
from typing import Optional

__all__ = [
    "SOURCE_ONLINE", "SOURCE_MANUAL", "SOURCE_UPLOAD",
    "blank", "get", "set_online", "set_manual", "set_upload",
    "sync_from_analysis", "code_for_source", "reset",
    "detect_code", "identity_for_history", "signature_part",
]

SOURCE_ONLINE = "online"
SOURCE_MANUAL = "manual"
SOURCE_UPLOAD = "upload"

try:                                   # 离线评测脚本里没有 streamlit，做优雅降级
    import streamlit as st             # type: ignore
except Exception:                      # pragma: no cover
    st = None                          # type: ignore

_K_NAME = "ctx_name"
_K_CODE = "ctx_code"
_K_SRC = "ctx_source"
_K_PERIOD = "ctx_period"
_K_MAT = "ctx_material"

# A 股代码：6 位，首位 0/3/6/8/9（沪 60/68，深 00/30，北 4/8 的 6 位新代码）
_CODE_RE = re.compile(r"(?<!\d)([03689]\d{5})(?!\d)")
# 紧跟单位的数字是金额/数量，不是证券代码
_UNIT_NEXT = ("元", "万", "亿", "吨", "台", "辆", "人", "股", "件", "次",
              "家", "户", "笔", "%")


def blank() -> dict:
    """一个空的上下文（code 为空 = 身份未知）。"""
    return {"name": "", "code": "", "source": "", "period": ""}


def _state():
    return st.session_state if st is not None else {}


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def get() -> dict:
    """取当前上下文；session 不可用时返回空上下文（纯函数环境可正常调用）。"""
    s = _state()
    if not s:
        return blank()
    return {
        "name": str(s.get(_K_NAME) or ""),
        "code": str(s.get(_K_CODE) or ""),
        "source": str(s.get(_K_SRC) or ""),
        "period": str(s.get(_K_PERIOD) or ""),
    }


def detect_code(text: str) -> str:
    """从**本次材料**里识别 6 位证券代码；识别不到返回空串。

    宁可返回空，也不猜 —— 材料里没有代码时，身份就是"未知公司"。
    """
    t = text or ""
    for m in _CODE_RE.finditer(t):
        tail = t[m.end():m.end() + 2]
        if tail and tail[0] in _UNIT_NEXT:
            continue                       # "150000万元" 这类是金额，不是代码
        return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def _write(name: str = "", code: str = "", source: str = "", period: str = "") -> None:
    s = _state()
    if not s:
        return
    s[_K_NAME] = name or ""
    s[_K_CODE] = code or ""
    s[_K_SRC] = source or ""
    s[_K_PERIOD] = period or ""


def set_online(name: str = "", code: str = "", period: str = "") -> None:
    """在线取数成功 → 上下文锁定为该证券。"""
    s = _state()
    _write(name=name, code=code, source=SOURCE_ONLINE, period=period)
    if s:
        s.pop(_K_MAT, None)      # 在线来源不参与"材料指纹保留名称"的逻辑


def set_manual(text: str = "", material_key: str = "") -> None:
    """切到手动粘贴：身份按**本次材料**重建。

    ⚠️ 代码只能来自本次粘贴的材料：没有就置空 —— 否则就会出现
    "QA测试公司 · 600519" 这种把虚构公司挂到茅台名下的问题（B02）。

    :param material_key: 本次材料的内容指纹。同一份材料（指纹未变）时
        **保留**已识别到的公司名称 —— 否则手动模式下每一轮 rerun 都会把
        刚分析出来的公司名清掉，推荐问题与顶栏在下一轮又变回"尚未选择"。
    """
    s = _state()
    keep = ""
    if (material_key and s.get(_K_MAT) == material_key
            and s.get(_K_SRC) in (SOURCE_MANUAL, SOURCE_UPLOAD)):
        keep = s.get(_K_NAME) or ""
    _write(name=keep, code=detect_code(text), source=SOURCE_MANUAL, period="")
    if material_key and s:
        s[_K_MAT] = material_key


def set_upload(text: str = "", material_key: str = "") -> None:
    """上传材料：与手动粘贴同一条规则（代码只认材料里写明的）。"""
    s = _state()
    keep = ""
    if (material_key and s.get(_K_MAT) == material_key
            and s.get(_K_SRC) in (SOURCE_MANUAL, SOURCE_UPLOAD)):
        keep = s.get(_K_NAME) or ""
    _write(name=keep, code=detect_code(text), source=SOURCE_UPLOAD, period="")
    if material_key and s:
        s[_K_MAT] = material_key


def sync_from_analysis(analysis: Optional[dict]) -> None:
    """分析完成后回写：名称与报告期只认**本次分析结果**。

    手动材料里写得像公司名的东西（例如标题"QA测试公司2025年报"）由模型抽出的
    `company` 字段提供；代码保持原样（手动来源恒为空）。
    """
    if not isinstance(analysis, dict):
        return
    s = _state()
    if not s:
        return
    nm = str(analysis.get("company") or "").strip()
    if nm:
        s[_K_NAME] = nm
    pd = str(analysis.get("score_period") or "").strip()
    if pd:
        s[_K_PERIOD] = pd


def reset() -> None:
    """清空当前对象（切换数据来源 / 清空数据时调用）。"""
    s = _state()
    if not s:
        return
    for k in (_K_NAME, _K_CODE, _K_SRC, _K_PERIOD):
        s.pop(k, None)


# ---------------------------------------------------------------------------
# 受控取值：跨来源继承的闸门
# ---------------------------------------------------------------------------
def code_for_source(*allow: str) -> str:
    """只有在**指定来源**下才返回证券代码，其它来源一律返回空串。

    用途：年报趋势 / 年度时间轴这类"必须联网按代码再取一次数"的功能，
    只允许在线来源使用。手动来源返回空 → 趋势为空 → 四象限图不会再拿
    上一次在线公司的指标顶上（B01）。
    """
    ctx = get()
    if allow and ctx["source"] not in allow:
        return ""
    return ctx["code"]


def identity_for_history(analysis: Optional[dict] = None) -> tuple:
    """历史记录要存的 (名称, 代码)。

    名称优先取本次分析结果（模型抽出的公司名），代码取上下文；
    手动来源的上下文代码为空 → 记录显示"QA测试公司 · —"，而不是挂到 600519。
    """
    ctx = get()
    name = ctx["name"]
    if (not name) and isinstance(analysis, dict):
        name = str(analysis.get("company") or "").strip()
    return (name or "", ctx["code"] or "")


def signature_part() -> str:
    """参与「数据指纹」的片段：来源变化也算数据版本变化。"""
    ctx = get()
    return f"{ctx['source']}|{ctx['code']}|{ctx['name']}"

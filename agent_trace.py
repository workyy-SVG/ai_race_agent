# -*- coding: utf-8 -*-
"""Agent 执行轨迹（工具调用链）记录器
=====================================

为什么需要这个模块
------------------
大赛命题是「让 AI Agent 赋能千行百业」。一个"Agent"和一段"调用大模型
然后打印结果"的脚本，最本质的区别是：**Agent 会自己决定调用哪个工具、
按什么顺序调用、失败了怎么换路**。但这个区别如果只存在于代码里，评委
翻完 PPT 和网页也看不到 —— 页面上一如既往地只呈现最终结果。

这个模块把一次分析里真实发生的每一步（取数 → 检索 → 抽取 → 评分 →
校验 → 核对）落成一条可展示的轨迹：谁调用了什么、传了什么、拿回了什么、
花了多久、有没有降级。于是「Agent 做了什么」从代码事实变成页面事实。

设计约束（与 pipeline.py 保持一致的两条）
-----------------------------------------
1. **纯 Python，不依赖 Streamlit**：可脱离运行时单测，评测脚本也能复用。
2. **只记录，不改变业务结果**：任何一步记录失败都不能影响分析本身
   —— 所有对外方法内部都吞掉异常，轨迹是"观测层"，不是"控制层"。

用法
----
    tr = Tracer()
    with tr.span("LLM", "结构化财务抽取", inp="DeepSeek · 输入 8200 字符") as s:
        raw = llm_once(...)
        s.out = f"返回 JSON {len(raw)} 字符"
    # 直接收结果的一步（无异常风险）可一步记完：
    tr.hit("规则引擎", "四维评分", inp="消费行业 · 覆盖率 100%", out="综合 9.4 / 10")
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

# 状态常量：页面渲染靠这几个值决定图标，别散落成字符串字面量
OK = "ok"          # 正常完成
DEGRADED = "degraded"  # 走了降级/兜底路径（Agent 自己换了路，但仍产出结果）
FAILED = "failed"      # 这一步彻底失败
SKIPPED = "skipped"    # 条件不满足，本步未执行（如实说明"没做"，不假装做了）

_STATUS_TEXT = {
    OK: "✅ 成功",
    DEGRADED: "🟡 降级",
    FAILED: "❌ 失败",
    SKIPPED: "⚪ 跳过",
}


class Step:
    """轨迹中的一步。span() 返回它，业务代码按需填 out / note / status。"""

    __slots__ = ("seq", "tool", "action", "inp", "out", "note",
                 "status", "t_start", "elapsed_ms", "kind")

    def __init__(self, seq: int, tool: str, action: str, inp: str = "", kind: str = "tool"):
        self.seq = seq
        self.tool = tool
        self.action = action
        self.inp = inp or ""
        self.out = ""
        self.note = ""
        self.status = OK
        self.kind = kind          # tool（工具调用） / decision（自主决策） / check（自检）
        self.t_start = time.time()
        self.elapsed_ms = 0

    def finish(self) -> None:
        self.elapsed_ms = int((time.time() - self.t_start) * 1000)

    def to_row(self) -> Dict[str, Any]:
        return {
            "#": self.seq,
            "阶段": self.tool,
            "动作": self.action,
            "输入": self.inp,
            "产出": self.out or "—",
            "耗时": f"{self.elapsed_ms} ms" if self.elapsed_ms >= 0 else "—",
            "状态": _STATUS_TEXT.get(self.status, self.status),
            "备注": self.note or "",
            "_kind": self.kind,
            "_status": self.status,
        }


class Tracer:
    """一次分析会话的轨迹台账。放在 st.session_state 里跨交互累积。"""

    def __init__(self, max_steps: int = 80):
        self._steps: List[Step] = []
        self._seq = 0
        self.max_steps = max_steps

    # ---- 记录入口 ----
    @contextmanager
    def span(self, tool: str, action: str, inp: str = "",
             kind: str = "tool") -> Iterator[Step]:
        """包住一步真实调用；抛异常时自动标 FAILED 并把异常类型写进备注。"""
        st = self._new(tool, action, inp, kind)
        try:
            yield st
        except Exception as e:  # 观测层绝不能把业务异常吞掉，只做标记后继续抛
            st.status = FAILED
            st.out = "调用未返回"
            st.note = f"{type(e).__name__}: {e}"
            st.finish()
            raise
        finally:
            if st.elapsed_ms == 0:
                st.finish()

    def hit(self, tool: str, action: str, inp: str = "", out: str = "",
            note: str = "", status: str = OK, kind: str = "tool",
            elapsed_ms: int = 0) -> Step:
        """一步记完（用于不会抛异常的规则计算类步骤）。"""
        st = self._new(tool, action, inp, kind)
        st.out = out
        st.note = note
        st.status = status
        st.elapsed_ms = int(elapsed_ms)
        return st

    def _new(self, tool: str, action: str, inp: str, kind: str) -> Step:
        self._seq += 1
        st = Step(self._seq, tool, action, inp, kind)
        self._steps.append(st)
        # 只保留最近 N 步：一个会话里反复分析不该让台账无限膨胀
        if len(self._steps) > self.max_steps:
            self._steps = self._steps[-self.max_steps:]
        return st

    # ---- 读取 ----
    @property
    def steps(self) -> List[Step]:
        return list(self._steps)

    def rows(self) -> List[Dict[str, Any]]:
        return [s.to_row() for s in self._steps]

    def summary(self) -> Dict[str, Any]:
        """顶部卡片用的一句话总览。"""
        n = len(self._steps)
        return {
            "total": n,
            "tools": sum(1 for s in self._steps if s.kind == "tool"),
            "decisions": sum(1 for s in self._steps if s.kind == "decision"),
            "checks": sum(1 for s in self._steps if s.kind == "check"),
            "degraded": sum(1 for s in self._steps if s.status == DEGRADED),
            "failed": sum(1 for s in self._steps if s.status == FAILED),
            "elapsed_ms": sum(s.elapsed_ms for s in self._steps),
        }

    def clear(self) -> None:
        self._steps = []
        self._seq = 0


def decisions_of(tracer: Optional["Tracer"]) -> List[Dict[str, str]]:
    """把 kind=decision 的步骤单独抽出来，作为「Agent 自主决策点」展示。

    为什么单独抽：工具调用表回答"做了什么"，决策点回答"为什么这么做"。
    Agent 的价值更大程度在后半句 —— 同样调一次接口，会不会自己判断
    「这是中报不能当年报用」「这个字段缺了不能硬凑分」，才是分水岭。
    """
    if tracer is None:
        return []
    return [
        {"决策点": s.action, "判断依据": s.inp, "结果": s.out or s.note or "—"}
        for s in tracer.steps
        if s.kind == "decision"
    ]


if __name__ == "__main__":  # pragma: no cover - 纯自检
    import json

    tr = Tracer()
    with tr.span("在线取数", "拉取财务指标", inp="600519 · 8 期 · 仅年报") as s:
        time.sleep(0.01)
        s.out = "拿到 8 期指标，最近年报 2025年报"
    with tr.span("LLM", "结构化抽取", inp="DeepSeek · 8200 字符") as s:
        s.out = "JSON 1.2 KB · 指标 6 项"
    tr.hit("规则引擎", "四维评分", inp="消费 · 覆盖率 100%", out="9.4 / 10", kind="check")
    tr.hit("口径控制", "锁定年报口径", inp="存在中报 2026Q1",
           out="已排除，仅用年报", kind="decision")
    try:
        with tr.span("RAG", "年报检索", inp="风险章节") as s:
            raise RuntimeError("未建索引")
    except RuntimeError:
        pass

    print(json.dumps({"summary": tr.summary(), "decisions": decisions_of(tr)},
                     ensure_ascii=False, indent=2))
    assert tr.summary()["failed"] == 1
    assert tr.summary()["decisions"] == 1
    print("agent_trace 自检通过")

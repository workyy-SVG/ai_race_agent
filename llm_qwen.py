"""
千问 (DashScope) 客户端封装
============================
负责: 调用千问做"二次校验 + 中文润色"
设计:
  - 优先读环境变量 / st.secrets, 缺失则优雅禁用
  - 任何异常都不抛, 返回 None 让上游用原文兜底
  - 同一接口支持 "refine_answer" / "generate_with_rag" 两种调用
"""
from __future__ import annotations

import os
import logging
from typing import List, Dict, Any, Optional

import dashscope
from dashscope import Generation

logger = logging.getLogger(__name__)

QWEN_MODEL_DEFAULT = "qwen-plus"


def _ensure_key() -> Optional[str]:
    key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")
    if not key:
        try:
            import streamlit as st
            key = st.secrets.get("QWEN_API_KEY")
        except Exception:
            key = None
    if not key:
        return None
    dashscope.api_key = key
    return key


def _model_name() -> str:
    try:
        import streamlit as st
        return st.secrets.get("QWEN_MODEL", QWEN_MODEL_DEFAULT)
    except Exception:
        return QWEN_MODEL_DEFAULT


def is_available() -> bool:
    return _ensure_key() is not None


def _call(prompt: str, system: Optional[str] = None, temperature: float = 0.4) -> Optional[str]:
    if not _ensure_key():
        return None
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # 60 秒超时保护: 必须 shutdown(wait=False), 否则退出时仍等挂起的 HTTP
    from concurrent.futures import ThreadPoolExecutor
    _single = ThreadPoolExecutor(max_workers=1)
    try:
        try:
            resp = _single.submit(
                Generation.call,
                model=_model_name(),
                messages=messages,
                temperature=temperature,
                result_format="message",
                top_p=0.85,
            ).result(timeout=60)
        finally:
            _single.shutdown(wait=False)
        if resp.status_code != 200:
            logger.warning("千问调用失败 [%s]: %s", resp.status_code, resp.message)
            return None
        return resp.output.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("千问调用异常: %s", e)
        return None


def refine_answer(raw_answer: str, context: Optional[str] = None) -> Optional[str]:
    """基于 DeepSeek 原始输出做二次校验 + 中文润色.

    Args:
        raw_answer: DeepSeek 输出的结构化分析
        context: 可选的 RAG 检索片段, 用作 fact-check 依据
    Returns:
        润色后的文本, 失败时返回 None
    """
    system = (
        "你是金融投研报告的『中文润色与一致性校验员』。"
        "输入是另一模型初稿, 请你: "
        "1) 保留所有数据/数字/结论不变; "
        "2) 把中文表达润色得更专业、连贯、口语化更少; "
        "3) 修复个别不通顺/重复的句式; "
        "4) 不要新增未在原文出现的事实; "
        "5) 输出 Markdown, 标题层级与原文一致。"
    )
    if context:
        user = (
            "【事实校验素材】\n" + context +
            "\n\n【待润色初稿】\n" + raw_answer +
            "\n\n请基于事实校验素材, 输出润色后的最终稿。"
        )
    else:
        user = "请润色以下初稿: \n\n" + raw_answer

    return _call(user, system=system, temperature=0.3)


def generate_with_rag(
    question: str,
    rag_context: str,
    style_hint: str = "structured",
) -> Optional[str]:
    """RAG 模式: 千问拿到检索片段, 直接产出答案 (用于对话/追问)."""
    system = (
        "你是金融研报分析助手。用户会问关于公司年报的问题。"
        "请严格基于【检索片段】作答, 不要编造检索片段外的数据。"
        "若片段不足以回答, 请明确说明。"
        "末尾用 [n] 标注引用, 形如 [1][3]。"
    )
    if style_hint == "structured":
        system += "请以 Markdown 列表 / 段落形式分点作答。"

    user = (
        "【检索片段】\n" + rag_context + "\n\n"
        "【用户问题】\n" + question + "\n\n"
        "请严格按要求作答。"
    )
    return _call(user, system=system, temperature=0.2)


# ---------------------------------------------------------------------------
# CLI 自检
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    print(json.dumps({
        "module": "llm_qwen",
        "available": is_available(),
        "model_default": QWEN_MODEL_DEFAULT,
    }, ensure_ascii=False, indent=2))

# -*- coding: utf-8 -*-
"""
金融智能投研 Agent —— 部署版（Streamlit Cloud 友好）

部署时需要做的事：
  1. 在 Streamlit Cloud 的 Secrets 里填入：
        DEEPSEEK_API_KEY = "sk-xxx"
        APP_USER = "judge"
        APP_PASSWORD = "ai2026"
  2. 直接部署即可，本地无需任何 .toml 文件。

本地运行时（可选），在项目根目录创建 .streamlit/secrets.toml，内容同上。
"""

import os
import streamlit as st
from openai import OpenAI

# ============ 配置：优先从 Secrets 读取，其次从环境变量 ============
def _get_secret(key, default=None):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)

DEEPSEEK_API_KEY = _get_secret("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = _get_secret("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = _get_secret("DEEPSEEK_MODEL", "deepseek-chat")

APP_USER = _get_secret("APP_USER", "judge")
APP_PASSWORD = _get_secret("APP_PASSWORD", "ai2026")

# ============ 登录页：账号 + 密码 ============
def check_login():
    if st.session_state.get("authenticated", False):
        return True

    st.set_page_config(page_title="金融智能投研 Agent", layout="centered")
    st.markdown("## 🔒 金融智能投研 Agent")
    st.caption("请输入访问账号和密码后进入系统")
    with st.form("login_form", clear_on_submit=False):
        user = st.text_input("账号", placeholder="例如：judge")
        pw = st.text_input("密码", type="password", placeholder="请输入访问密码")
        submitted = st.form_submit_button("进入系统", use_container_width=True)
        if submitted:
            if user == APP_USER and pw == APP_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("账号或密码错误，请重试")
    return False


if not check_login():
    st.stop()

# ============ 已登录：进入主界面 ============
st.set_page_config(page_title="金融智能投研 Agent", layout="wide")
st.title("📈 金融智能投研 Agent")
st.caption("粘贴或上传一家公司的财报数据，向它提问，即可得到结构化投研分析")

with st.sidebar:
    st.success(f"已登录 · 账号：{APP_USER}")
    if st.button("退出登录"):
        st.session_state["authenticated"] = False
        st.rerun()

# 给大模型设定"投研分析师"的角色
SYSTEM_PROMPT = """你是一名专业、严谨的金融投研分析师。请基于用户提供的财务数据，输出结构化分析，必须包含以下四部分：

1. 【公司概况】一句话概括这家公司的主营业务与所处阶段。
2. 【财务分析】从盈利能力（毛利率、净利率、ROE）、成长性（营收/净利润增速）、偿债能力（资产负债率）、经营质量（现金流）等维度分析，给出具体数字和趋势判断。
3. 【风险提示】列出 2-3 条主要风险（如行业竞争、债务、现金流、宏观政策等）。
4. 【投资建议】基于以上分析，给出谨慎、客观的建议（仅供参考，不构成投资决策依据）。

要求：数字必须引用用户给的数据，不要凭空编造；数据不足时明确指出"缺少XX数据，无法判断"；语言通俗易懂。"""

# ---------- 数据输入区 ----------
col1, col2 = st.columns(2)
with col1:
    data_text = st.text_area(
        "① 财务数据（粘贴财报摘要，例如：2025年营收100亿、净利润12亿、毛利率35%……）",
        height=220,
    )
with col2:
    up = st.file_uploader("② 或上传财报文本文件（.txt / .md）", type=["txt", "md"])
    uploaded_text = ""
    if up is not None:
        uploaded_text = up.getvalue().decode("utf-8", errors="ignore")

raw_data = (data_text + "\n" + uploaded_text).strip()

# ---------- 对话区 ----------
if "messages" not in st.session_state:
    st.session_state.messages = []

for m in st.session_state.messages:
    st.chat_message(m["role"]).write(m["content"])

question = st.chat_input("例如：帮我分析一下这家公司的盈利能力和风险")

if question:
    if not raw_data:
        st.warning("请先在左侧粘贴/上传财务数据，再提问。")
    elif not DEEPSEEK_API_KEY:
        st.error("系统未配置 DEEPSEEK_API_KEY，请联系管理员。")
    else:
        st.chat_message("user").write(question)
        st.session_state.messages.append({"role": "user", "content": question})

        user_content = f"以下是这家公司的财务数据：\n\n{raw_data}\n\n请分析并回答：{question}"
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

        with st.chat_message("assistant"):
            with st.spinner("正在分析……"):
                resp = client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.3,
                    stream=True,
                )
                out = st.empty()
                full = ""
                for chunk in resp:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        full += delta
                        out.write(full)
        st.session_state.messages.append({"role": "assistant", "content": full})

st.divider()
st.caption("💡 比赛提示：这版是最小 Demo。后续可加：多公司横向对比、PDF 财报解析、RAG 检索、风险预警等，用来拿『分析过程』和『创新性』的分数。")

# -*- coding: utf-8 -*-
"""
金融智能投研 Agent v4 —— 在线取数 + 结构化分析 + 金融主题界面

功能清单：
  0. 【新】在线自动取数：输入股票代码/名称，自动从巨潮资讯网 + 东方财富拉取
     行情、多期财务指标、年报 PDF，一键生成分析文本（无需自己找财报）
  1. PDF / txt / md 财报上传解析 + 手动粘贴
  2. 结构化分析：四维评分卡片 + 总分
  3. 财务指标柱状图 + 多年趋势图
  4. 一键生成完整研报并支持下载
  5. 双公司横向对比模式（支持在线取数）
  6. 内置行业知识库（主界面横向按钮选择，分析时自动注入行业框架）
  7. 【修】追问对话：上下文实时刷新、可连续多轮提问

密钥说明：所有敏感信息从 Streamlit Secrets / 环境变量读取，代码中不含任何 key。
"""

import os
import re
import json
import io
import base64
import math
import tempfile
from pathlib import Path
import streamlit as st
import pandas as pd
from openai import OpenAI

import data_source as ds
try:
    import scoring
except ImportError:
    scoring = None  # 缺失时退回 LLM 评分并标记

# RAG + 双模型 (Plan B)  -- 千问用作润色 + RAG 检索回答, DeepSeek 仍负责结构化分析
try:
    import rag_engine as rag
except ImportError:
    rag = None  # 模块可选缺失, 走兜底
try:
    import llm_qwen as lq
except ImportError:
    lq = None

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
APP_USER = _get_secret("APP_USER", "zzy")
APP_PASSWORD = _get_secret("APP_PASSWORD", "zzy2026")

MAX_CHARS = 12000  # 送入大模型的财报文本最大长度（控制费用与时延）

# ============ 行业知识库（内置静态知识，分析时自动注入） ============
INDUSTRY_KB = {
    "通用": """【行业知识库 · 通用框架】
- 分析重点：盈利能力（毛利率/净利率/ROE）、成长性（营收与净利润增速）、偿债能力（资产负债率/流动比率）、经营质量（经营现金流/净利润比）。
- 常见风险信号：应收账款增速远超营收、存货异常堆积、有息负债率高、经营现金流持续为负、毛利率异常高于同行。
- 经验法则：ROE 长期 >15% 属优秀；资产负债率 >70% 需警惕（金融/地产行业除外）；经营现金流/净利润 长期 <0.5 说明利润质量存疑。""",

    "消费": """【行业知识库 · 消费/白酒/食品饮料】
- 商业模式核心：品牌溢价决定毛利率（高端白酒毛利率可达 80%+，普通食品约 20-40%）。
- 关键指标：预收账款/合同负债（反映渠道信心）、存货中的基酒（白酒越陈越值钱，不算减值风险）、大单品占比。
- 风险信号：渠道压货（应收+存货双高）、提价失灵、消费税政策变化、年轻群体消费习惯迁移。""",

    "金融": """【行业知识库 · 银行/保险/金融】
- 银行不看毛利率，核心看：净息差（NIM）、不良贷款率、拨备覆盖率、资本充足率、ROA。
- 经验区间：不良率 <1.5% 较健康；拨备覆盖率 >150% 属审慎；净息差 2% 左右为行业常态。
- 风险信号：不良率连续上行、房地产/城投敞口过大、核心一级资本充足率逼近监管红线。
- 保险公司关注：新业务价值(NBV)增速、内涵价值(EV)、综合成本率（财险 <100% 才盈利）。""",

    "新能源": """【行业知识库 · 新能源/制造业】
- 商业模式核心：技术迭代快、重资产、周期性强（产能扩张→价格战→出清）。
- 关键指标：产能利用率、单位成本下降曲线、在手订单、资本开支/折旧比、市占率变化。
- 风险信号：产品价格年降幅 >15%、应收账款回款期拉长、扩产周期撞上行业价格战、技术路线切换（如固态电池替代液态）。
- 光伏/锂电特别关注：硅料/碳酸锂价格周期、欧美贸易政策。""",

    "医药": """【行业知识库 · 医药生物】
- 商业模式核心：研发管线决定长期价值，销售能力决定当期业绩。
- 关键指标：研发费用率（创新药企 15-30%）、核心产品收入占比、集采风险敞口、在研管线进度（I/II/III期）。
- 风险信号：单一产品收入占比 >80%、核心品种纳入集采、研发管线多年无进展、销售费用率畸高（合规风险）。
- 经验法则：创新药看管线估值，仿制药看成本与规模，器械看渠道与集采。""",

    "互联网": """【行业知识库 · 互联网/科技】
- 商业模式核心：网络效应与规模经济，用户数据是核心资产。
- 关键指标：MAU/DAU 增长与留存、ARPU（单用户价值）、获客成本(CAC)/用户终身价值(LTV)比、云业务增速与利润率。
- 风险信号：用户增长停滞但获客成本飙升、监管反垄断处罚、数据合规风险、研发资本化比例异常。
- 经验法则：LTV/CAC >3 才算健康增长；互联网公司常用 P/S 估值而非 PE。""",
}

# ============ 通用系统提示词 ============
BASE_SYSTEM_PROMPT = """你是一名专业、严谨的金融投研分析师。请基于用户提供的财务数据，输出结构化分析。

分析必须包含以下四部分：
1. 【公司概况】一句话概括主营业务与所处阶段。
2. 【财务分析】从盈利能力（毛利率、净利率、ROE）、成长性（营收/净利润增速）、偿债能力（资产负债率）、经营质量（现金流）等维度展开，给出具体数字和趋势判断。
3. 【风险提示】列出 2-4 条主要风险，说明依据。
4. 【投资建议】谨慎、客观地给出综合判断（仅供参考，不构成投资决策依据）。

硬性要求：
- 所有数字必须引用用户提供的数据，禁止凭空编造。
- 数据不足时明确写出"缺少XX数据，无法判断"。
- 语言专业但通俗易懂，使用 markdown 小标题组织内容。
- 结合下方行业知识库中的经验法则进行分析。"""

# ============ 全局样式（金融主题：深藏青 + 金色） ============
CUSTOM_CSS = """
<style>
  /* ==========================================================
     金融智能投研 Agent · 亮色冷蓝白主题 (v3)
     主背景: #EEF2F7   卡片: #FFFFFF   品牌主色: #1E40AF (深蓝)
     资本金: #D9A441   涨: #DC2626   跌: #16A34A
     文字: #0B1F3A / #334155 / #64748B
     侧边栏: #DDE7F2 (比主背景深一档, 制造分区)
     ========================================================== */

  /* ===== 全局背景：冷蓝灰 + 微弱光斑 ===== */
  .stApp {
    background:
      radial-gradient(1250px 620px at 10% -10%, rgba(30, 64, 175, 0.10) 0%, rgba(30, 64, 175, 0) 58%),
      radial-gradient(950px 520px at 94% 2%, rgba(217, 164, 65, 0.10) 0%, rgba(217, 164, 65, 0) 55%),
      linear-gradient(170deg, #EEF2F7 0%, #E5ECF4 58%, #DDE7F2 100%);
    background-attachment: fixed;
  }
  header[data-testid="stHeader"] { background: transparent; }
  .block-container { padding-top: 1.4rem; color: #334155; }

  /* ===== 顶栏横幅：深蓝渐变 + 金色高光（科技投研感） ===== */
  .hero {
    background: linear-gradient(120deg, #0B1F3A 0%, #1E3A8A 38%, #1E40AF 100%);
    border: 1px solid rgba(219, 234, 254, 0.6);
    border-radius: 14px;
    padding: 22px 28px 18px 28px;
    margin-bottom: 14px;
    color: #F8FAFC;
    box-shadow: 0 10px 32px rgba(30, 64, 175, 0.30),
                0 0 0 1px rgba(217, 164, 65, 0.25) inset;
  }
  .hero h1 {
    color: #F8FAFC;
    font-size: 30px;
    font-weight: 800;
    margin: 0 0 4px 0;
    letter-spacing: 1px;
    text-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
  }
  .hero h1 .gold { color: #F5C563 !important; font-weight: 800; }
  .hero h1 .purple { color: #C9D9FF !important; font-weight: 800; }
  .hero p { color: #DBEAFE; font-size: 14px; margin: 0; }
  .hero .tagline { color: #93B5E0; font-size: 12px; margin-top: 6px; }

  /* ===== 通用卡片：白底 + 蓝边 ===== */
  div[data-testid="stMetric"] {
    background: linear-gradient(140deg, #FFFFFF 0%, #F8FAFC 100%);
    border: 1px solid #D6E0EC;
    border-radius: 12px;
    padding: 14px 16px 8px 16px;
    box-shadow: 0 4px 14px rgba(15, 35, 70, 0.06),
                0 0 0 1px rgba(30, 64, 175, 0.04) inset;
  }
  div[data-testid="stMetric"] label {
    color: #64748B !important;
    font-weight: 600;
  }
  div[data-testid="stMetric"] div {
    color: #0B1F3A !important;
    font-weight: 800;
  }
  div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
    background: linear-gradient(90deg, #0B1F3A 0%, #1E40AF 100%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  div[data-testid="stMetricDelta"] svg { fill: #D9A441; }

  /* ===== Expander / 区块卡片化 ===== */
  div[data-testid="stExpander"] {
    background: #FFFFFF;
    border: 1px solid #D6E0EC;
    border-radius: 12px;
    box-shadow: 0 2px 10px rgba(15, 35, 70, 0.05);
    margin-bottom: 4px;
  }
  div[data-testid="stExpander"] details { background: transparent; border: none; }
  div[data-testid="stExpander"] summary {
    font-weight: 700;
    color: #0B1F3A !important;
    padding: 10px 4px;
  }
  div[data-testid="stExpander"] summary * { color: #0B1F3A !important; }
  div[data-testid="stExpander"] summary:hover { color: #1E40AF !important; }

  /* ===== 行业选择 radio 药丸化（亮色版） ===== */
  div[role="radiogroup"] label {
    background: #FFFFFF;
    border: 1px solid #D6E0EC;
    border-radius: 999px;
    padding: 5px 12px;
    margin: 3px 2px;
    transition: all .18s ease;
    color: #475569 !important;
  }
  div[role="radiogroup"] label:hover {
    border-color: #1E40AF;
    box-shadow: 0 0 12px rgba(30, 64, 175, 0.18);
  }
  div[role="radiogroup"] label:has(input:checked) {
    background: linear-gradient(90deg, #1E40AF, #2563EB);
    border-color: #1E40AF;
    box-shadow: 0 0 16px rgba(30, 64, 175, 0.40);
  }
  div[role="radiogroup"] label:has(input:checked) div { color: #F5C563 !important; font-weight: 700; }

  /* ===== 按钮：主按钮深蓝渐变（强光晕） ===== */
  .stButton > button[kind="primary"] {
    background: linear-gradient(90deg, #1E40AF, #2563EB);
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    font-weight: 700;
    box-shadow: 0 4px 16px rgba(30, 64, 175, 0.45);
  }
  .stButton > button[kind="primary"]:hover {
    background: linear-gradient(90deg, #1E3A8A, #1E40AF);
    color: #F5C563;
    box-shadow: 0 6px 22px rgba(30, 64, 175, 0.65);
  }
  .stButton > button { color: #334155; }

  /* ===== 标签页：亮色 ===== */
  .stTabs [data-baseweb="tab-list"] { gap: 8px; }
  .stTabs [data-baseweb="tab"] {
    background-color: #FFFFFF;
    border-radius: 10px 10px 0 0;
    padding: 8px 20px;
    font-weight: 600;
    color: #64748B;
    border: 1px solid #D6E0EC;
  }
  .stTabs [aria-selected="true"] {
    background: linear-gradient(180deg, #2563EB, #1E40AF) !important;
    color: #F5C563 !important;
    border-color: #1E40AF !important;
    box-shadow: 0 -2px 16px rgba(30, 64, 175, 0.40);
  }

  /* ===== 侧边栏：冷蓝灰 + 蓝边条（与主背景区分） ===== */
  section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #DDE7F2 0%, #E8EFF7 55%, #DDE7F2 100%);
    border-right: 2px solid rgba(30, 64, 175, 0.30);
    box-shadow: 6px 0 20px rgba(15, 35, 70, 0.06);
  }
  section[data-testid="stSidebar"] * { color: #1E3A5F !important; }
  section[data-testid="stSidebar"] h3 {
    border-left: 4px solid #1E40AF;
    padding-left: 8px;
    color: #0B1F3A !important;
  }
  section[data-testid="stSidebar"] .stButton > button {
    background: #FFFFFF;
    border: 1px solid #D6E0EC;
    color: #334155;
    border-radius: 8px;
  }
  section[data-testid="stSidebar"] .stButton > button:hover {
    border-color: #1E40AF;
    color: #1E40AF !important;
  }
  /* 侧边栏品牌头 */
  .sb-brand {
    background: linear-gradient(120deg, #1E3A8A 0%, #2563EB 100%);
    border: 1px solid rgba(217, 164, 65, 0.65);
    border-radius: 12px;
    padding: 12px 14px;
    color: #FFFFFF !important;
    font-weight: 800;
    font-size: 15.5px;
    letter-spacing: 0.5px;
    box-shadow: 0 6px 18px rgba(30, 64, 175, 0.40);
  }
  .sb-brand .sb-sub {
    display: block;
    color: #F5C563 !important;
    font-size: 11px;
    font-weight: 600;
    margin-top: 4px;
    letter-spacing: 0.3px;
  }
  /* 侧边栏内容卡片 */
  .sb-card {
    background: rgba(255, 255, 255, 0.7);
    border: 1px solid #D6E0EC;
    border-radius: 10px;
    padding: 10px 12px;
    margin: 8px 0;
  }
  .sb-card .sb-title, .sb-title {
    color: #1E40AF !important;
    font-weight: 700;
    font-size: 13px;
    margin-bottom: 6px;
    letter-spacing: 0.5px;
  }
  .sb-card ol, .sb-card ul {
    margin: 0;
    padding-left: 18px;
    color: #334155 !important;
    font-size: 12.5px;
    line-height: 1.8;
  }
  .sb-chip {
    display: inline-block;
    background: rgba(30, 64, 175, 0.10);
    border: 1px solid rgba(30, 64, 175, 0.30);
    color: #1E40AF !important;
    border-radius: 999px;
    padding: 1px 9px;
    font-size: 11px;
    margin: 2px 2px;
    white-space: nowrap;
  }
  .sb-quota-head { font-size: 12px; margin-bottom: 4px; color: #475569 !important; }
  .sb-quota-head b { color: #D9A441 !important; }
  .sb-quota-bar {
    height: 6px;
    background: rgba(15, 35, 70, 0.08);
    border-radius: 999px;
    overflow: hidden;
  }
  .sb-quota-bar div { height: 100%; border-radius: 999px; }

  /* ===== 提示条 ===== */
  div[data-testid="stSuccess"] {
    background-color: rgba(22, 163, 74, 0.10);
    border-left: 4px solid #16A34A;
    color: #14532D !important;
    border-radius: 0 10px 10px 0;
  }
  div[data-testid="stWarning"] {
    background-color: rgba(217, 164, 65, 0.15);
    border-left: 4px solid #D9A441;
    color: #78350F !important;
    border-radius: 0 10px 10px 0;
  }
  div[data-testid="stError"] {
    background-color: rgba(220, 38, 38, 0.10);
    border-left: 4px solid #DC2626;
    color: #7F1D1D !important;
    border-radius: 0 10px 10px 0;
  }
  div[data-testid="stInfo"] {
    background-color: rgba(30, 64, 175, 0.08);
    border-left: 4px solid #1E40AF;
    color: #1E3A8A !important;
    border-radius: 0 10px 10px 0;
  }

  /* ===== 表格 (dataframe) ===== */
  .stDataFrame, [data-testid="stDataFrame"] {
    background: #FFFFFF;
    border-radius: 10px;
    border: 1px solid #D6E0EC;
    box-shadow: 0 2px 10px rgba(15, 35, 70, 0.05);
  }

  /* ===== 页脚 ===== */
  .footer-note {
    text-align: center;
    color: #64748B;
    font-size: 12px;
    padding: 8px;
  }
  .footer-note b { color: #D9A441; }

  /* ===== 双模型徽章 ===== */
  .dual-badge {
    display: inline-block;
    background: linear-gradient(90deg, #1E40AF 0%, #93B5E0 100%);
    color: #FFFFFF !important;
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
    margin: 6px 0;
    box-shadow: 0 4px 14px rgba(30, 64, 175, 0.40);
  }

  /* ===== 区块小标题：深色 ===== */
  h3, h4, h5 { color: #0B1F3A !important; font-weight: 700 !important; }
  h3, h4 { letter-spacing: 0.3px; }
  p, li, span, div { color: #334155; }
  small, .stCaption, [data-testid="stCaptionContainer"] { color: #64748B !important; }
  code { background: #E0E9F5; color: #1E3A8A; padding: 1px 6px; border-radius: 4px; }
  a { color: #1E40AF !important; }

  /* ===== RAG 检索结果卡 ===== */
  .rag-hit {
    background: #FFFFFF;
    border-left: 3px solid #1E40AF;
    border-top: 1px solid #D6E0EC;
    border-right: 1px solid #D6E0EC;
    border-bottom: 1px solid #D6E0EC;
    border-radius: 8px;
    padding: 10px 12px;
    margin: 6px 0;
    color: #334155;
    font-size: 12.5px;
    line-height: 1.7;
  }
  .rag-hit .rag-rank {
    color: #D9A441;
    font-weight: 700;
    margin-right: 8px;
  }
  .rag-hit .rag-page {
    color: #1E40AF;
    font-weight: 700;
  }
  .rag-hit .rag-sim-high { color: #16A34A; font-weight: 700; }
  .rag-hit .rag-sim-mid { color: #D9A441; font-weight: 700; }
  .rag-hit .rag-sim-low { color: #94A3B8; font-weight: 700; }

  /* ===== 图表解读卡 ===== */
  .insight-card {
    background: linear-gradient(120deg, rgba(30, 64, 175, 0.08) 0%, rgba(255, 255, 255, 0.6) 100%);
    border: 1px solid #D6E0EC;
    border-left: 4px solid #D9A441;
    border-radius: 10px;
    padding: 10px 14px;
    margin: 8px 0 14px 0;
    color: #334155;
    font-size: 13px;
    line-height: 1.75;
    box-shadow: 0 2px 10px rgba(15, 35, 70, 0.05);
  }
  .insight-card b { color: #1E3A8A; }
  .insight-card .up { color: #DC2626; font-weight: 700; }
  .insight-card .down { color: #16A34A; font-weight: 700; }
  .insight-card .neutral { color: #64748B; }

  /* ===== 分割线 ===== */
  hr { border: none; border-top: 1px dashed rgba(30, 64, 175, 0.20); }

  /* ===== 散点图四象限说明卡 ===== */
  .quad-note {
    background: #FFFFFF;
    border: 1px solid #D6E0EC;
    border-radius: 10px;
    padding: 10px 14px;
    margin: 6px 0 12px 0;
    font-size: 12.5px;
    line-height: 1.8;
    color: #334155;
  }
  .quad-note .q-star { color: #DC2626; font-weight: 700; }
  .quad-note .q-cash { color: #D9A441; font-weight: 700; }
  .quad-note .q-quest { color: #1E40AF; font-weight: 700; }
  .quad-note .q-dog { color: #94A3B8; font-weight: 700; }
</style>
"""

# ============ 页面与登录 ============
st.set_page_config(page_title="金融智能投研 Agent", layout="wide")
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

def check_login():
    if st.session_state.get("authenticated", False):
        return True

    # ---- 方案1: 免登录链接 (URL 携带 ?key=xxx) ----
    _demo_key = _get_secret("DEMO_KEY", "ai2026demo")
    _qp_key = st.query_params.get("key", "")
    if _qp_key and _qp_key == _demo_key:
        st.session_state["authenticated"] = True
        st.session_state["demo_mode"] = True
        st.rerun()

    st.markdown(
        """
        <div class="hero" style="text-align:center; max-width:560px; margin:40px auto 10px auto;">
            <h1>📈 金融智能投研 <span class="gold">Agent</span></h1>
            <p>AI 驱动的财报深度分析 · 结构化评分 · 双公司对比 · 行业知识库</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("login_form", clear_on_submit=False):
        user = st.text_input("账号", placeholder="请输入访问账号")
        pw = st.text_input("密码", type="password", placeholder="请输入访问密码")
        submitted = st.form_submit_button("进入系统", use_container_width=True)
        if submitted:
            if user == APP_USER and pw == APP_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("账号或密码错误，请重试")
    st.caption("🔒 本系统仅供大赛评审访问 · 分析结果仅供参考，不构成投资建议")
    return False

if not check_login():
    st.stop()

# ============ 工具函数 ============
def make_client():
    # timeout=60: 单次请求 60 秒上限（流式则约束相邻 chunk 间隔），防网络抽风无限挂起
    # max_retries=2: 连接类错误自动重试 2 次
    return OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        timeout=60.0,
        max_retries=2,
    )

# ============ 使用日志（可观测性：只记动作/耗时/成败，不记密钥与正文） ============
USAGE_LOG = Path("usage_log.jsonl")

def log_event(action: str, ok: bool = True, dur: float = 0.0, extra: str = ""):
    try:
        import datetime
        rec = {
            "t": datetime.datetime.now().isoformat(timespec="seconds"),
            "action": str(action)[:24],
            "ok": bool(ok),
            "dur": round(float(dur), 2),
            "target": str(st.session_state.get("current_target") or "")[:40],
            "extra": str(extra)[:80],
        }
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志失败绝不影响主流程

# ============ 方案3: 每日 LLM 调用熔断 (防 API 额度被刷爆) ============
DAILY_LIMIT = int(_get_secret("DAILY_LLM_LIMIT", "100"))
USAGE_FILE = Path("/tmp/llm_usage.txt") if os.path.exists("/tmp") else Path("llm_usage.txt")

def _today():
    import datetime
    return datetime.date.today().isoformat()

def _read_usage():
    try:
        parts = USAGE_FILE.read_text(encoding="utf-8").strip().split("|")
        if len(parts) == 2 and parts[0] == _today():
            return int(parts[1])
    except Exception:
        pass
    return 0

def _bump_usage():
    try:
        USAGE_FILE.write_text(f"{_today()}|{_read_usage() + 1}", encoding="utf-8")
    except Exception:
        pass

def check_quota():
    """返回剩余额度是否大于 0；用尽时在页面提示并熔断。"""
    if _read_usage() >= DAILY_LIMIT:
        st.error(
            f"⚠️ 今日 LLM 分析额度（{DAILY_LIMIT} 次）已用完，为保护服务资源已暂停生成。"
            "请明日再试或联系管理员。"
        )
        return False
    return True

def llm_once(messages, temperature=0.2, action="llm"):
    import time
    _t0 = time.time()
    if not check_quota():
        return ""
    _bump_usage()
    try:
        resp = make_client().chat.completions.create(
            model=DEEPSEEK_MODEL, messages=messages, temperature=temperature, stream=False
        )
        out = resp.choices[0].message.content or ""
        log_event(action, ok=True, dur=time.time() - _t0)
        return out
    except Exception as e:
        log_event(action, ok=False, dur=time.time() - _t0, extra=type(e).__name__)
        raise

def llm_stream(messages, placeholder, temperature=0.3, action="chat"):
    import time
    _t0 = time.time()
    if not check_quota():
        placeholder.error(f"⚠️ 今日 LLM 分析额度（{DAILY_LIMIT} 次）已用完，为保护服务资源已暂停生成。")
        return ""
    _bump_usage()
    try:
        resp = make_client().chat.completions.create(
            model=DEEPSEEK_MODEL, messages=messages, temperature=temperature, stream=True
        )
        full = ""
        for chunk in resp:
            delta = chunk.choices[0].delta.content
            if delta:
                full += delta
                placeholder.write(full)
        log_event(action, ok=True, dur=time.time() - _t0, extra=f"out_len={len(full)}")
        return full
    except Exception as e:
        log_event(action, ok=False, dur=time.time() - _t0, extra=type(e).__name__)
        raise

def extract_pdf_text(file):
    try:
        import pypdf
    except ImportError:
        return None
    try:
        reader = pypdf.PdfReader(file)
        pages = []
        for page in reader.pages:
            t = page.extract_text() or ""
            pages.append(t)
        return "\n".join(pages)
    except Exception:
        return None


def extract_pdf_bytes(data: bytes):
    """从 bytes 解析 PDF 文本（用于在线下载的年报）。"""
    try:
        import pypdf
    except ImportError:
        return None
    try:
        return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None

def read_upload(up):
    if up is None:
        return "", None
    name = (up.name or "").lower()
    if name.endswith(".pdf"):
        txt = extract_pdf_text(up)
        if txt is None:
            return "", "PDF 解析失败（服务器组件缺失或文件加密），请改用 txt/md 上传或直接粘贴文本。"
        if not txt.strip():
            return "", "该 PDF 提取不到文本（可能是纯扫描件），请粘贴财报文字。"
        return txt, None
    return up.getvalue().decode("utf-8", errors="ignore"), None

def parse_json_loose(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


# ============ LLM 输出严格校验（页面永不因模型格式异常而崩溃） ============
def _safe_num(v, lo=None, hi=None):
    """转数值并夹范围；非法值返回 None。"""
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        if lo is not None:
            f = max(lo, f)
        if hi is not None:
            f = min(hi, f)
        return f
    except (TypeError, ValueError):
        return None


def validate_analysis(a):
    """
    校验并修复结构化分析结果。
    返回 (analysis, issues)。所有字段兜底到安全默认值，异常项被剔除/修复。
    """
    issues = []
    if not isinstance(a, dict):
        return {}, ["模型输出不是 JSON 对象，已丢弃"]

    out = {}

    # -- 基本字段 --
    out["company"] = str(a.get("company") or "目标公司").strip()[:50] or "目标公司"
    out["summary"] = str(a.get("summary") or "").strip()[:400]

    # -- metrics: list[dict]，逐项校验 --
    metrics = []
    raw_metrics = a.get("metrics")
    if isinstance(raw_metrics, list):
        for m in raw_metrics[:12]:
            if not isinstance(m, dict):
                continue
            item = str(m.get("item") or "").strip()[:30]
            if not item:
                continue
            val = m.get("value")
            val_out = _safe_num(val)
            if val_out is None and val not in (None, ""):
                val_out = str(val)[:20]  # 文本型数值（如 "91.5%"）保留原文
            elif val_out is not None:
                val_out = round(val_out, 4)
            metrics.append({
                "item": item,
                "value": val_out if val_out is not None else (str(val)[:20] if val not in (None, "") else "N/A"),
                "unit": str(m.get("unit") or "").strip()[:10],
                "comment": str(m.get("comment") or "").strip()[:120],
            })
    if not metrics:
        issues.append("metrics 缺失或格式异常，已置空")
    out["metrics"] = metrics

    # -- chart_data: 仅保留合法数值，最多 6 项 --
    chart = []
    raw_chart = a.get("chart_data")
    if isinstance(raw_chart, list):
        for c in raw_chart:
            if not isinstance(c, dict):
                continue
            v = _safe_num(c.get("value"), lo=-1e6, hi=1e7)
            if v is None:
                continue
            item = str(c.get("item") or "").strip()[:20]
            if item:
                chart.append({"item": item, "value": v})
    chart = chart[:6]
    if isinstance(raw_chart, list) and len(raw_chart) > 0 and not chart:
        issues.append("chart_data 全部非法（非数值），图表已隐藏")

    # -- chart_data 与 metrics 交叉校准：同名金额指标以指标明细为准 --
    # （模型分别提取两处时可能口径不一致，如图表取到中报、明细取到年报，
    #   导致柱状图与明细表对不上；此处强制对齐明细表的数值）
    if chart and metrics:
        m_lookup = {}
        for m in metrics:
            try:
                mv = float(m["value"])
            except (TypeError, ValueError):
                continue
            if m.get("unit", "") not in ("", "亿元", "亿"):
                continue  # 只校准金额类，比率(%)不参与
            m_lookup[m["item"].replace(" ", "")] = mv

        def _match_key(name: str):
            if name in m_lookup:
                return name
            for k in m_lookup:
                if k and (k in name or name in k):
                    return k
            return None

        fixed_chart = []
        for c in chart:
            k = _match_key(c["item"].replace(" ", ""))
            if k is not None:
                mv = m_lookup[k]
                if abs(mv - c["value"]) > max(0.5, abs(c["value"]) * 0.05):
                    issues.append(
                        f"图表项「{c['item']}」({c['value']}) 与指标明细 ({mv}) 口径不一致，已按明细校准"
                    )
                    c = {"item": c["item"], "value": round(mv, 4)}
            fixed_chart.append(c)
        chart = fixed_chart
    out["chart_data"] = chart

    # -- trend: 数值 + 期别标签，去重 --
    trend = []
    seen_periods = set()
    raw_trend = a.get("trend")
    if isinstance(raw_trend, list):
        for t in raw_trend:
            if not isinstance(t, dict):
                continue
            period = str(t.get("period") or "").strip()[:20]
            rev = _safe_num(t.get("revenue"), lo=-1e6, hi=1e7)
            np_ = _safe_num(t.get("net_profit"), lo=-1e6, hi=1e7)
            if rev is None and np_ is None:
                continue
            if not period or period in seen_periods:
                continue
            seen_periods.add(period)
            trend.append({"period": period, "revenue": rev, "net_profit": np_})
    out["trend"] = trend

    # -- scores / total_score: 夹在 [0,10]；总分非法时回退为维度均分 --
    raw_scores = a.get("scores")
    scores = {}
    if isinstance(raw_scores, dict):
        for k in ["盈利能力", "成长性", "偿债能力", "现金流质量"]:
            v = _safe_num(raw_scores.get(k), lo=0, hi=10)
            if v is not None:
                scores[k] = round(v, 1)
    out["scores"] = scores
    _svals = list(scores.values())
    _avg = round(sum(_svals) / len(_svals), 1) if _svals else None
    raw_total = a.get("total_score")
    _raw_total_num = _safe_num(raw_total)  # 不夹范围，用于判断是否非法
    total = _safe_num(raw_total, lo=0, hi=10)
    if total is None or _raw_total_num is None or not (0 <= _raw_total_num <= 10):
        # 总分缺失或越界 → 用维度分数均分兜底
        total = _avg
        if _raw_total_num is not None and not (0 <= _raw_total_num <= 10):
            issues.append(f"total_score 非法（{raw_total}），已按维度均分回退为 {total}")
    if total is not None:
        total = round(total, 1)
    out["total_score"] = total
    if not scores:
        issues.append("评分缺失（数据不足或规则引擎未启用）")

    # -- risks / highlights: 字符串列表，截断 --
    def _str_list(x, maxn, name):
        if not isinstance(x, list):
            if x not in (None, ""):
                issues.append(f"{name} 格式异常，已置空")
            return []
        cleaned = []
        for it in x:
            s = str(it or "").strip()
            if s:
                cleaned.append(s[:150])
        return cleaned[:maxn]

    out["risks"] = _str_list(a.get("risks"), 4, "risks")
    out["highlights"] = _str_list(a.get("highlights"), 4, "highlights")

    # -- 评分依据（规则引擎生成，原样保留） --
    if isinstance(a.get("score_reasons"), dict):
        out["score_reasons"] = {
            str(k): [str(x)[:200] for x in (v if isinstance(v, list) else [])]
            for k, v in a["score_reasons"].items()
        }
    if a.get("score_engine"):
        out["score_engine"] = str(a["score_engine"])[:40]
    if isinstance(a.get("score_missing"), list):
        out["score_missing"] = [str(x)[:30] for x in a["score_missing"]][:12]

    # -- 标准化原始指标（ROE / 营收增速 等，供「象限定位」散点图取数）--
    #    注意：这里必须显式透传，否则 metrics_raw 会在校验环节被丢掉，
    #    导致散点图永远拿不到数据（历史 bug）。
    _mraw = a.get("metrics_raw")
    if isinstance(_mraw, dict):
        _mraw_clean = {}
        for _k, _v in list(_mraw.items())[:40]:
            _num = _safe_num(_v)
            if _num is not None:
                _mraw_clean[str(_k)[:30]] = _num
        if _mraw_clean:
            out["metrics_raw"] = _mraw_clean
    if a.get("score_source"):
        out["score_source"] = str(a["score_source"])[:60]

    return out, issues


def render_score_card(label, value):
    if value is None:
        st.metric(label, "数据不足")
    else:
        st.metric(label, f"{value} / 10")
        st.progress(min(max(value / 10.0, 0.0), 1.0))


# ============ 图表与解读（雷达 / 圆环 / 横向对比 / 自动解读） ============
import os as _os
_MPL_DIR = _os.environ.get("MPLCONFIGDIR") or _os.path.join(_os.path.expanduser("~"), ".workbuddy", "matplotlib")
_os.makedirs(_MPL_DIR, exist_ok=True)
_os.environ["MPLCONFIGDIR"] = _MPL_DIR
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from io import BytesIO

# 亮色冷蓝白主题色板（与全站一致）
_PAL = {
    "purple": "#1E40AF",   # 兼容旧引用（v3 起语义为「深蓝」）
    "blue":   "#1E40AF",   # 品牌主色：深蓝（科技投研感）
    "gold":   "#D9A441",   # 资本金
    "up":     "#DC2626",   # 涨
    "down":   "#16A34A",   # 跌
    "neutral":"#94A3B8",
    "card":   "#FFFFFF",   # matplotlib 卡片底色（亮色版）
    "text":   "#334155",   # matplotlib 正文色
    "title":  "#0B1F3A",   # matplotlib 标题色
}

# matplotlib 亮色全局设置（含中文字体自动探测）
plt.rcParams.update({
    "figure.facecolor":  _PAL["card"],
    "axes.facecolor":    _PAL["card"],
    "axes.edgecolor":    "#D6E0EC",   # 浅色边框
    "axes.labelcolor":   _PAL["text"],
    "axes.titlecolor":   _PAL["title"],
    "xtick.color":       _PAL["text"],
    "ytick.color":       _PAL["text"],
    "text.color":        _PAL["text"],
    "grid.color":        "#E2E8F0",   # 浅色网格
    "grid.alpha":        0.6,
    "font.size":         10,
})
# 中文字体回退链：Streamlit Cloud 容器常用 Noto Sans CJK，
# Windows 本地有 Microsoft YaHei；任一可用即可
import matplotlib.font_manager as _fm
_CJK_CANDIDATES = [
    "Microsoft YaHei", "PingFang SC", "SimHei", "Heiti SC",
    "Noto Sans CJK SC", "Noto Sans SC", "WenQuanYi Micro Hei",
    "WenQuanYi Zen Hei", "Source Han Sans SC", "Source Han Sans CN",
    "Arial Unicode MS", "Sarasa Gothic SC",
]
_available = {f.name for f in _fm.fontManager.ttflist}
_picked = next((n for n in _CJK_CANDIDATES if n in _available), None)
if _picked:
    plt.rcParams["font.sans-serif"] = [_picked] + plt.rcParams.get("font.sans-serif", [])
    plt.rcParams["font.family"] = ["sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False
else:
    # 兜底：尝试用 Noto Sans CJK 路径直接注册（部分 Streamlit 镜像字体未入管理器）
    _try_paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for _p in _try_paths:
        if _os.path.exists(_p):
            try:
                _fm.fontManager.addfont(_p)
                _picked = _fm.FontProperties(fname=_p).get_name()
                plt.rcParams["font.sans-serif"] = [_picked]
                plt.rcParams["axes.unicode_minus"] = False
                break
            except Exception:
                continue
# 标记是否找到中文字体（供提示条使用）
_HAS_CJK = _picked is not None


def _fig_to_png_bytes(fig, dpi=140) -> bytes:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def show_png(png: bytes, alt: str = "chart") -> bool:
    """把 PNG 字节以内联 base64 的方式画到页面上。

    为什么不用 st.image：st.image(bytes) 会走 Streamlit 的媒体文件端点
    (/media/<hash>.png)，该请求在部分部署环境下会被浏览器扩展 / 代理 /
    会话校验拦截，表现为页面上只剩一个碎图占位符（图表等于消失）。
    内联 data-URI 把图片直接写进 DOM，不产生任何额外网络请求，最稳。
    """
    if not png:
        return False
    try:
        b64 = base64.b64encode(png).decode("ascii")
    except Exception:
        return False
    st.markdown(
        f'<div class="chart-frame">'
        f'<img alt="{alt}" src="data:image/png;base64,{b64}" '
        f'style="width:100%;height:auto;display:block;border-radius:12px;">'
        f'</div>',
        unsafe_allow_html=True,
    )
    return True


def render_score_radar(scores: dict, total: float | None = None) -> bytes | None:
    """四维评分雷达图（matplotlib, 输出 PNG 字节）。"""
    items = [k for k, v in (scores or {}).items() if isinstance(v, (int, float))]
    if len(items) < 3:
        return None
    vals = [float(scores[k]) for k in items]
    angles = np.linspace(0, 2 * np.pi, len(items), endpoint=False).tolist()
    vals_c = vals + [vals[0]]
    angles_c = angles + [angles[0]]

    fig = plt.figure(figsize=(4.6, 4.2), facecolor=_PAL["card"])
    ax = fig.add_subplot(111, polar=True)
    ax.set_facecolor(_PAL["card"])
    ax.set_theta_offset(np.pi / 2)
    ax.set_theta_direction(-1)
    ax.set_ylim(0, 10)
    ax.set_yticks([2, 4, 6, 8, 10])
    ax.set_yticklabels(["2", "4", "6", "8", "10"], color=_PAL["neutral"], fontsize=8)
    ax.set_xticks(angles)
    ax.set_xticklabels(items, color=_PAL["text"], fontsize=11, fontweight="bold")
    ax.grid(color="#E2E8F0", alpha=0.7)
    ax.spines["polar"].set_color("#D6E0EC")

    ax.plot(angles_c, vals_c, color=_PAL["purple"], linewidth=2.2, marker="o",
            markersize=7, markerfacecolor=_PAL["gold"], markeredgecolor=_PAL["purple"])
    ax.fill(angles_c, vals_c, color=_PAL["purple"], alpha=0.22)

    if total is not None:
        ax.set_title(f"四维评分雷达 · 综合 {total:.1f} / 10",
                     color=_PAL["title"], fontsize=12, pad=18, fontweight="bold")
    else:
        ax.set_title("四维评分雷达", color=_PAL["title"], fontsize=12, pad=18, fontweight="bold")
    return _fig_to_png_bytes(fig)


def render_metric_donut(chart_data: list) -> bytes | None:
    """关键指标圆环图（按指标类型分组：金额 vs 比率）。"""
    rows = [(d.get("item", ""), d.get("value", 0))
            for d in (chart_data or [])
            if isinstance(d.get("value"), (int, float))]
    if len(rows) < 2:
        return None
    # 取绝对值用于面积分配
    sizes = [abs(v) for _, v in rows]
    labels = [n for n, _ in rows]

    fig, ax = plt.subplots(figsize=(5.0, 3.8), facecolor=_PAL["card"])
    palette = [_PAL["purple"], _PAL["gold"], _PAL["up"], _PAL["down"], _PAL["neutral"], "#7B6AFF"]
    colors = (palette * 3)[: len(rows)]
    wedges, _, autotexts = ax.pie(
        sizes, labels=None, colors=colors, startangle=90,
        wedgeprops=dict(width=0.42, edgecolor=_PAL["card"], linewidth=2),
        autopct=lambda p: f"{p:.0f}%" if p >= 8 else "",
        textprops=dict(color=_PAL["text"], fontsize=10),
    )
    for t in autotexts:
        t.set_color(_PAL["title"])
        t.set_fontweight("bold")

    # 中心写最大值
    max_idx = int(np.argmax(sizes))
    ax.text(0, 0.08, labels[max_idx], ha="center", va="center",
            color=_PAL["gold"], fontsize=11, fontweight="bold")
    ax.text(0, -0.18, f"{rows[max_idx][1]:,.1f}", ha="center", va="center",
            color=_PAL["title"], fontsize=12, fontweight="bold")
    ax.set_title("关键财务指标占比", color=_PAL["title"], fontsize=12, pad=10, fontweight="bold")
    # 图例放右侧
    ax.legend(wedges, labels, loc="center left",
              bbox_to_anchor=(1.02, 0.5), frameon=False,
              labelcolor=_PAL["text"], fontsize=9)
    return _fig_to_png_bytes(fig)


def render_horizontal_bar(chart_data: list) -> bytes | None:
    """关键指标横向柱状图（替代垂直柱，更适合长指标名）。"""
    rows = [(d.get("item", ""), d.get("value", 0))
            for d in (chart_data or [])
            if isinstance(d.get("value"), (int, float))]
    if len(rows) < 2:
        return None
    rows.sort(key=lambda x: x[1])
    names = [r[0] for r in rows]
    vals = [r[1] for r in rows]
    # 颜色：金/紫交替标识最大值
    colors = [_PAL["purple"]] * len(rows)
    colors[-1] = _PAL["gold"]  # 最大的用金色
    if len(rows) > 1:
        colors[0] = _PAL["neutral"]  # 最小的用灰色

    fig, ax = plt.subplots(figsize=(5.0, max(2.6, 0.42 * len(rows) + 1.0)),
                           facecolor=_PAL["card"])
    bars = ax.barh(names, vals, color=colors, edgecolor="none", height=0.7)
    ax.set_xlabel("数值（亿元）", color=_PAL["neutral"], fontsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#D6E0EC")
    ax.spines["bottom"].set_color("#D6E0EC")
    ax.tick_params(axis="y", colors=_PAL["text"], labelsize=10)
    ax.tick_params(axis="x", colors=_PAL["neutral"], labelsize=9)
    for bar, v in zip(bars, vals):
        ax.text(bar.get_width() + max(vals) * 0.01, bar.get_y() + bar.get_height() / 2,
                f"{v:,.1f}", va="center", ha="left",
                color=_PAL["text"], fontsize=9)
    ax.set_xlim(0, max(vals) * 1.18)
    ax.set_title("关键指标横向对比（金色 = 最高 / 灰色 = 最低）",
                 color=_PAL["title"], fontsize=11, pad=10, fontweight="bold")
    ax.grid(axis="x", linestyle="--", alpha=0.4)
    return _fig_to_png_bytes(fig)


# ---- 散点图行业基准线（分数与象限必须「同业对标」，不能全行业一把尺）----
# 逻辑：银行天然高杠杆、薄利差，ROE 11% 已属优秀；新能源是高增长赛道，
#      增速基准理应更高。用同一根 15%/10% 的尺子会给银行"误判成瘦狗"。
_SCATTER_ROE_LINE = 15.0    # 兜底：通用 ROE 优秀线（长期 >15% 优秀）
_SCATTER_GROW_LINE = 10.0   # 兜底：通用营收增速稳健线（10-20% 稳健）

_INDUSTRY_BASE = {
    "通用":   (15.0, 10.0),   # 通用制造/多元
    "消费":   (18.0, 8.0),    # 高端消费 ROE 高、增速稳
    "金融":   (11.0, 6.0),    # 银行/保险：ROE 11% 已属优秀，增速锚定息差
    "新能源": (12.0, 20.0),   # 高增长赛道，增速基准上调
    "医药":   (14.0, 12.0),   # 研发驱动，成长性权重高
    "互联网": (14.0, 15.0),   # 平台型，增长与回报并重
}


def _industry_base(industry: str) -> tuple:
    """按行业返回 (ROE 优秀线, 营收增速稳健线)；未收录行业走通用兜底。"""
    return _INDUSTRY_BASE.get(industry or "通用", (_SCATTER_ROE_LINE, _SCATTER_GROW_LINE))


def render_scatter_roe_growth(roe, revenue_yoy, name: str = "本公司",
                              industry: str = "通用") -> bytes | None:
    """ROE × 营收增速 四象限定位散点图（明星 / 现金牛 / 问题 / 瘦狗）。

    X 轴 = ROE(%)，Y 轴 = 营收同比(%)。虚线随【行业】调整：
    银行 ROE 线降到 11%（否则优秀的银行会被误判为瘦狗），
    新能源增速线升到 20%（否则高增长赛道全成"问题型"）。
    """
    try:
        roe = float(roe)
        revenue_yoy = float(revenue_yoy)
    except (TypeError, ValueError):
        return None
    roe_line, grow_line = _industry_base(industry)

    fig, ax = plt.subplots(figsize=(5.6, 4.4), facecolor=_PAL["card"])
    ax.set_facecolor(_PAL["card"])

    x_lo, x_hi = min(0.0, roe) - 5.0, max(30.0, roe) + 6.0
    y_lo, y_hi = min(-10.0, revenue_yoy) - 8.0, max(40.0, revenue_yoy) + 8.0

    # 四象限底色（淡色块，区分区域）
    ax.fill_between([roe_line, x_hi], grow_line, y_hi, color=_PAL["up"], alpha=0.07)
    ax.fill_between([x_lo, roe_line], grow_line, y_hi, color=_PAL["purple"], alpha=0.08)
    ax.fill_between([roe_line, x_hi], y_lo, grow_line, color=_PAL["gold"], alpha=0.08)
    ax.fill_between([x_lo, roe_line], y_lo, grow_line, color=_PAL["neutral"], alpha=0.06)

    # 基准线（行业自适应）
    ax.axvline(roe_line, color=_PAL["neutral"], linestyle="--", linewidth=1.1, alpha=0.85)
    ax.axhline(grow_line, color=_PAL["neutral"], linestyle="--", linewidth=1.1, alpha=0.85)
    ax.text(roe_line + 0.4, y_hi - 1.2, f"ROE={roe_line:g}%",
            color=_PAL["neutral"], fontsize=8, va="top")
    ax.text(x_lo + 0.6, grow_line + 0.8, f"增速={grow_line:g}%",
            color=_PAL["neutral"], fontsize=8, ha="left", va="bottom")

    # 象限文字标注
    xm_r, xm_l = (roe_line + x_hi) / 2, (x_lo + roe_line) / 2
    ym_t, ym_b = y_hi - (y_hi - grow_line) * 0.32, y_lo + (grow_line - y_lo) * 0.32
    ax.text(xm_r, ym_t, "明星\n高盈利·高增长", color=_PAL["up"], fontsize=9.5,
            ha="center", va="center", fontweight="bold", alpha=0.9)
    ax.text(xm_l, ym_t, "问题\n低盈利·高增长", color="#1E40AF", fontsize=9.5,
            ha="center", va="center", fontweight="bold", alpha=0.9)
    ax.text(xm_r, ym_b, "现金牛\n高盈利·低增长", color=_PAL["gold"], fontsize=9.5,
            ha="center", va="center", fontweight="bold", alpha=0.9)
    ax.text(xm_l, ym_b, "瘦狗\n低盈利·低增长", color=_PAL["neutral"], fontsize=9.5,
            ha="center", va="center", fontweight="bold", alpha=0.9)

    # 本公司点位（金心紫边 + 标注）
    ax.scatter([roe], [revenue_yoy], s=330, color=_PAL["gold"],
               edgecolor=_PAL["purple"], linewidth=2.6, zorder=6)
    ax.annotate(f"{name}\nROE {roe:.1f}% · 增速 {revenue_yoy:+.1f}%",
                xy=(roe, revenue_yoy), xytext=(10, 12), textcoords="offset points",
                color=_PAL["title"], fontsize=9.5, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.32", fc="#F1F5F9", ec=_PAL["purple"], lw=1.0, alpha=0.95))

    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(y_lo, y_hi)
    ax.set_xlabel("ROE 净资产收益率（%）", color=_PAL["text"], fontsize=9.5)
    ax.set_ylabel("营收同比增速（%）", color=_PAL["text"], fontsize=9.5)
    ax.tick_params(colors=_PAL["text"], labelsize=8.5)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for _s in ("left", "bottom"):
        ax.spines[_s].set_color("#D6E0EC")
    ax.grid(linestyle=":", alpha=0.25)
    ax.set_title(f"ROE × 营收增速 · 象限定位（{industry}基准）", color=_PAL["title"],
                 fontsize=11.5, pad=10, fontweight="bold")
    return _fig_to_png_bytes(fig, dpi=150)


def build_quadrant_insight(roe, revenue_yoy, name: str = "该公司",
                           industry: str = "通用") -> str:
    """四象限定位自动解读（HTML，基准线按行业自适应）。"""
    try:
        roe = float(roe)
        revenue_yoy = float(revenue_yoy)
    except (TypeError, ValueError):
        return ""
    roe_line, grow_line = _industry_base(industry)
    hi_roe = roe >= roe_line
    hi_gro = revenue_yoy >= grow_line

    if hi_roe and hi_gro:
        quad, cls, desc = "明星型（高盈利 · 高增长）", "up", (
            "盈利与成长双优，处于扩张与回报同向的稀缺区间，通常是市场愿意给予较高估值的形态。"
        )
    elif hi_roe and not hi_gro:
        quad, cls, desc = "现金牛型（高盈利 · 低增长）", "neutral", (
            "盈利扎实但增长放缓，现金流与分红能力是主要看点，需关注增长瓶颈是否来自行业天花板。"
        )
    elif (not hi_roe) and hi_gro:
        quad, cls, desc = "问题型（低盈利 · 高增长）", "up", (
            "增长快但资本回报偏低，应排查是否「增收不增利」——高杠杆扩张或低效投入往往会吞噬股东回报。"
        )
    else:
        quad, cls, desc = "瘦狗型（低盈利 · 低增长）", "down", (
            "盈利与增长双弱，需重点排查主业竞争力、资产质量与是否存在减值风险。"
        )

    parts = [
        f"• 定位：<b>{name}</b> 落在 <span class=\"{cls}\">{quad}</span> 象限"
        f"（ROE <b>{roe:.1f}%</b> vs {industry}优秀线 {roe_line:g}%；"
        f"营收增速 <b>{revenue_yoy:+.1f}%</b> vs {industry}稳健线 {grow_line:g}%）。",
        f"• 含义：{desc}",
        f"• 对标口径：象限基准线随【行业知识库】切换（当前「{industry}」= ROE {roe_line:g}% / 增速 {grow_line:g}%）。"
        f"银行业天然高杠杆薄利差，因此 ROE 门槛低于消费；新能源属高增长赛道，增速门槛高于传统制造——"
        f"<b>同业对标才是有效的财务判断</b>。",
    ]
    return ('<div class="insight-card">📖 <b>象限解读</b><br>' + "<br>".join(parts) + "</div>")


def build_chart_insight(chart_data: list) -> str:
    """从柱状图数据生成 3-4 条自动解读（HTML, 涨跌用色）。"""
    rows = [(d.get("item", ""), float(d.get("value", 0)))
            for d in (chart_data or [])
            if isinstance(d.get("value"), (int, float))]
    if not rows:
        return '<div class="insight-card">📊 暂无足够数据可解读。</div>'
    rows_sorted = sorted(rows, key=lambda x: x[1], reverse=True)
    total = sum(v for _, v in rows_sorted if v > 0)
    max_name, max_val = rows_sorted[0]
    min_name, min_val = rows_sorted[-1]
    max_share = (max_val / total * 100) if total else 0
    lines = [
        f"• <b>{max_name}</b> 数值最高（<b>{max_val:,.2f}</b>），"
        f"占总指标金额的 <b>{max_share:.1f}%</b>，是该公司当前阶段的核心量级。",
        f"• <b>{min_name}</b> 数值最低（{min_val:,.2f}），"
        f"与最高项相差 <b>{max_val - min_val:,.2f}</b>，指标结构呈明显梯度。",
    ]
    # 中位对比
    if len(rows_sorted) >= 3:
        mid = rows_sorted[len(rows_sorted) // 2]
        lines.append(
            f"• 中位项 <b>{mid[0]}</b>（{mid[1]:,.2f}）居于 {len(rows_sorted)} 项中第 "
            f"{len(rows_sorted) // 2 + 1} 位，分布偏度可通过最大/最小比 "
            f"<b>{(max_val / min_val) if min_val else 0:.1f}×</b> 看出。"
        )
    if len(rows_sorted) >= 2 and total > 0:
        lines.append(
            f"• 本期可量化指标合计 <b>{total:,.2f}</b> 亿元（仅含已提取金额类项），"
            f"为后续趋势分析提供基线。"
        )
    body = "<br>".join(lines)
    return f'<div class="insight-card">📖 <b>图表解读</b>（自动生成，基于本期数据）<br>{body}</div>'


def build_trend_insight(trend_rows: list) -> str:
    """多年趋势自动解读（涨跌/峰值/谷值/累计涨幅）。"""
    rows = [r for r in (trend_rows or []) if isinstance(r.get("revenue"), (int, float))]
    if len(rows) < 2:
        return '<div class="insight-card">📈 趋势数据不足（需 ≥2 个年报期）。</div>'
    rev = [float(r["revenue"]) for r in rows]
    periods = [r.get("period", "") for r in rows]
    npr = [float(r.get("net_profit") or 0) for r in rows]

    # 营收复合增速
    first, last = rev[0], rev[-1]
    yrs = max(len(rev) - 1, 1)
    cagr = ((last / first) ** (1 / yrs) - 1) * 100 if first > 0 else 0
    # 峰值/谷值
    peak_i, trough_i = int(np.argmax(rev)), int(np.argmin(rev))
    # 同比波动
    if len(rev) >= 2:
        yoy = (rev[-1] - rev[-2]) / rev[-2] * 100 if rev[-2] else 0
        yoy_cls = "up" if yoy > 0 else ("down" if yoy < 0 else "neutral")
        yoy_text = f'<span class="{yoy_cls}">{"▲" if yoy > 0 else ("▼" if yoy < 0 else "─")} {abs(yoy):.1f}%</span>'
    else:
        yoy_text = "—"

    parts = [
        f"• 营收区间 <b>{min(rev):,.1f} – {max(rev):,.1f}</b> 亿元，"
        f"从 <b>{periods[0]}</b> 到 <b>{periods[-1]}</b>，"
        f"复合增速 <b>{cagr:+.1f}%</b>，整体{'扩张' if cagr > 0 else '收缩'}。",
        f"• 最新一期同比 <b>{yoy_text}</b>，"
        f"峰值出现在 <b>{periods[peak_i]}</b>（{rev[peak_i]:,.1f} 亿元），"
        f"谷值在 <b>{periods[trough_i]}</b>（{rev[trough_i]:,.1f} 亿元）。",
    ]
    if any(n > 0 for n in npr):
        # 净利润率演变
        margins = [n / r * 100 if r else 0 for n, r in zip(npr, rev)]
        m_first, m_last = margins[0], margins[-1]
        dm = m_last - m_first
        d_cls = "up" if dm > 0 else ("down" if dm < 0 else "neutral")
        parts.append(
            f"• 净利润率由 <b>{m_first:.1f}%</b> 演变至 <b>{m_last:.1f}%</b>，"
            f"期间<span class=\"{d_cls}\">{dm:+.1f} pp</span>，"
            f"盈利质量{'改善' if dm > 0 else ('承压' if dm < 0 else '持平')}。"
        )
    body = "<br>".join(parts)
    return f'<div class="insight-card">📖 <b>趋势解读</b>（基于年报口径）<br>{body}</div>'


def build_radar_insight(scores: dict) -> str:
    """四维评分雷达图自动解读。"""
    items = [(k, float(v)) for k, v in (scores or {}).items() if isinstance(v, (int, float))]
    if len(items) < 2:
        return '<div class="insight-card">📊 评分数据不足。</div>'
    items_sorted = sorted(items, key=lambda x: x[1], reverse=True)
    strong, weak = items_sorted[0], items_sorted[-1]
    avg = sum(v for _, v in items) / len(items)
    gap = strong[1] - weak[1]
    parts = [
        f"• <b>{strong[0]}</b> 为最强维度（<b>{strong[1]:.1f}</b> 分），"
        f"<b>{weak[0]}</b> 为最弱维度（{weak[1]:.1f} 分），极差 <b>{gap:.1f}</b> 分。",
        f"• 四维均值 <b>{avg:.1f}</b> 分，"
        + ("整体表现稳健" if avg >= 7 else ("处于中游" if avg >= 5 else "整体偏弱")) + "。",
    ]
    if gap >= 4:
        parts.append(
            f"• 维度差异显著（≥4 分），建议优先关注「{weak[0]}」的拖累因素——"
            f"这是该 Agent 在评分依据中提示「主要问题」的位置。"
        )
    body = "<br>".join(parts)
    return f'<div class="insight-card">📖 <b>评分解读</b><br>{body}</div>'


def render_rag_hits_panel(rag_hits: list) -> str:
    """RAG 检索过程可视化：Top-K 章节、相似度、页码、摘要（HTML 字符串）。"""
    if not rag_hits:
        return ""
    rows = []
    for i, h in enumerate(rag_hits[:8], 1):
        # 兼容多种字段命名
        page = h.get("page") or h.get("page_number") or h.get("metadata", {}).get("page") or "?"
        sim = h.get("score") or h.get("similarity") or 0
        try:
            sim = float(sim)
        except (TypeError, ValueError):
            sim = 0.0
        if sim >= 0.75:
            sim_cls = "rag-sim-high"
        elif sim >= 0.5:
            sim_cls = "rag-sim-mid"
        else:
            sim_cls = "rag-sim-low"
        text = (h.get("text") or h.get("content") or "").replace("\n", " ").strip()
        text = text[:160] + ("…" if len(text) > 160 else "")
        rows.append(
            f'<div class="rag-hit">'
            f'<span class="rag-rank">#{i}</span>'
            f'<span class="rag-page">📄 年报第 {page} 页</span>'
            f'&nbsp;|&nbsp;<span class="{sim_cls}">相似度 {sim:.2f}</span>'
            f'<br><span style="color:#CBD5E1;font-size:12px;line-height:1.6;">{text}</span>'
            f'</div>'
        )
    return (
        '<div style="margin: 10px 0 6px 0;color:#D9A441;font-weight:700;font-size:14px;">'
        '📚 RAG 检索过程（Top-{} 相关章节 · 按相似度排序）</div>'
        + "".join(rows)
    )


# ============ 在线取数：UI 组件（可复用） ============
def online_fetch_panel(key_prefix: str, default_name: str = ""):
    """
    渲染「在线取数」面板，返回 (summary_text, company_name, extra_info)。
    - 输入股票代码/名称 → 自动拉取行情 + 多期财务指标 → 生成分析文本
    - 可从报告列表一键下载年报 PDF 并解析
    结果存入 session_state[key_prefix + "_online"], 避免与手动输入冲突。
    """
    st.markdown("##### 🌐 在线取数（自动从巨潮资讯网 + 东方财富获取）")
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        # 热门股票下拉框：不用记代码，直接选；也支持手动输入
        _POPULAR_STOCKS = [
            ("— 手动输入 —", ""),
            # 消费
            ("贵州茅台 (600519)", "600519"),
            ("五粮液 (000858)", "000858"),
            ("伊利股份 (600887)", "600887"),
            ("中国中免 (601888)", "601888"),
            ("海天味业 (603288)", "603288"),
            # 金融
            ("招商银行 (600036)", "600036"),
            ("兴业银行 (601166)", "601166"),
            ("平安银行 (000001)", "000001"),
            ("工商银行 (601398)", "601398"),
            ("中国人寿 (601628)", "601628"),
            ("中国平安 (601318)", "601318"),
            # 科技 / 医药
            ("宁德时代 (300750)", "300750"),
            ("比亚迪 (002594)", "002594"),
            ("立讯精密 (002475)", "002475"),
            ("海康威视 (002415)", "002415"),
            ("京东方A (000725)", "000725"),
            ("恒瑞医药 (600276)", "600276"),
            ("迈瑞医疗 (300760)", "300760"),
            # 制造 / 能源
            ("中国神华 (601088)", "601088"),
            ("陕西煤业 (601225)", "601225"),
            ("三一重工 (600031)", "600031"),
            ("隆基绿能 (601012)", "601012"),
            ("长江电力 (600900)", "600900"),
        ]
        # 1. 热门股票下拉框
        _choice = st.selectbox(
            "📋 热门股票（点选无需查代码）",
            options=[s[0] for s in _POPULAR_STOCKS],
            index=0,
            key=f"{key_prefix}_pickle",
            help="直接选择一家公司，代码会自动填到下方输入框；也可手动输入任意 A 股代码。",
        )
        _code = next((s[1] for s in _POPULAR_STOCKS if s[0] == _choice), "")
        # 2. 手动输入框（默认填入下拉选中的代码；用 session_state 同步确保下拉选完立即生效）
        if _code:
            st.session_state[f"{key_prefix}_kw"] = _code
        kw = st.text_input(
            "股票代码或名称（可编辑；确认后点右侧 🔍 获取数据）",
            value=st.session_state.get(f"{key_prefix}_kw", _code),
            placeholder="例如：600519 或 贵州茅台",
            key=f"{key_prefix}_kw",
        )
    with c2:
        periods = st.selectbox("报告期数", [1, 2, 3, 4, 6, 8], index=2, key=f"{key_prefix}_periods")
    with c3:
        st.write("")
        st.write("")
        do_fetch = st.button("🔍 获取数据", use_container_width=True, key=f"{key_prefix}_fetch")

    if do_fetch and kw.strip():
        # 取数之前先把该面板对应的旧研报/旧分析/旧 PDF 文本清掉，
        # 避免"切到新公司后研报还显示上一家"的问题
        st.session_state[f"{key_prefix}_pdftext"] = ""
        st.session_state[f"{key_prefix}_reports"] = []
        for _k in (f"{key_prefix}_pdf_path", f"{key_prefix}_pdf_year",
                   f"{key_prefix}_pdf_title", f"{key_prefix}_rag_status"):
            st.session_state.pop(_k, None)
        if key_prefix == "single":
            st.session_state.pop("report", None)
            st.session_state.pop("analysis", None)
            st.session_state["messages"] = []
        with st.spinner("正在联网获取数据……"):
            hits = ds.search_stock(kw.strip())
            if not hits:
                st.error("未找到该股票，请检查代码或名称（支持 A 股 6 位代码）。")
            else:
                code = hits[0]["code"]
                name = hits[0]["name"]
                text = ds.build_summary_text(code, name=name, periods=periods)
                if not text:
                    st.error("数据获取失败（可能是网络受限或该股票无公开数据），可改用手动粘贴。")
                else:
                    st.session_state[f"{key_prefix}_online"] = text
                    st.session_state[f"{key_prefix}_online_name"] = name
                    st.session_state[f"{key_prefix}_online_code"] = code
                    st.session_state[f"{key_prefix}_online_org"] = hits[0].get("orgId", "")
                    # 自动匹配行业知识库（不再直接修改已渲染的 industry radio，
                    # 而是保存到 session_state 由顶部 radio 在下一轮渲染时读取）
                    gi = ds.guess_industry(name)
                    if gi:
                        st.session_state[f"{key_prefix}_auto_industry"] = gi

    text = st.session_state.get(f"{key_prefix}_online", "")
    name = st.session_state.get(f"{key_prefix}_online_name", "")
    code = st.session_state.get(f"{key_prefix}_online_code", "")
    org = st.session_state.get(f"{key_prefix}_online_org", "")

    if text:
        auto_ind = st.session_state.get(f"{key_prefix}_auto_industry")
        tip = f"✅ 已获取 **{name}（{code}）** 数据，共 {len(text)} 字"
        if auto_ind:
            tip += f" · 建议行业：**{auto_ind}**"
        st.success(tip)
        with st.expander("查看获取到的原始数据", expanded=False):
            st.markdown(text)

        # 年报 PDF 一键获取：默认展开，且解析成功后保持展开
        pdf_done = bool(st.session_state.get(f"{key_prefix}_pdftext"))
        _rag_ok = bool(st.session_state.get(f"{key_prefix}_rag_status", {}).get("ok"))
        with st.expander("📄 一键获取并在云端解析年报 PDF（可选，数据更全）", expanded=not pdf_done):
            rtype = st.radio("报告类型", ["年报", "半年报", "一季报", "三季报"], horizontal=True, key=f"{key_prefix}_rtype")
            if st.button("获取报告列表", key=f"{key_prefix}_listbtn"):
                reports = ds.list_reports(code, org, rtype, limit=8)
                st.session_state[f"{key_prefix}_reports"] = reports
                if not reports:
                    st.warning("未找到该类型的完整版报告，可换一个报告类型，或改用手动上传 PDF。")
            reports = st.session_state.get(f"{key_prefix}_reports", [])
            if reports:
                st.caption("📋 已自动过滤摘要版 / 英文版，仅保留完整报告（内容厚、含财报明细与风险章节）。")
                opts = [f"{r['date']} {r['title']}" for r in reports]
                idx = st.selectbox("选择报告", range(len(opts)), format_func=lambda i: opts[i], key=f"{key_prefix}_rsel")
                if st.button("下载并解析该报告", key=f"{key_prefix}_dlbtn"):
                    with st.spinner("正在下载并解析 PDF（大文件约 10-20 秒）……"):
                        pdf = ds.download_pdf(reports[idx]["url"])
                        if not pdf:
                            st.error("PDF 下载失败，请稍后重试或改用手动上传。")
                        else:
                            ptxt = extract_pdf_bytes(pdf)
                            if not ptxt or len(ptxt) < 200:
                                st.warning("该 PDF 提取不到文字（可能是扫描件），请改用手动粘贴。")
                            else:
                                # 保存 PDF 到临时文件（供 RAG 按页建库）
                                _yr = re.sub(r"\D", "", reports[idx].get("date") or "")[:4]
                                _pdf_path = Path(tempfile.gettempdir()) / f"{code}_{_yr}_{rtype}.pdf"
                                try:
                                    _pdf_path.write_bytes(pdf)
                                except Exception:
                                    _pdf_path = None
                                st.session_state[f"{key_prefix}_pdftext"] = ptxt
                                st.session_state[f"{key_prefix}_pdf_title"] = reports[idx]["title"]
                                st.session_state[f"{key_prefix}_pdf_year"] = _yr
                                st.session_state[f"{key_prefix}_pdf_path"] = str(_pdf_path) if _pdf_path else ""
                                st.session_state.pop(f"{key_prefix}_rag_status", None)
                                st.success(f"✅ 已解析《{reports[idx]['title']}》，提取 {len(ptxt)} 字。")
                                # 【RAG 接通关键步】立即按页切片并构建向量索引
                                if (
                                    rag is not None
                                    and _pdf_path
                                    and code and _yr
                                    and st.session_state.get("use_dual_model", True)
                                ):
                                    import time as _time
                                    _t0 = _time.time()
                                    # 实时进度条：40 万字完整年报约 100+ 批 Embedding，
                                    # 静态 spinner 会像卡死；逐批回报消除"没反应"的观感
                                    _pbar = st.progress(
                                        0.0, text="正在构建年报向量索引（8 路并发 Embedding）……"
                                    )

                                    def _pcb(done: int, total: int):
                                        _pbar.progress(
                                            min(done / max(total, 1), 1.0),
                                            text=f"正在构建年报向量索引… 批次 {done}/{total}"
                                                 f"（完整年报约需 40-80 秒，单批失败会自动重试，请勿关闭页面）",
                                        )

                                    _st = rag.build_index_from_pdf(_pdf_path, code, _yr, progress_cb=_pcb)
                                    _pbar.empty()
                                    log_event(
                                        "rag_build", ok=bool(_st.get("ok")),
                                        dur=_time.time() - _t0,
                                        extra=f"chunks={_st.get('chunks', 0)}",
                                    )
                                    st.session_state[f"{key_prefix}_rag_status"] = _st
                                    if _st.get("ok"):
                                        st.success(
                                            f"📚 RAG 索引构建成功：{_st['chunks']} 个文本块 · "
                                            f"覆盖 {_st.get('pages', 0)} 页，后续分析与追问将自动检索引用（含页码）。"
                                        )
                                        if "⚠️" in _st.get("msg", ""):
                                            st.warning(_st["msg"])
                                    else:
                                        st.warning(f"RAG 索引未构建：{_st.get('msg', '未知原因')}（分析仍可用年报开头文本）")

        # RAG 状态提示
        if _rag_ok:
            _st_ = st.session_state.get(f"{key_prefix}_rag_status", {})
            st.caption(
                f"📚 年报向量库已就绪：{_st_.get('chunks', 0)} chunks · "
                f"{_st_.get('pages', 0)} 页 · 分析/研报/追问将按问题检索相关章节并标注页码"
            )

    pdftext = st.session_state.get(f"{key_prefix}_pdftext", "")
    return text, name, pdftext

# ============ 顶部横幅 + 侧边栏 ============
st.markdown(
    """
    <div class="hero">
        <h1>📈 金融智能投研 <span class="gold">Agent</span></h1>
        <p>输入股票代码即可自动取数，一键完成结构化分析、四维评分、趋势图表与研报生成</p>
        <p class="tagline">在线取数（巨潮+东财）· 年报 PDF 解析 · 双公司对比 · 六大行业知识库 · 多轮追问</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    # 品牌头（金色描边卡片，替代裸标题）
    st.markdown(
        '<div class="sb-brand">📊 金融智能投研 Agent'
        '<span class="sb-sub">规则评分引擎 · RAG 按页检索 · 双模型协同</span></div>',
        unsafe_allow_html=True,
    )

    # 快速上手（卡片）
    st.markdown(
        '<div class="sb-card"><div class="sb-title">🧭 快速上手</div>'
        "<ol>"
        "<li>选择行业知识库</li>"
        "<li>输入股票代码（如 600519）</li>"
        "<li>点「🔍 获取数据」自动取数</li>"
        "<li>点「🚀 开始结构化分析」</li>"
        "</ol></div>",
        unsafe_allow_html=True,
    )

    # 当前版本能力（徽章墙，比 8 行列表紧凑）
    st.markdown(
        '<div class="sb-card"><div class="sb-title">⚙️ 当前版本能力</div>'
        '<span class="sb-chip">🌐 在线取数</span>'
        '<span class="sb-chip">📄 年报解析</span>'
        '<span class="sb-chip">📚 RAG 检索</span>'
        '<span class="sb-chip">四维评分</span>'
        '<span class="sb-chip">📈 趋势图表</span>'
        '<span class="sb-chip">📝 研报导出</span>'
        '<span class="sb-chip">双公司对比</span>'
        '<span class="sb-chip">多轮追问</span>'
        "</div>",
        unsafe_allow_html=True,
    )

    # 双模型开关 (Plan B)
    st.markdown('<div class="sb-title" style="margin:12px 0 2px 2px;">🤖 双模型协同</div>', unsafe_allow_html=True)
    use_dual_model = st.toggle(
        "启用 千问 润色 + RAG",
        value=True,
        help="DeepSeek 出分析 → 千问二次校验并润色。当用户上传了年报 PDF 时，会先做 RAG 检索以提升事实一致性。",
        key="use_dual_model",
    )
    if lq is not None:
        _q_avail = lq.is_available()
        st.caption(("🟢 千问 API 已就绪" if _q_avail else "🟡 千问 API 未配置, 仅走 DeepSeek"))
    else:
        st.caption("🟡 llm_qwen 模块未加载")
    if rag is not None:
        st.caption("📚 RAG 引擎已加载 · Top-K=6")
    else:
        st.caption("🟡 rag_engine 模块未加载")
    st.markdown("---")
    if st.session_state.get("demo_mode"):
        st.markdown("👤 当前访问：**🎤 评委演示模式（免登录）**")
    else:
        st.markdown(f"👤 当前登录：**{APP_USER}**")
    # 额度：数字 + 可视化进度条（金色渐变，用满变红）
    _used = _read_usage()
    _ratio = min(_used / max(DAILY_LIMIT, 1), 1.0)
    _bar_color = "linear-gradient(90deg,#E8B84B,#F5D98B)" if _ratio < 0.8 else "linear-gradient(90deg,#D9662F,#E8B84B)"
    st.markdown(
        f'<div class="sb-quota-head">🔋 今日剩余分析额度 <b>{max(DAILY_LIMIT - _used, 0)} / {DAILY_LIMIT}</b></div>'
        f'<div class="sb-quota-bar"><div style="width:{_ratio * 100:.0f}%;background:{_bar_color};"></div></div>',
        unsafe_allow_html=True,
    )
    if st.button("退出登录", use_container_width=True):
        st.session_state["authenticated"] = False
        st.session_state["demo_mode"] = False
        st.rerun()
    st.markdown(
        '<p class="footer-note">金融智能投研 Agent · 大赛参赛作品</p>',
        unsafe_allow_html=True,
    )

# ---- 行业知识库：主界面横向按钮（不再藏在侧边栏下拉框里） ----
# 默认索引：用户手动选过就沿用；否则取在线取数自动识别的行业
_auto_matched = st.session_state.get("single_auto_industry", "")
_user_selected = st.session_state.get("industry", "")
_default_industry = (
    _user_selected
    if _user_selected in INDUSTRY_KB
    else (_auto_matched if _auto_matched in INDUSTRY_KB else "通用")
)
_default_idx = list(INDUSTRY_KB.keys()).index(_default_industry)

# 如果已经在线取数，标题直接显示公司名称+代码+匹配行业
_single_name = st.session_state.get("single_online_name", "")
_single_code = st.session_state.get("single_online_code", "")
if _single_name and _single_code:
    st.markdown(
        f"##### 📚 行业知识库 —— 当前分析对象：**{_single_name}（{_single_code}）** → **{_default_industry}**\n"
        f"*将自动注入该行业的分析框架与经验法则*"
    )
else:
    st.markdown("##### 📚 行业知识库 —— 请选择待分析公司所属行业（将自动注入该行业的分析框架与经验法则）")

industry = st.radio(
    "行业",
    list(INDUSTRY_KB.keys()),
    index=_default_idx,
    horizontal=True,
    label_visibility="collapsed",
    key="industry",
)
# 如果是自动匹配过来的，提示一下
if _auto_matched and industry == _auto_matched:
    st.caption(f"🤖 已根据在线取数结果自动匹配行业：**{industry}**（如需切换，点击上方按钮即可）")
else:
    st.caption(f"当前行业：**{industry}** · " + INDUSTRY_KB[industry].splitlines()[0].replace("【", "").replace("】", "·").strip(" -"))

def system_prompt_with_kb():
    return BASE_SYSTEM_PROMPT + "\n\n" + INDUSTRY_KB[industry]

tab1, tab2, tab3 = st.tabs(["🏢 单公司深度分析", "⚖️ 双公司对比", "📖 使用说明"])

# ---------------------------------------------------------------
# Tab 1：单公司深度分析
# ---------------------------------------------------------------
with tab1:
    # ---- 数据来源选择 ----
    src_mode = st.radio(
        "数据来源",
        ["🌐 在线自动取数（推荐）", "✍️ 手动粘贴 / 上传"],
        horizontal=True,
        label_visibility="collapsed",
        key="single_src",
    )

    online_text, online_name, online_pdftext = "", "", ""
    uploaded_text = ""
    data_text = ""

    if src_mode.startswith("🌐"):
        online_text, online_name, online_pdftext = online_fetch_panel("single")
        st.markdown("##### ✍️ 或追加手动输入（可选）")
        data_text = st.text_area(
            "粘贴补充的财报摘要（可留空）",
            height=120,
            key="single_text",
        )
    else:
        col1, col2 = st.columns(2)
        with col1:
            data_text = st.text_area(
                "① 粘贴财报摘要（营收、净利润、毛利率、负债等）",
                height=200,
                key="single_text",
            )
        with col2:
            up = st.file_uploader("② 或上传财报文件（支持 PDF / txt / md）", type=["pdf", "txt", "md"], key="single_upload")
            uploaded_text, upload_err = read_upload(up)
            if upload_err:
                st.warning(upload_err)

    # 合并所有数据源
    # 年报全文不再只截开头送模型：开头 3000 字供"公司概况"，
    # 财务报表/风险等章节改由 RAG 按问题检索注入（见分析/研报/追问流程）
    parts = []
    if online_text:
        parts.append(online_text)
    if online_pdftext:
        parts.append("【年报原文 · 开头摘录（全文已入向量库，可按问题检索）】\n" + online_pdftext[:3000])
    if data_text and data_text.strip():
        parts.append(data_text.strip())
    if uploaded_text:
        parts.append(uploaded_text.strip())
    raw_data = "\n\n".join(parts).strip()

    if online_name:
        st.caption(f"当前分析对象：**{online_name}**")
        # 记录当前分析对象，用于"是否需要清空旧结果"的判断
        st.session_state["current_target"] = online_name
    elif st.text_input is not None and not online_name:
        # 手动模式时不强提示
        pass

    c1, c2 = st.columns(2)
    with c1:
        run_btn = st.button("🚀 开始结构化分析", disabled=not raw_data, use_container_width=True, type="primary")
    with c2:
        report_btn = st.button("📝 生成完整研报", disabled=not raw_data, use_container_width=True)

    if not raw_data:
        st.info("请先通过「在线自动取数」获取数据，或手动粘贴 / 上传财报，再开始分析。")
    elif not DEEPSEEK_API_KEY and (run_btn or report_btn):
        st.error("系统未配置 DEEPSEEK_API_KEY，请联系管理员。")

    # ---- 结构化分析（评分 + 图表 + 风险） ----
    # 【统一清空】根据 公司/代码/报告期数/年报/数据来源/行业 生成数据指纹，
    # 指纹变化（切公司、换报告期、换年报、换行业、清空数据）时统一清空旧结果
    def _data_sig():
        return "|".join([
            src_mode,
            st.session_state.get("single_online_code", ""),
            st.session_state.get("single_online_name", ""),
            str(st.session_state.get("single_periods", "")),
            st.session_state.get("single_pdf_title", ""),
            industry,
            "hasdata" if raw_data else "nodata",
        ])

    _sig_now = _data_sig()
    _sig_old = st.session_state.get("data_sig")
    if _sig_old and _sig_old != _sig_now:
        # 数据版本变了：统一清空旧分析/旧研报/旧对话
        st.session_state.pop("analysis", None)
        st.session_state.pop("report", None)
        st.session_state.pop("analysis_data", None)
        st.session_state.pop("rag_hits_last", None)
        st.session_state["messages"] = []
        st.info("🔄 检测到分析对象或数据版本已变化，旧的分析结果与研报已清空，请重新点击「🚀 开始结构化分析」。")
    st.session_state["data_sig"] = _sig_now

    # 【RAG 按问题检索】不再把年报当"开头几千字"硬塞给模型，
    # 而是针对"财务数据/经营讨论/风险"检索最相关的章节（带页码）
    def _rag_retrieve(query, top_k=8):
        _code = st.session_state.get("single_online_code", "")
        _year = st.session_state.get("single_pdf_year", "")
        if (
            rag is None or not _code or not _year
            or not st.session_state.get("single_pdftext")
            or not rag.has_index(_code, _year)
        ):
            return []
        try:
            return rag.retrieve(query, _code, _year, top_k=top_k)
        except Exception:
            return []

    if run_btn and raw_data and DEEPSEEK_API_KEY:
        with st.spinner("正在提取关键指标并评分……（约 10-30 秒）"):
            try:
                # 1) RAG 检索年报相关章节（财务报表/风险章节往往不在开头）
                rag_hits = _rag_retrieve(
                    f"{online_name or '目标公司'} 主要财务数据 财务报表 经营情况讨论 风险因素", top_k=8
                )
                st.session_state["rag_hits_last"] = rag_hits

                # 2) LLM 只负责"提取指标 + 解读"，不再直接打分（分数由规则引擎计算）
                json_instruction = (
                    "请只输出一个 JSON 对象，不要输出任何其他文字。格式如下：\n"
                    "{\n"
                    '  "company": "公司名称，未知则填 目标公司",\n'
                    '  "summary": "一句话公司概况",\n'
                    '  "metrics": [{"item": "指标名", "value": 数值, "unit": "单位", "comment": "一句话解读"}],\n'
                    '  "chart_data": [{"item": "指标名", "value": 数值}],\n'
                    '  "trend": [{"period": "2021年报", "revenue": 数值, "net_profit": 数值}],\n'
                    '  "risks": ["风险1", "风险2", "风险3"],\n'
                    '  "highlights": ["亮点1", "亮点2"]\n'
                    "}\n"
                    "要求：metrics 提取 4-8 个关键财务指标；chart_data 最多 6 项且单位统一为亿元，"
                    "用于画柱状图（不要放比率类指标）；\n"
                    "trend 只放**同口径的年报期**数据（单位亿元），数据中没有多年年报则填空数组 []，"
                    "禁止把一季报/半年报等中期累计数据当作年度数据放入 trend；\n"
                    "注意：数据中可能包含一季报/半年报/三季报等中期报告，其金额为年初至今累计值，"
                    "解读时必须标注报告期口径，不得与年报全年数据直接比较；\n"
                    "不要输出任何评分（scores 由系统规则引擎计算）；数据不足的字段填 null。\n\n"
                    f"行业：{industry}\n\n财务数据：\n"
                )
                _ctx = json_instruction + raw_data[:MAX_CHARS]
                if rag_hits:
                    _ctx += (
                        "\n\n【年报相关章节 · RAG 检索（引用时请注明页码）】\n"
                        + rag.format_citations(rag_hits)
                    )
                raw_json = llm_once(
                    [
                        {"role": "system", "content": "你是严谨的金融投研分析师，只输出 JSON。"},
                        {"role": "user", "content": _ctx},
                    ],
                    temperature=0.1,
                    action="analysis",
                )
                analysis = parse_json_loose(raw_json)

                # 3) 规则引擎评分（固定阈值 → 同样的数据永远得到同样的分数）
                if scoring is not None:
                    _metrics_for_score = {}
                    _oc = st.session_state.get("single_online_code", "")
                    if _oc:
                        try:
                            _fina = ds.get_financial_indicators(
                                _oc, periods=int(st.session_state.get("single_periods", 4) or 4)
                            )
                            if _fina:
                                _metrics_for_score = _fina[0]  # 最新报告期的标准化指标
                        except Exception:
                            _metrics_for_score = {}
                    if not _metrics_for_score:
                        # 手动模式：正则抽取 → 抽不到再让 LLM 抽（LLM 只提取数字，不打分）
                        _metrics_for_score = scoring.extract_metrics_regex(raw_data)
                        _need_llm = [k for k in (
                            "roe", "gross_margin", "net_margin", "revenue_yoy", "net_profit_yoy",
                            "debt_ratio", "current_ratio", "ocf_to_revenue",
                        ) if k not in _metrics_for_score]
                        if _need_llm and raw_data:
                            try:
                                _mj = llm_once(
                                    [
                                        {"role": "system", "content": "你是严谨的财务数据抽取器，只输出 JSON。"},
                                        {"role": "user", "content": scoring.METRICS_EXTRACTION_PROMPT + raw_data[:MAX_CHARS]},
                                    ],
                                    temperature=0.0,
                                    action="metrics_extract",
                                )
                                _m = parse_json_loose(_mj)
                                if isinstance(_m, dict):
                                    for k in _need_llm:
                                        v = _m.get(k)
                                        if v is not None:
                                            _metrics_for_score[k] = v
                            except Exception:
                                pass
                    sc = scoring.compute_scores(_metrics_for_score, industry)
                    analysis["scores"] = sc["scores"]
                    analysis["total_score"] = sc["total_score"]
                    analysis["score_reasons"] = sc["reasons"]
                    analysis["score_missing"] = sc["missing"]
                    analysis["score_engine"] = "规则引擎 v1"
                    # 保存标准化原始指标（供 ROE×营收增速 散点图取数）
                    analysis["metrics_raw"] = dict(_metrics_for_score or {})
                    analysis["score_source"] = (
                        "在线财务指标（最新报告期）" if st.session_state.get("single_online_code") else "文本抽取"
                    )

                # 4) 严格校验（字段/类型/范围/缺失值），异常项修复或剔除
                analysis, v_issues = validate_analysis(analysis)
                for _iss in v_issues:
                    st.warning(f"输出校验：{_iss}")

                st.session_state["analysis"] = analysis
                st.session_state["analysis_data"] = raw_data
                # 每次重新分析时清空旧的追问记录与旧的研报，避免上下文/报告串台
                st.session_state["messages"] = []
                st.session_state.pop("report", None)
            except Exception as e:
                st.error(f"结构化分析失败：{e}。可重试，或检查数据是否完整。")

    analysis = st.session_state.get("analysis")
    if analysis:
        st.divider()
        st.subheader(f"📊 结构化分析结果：{analysis.get('company', '目标公司')}")
        if analysis.get("summary"):
            st.caption(analysis["summary"])

        scores = analysis.get("scores", {}) or {}
        total = analysis.get("total_score")
        sc1, sc2, sc3, sc4, sc5 = st.columns(5)
        with sc1:
            render_score_card("盈利能力", scores.get("盈利能力"))
        with sc2:
            render_score_card("成长性", scores.get("成长性"))
        with sc3:
            render_score_card("偿债能力", scores.get("偿债能力"))
        with sc4:
            render_score_card("现金流质量", scores.get("现金流质量"))
        with sc5:
            if total is None:
                st.metric("综合评分", "数据不足")
            else:
                st.metric("综合评分", f"{total} / 10")
                st.progress(min(max(total / 10.0, 0.0), 1.0))

        # 评分依据（规则引擎 · 可复现可解释）
        if analysis.get("score_engine"):
            st.caption(
                f"📐 评分由 **{analysis['score_engine']}** 按固定阈值计算"
                + (f" · 数据来源：{analysis.get('score_source')}" if analysis.get("score_source") else "")
                + " —— 同样的数据必然得到同样的分数，大模型仅负责解读"
            )
            _reasons = analysis.get("score_reasons") or {}
            _missing = analysis.get("score_missing") or []
            if _reasons or _missing:
                with st.expander("📌 查看评分依据（每个分数怎么算出来的）"):
                    for _dim, _rs in _reasons.items():
                        _sc = scores.get(_dim)
                        st.markdown(
                            f"**{_dim}：{_sc if _sc is not None else '数据不足'} 分**"
                            + (f"（{len(_rs)} 项指标加权）" if _rs else "")
                        )
                        for _r in _rs:
                            st.markdown(f"&nbsp;&nbsp;- {_r}")
                    if _missing:
                        st.caption("未参与计分的缺失指标：" + "、".join(_missing))

        left, right = st.columns(2)
        with left:
            chart_data = analysis.get("chart_data") or []
            chart_data = [d for d in chart_data if isinstance(d.get("value"), (int, float))]
            if chart_data:
                st.markdown("##### 📊 关键财务指标（单位：亿元）")
                # 横向柱状图（更适合长指标名 + 亮色）
                _hb_png = render_horizontal_bar(chart_data)
                if _hb_png:
                    show_png(_hb_png, alt="关键财务指标横向对比图")
                st.markdown(
                    build_chart_insight(chart_data),
                    unsafe_allow_html=True,
                )
            else:
                st.info("图表数据不足（缺少可画图的金额类指标）。")
        with right:
            # 圆环图（与柱状图不同视角，看占比与最大项）
            chart_data_right = analysis.get("chart_data") or []
            chart_data_right = [d for d in chart_data_right if isinstance(d.get("value"), (int, float))]
            if len(chart_data_right) >= 2:
                st.markdown("##### 🍩 指标占比与分布")
                _donut_png = render_metric_donut(chart_data_right)
                if _donut_png:
                    show_png(_donut_png, alt="指标占比圆环图")
            metrics = analysis.get("metrics") or []
            if metrics:
                st.markdown("##### 📋 指标明细")
                mdf = pd.DataFrame(
                    [
                        {
                            "指标": m.get("item", ""),
                            "数值": m.get("value", ""),
                            "单位": m.get("unit", ""),
                            "解读": m.get("comment", ""),
                        }
                        for m in metrics
                    ]
                )
                st.dataframe(mdf, use_container_width=True, hide_index=True)

        # ---- 雷达图（4 维评分可视化） + 趋势图 ----
        if not _HAS_CJK:
            st.caption("ℹ️ 部署环境未找到中文字体，图表内文字可能显示为方框；数据本身不受影响。")
        _radar_left, _radar_right = st.columns(2)
        with _radar_left:
            if scores and any(isinstance(v, (int, float)) for v in scores.values()):
                st.markdown("##### 🎯 四维评分雷达")
                _radar_png = render_score_radar(scores, total if isinstance(total, (int, float)) else None)
                if _radar_png:
                    show_png(_radar_png, alt="四维评分雷达图")
                st.markdown(build_radar_insight(scores), unsafe_allow_html=True)
        with _radar_right:
            st.markdown("##### 📈 年报趋势（仅年报口径）")
            _oc = st.session_state.get("single_online_code", "")
            _trend_annual = []
            if _oc:
                try:
                    _trend_annual = ds.build_annual_trend(_oc, years=5) or []
                except Exception:
                    _trend_annual = []
            if len(_trend_annual) >= 2:
                st.caption("数据直接来自财务指标接口的年报记录（REPORT_TYPE=年报）。")
                tdf = pd.DataFrame(_trend_annual).set_index("period")
                keep = [c for c in ["revenue", "net_profit"] if c in tdf.columns]
                if keep:
                    st.line_chart(
                        tdf[keep], height=240,
                        color=["#1E40AF", "#D9A441"],
                    )
                    st.markdown(build_trend_insight(_trend_annual), unsafe_allow_html=True)
            else:
                # 手动模式：使用 LLM 抽取的 trend
                trend = analysis.get("trend") or []
                if len(trend) >= 2:
                    tdf = pd.DataFrame(trend)
                    if "period" in tdf.columns:
                        tdf = tdf.set_index("period")
                    keep = [c for c in ["revenue", "net_profit"] if c in tdf.columns]
                    if keep:
                        st.line_chart(tdf[keep], height=240, color=["#1E40AF", "#D9A441"])
                        st.markdown(build_trend_insight(trend), unsafe_allow_html=True)
                else:
                    st.info("趋势数据不足（需 ≥2 个年报期）。")

        # ---- 【新增】ROE × 营收增速 四象限定位散点图 ----
        _mraw = analysis.get("metrics_raw") or {}
        _roe_v = _mraw.get("roe")
        _growth_v = _mraw.get("revenue_yoy")

        # 兜底补齐：在线/文本两种模式下指标命名不完全一致，
        # 缺哪一项就从「指标明细表 + 年报趋势」里补回，保证散点图尽量画得出来。
        def _num_from_text(x):
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                return float(x)
            try:
                return float(str(x).replace("%", "").replace(",", "").strip())
            except (TypeError, ValueError):
                return None

        if not isinstance(_roe_v, (int, float)):
            for _m in (analysis.get("metrics") or []):
                if "ROE" in str(_m.get("item") or "").upper():
                    _roe_v = _num_from_text(_m.get("value"))
                    if isinstance(_roe_v, (int, float)):
                        break
            if not isinstance(_roe_v, (int, float)):
                for _r in (_trend_annual or []):
                    _roe_v = _num_from_text(_r.get("roe"))
                    if isinstance(_roe_v, (int, float)):
                        break
        if not isinstance(_growth_v, (int, float)):
            _rev_by_year = []
            for _r in (_trend_annual or []):
                _yr = re.sub(r"\D", "", str(_r.get("period") or ""))[:4]
                _rv = _num_from_text(_r.get("revenue"))
                if _yr and _rv is not None:
                    _rev_by_year.append((int(_yr), _rv))
            if len(_rev_by_year) >= 2:
                _rev_by_year.sort()
                _p, _l = _rev_by_year[-2][1], _rev_by_year[-1][1]
                if _p:
                    _growth_v = round((_l / _p - 1) * 100, 2)

        if isinstance(_roe_v, (int, float)) and isinstance(_growth_v, (int, float)):
            st.markdown("##### 🎯 ROE × 营收增速 · 象限定位")
            _rl, _gl = _industry_base(industry)
            _q_left, _q_right = st.columns([3, 2])
            with _q_left:
                _scat_png = render_scatter_roe_growth(
                    _roe_v, _growth_v, name=(analysis.get("company") or "本公司"),
                    industry=industry,
                )
                if _scat_png:
                    show_png(_scat_png, alt="ROE×营收增速四象限定位图")
            with _q_right:
                st.markdown(
                    f'<div class="quad-note">'
                    f'🎯 <b>四象限速览</b>（当前行业「{industry}」基准：ROE {_rl:g}% · 增速 {_gl:g}%）<br>'
                    f'<span class="q-star">■ 明星</span> ROE≥{_rl:g}% 且 增速≥{_gl:g}%<br>'
                    f'<span class="q-cash">■ 现金牛</span> ROE≥{_rl:g}% 且 增速&lt;{_gl:g}%<br>'
                    f'<span class="q-quest">■ 问题</span> ROE&lt;{_rl:g}% 且 增速≥{_gl:g}%<br>'
                    f'<span class="q-dog">■ 瘦狗</span> ROE&lt;{_rl:g}% 且 增速&lt;{_gl:g}%<br>'
                    f'<span style="color:#7C8CA6">切换行业知识库按钮，基准线会随之调整。</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            st.markdown(
                build_quadrant_insight(_roe_v, _growth_v,
                                       name=(analysis.get("company") or "该公司"),
                                       industry=industry),
                unsafe_allow_html=True,
            )
        else:
            st.caption(
                "ℹ️ 象限定位图需 ROE 与营收同比两项指标；当前数据不足未绘制"
                "（完整分析模式下会自动补齐）。"
            )

        colr, colh = st.columns(2)
        with colr:
            risks = analysis.get("risks") or []
            if risks:
                st.markdown("##### ⚠️ 风险提示")
                for r in risks:
                    st.markdown(f"- {r}")
        with colh:
            highlights = analysis.get("highlights") or []
            if highlights:
                st.markdown("##### ✨ 投资亮点")
                for h in highlights:
                    st.markdown(f"- {h}")

        # ---- RAG 检索过程可视化（默认展开，让评委看到按页检索的实锤） ----
        _last_rag = st.session_state.get("rag_hits_last") or []
        if _last_rag:
            with st.expander(f"📚 RAG 检索过程 · Top-{min(len(_last_rag), 8)} 年报相关章节（按相似度排序，已默认展开）", expanded=True):
                st.caption("以下片段是分析时从年报全文中按问题检索得到的关键章节，AI 回答的页码引用即源于此。")
                st.markdown(render_rag_hits_panel(_last_rag), unsafe_allow_html=True)

    # ---- 完整研报生成 ----
    if report_btn and raw_data and DEEPSEEK_API_KEY:
        st.divider()
        st.subheader("📝 完整投研报告")
        # 【RAG 接通】索引已在"下载并解析该报告"时构建完成，
        # 这里按研报主题检索相关章节（含页码），注入 DeepSeek 上下文
        _rag_hits: list = []
        if use_dual_model and rag is not None:
            _code = st.session_state.get("single_online_code", "")
            _yr = st.session_state.get("single_pdf_year", "")
            _pdf_path = st.session_state.get("single_pdf_path", "")
            if st.session_state.get("single_pdftext") and _code and _yr:
                # 兜底：如果索引还没建（如旧会话数据），现场补建一次
                if not rag.has_index(_code, _yr) and _pdf_path and Path(_pdf_path).exists():
                    with st.spinner("正在补建年报向量索引……"):
                        _built = rag.build_index_from_pdf(_pdf_path, _code, _yr)
                        st.session_state["single_rag_status"] = _built
                if rag.has_index(_code, _yr):
                    with st.spinner("正在从年报全文检索相关章节……"):
                        _rag_hits = rag.retrieve(
                            f"{online_name or '目标公司'} 财务表现 经营讨论 业务亮点 风险因素 未来展望",
                            _code, _yr, top_k=6,
                        )
                    st.session_state["rag_build_status"] = (
                        st.session_state.get("single_rag_status")
                        or {"ok": True, "chunks": len(_rag_hits), "pages": 0, "msg": "复用已有索引"}
                    )
        box = st.empty()
        with st.spinner("正在生成完整研报……"):
            try:
                context = f"行业：{industry}\n\n财务数据：\n{raw_data[:MAX_CHARS]}"
                if analysis:
                    context += f"\n\n已有的结构化评分（供参考，保持一致）：{json.dumps(analysis.get('scores', {}), ensure_ascii=False)}"
                if use_dual_model and _rag_hits:
                    context += f"\n\n【RAG 检索片段（来自年报全文）】\n{rag.format_citations(_rag_hits)}"
                report = llm_stream(
                    [
                        {"role": "system", "content": system_prompt_with_kb()},
                        {"role": "user", "content": context},
                    ],
                    box,
                    action="report",
                )
                # 千问润色 (Plan B 双模型)
                if use_dual_model and lq is not None and lq.is_available():
                    _refined = lq.refine_answer(
                        report,
                        rag.format_citations(_rag_hits) if _rag_hits else None,
                    )
                    if _refined:
                        report = _refined
                        st.session_state["dual_model_refined"] = True
                    else:
                        st.session_state["dual_model_refined"] = False
                else:
                    st.session_state["dual_model_refined"] = False
                st.session_state["report"] = report
            except Exception as e:
                st.error(f"研报生成失败：{e}")

    report = st.session_state.get("report")
    if report and not report_btn:
        st.divider()
        st.subheader("📝 完整投研报告")
        if st.session_state.get("dual_model_refined"):
            st.markdown(
                '<span class="dual-badge">🤖 双模型协同 · DeepSeek 分析 + 千问润色</span>',
                unsafe_allow_html=True,
            )
        _rstatus = st.session_state.get("rag_build_status")
        if _rstatus and _rstatus.get("ok"):
            st.caption(
                f"📚 RAG 检索增强已启用：{_rstatus.get('chunks', 0)} chunks"
                + (f" · 覆盖 {_rstatus.get('pages', 0)} 页" if _rstatus.get("pages") else "")
                + " · 研报中的年报内容均来自全文检索并标注页码"
            )
        st.markdown(report)

    if report:
        st.download_button(
            "⬇️ 下载研报（Markdown）",
            data=report,
            file_name="投研分析报告.md",
            mime="text/markdown",
            use_container_width=True,
        )

    # ---- 追问对话（多轮，上下文实时更新） ----
    st.divider()
    st.markdown("##### 💬 继续追问")
    st.caption("基于当前财务数据与已生成的分析继续提问，支持连续多轮对话。")

    if "messages" not in st.session_state:
        st.session_state["messages"] = []

    # 渲染历史消息
    for m in st.session_state["messages"]:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    # 清空对话按钮
    if st.session_state["messages"]:
        if st.button("🗑️ 清空对话", key="clear_chat"):
            st.session_state["messages"] = []
            st.rerun()

    # 【智能推荐提问】根据当前公司名称/行业/最新评分结果实时生成，点击即问
    _company = online_name or st.session_state.get("current_target", "")
    _suggestions: list = []
    if _company:
        _suggestions.append(f"{_company}近三年营收与净利润的增长趋势如何？主要驱动因素是什么？")
        _sc = {}
        if analysis and isinstance(analysis.get("scores"), dict):
            _sc = {k: v for k, v in analysis["scores"].items() if isinstance(v, (int, float))}
        if _sc:
            _wk = min(_sc, key=_sc.get)
            _suggestions.append(f"评分中「{_wk}」得分最低（{_sc[_wk]} 分），主要原因是什么？有哪些改善空间？")
            _bst = max(_sc, key=_sc.get)
            _suggestions.append(f"{_company}的「{_bst}」得分最高（{_sc[_bst]} 分），核心优势体现在哪些财务指标上？")
        # 金融/银行类行业判断（行业 KB 的 key 是「金融」；兼容直接选「银行」的场景）
        if any(_k in industry for _k in ("金融", "银行")):
            _suggestions.append(f"{_company}的不良贷款率、拨备覆盖率与资本充足率是否满足监管要求？")
        else:
            _suggestions.append(f"{_company}的毛利率与同行业可比公司相比处于什么水平？")
        if rag is not None:
            _suggestions.append(f"年报风险章节提到{_company}面临哪些主要风险？请结合原文并标注页码。")
    if _suggestions:
        st.caption("🎯 智能推荐（根据当前公司与最新评分实时生成，点击即问）：")
        _qcols = st.columns(2)
        for _i, _sq in enumerate(_suggestions[:6]):
            with _qcols[_i % 2]:
                if st.button(_sq, key=f"suggest_q_{_i}", use_container_width=True):
                    st.session_state["_pending_question"] = _sq
                    st.rerun()

    # 问题来源：推荐问题点击（_pending_question）或手动输入
    _pending = st.session_state.get("_pending_question", "")
    if _pending:
        try:
            del st.session_state["_pending_question"]
        except KeyError:
            pass
    question = _pending or st.chat_input("例如：这家公司的毛利率在同行业处于什么水平？", key="chat_q")
    if question:
        # 【长度保护】超长输入截断到 500 字，防单次调用成本与耗时失控
        if len(question) > 500:
            st.warning(f"⚠️ 问题过长（{len(question)} 字），已截取前 500 字进行回答。")
            question = question[:500]
        if not raw_data:
            st.warning("请先获取或粘贴财务数据，再提问。")
        elif not DEEPSEEK_API_KEY:
            st.error("系统未配置 DEEPSEEK_API_KEY，请联系管理员。")
        else:
            # 1) 追加用户消息并立即回显
            st.session_state["messages"].append({"role": "user", "content": question})
            with st.chat_message("user"):
                st.markdown(question)

            # 2) 组装上下文：数据 + 结构化评分 + RAG 检索 + 历史对话
            base_context = f"【行业】{industry}\n\n【财务数据】\n{raw_data[:MAX_CHARS]}"
            if analysis:
                base_context += f"\n\n【已生成的结构化评分与结论】\n{json.dumps(analysis, ensure_ascii=False)[:2000]}"

            # 【RAG 接通】按用户问题实时检索年报相关章节（含页码）
            _q_hits = []
            if rag is not None:
                _q_hits = _rag_retrieve(question, top_k=6)
            if _q_hits:
                base_context += (
                    "\n\n【年报相关章节 · 按问题检索（回答时请注明引用页码，"
                    "如「见年报第X页」；检索片段之外的内容不要编造）】\n"
                    + rag.format_citations(_q_hits)
                )
                st.caption(f"📚 已从年报检索到 {len(_q_hits)} 个相关片段（含页码），正在结合原文回答……")

            history = st.session_state["messages"][:-1]  # 不含刚追加的这条
            history = history[-6:]  # 最多保留最近 3 轮
            msgs = [{"role": "system", "content": system_prompt_with_kb()}]
            msgs.append({"role": "user", "content": base_context + "\n\n以上是背景资料，请基于它回答后续问题。"})
            msgs.append({"role": "assistant", "content": "好的，我已了解该公司的财务数据与行业背景，请提问。"})
            msgs.extend(history)
            msgs.append({"role": "user", "content": question})

            # 3) 流式生成回答
            try:
                with st.chat_message("assistant"):
                    box = st.empty()
                    with st.spinner("正在分析……"):
                        answer = llm_stream(msgs, box, action="followup")
                st.session_state["messages"].append({"role": "assistant", "content": answer})
            except Exception as e:
                st.error(f"调用失败：{e}")

# ---------------------------------------------------------------
# Tab 2：双公司对比
# ---------------------------------------------------------------
with tab2:
    st.markdown("输入两家公司的财务数据（支持在线自动取数），生成横向对比分析与结论。")
    ca, cb = st.columns(2)
    with ca:
        st.markdown("##### 🏢 公司 A")
        use_online_a = st.checkbox("🌐 在线自动取数", key="online_a", value=True)
        if use_online_a:
            ot_a, on_a, op_a = online_fetch_panel("cmp_a")
            name_a = on_a or st.text_input("公司名称 A（可选）", value="公司A", key="name_a")
            data_a = "\n\n".join([x for x in [ot_a, op_a[:4000] if op_a else ""] if x]).strip()
        else:
            name_a = st.text_input("公司名称 A", value="公司A", key="name_a")
            text_a = st.text_area("粘贴财报数据 A", height=160, key="text_a")
            up_a = st.file_uploader("或上传文件 A（PDF / txt / md）", type=["pdf", "txt", "md"], key="up_a")
            file_a, err_a = read_upload(up_a)
            if err_a:
                st.warning(err_a)
            data_a = (text_a + "\n" + file_a).strip()
    with cb:
        st.markdown("##### 🏢 公司 B")
        use_online_b = st.checkbox("🌐 在线自动取数", key="online_b", value=True)
        if use_online_b:
            ot_b, on_b, op_b = online_fetch_panel("cmp_b")
            name_b = on_b or st.text_input("公司名称 B（可选）", value="公司B", key="name_b")
            data_b = "\n\n".join([x for x in [ot_b, op_b[:4000] if op_b else ""] if x]).strip()
        else:
            name_b = st.text_input("公司名称 B", value="公司B", key="name_b")
            text_b = st.text_area("粘贴财报数据 B", height=160, key="text_b")
            up_b = st.file_uploader("或上传文件 B（PDF / txt / md）", type=["pdf", "txt", "md"], key="up_b")
            file_b, err_b = read_upload(up_b)
            if err_b:
                st.warning(err_b)
            data_b = (text_b + "\n" + file_b).strip()

    # 【统一清空】对比对象变化时清空旧对比报告
    def _cmp_sig():
        return "|".join([
            str(st.session_state.get("name_a", "")),
            str(st.session_state.get("name_b", "")),
            st.session_state.get("cmp_a_online_code", ""),
            st.session_state.get("cmp_b_online_code", ""),
            "A" if data_a else "-",
            "B" if data_b else "-",
        ])

    _csig_now = _cmp_sig()
    _csig_old = st.session_state.get("cmp_sig")
    if _csig_old and _csig_old != _csig_now and st.session_state.get("cmp_report"):
        st.session_state.pop("cmp_report", None)
        st.info("🔄 对比对象已变化，旧的对比报告已清空，请重新点击「⚖️ 开始对比分析」。")
    st.session_state["cmp_sig"] = _csig_now

    cmp_btn = st.button("⚖️ 开始对比分析", disabled=not (data_a and data_b), use_container_width=True, type="primary")

    if cmp_btn and data_a and data_b:
        if not DEEPSEEK_API_KEY:
            st.error("系统未配置 DEEPSEEK_API_KEY，请联系管理员。")
        else:
            box = st.empty()
            with st.spinner("正在对比分析……（约 20-40 秒）"):
                try:
                    cmp_prompt = (
                        f"请对比两家公司的财务表现：\n\n"
                        f"【{name_a}】\n{data_a[:MAX_CHARS]}\n\n"
                        f"【{name_b}】\n{data_b[:MAX_CHARS]}\n\n"
                        "输出要求（markdown 格式）：\n"
                        "1. 先给出『核心指标对比表』（markdown 表格，维度至少包括：营收、净利润、毛利率、净利率、ROE、资产负债率、现金流，数据缺失的单元格写 N/A）；\n"
                        "2. 逐维度点评谁更强、依据是什么；\n"
                        "3. 【综合结论】分别用一句话总结两家公司的优劣势与适配的投资者类型；\n"
                        "4. 【风险提示】说明对比结论的局限性（数据口径、行业差异等）。\n"
                        "所有数字必须来自提供的数据，禁止编造。"
                    )
                    cmp_result = llm_stream(
                        [
                            {"role": "system", "content": system_prompt_with_kb()},
                            {"role": "user", "content": cmp_prompt},
                        ],
                        box,
                        temperature=0.3,
                        action="compare",
                    )
                    st.session_state["cmp_report"] = cmp_result
                except Exception as e:
                    st.error(f"对比分析失败：{e}")

    cmp_report = st.session_state.get("cmp_report")
    if cmp_report and not cmp_btn:
        st.markdown(cmp_report)
    if cmp_report:
        st.download_button(
            "⬇️ 下载对比报告（Markdown）",
            data=cmp_report,
            file_name="双公司对比报告.md",
            mime="text/markdown",
            use_container_width=True,
        )

# ---------------------------------------------------------------
# Tab 3：使用说明
# ---------------------------------------------------------------
with tab3:
    st.markdown(
        """
### 📖 使用说明

**本智能体提供六大能力：**

| 功能 | 入口 | 说明 |
|---|---|---|
| 🌐 在线自动取数 | 「单公司深度分析」页顶部 | 输入股票代码/名称，自动从巨潮资讯网与东方财富获取行情、多期财务指标、年报 PDF，无需自己找财报 |
| 📄 年报 PDF 云端解析 + RAG | 取数面板内 | 一键下载年报 → 按页切片构建向量索引；分析与追问自动检索相关章节，结论标注「见年报第X页」 |
| 📐 规则引擎评分 | 「开始结构化分析」 | 四维评分由固定阈值规则计算（同一数据必然同一分数），页面可展开查看每个分数的完整依据链 |
| 单公司深度分析 | 「单公司深度分析」页 | 一键得到四维评分、指标图表、年报口径趋势、风险与亮点 |
| 💬 多轮追问 | 「单公司深度分析」页底部 | 基于数据与已有结论连续追问；年报问题实时检索原文片段（带页码） |
| ⚖️ 双公司对比 | 「双公司对比」页 | 两家公司在线取数或手动输入，生成对比表与综合结论 |
| 📝 研报导出 | 各页的下载按钮 | 分析结果可保存为 Markdown 报告（双模型润色） |

**行业知识库：**
页面顶部横向按钮可选择行业（通用、消费、金融、新能源、医药、互联网）。
系统会自动将该行业的分析框架、关键指标、经验法则与风险信号注入分析过程，
使结论更贴合行业特性，而不是泛泛而谈。银行/保险等特殊行业已内置专用指标
（不良贷款率、拨备覆盖率、净息差、资本充足率等）。

**数据来源：**
- 财务数据与行情：东方财富公开接口
- 公告与年报 PDF：巨潮资讯网（官方指定信息披露平台）
- 大模型：DeepSeek（推理与生成）

**数据建议：**
- 在线取数会自动带入多个报告期，可进一步上传年报 PDF 补充细节；
- 扫描版 PDF 无法提取文字，请改用在线取数或手动粘贴；
- 分析结果仅供参考，不构成投资建议。

**技术说明：**
- 应用框架：Streamlit · 部署：Streamlit Community Cloud
- PDF 解析：pypdf（按页解析，保留页码）· 向量检索：ChromaDB + DashScope Embedding
- 评分：scoring.py 固定规则引擎（可复现、附依据链）· 输出校验：字段/类型/范围自动校验
- 对照实验：仓库内 `eval_cases.py` + `docs/对照实验报告.md`（Agent vs 直接大模型）
        """
    )

st.divider()
st.markdown(
    '<p class="footer-note">💡 <b>金融智能投研 Agent</b> · 第二届全国人工智能应用创新大赛参赛作品 · 分析结果仅供参考，不构成投资建议</p>',
    unsafe_allow_html=True,
)

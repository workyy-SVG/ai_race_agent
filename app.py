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
import streamlit as st
import pandas as pd
from openai import OpenAI

import data_source as ds

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
  /* ===== 顶栏横幅 ===== */
  .hero {
    background: linear-gradient(120deg, #0A1F44 0%, #123A6B 60%, #0A1F44 100%);
    border-radius: 14px;
    padding: 22px 28px 18px 28px;
    margin-bottom: 6px;
    color: #ffffff;
  }
  .hero h1 {
    color: #ffffff;
    font-size: 30px;
    font-weight: 800;
    margin: 0 0 4px 0;
    letter-spacing: 1px;
  }
  .hero h1 .gold {
    color: #E8B84B !important;
    font-weight: 800;
  }
  .hero p { color: #C9D6E8; font-size: 14px; margin: 0; }
  .hero .tagline { color: #8FA6C6; font-size: 12px; margin-top: 6px; }

  /* ===== 通用卡片 ===== */
  div[data-testid="stMetric"] {
    background: #F6F8FC;
    border: 1px solid #E2E8F2;
    border-radius: 12px;
    padding: 12px 14px 6px 14px;
  }
  div[data-testid="stMetric"] label {
    color: #5A6B84;
    font-weight: 600;
  }
  div[data-testid="stMetric"] div {
    color: #0A1F44;
    font-weight: 800;
  }

  /* ===== 按钮 ===== */
  .stButton > button[kind="primary"] {
    background: linear-gradient(90deg, #0A1F44, #17417E);
    color: #fff;
    border: none;
    border-radius: 8px;
    font-weight: 600;
  }
  .stButton > button[kind="primary"]:hover {
    background: linear-gradient(90deg, #17417E, #2A5AA8);
    color: #E8B84B;
  }

  /* ===== 标签页 ===== */
  .stTabs [data-baseweb="tab-list"] { gap: 8px; }
  .stTabs [data-baseweb="tab"] {
    background-color: #F1F4F9;
    border-radius: 10px 10px 0 0;
    padding: 8px 20px;
    font-weight: 600;
    color: #5A6B84;
  }
  .stTabs [aria-selected="true"] {
    background-color: #0A1F44 !important;
    color: #E8B84B !important;
  }

  /* ===== 侧边栏 ===== */
  section[data-testid="stSidebar"] {
    background-color: #0A1F44;
  }
  section[data-testid="stSidebar"] * {
    color: #D7E1F0 !important;
  }
  section[data-testid="stSidebar"] .stButton > button {
    background: transparent;
    border: 1px solid #3D5A85;
    color: #D7E1F0;
    border-radius: 8px;
  }

  /* ===== 提示条 ===== */
  div[data-testid="stSuccess"] {
    background-color: #EAF3EA;
    border-left: 4px solid #2E7D32;
  }
  div[data-testid="stWarning"] {
    border-left: 4px solid #E8B84B;
  }
  div[data-testid="stError"] {
    border-left: 4px solid #C0392B;
  }

  /* ===== 页脚 ===== */
  .footer-note {
    text-align: center;
    color: #8A98AC;
    font-size: 12px;
    padding: 8px;
  }
  .footer-note b { color: #0A1F44; }

  /* ===== 区块小标题 ===== */
  h3, h4, h5 { color: #0A1F44 !important; font-weight: 700 !important; }

  /* ===== 分割线 ===== */
  hr { border: none; border-top: 1px dashed #C9D6E8; }
</style>
"""

# ============ 页面与登录 ============
st.set_page_config(page_title="金融智能投研 Agent", layout="wide")
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

def check_login():
    if st.session_state.get("authenticated", False):
        return True
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
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

def llm_once(messages, temperature=0.2):
    resp = make_client().chat.completions.create(
        model=DEEPSEEK_MODEL, messages=messages, temperature=temperature, stream=False
    )
    return resp.choices[0].message.content or ""

def llm_stream(messages, placeholder, temperature=0.3):
    resp = make_client().chat.completions.create(
        model=DEEPSEEK_MODEL, messages=messages, temperature=temperature, stream=True
    )
    full = ""
    for chunk in resp:
        delta = chunk.choices[0].delta.content
        if delta:
            full += delta
            placeholder.write(full)
    return full

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

def render_score_card(label, value):
    if value is None:
        st.metric(label, "数据不足")
    else:
        st.metric(label, f"{value} / 10")
        st.progress(min(max(value / 10.0, 0.0), 1.0))


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
        kw = st.text_input(
            "股票代码或名称",
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
                    # 自动匹配行业知识库
                    gi = ds.guess_industry(name)
                    if gi:
                        st.session_state[f"{key_prefix}_auto_industry"] = gi
                        # 同步切换到顶部的行业 radio（仅单公司面板）
                        if key_prefix == "single" and gi in INDUSTRY_KB:
                            st.session_state["industry"] = gi

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
        with st.expander("📄 一键获取并在云端解析年报 PDF（可选，数据更全）", expanded=not pdf_done):
            rtype = st.radio("报告类型", ["年报", "半年报", "一季报", "三季报"], horizontal=True, key=f"{key_prefix}_rtype")
            if st.button("获取报告列表", key=f"{key_prefix}_listbtn"):
                reports = ds.list_reports(code, org, rtype, limit=8)
                st.session_state[f"{key_prefix}_reports"] = reports
            reports = st.session_state.get(f"{key_prefix}_reports", [])
            if reports:
                opts = [f"{r['date']} {r['title']}" for r in reports]
                idx = st.selectbox("选择报告", range(len(opts)), format_func=lambda i: opts[i], key=f"{key_prefix}_rsel")
                if st.button("下载并解析该报告", key=f"{key_prefix}_dlbtn"):
                    with st.spinner("正在下载并解析 PDF（大文件可能需要 10-30 秒）……"):
                        pdf = ds.download_pdf(reports[idx]["url"])
                        if not pdf:
                            st.error("PDF 下载失败，请稍后重试或改用手动上传。")
                        else:
                            ptxt = extract_pdf_bytes(pdf)
                            if not ptxt or len(ptxt) < 200:
                                st.warning("该 PDF 提取不到文字（可能是扫描件），请改用手动粘贴。")
                            else:
                                st.session_state[f"{key_prefix}_pdftext"] = ptxt
                                st.success(f"✅ 已解析《{reports[idx]['title']}》，提取 {len(ptxt)} 字。")

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
    st.markdown("### 🧭 导航")
    st.markdown(
        """
        **快速上手**
        1. 下方选择行业知识库
        2. 输入股票代码（如 600519）
        3. 点「🔍 获取数据」自动取数
        4. 点「🚀 开始结构化分析」

        **当前版本能力**
        - 🌐 在线自动取数（巨潮+东财）
        - 📄 年报 PDF 云端解析
        - 四维评分 + 总分
        - 指标柱状图 + 多年趋势
        - 一键研报导出
        - 双公司对比
        - 六大行业知识库
        - 多轮追问对话
        """
    )
    st.markdown("---")
    st.markdown(f"👤 当前登录：**{APP_USER}**")
    if st.button("退出登录", use_container_width=True):
        st.session_state["authenticated"] = False
        st.rerun()
    st.markdown(
        '<p class="footer-note">金融智能投研 Agent · 大赛参赛作品</p>',
        unsafe_allow_html=True,
    )

# ---- 行业知识库：主界面横向按钮（不再藏在侧边栏下拉框里） ----
st.markdown("##### 📚 行业知识库 —— 请选择待分析公司所属行业（将自动注入该行业的分析框架与经验法则）")
# 默认索引：以「通用」为默认，避免每次刷新都跳到第一个非通用行业
_default_idx = list(INDUSTRY_KB.keys()).index(
    st.session_state.get("industry", "通用")
    if st.session_state.get("industry", "通用") in INDUSTRY_KB
    else "通用"
)
industry = st.radio(
    "行业",
    list(INDUSTRY_KB.keys()),
    index=_default_idx,
    horizontal=True,
    label_visibility="collapsed",
    key="industry",
)
# 如果是自动匹配过来的，提示一下
if st.session_state.get("single_auto_industry") and st.session_state.get("industry") == st.session_state.get("single_auto_industry"):
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
    parts = []
    if online_text:
        parts.append(online_text)
    if online_pdftext:
        parts.append("【年报原文摘录】\n" + online_pdftext[:6000])
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
    # 安全网：如果旧的 analysis 对应的公司与当前 raw_data 推断的公司不一致，
    # 自动丢弃旧 analysis 并清空旧报告，避免"分析已切换但研报还显示旧公司"
    _current_target = (online_name or "").strip()
    _analysis = st.session_state.get("analysis") or {}
    if _analysis and _current_target and _analysis.get("company", "") != _current_target:
        st.session_state.pop("analysis", None)
        st.session_state.pop("report", None)
        st.session_state["messages"] = []
        _analysis = None
        st.info(f"🔄 检测到分析对象已切换为 **{_current_target}**，旧的研报已清空，请重新点击「🚀 开始结构化分析」。")

    if run_btn and raw_data and DEEPSEEK_API_KEY:
        with st.spinner("正在提取关键指标并评分……（约 10-30 秒）"):
            try:
                json_instruction = (
                    "请只输出一个 JSON 对象，不要输出任何其他文字。格式如下：\n"
                    "{\n"
                    '  "company": "公司名称，未知则填 目标公司",\n'
                    '  "summary": "一句话公司概况",\n'
                    '  "metrics": [{"item": "指标名", "value": 数值, "unit": "单位", "comment": "一句话解读"}],\n'
                    '  "chart_data": [{"item": "指标名", "value": 数值}],\n'
                    '  "trend": [{"period": "2021年报", "revenue": 数值, "net_profit": 数值}],\n'
                    '  "scores": {"盈利能力": 1到10的整数, "成长性": 1到10的整数, "偿债能力": 1到10的整数, "现金流质量": 1到10的整数},\n'
                    '  "total_score": 1到10的整数,\n'
                    '  "risks": ["风险1", "风险2", "风险3"],\n'
                    '  "highlights": ["亮点1", "亮点2"]\n'
                    "}\n"
                    "要求：metrics 提取 4-8 个关键财务指标；chart_data 最多 6 项且单位统一为亿元，"
                    "用于画柱状图（不要放比率类指标）；trend 为多年（若数据中含多个报告期）营收与净利润，"
                    "单位统一为亿元，没有多年数据则填空数组 []；数据不足的维度填 null；分数基于行业经验法则。\n\n"
                    f"行业：{industry}\n\n财务数据：\n"
                )
                raw_json = llm_once(
                    [
                        {"role": "system", "content": "你是严谨的金融投研分析师，只输出 JSON。"},
                        {"role": "user", "content": json_instruction + raw_data[:MAX_CHARS]},
                    ],
                    temperature=0.1,
                )
                analysis = parse_json_loose(raw_json)
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

        left, right = st.columns(2)
        with left:
            chart_data = analysis.get("chart_data") or []
            chart_data = [d for d in chart_data if isinstance(d.get("value"), (int, float))]
            if chart_data:
                st.markdown("##### 关键财务指标（单位：亿元）")
                df = pd.DataFrame(chart_data)
                st.bar_chart(df, x="item", y="value", height=300)
            else:
                st.info("图表数据不足（缺少可画图的金额类指标）。")
        with right:
            metrics = analysis.get("metrics") or []
            if metrics:
                st.markdown("##### 指标明细")
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

        # ---- 多年趋势图 ----
        trend = analysis.get("trend") or []
        trend = [t for t in trend if isinstance(t.get("revenue"), (int, float)) or isinstance(t.get("net_profit"), (int, float))]
        if len(trend) >= 2:
            st.markdown("##### 📈 多年营收与净利润趋势（单位：亿元）")
            tdf = pd.DataFrame(trend)
            if "period" in tdf.columns:
                tdf = tdf.set_index("period")
            keep = [c for c in ["revenue", "net_profit"] if c in tdf.columns]
            if keep:
                st.line_chart(tdf[keep], height=280)

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

    # ---- 完整研报生成 ----
    if report_btn and raw_data and DEEPSEEK_API_KEY:
        st.divider()
        st.subheader("📝 完整投研报告")
        box = st.empty()
        with st.spinner("正在生成完整研报……"):
            try:
                context = f"行业：{industry}\n\n财务数据：\n{raw_data[:MAX_CHARS]}"
                if analysis:
                    context += f"\n\n已有的结构化评分（供参考，保持一致）：{json.dumps(analysis.get('scores', {}), ensure_ascii=False)}"
                report = llm_stream(
                    [
                        {"role": "system", "content": system_prompt_with_kb()},
                        {"role": "user", "content": context},
                    ],
                    box,
                )
                st.session_state["report"] = report
            except Exception as e:
                st.error(f"研报生成失败：{e}")

    report = st.session_state.get("report")
    if report and not report_btn:
        st.divider()
        st.subheader("📝 完整投研报告")
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

    question = st.chat_input("例如：这家公司的毛利率在同行业处于什么水平？", key="chat_q")
    if question:
        if not raw_data:
            st.warning("请先获取或粘贴财务数据，再提问。")
        elif not DEEPSEEK_API_KEY:
            st.error("系统未配置 DEEPSEEK_API_KEY，请联系管理员。")
        else:
            # 1) 追加用户消息并立即回显
            st.session_state["messages"].append({"role": "user", "content": question})
            with st.chat_message("user"):
                st.markdown(question)

            # 2) 组装上下文：数据 + 结构化评分 + 历史对话
            base_context = f"【行业】{industry}\n\n【财务数据】\n{raw_data[:MAX_CHARS]}"
            if analysis:
                base_context += f"\n\n【已生成的结构化评分与结论】\n{json.dumps(analysis, ensure_ascii=False)[:2000]}"

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
                        answer = llm_stream(msgs, box)
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
| 📄 年报 PDF 云端解析 | 取数面板内 | 一键下载并解析上市公司年报 PDF |
| 单公司深度分析 | 「单公司深度分析」页 | 一键得到四维评分、指标图表、多年趋势、风险与亮点 |
| 💬 多轮追问 | 「单公司深度分析」页底部 | 基于数据与已有结论连续追问，如行业对标、估值讨论 |
| ⚖️ 双公司对比 | 「双公司对比」页 | 两家公司在线取数或手动输入，生成对比表与综合结论 |
| 📝 研报导出 | 各页的下载按钮 | 分析结果可保存为 Markdown 报告 |

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
- PDF 解析：pypdf · 图表：Streamlit 内置图表组件
        """
    )

st.divider()
st.markdown(
    '<p class="footer-note">💡 <b>金融智能投研 Agent</b> · 第二届全国人工智能应用创新大赛参赛作品 · 分析结果仅供参考，不构成投资建议</p>',
    unsafe_allow_html=True,
)

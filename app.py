# -*- coding: utf-8 -*-
"""
金融智能投研 Agent v2 —— 增强版（Streamlit Cloud 部署）

功能清单：
  1. PDF / txt / md 财报上传解析 + 手动粘贴
  2. 结构化分析：四维评分卡片 + 总分
  3. 财务指标柱状图
  4. 一键生成完整研报并支持下载
  5. 双公司横向对比模式
  6. 内置行业知识库（分析时自动注入行业框架）

密钥说明：所有敏感信息从 Streamlit Secrets / 环境变量读取，代码中不含任何 key。
"""

import os
import re
import json
import streamlit as st
import pandas as pd
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

MAX_CHARS = 12000  # 送入大模型的财报文本最大长度（控制费用与时延）

# ============ 行业知识库（内置静态知识，分析时自动注入） ============
INDUSTRY_KB = {
    "通用框架": """【行业知识库 · 通用框架】
- 分析重点：盈利能力（毛利率/净利率/ROE）、成长性（营收与净利润增速）、偿债能力（资产负债率/流动比率）、经营质量（经营现金流/净利润比）。
- 常见风险信号：应收账款增速远超营收、存货异常堆积、有息负债率高、经营现金流持续为负、毛利率异常高于同行。
- 经验法则：ROE 长期 >15% 属优秀；资产负债率 >70% 需警惕（金融/地产行业除外）；经营现金流/净利润 长期 <0.5 说明利润质量存疑。""",

    "消费 / 白酒 / 食品饮料": """【行业知识库 · 消费/白酒/食品饮料】
- 商业模式核心：品牌溢价决定毛利率（高端白酒毛利率可达 80%+，普通食品约 20-40%）。
- 关键指标：预收账款/合同负债（反映渠道信心）、存货中的基酒（白酒越陈越值钱，不算减值风险）、大单品占比。
- 风险信号：渠道压货（应收+存货双高）、提价失灵、消费税政策变化、年轻群体消费习惯迁移。""",

    "银行 / 保险 / 金融": """【行业知识库 · 银行/保险/金融】
- 银行不看毛利率，核心看：净息差（NIM）、不良贷款率、拨备覆盖率、资本充足率、ROA。
- 经验区间：不良率 <1.5% 较健康；拨备覆盖率 >150% 属审慎；净息差 2% 左右为行业常态。
- 风险信号：不良率连续上行、房地产/城投敞口过大、核心一级资本充足率逼近监管红线。
- 保险公司关注：新业务价值(NBV)增速、内涵价值(EV)、综合成本率（财险 <100% 才盈利）。""",

    "新能源 / 制造业": """【行业知识库 · 新能源/制造业】
- 商业模式核心：技术迭代快、重资产、周期性强（产能扩张→价格战→出清）。
- 关键指标：产能利用率、单位成本下降曲线、在手订单、资本开支/折旧比、市占率变化。
- 风险信号：产品价格年降幅 >15%、应收账款回款期拉长、扩产周期撞上行业价格战、技术路线切换（如固态电池替代液态）。
- 光伏/锂电特别关注：硅料/碳酸锂价格周期、欧美贸易政策。""",

    "医药生物": """【行业知识库 · 医药生物】
- 商业模式核心：研发管线决定长期价值，销售能力决定当期业绩。
- 关键指标：研发费用率（创新药企 15-30%）、核心产品收入占比、集采风险敞口、在研管线进度（I/II/III期）。
- 风险信号：单一产品收入占比 >80%、核心品种纳入集采、研发管线多年无进展、销售费用率畸高（合规风险）。
- 经验法则：创新药看管线估值，仿制药看成本与规模，器械看渠道与集采。""",

    "互联网 / 科技": """【行业知识库 · 互联网/科技】
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

# ============ 页面与登录 ============
st.set_page_config(page_title="金融智能投研 Agent", layout="wide")

def check_login():
    if st.session_state.get("authenticated", False):
        return True
    st.markdown("## 🔒 金融智能投研 Agent")
    st.caption("请输入访问账号和密码后进入系统")
    with st.form("login_form", clear_on_submit=False):
        user = st.text_input("账号", placeholder="请输入账号")
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

# ============ 工具函数 ============
def make_client():
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

def llm_once(messages, temperature=0.2):
    """非流式调用，用于结构化 JSON 输出"""
    resp = make_client().chat.completions.create(
        model=DEEPSEEK_MODEL, messages=messages, temperature=temperature, stream=False
    )
    return resp.choices[0].message.content or ""

def llm_stream(messages, placeholder, temperature=0.3):
    """流式调用，用于长文本生成"""
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

def read_upload(up):
    """读取上传文件，返回 (文本, 错误信息)"""
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
    """容错解析模型返回的 JSON（去掉 markdown 代码围栏等）"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)

def score_color(v):
    if v is None:
        return "—"
    return str(v)

def render_score_card(label, value):
    col = st.columns(1)[0]
    if value is None:
        st.metric(label, "数据不足")
    else:
        st.metric(label, f"{value} / 10")
        st.progress(min(max(value / 10.0, 0.0), 1.0))

# ============ 主界面 ============
st.title("📈 金融智能投研 Agent")
st.caption("上传 PDF/文本财报 → 自动结构化分析、评分、图表 → 支持追问、双公司对比与研报导出")

with st.sidebar:
    st.success(f"已登录 · 账号：{APP_USER}")
    industry = st.selectbox("📚 行业知识库（自动注入分析框架）", list(INDUSTRY_KB.keys()), index=0)
    if st.button("退出登录"):
        st.session_state["authenticated"] = False
        st.rerun()

def system_prompt_with_kb():
    return BASE_SYSTEM_PROMPT + "\n\n" + INDUSTRY_KB[industry]

tab1, tab2, tab3 = st.tabs(["🏢 单公司深度分析", "⚖️ 双公司对比", "📖 使用说明"])

# ---------------------------------------------------------------
# Tab 1：单公司深度分析
# ---------------------------------------------------------------
with tab1:
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

    raw_data = (data_text + "\n" + uploaded_text).strip()

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        run_btn = st.button("🚀 开始结构化分析", disabled=not raw_data, use_container_width=True)
    with c2:
        report_btn = st.button("📝 生成完整研报", disabled=not raw_data, use_container_width=True)

    if not raw_data:
        st.info("请先粘贴或上传财务数据，再开始分析。")
    elif not DEEPSEEK_API_KEY and (run_btn or report_btn):
        st.error("系统未配置 DEEPSEEK_API_KEY，请联系管理员。")

    # ---- 结构化分析（评分 + 图表 + 风险） ----
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
                    '  "scores": {"盈利能力": 1到10的整数, "成长性": 1到10的整数, "偿债能力": 1到10的整数, "现金流质量": 1到10的整数},\n'
                    '  "total_score": 1到10的整数,\n'
                    '  "risks": ["风险1", "风险2", "风险3"],\n'
                    '  "highlights": ["亮点1", "亮点2"]\n'
                    "}\n"
                    "要求：metrics 提取 4-8 个关键财务指标；chart_data 最多 6 项且单位统一为亿元，"
                    "用于画柱状图（不要放比率类指标）；数据不足的维度填 null；分数基于行业经验法则。\n\n"
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
            except Exception as e:
                st.error(f"结构化分析失败：{e}。可重试，或检查数据是否完整。")

    analysis = st.session_state.get("analysis")
    if analysis:
        st.divider()
        st.subheader(f"📊 结构化分析结果：{analysis.get('company', '目标公司')}")
        if analysis.get("summary"):
            st.caption(analysis["summary"])

        # 评分卡片
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

        left, right = st.columns(2)
        with left:
            # 指标图表
            chart_data = analysis.get("chart_data") or []
            chart_data = [d for d in chart_data if isinstance(d.get("value"), (int, float))]
            if chart_data:
                st.markdown("##### 关键财务指标（单位：亿元）")
                df = pd.DataFrame(chart_data)
                st.bar_chart(df, x="item", y="value", height=300)
            else:
                st.info("图表数据不足（缺少可画图的金额类指标）。")
        with right:
            # 指标明细表
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

    # ---- 追问对话 ----
    st.divider()
    st.markdown("##### 💬 继续追问")
    if "messages" not in st.session_state:
        st.session_state.messages = []
    for m in st.session_state.messages:
        st.chat_message(m["role"]).write(m["content"])

    question = st.chat_input("例如：这家公司的毛利率在同行业处于什么水平？")
    if question:
        if not raw_data:
            st.warning("请先粘贴/上传财务数据，再提问。")
        elif not DEEPSEEK_API_KEY:
            st.error("系统未配置 DEEPSEEK_API_KEY，请联系管理员。")
        else:
            st.chat_message("user").write(question)
            st.session_state.messages.append({"role": "user", "content": question})
            context = f"行业：{industry}\n\n财务数据：\n{raw_data[:MAX_CHARS]}"
            if analysis:
                context += f"\n\n已生成的结构化评分：{json.dumps(analysis, ensure_ascii=False)}"
            user_content = context + f"\n\n请回答：{question}"
            try:
                with st.chat_message("assistant"):
                    box = st.empty()
                    with st.spinner("正在分析……"):
                        answer = llm_stream(
                            [
                                {"role": "system", "content": system_prompt_with_kb()},
                                {"role": "user", "content": user_content},
                            ],
                            box,
                        )
                st.session_state.messages.append({"role": "assistant", "content": answer})
            except Exception as e:
                st.error(f"调用失败：{e}")

# ---------------------------------------------------------------
# Tab 2：双公司对比
# ---------------------------------------------------------------
with tab2:
    st.markdown("输入两家公司的财务数据，生成横向对比分析与结论。")
    ca, cb = st.columns(2)
    with ca:
        st.markdown("##### 🏢 公司 A")
        name_a = st.text_input("公司名称 A", value="公司A", key="name_a")
        text_a = st.text_area("粘贴财报数据 A", height=160, key="text_a")
        up_a = st.file_uploader("或上传文件 A（PDF / txt / md）", type=["pdf", "txt", "md"], key="up_a")
        file_a, err_a = read_upload(up_a)
        if err_a:
            st.warning(err_a)
        data_a = (text_a + "\n" + file_a).strip()
    with cb:
        st.markdown("##### 🏢 公司 B")
        name_b = st.text_input("公司名称 B", value="公司B", key="name_b")
        text_b = st.text_area("粘贴财报数据 B", height=160, key="text_b")
        up_b = st.file_uploader("或上传文件 B（PDF / txt / md）", type=["pdf", "txt", "md"], key="up_b")
        file_b, err_b = read_upload(up_b)
        if err_b:
            st.warning(err_b)
        data_b = (text_b + "\n" + file_b).strip()

    cmp_btn = st.button("⚖️ 开始对比分析", disabled=not (data_a and data_b))

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

**本智能体提供四大能力：**

| 功能 | 入口 | 说明 |
|---|---|---|
| 单公司深度分析 | 「单公司深度分析」页 | 粘贴或上传财报（支持 PDF），一键得到四维评分、指标图表、风险与亮点 |
| 追问对话 | 「单公司深度分析」页底部 | 基于已输入数据继续追问，如行业对标、估值讨论 |
| 双公司对比 | 「双公司对比」页 | 输入两家公司数据，生成对比表与综合结论 |
| 研报导出 | 各页的下载按钮 | 分析结果可保存为 Markdown 报告 |

**行业知识库：**
左侧边栏可选择行业（消费、银行金融、新能源制造、医药、互联网科技等）。
系统会自动将该行业的分析框架、关键指标、经验法则与风险信号注入分析过程，
使结论更贴合行业特性，而不是泛泛而谈。

**数据建议：**
- 粘贴的数据越完整（营收、净利润、毛利率、ROE、负债率、现金流等），评分越准确；
- PDF 建议使用文本版年报（扫描件无法提取文字）；
- 分析结果仅供参考，不构成投资建议。

**技术说明：**
- 大模型：DeepSeek（推理与生成）
- 应用框架：Streamlit · 部署：Streamlit Community Cloud
- PDF 解析：pypdf · 图表：Streamlit 内置图表组件
        """
    )

st.divider()
st.caption("💡 金融智能投研 Agent · 第二届全国人工智能应用创新大赛参赛作品 · 分析结果仅供参考，不构成投资建议")

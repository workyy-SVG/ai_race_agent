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
  6. 内置行业财务基准库（主界面横向按钮选择；为四维评分/散点/象限提供同业对标基准）
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

# 统一预算计数（原子写入 + 分桶：llm / polish / embed）
try:
    import budget as _budget
except ImportError:  # 缺失时退回最小可用桩，绝不拖垮主流程
    class _budget:  # type: ignore
        BUCKET_LLM, BUCKET_POLISH, BUCKET_EMBED = "llm", "polish", "embed"
        @staticmethod
        def read(bucket): return 0
        @staticmethod
        def bump(bucket, n=1): return None
        @staticmethod
        def exceeded(bucket, limit): return False
        @staticmethod
        def remaining(bucket, limit): return int(limit)
        @staticmethod
        def summary(): return {}

try:
    import scoring
except ImportError:
    scoring = None  # 缺失时退回 LLM 评分并标记

# 输出校验层（纯函数，页面与离线评测共用同一实现）：
#   validate_analysis / parse_json_loose / _safe_num / verify_evidence
# 独立成模块的意义：eval_cases.py 必须能调用**同一份**校验代码，
# 否则"评测声称走了全流程校验"就是一句无法验证的话。
from analysis_validate import (  # noqa: E402
    validate_analysis, parse_json_loose, _safe_num, verify_evidence,
    MIN_SCORE_COVERAGE,
)

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

# ---- 模型名兼容链（防止"评委点分析 → Model Not Exist"的致命演示事故）----
# DeepSeek 官方公告：旧模型名 deepseek-chat / deepseek-reasoner 已于 2026-07-24
# 15:59 UTC 正式停用，当前可用名为 deepseek-v4-flash / deepseek-v4-pro。
# 若 Secrets 里仍写着旧名（或写了任何不存在的名字），下面会在调用失败时自动
# 按 _MODEL_FALLBACKS 顺序回退，并把真正生效的名字记到日志与侧边栏。
DEEPSEEK_MODEL = _get_secret("DEEPSEEK_MODEL", "deepseek-v4-flash")
_MODEL_FALLBACKS = ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat"]
_model_in_use = {"name": DEEPSEEK_MODEL}   # 运行时记录真正调用成功的模型名


def _model_candidates():
    """按 [配置值, 回退链...] 去重后的候选模型名。"""
    seen, out = set(), []
    for _m in [DEEPSEEK_MODEL] + _MODEL_FALLBACKS:
        if _m and _m not in seen:
            seen.add(_m)
            out.append(_m)
    return out


def _is_model_error(e: Exception) -> bool:
    """判断异常是否为「模型名不存在/不可用」类错误（可安全换名重试）。"""
    _s = f"{type(e).__name__}: {e}".lower()
    for _kw in ("model", "not exist", "does not exist", "invalid_model", "unsupported"):
        if _kw in _s:
            return True
    return False


# ---- 访问凭据 ----
# ⚠️ 安全约定（公开部署必读，模板见 .streamlit/secrets.toml.example）：
#   正式 / 公开部署必须在 Secrets 中【显式】设置 APP_USER / APP_PASSWORD / DEMO_KEY。
#   代码里保留默认值，只是为了让「本地开发 + 评委现场演示」开箱即用；
#   一旦探测到仍在用默认值，会在侧边栏给出橙色提醒（不阻断演示）。
#   注意：.streamlit/secrets.toml 已在 .gitignore 中，真实口令不会进仓库。
_DEFAULT_USER = "zzy"
_DEFAULT_PW = "zzy2026"
APP_USER = _get_secret("APP_USER", _DEFAULT_USER)
APP_PASSWORD = _get_secret("APP_PASSWORD", _DEFAULT_PW)

# 是否仍在使用内置默认凭据（用于侧边栏安全提醒）
USING_DEFAULT_CREDS = (
    _get_secret("APP_USER") is None or _get_secret("APP_PASSWORD") is None
)

_DEFAULT_DEMO_KEY = "ai2026demo"

def _demo_keys():
    """返回当前有效的免登录口令集合。

    安全设计（对应评审建议"演示链接使用随机、短期、可撤销的令牌"）：
      · DEMO_KEY 支持【逗号分隔的多令牌】——这样换口令时新旧可短期并存，随时可撤销；
      · DEMO_KEY_EXPIRE 可选（YYYY-MM-DD）——到期后所有演示令牌自动失效；
      · 未配置时回落到内置默认令牌，保证评委现场扫码/点链接仍可用。
    """
    raw = _get_secret("DEMO_KEY", _DEFAULT_DEMO_KEY)
    keys = [k.strip() for k in str(raw).split(",") if k.strip()]
    exp = _get_secret("DEMO_KEY_EXPIRE", "")
    if exp:
        try:
            import datetime as _dt
            if _dt.date.today() > _dt.date.fromisoformat(str(exp).strip()):
                return []  # 已过期 → 演示令牌全部失效
        except Exception:
            pass  # 到期日格式错误时不静默失效，避免"填错就进不去"
    return keys

MAX_CHARS = 12000  # 送入大模型的财报文本最大长度（控制费用与时延）

# ============ 行业财务基准库（同业对标参数 · 内置静态知识，分析时自动注入） ============
# 定位说明：这是「金融引擎的参数层」，不是「多行业内容平台」。
# 每个行业条目只提供【财务口径】的对标基准 + 风险因子，用于差异化评分与象限判定。
# ⚠️ 扩展时请守住三条红线：不讲行业业务/技术科普、不输出行业研究报告、
#    不取消行业基准差异化（一把尺子量所有行业 = 银行 ROE 11% 被误判成瘦狗）。
INDUSTRY_KB = {
    "通用": """【行业财务基准 · 通用框架】
- 分析重点：盈利能力（毛利率/净利率/ROE）、成长性（营收与净利润增速）、偿债能力（资产负债率/流动比率）、经营质量（经营现金流/净利润比）。
- 常见风险信号：应收账款增速远超营收、存货异常堆积、有息负债率高、经营现金流持续为负、毛利率异常高于同行。
- 经验法则：ROE 长期 >15% 属优秀；资产负债率 >70% 需警惕（金融/地产行业除外）；经营现金流/净利润 长期 <0.5 说明利润质量存疑。""",

    "消费": """【行业财务基准 · 消费/白酒/食品饮料】
- 商业模式核心：品牌溢价决定毛利率（高端白酒毛利率可达 80%+，普通食品约 20-40%）。
- 关键指标：预收账款/合同负债（反映渠道信心）、存货中的基酒（白酒越陈越值钱，不算减值风险）、大单品占比。
- 风险信号：渠道压货（应收+存货双高）、提价失灵、消费税政策变化、年轻群体消费习惯迁移。""",

    "金融": """【行业财务基准 · 银行/保险/金融】
- 银行不看毛利率，核心看：净息差（NIM）、不良贷款率、拨备覆盖率、资本充足率、ROA。
- 经验区间：不良率 <1.5% 较健康；拨备覆盖率 >150% 属审慎；净息差 2% 左右为行业常态。
- 风险信号：不良率连续上行、房地产/城投敞口过大、核心一级资本充足率逼近监管红线。
- 保险公司关注：新业务价值(NBV)增速、内涵价值(EV)、综合成本率（财险 <100% 才盈利）。""",

    "新能源": """【行业财务基准 · 新能源/制造业】
- 商业模式核心：技术迭代快、重资产、周期性强（产能扩张→价格战→出清）。
- 关键指标：产能利用率、单位成本下降曲线、在手订单、资本开支/折旧比、市占率变化。
- 风险信号：产品价格年降幅 >15%、应收账款回款期拉长、扩产周期撞上行业价格战、技术路线切换（如固态电池替代液态）。
- 光伏/锂电特别关注：硅料/碳酸锂价格周期、欧美贸易政策。""",

    "医药": """【行业财务基准 · 医药生物】
- 商业模式核心：研发管线决定长期价值，销售能力决定当期业绩。
- 关键指标：研发费用率（创新药企 15-30%）、核心产品收入占比、集采风险敞口、在研管线进度（I/II/III期）。
- 风险信号：单一产品收入占比 >80%、核心品种纳入集采、研发管线多年无进展、销售费用率畸高（合规风险）。
- 经验法则：创新药看管线估值，仿制药看成本与规模，器械看渠道与集采。""",

    "互联网": """【行业财务基准 · 互联网/科技】
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
- 结合下方【行业财务基准】中的同业对标基准、关键指标与风险信号展开分析；
  注意不同行业的合理区间本就不同（例如银行业 ROE 11% 已属优秀，白酒业 ROE 11% 则明显落后），
  切勿用同一把尺子衡量所有行业。"""

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
    font-size: 30px;
    font-weight: 800;
    margin: 0 0 4px 0;
    letter-spacing: 1px;
    text-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
  }
  /* ⚠️ 深色横幅上的文字必须显式指定颜色并带 !important：
     全站有 `p, li, span, div { color:#334155 }` 这类元素级规则，
     一旦某个祖先/包装元素命中它，横幅文字就会变成 #334155 深灰，
     压在深蓝底上实测对比度仅 1.5:1（几乎看不见）。
     这里连子元素一起上色，避免任何包装元素把颜色带走。 */
  .hero h1, .hero h1 *, .hero p, .hero p * { color: #F8FAFC !important; }
  .hero p, .hero p * { color: #DBEAFE !important; font-size: 14px; margin: 0; }
  .hero .tagline, .hero .tagline * { color: #A9C6E8 !important; font-size: 12px; margin-top: 6px; }
  .hero h1 .gold { color: #F5C563 !important; font-weight: 800; }
  .hero h1 .purple { color: #C9D9FF !important; font-weight: 800; }

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
  /* ⚠️ 这里原来是 `section[data-testid="stSidebar"] * { color:#1E3A5F !important }`
     —— 通配符 + !important 的特异性(0,1,1) 高于 `.sb-brand`(0,1,0)，
     会把品牌卡里本该是白色的标题刷成深蓝 #1E3A5F，压在蓝色渐变卡上
     实测对比度只有 1.83:1（截图里几乎看不见）。
     改为只给 Streamlit 原生文本上色，自定义组件各自保留自己的配色。 */
  section[data-testid="stSidebar"] p,
  section[data-testid="stSidebar"] li,
  section[data-testid="stSidebar"] label,
  section[data-testid="stSidebar"] small,
  section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
  section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] *,
  section[data-testid="stSidebar"] [data-testid="stWidgetLabel"],
  section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] * {
    color: #1E3A5F !important;
  }
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
  /* 侧边栏品牌头（深蓝渐变卡） */
  .sb-brand {
    background: linear-gradient(120deg, #16266B 0%, #1E3A8A 48%, #1D4ED8 100%);
    border: 1px solid rgba(217, 164, 65, 0.65);
    border-radius: 12px;
    padding: 12px 14px;
    font-weight: 800;
    font-size: 15.5px;
    letter-spacing: 0.5px;
    box-shadow: 0 6px 18px rgba(30, 64, 175, 0.40);
  }
  /* 白字同时作用于子元素 —— 只写 .sb-brand 时，任何作用到子元素的
     通配符规则都会把文字压成深色（本次事故的根因），故在此加固 */
  .sb-brand, .sb-brand * { color: #FFFFFF !important; }
  .sb-brand .sb-sub, .sb-brand .sb-sub * {
    display: block;
    color: #FFDFA0 !important;   /* 11px 小字：金色提亮后对比度约 5:1 */
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
  /* 渐变右端原来是 #93B5E0（浅蓝），白字压上去只有 2.2:1 看不清；
     改为深蓝渐变右端 #2563EB，白字对比度 5.2:1 */
  .dual-badge {
    display: inline-block;
    background: linear-gradient(90deg, #1E3A8A 0%, #2563EB 100%);
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

  /* ===== 同业对标基准条（行业选择器下方） =====
     说明: 只用类选择器, 不用通配符 + !important
     (通配符特异性 (0,1,1) 会压掉组件自身配色, 见 MEMORY 设计约定 16) */
  .base-bar {
    background: #FFFFFF;
    border: 1px solid #BFDBFE;
    border-left: 3px solid #1E40AF;
    border-radius: 8px;
    padding: 9px 14px;
    margin: 4px 0 12px 0;
    font-size: 13px;
    line-height: 1.75;
    color: #1E3A8A;
    text-align: left;
  }
  .base-bar b { color: #1E40AF; }
  .base-bar .bb-num { color: #B45309; font-weight: 700; }
  .base-bar .bb-note { color: #64748B; }
</style>
"""

# ============ 页面与登录 ============
st.set_page_config(page_title="金融智能投研 Agent", layout="wide")
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

def check_login():
    if st.session_state.get("authenticated", False):
        return True

    # ---- 方案1: 免登录链接 (URL 携带 ?key=xxx) ----
    _qp_key = st.query_params.get("key", "")
    if _qp_key and _qp_key in _demo_keys():
        st.session_state["authenticated"] = True
        st.session_state["demo_mode"] = True
        st.rerun()

    st.markdown(
        """
        <div class="hero" style="text-align:center; max-width:560px; margin:40px auto 10px auto;">
            <h1 style="color:#F8FAFC !important;font-size:30px;font-weight:800;margin:0 0 4px 0;letter-spacing:1px;">📈 金融智能投研 <span class="gold" style="color:#F5C563 !important;">Agent</span></h1>
            <p style="color:#DBEAFE !important;font-size:14px;margin:0;">AI 驱动的财报深度分析 · 结构化评分 · 双公司对比 · 同业对标基准</p>
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

# ============ 方案3: 每日调用熔断 (防 API 额度被刷爆) ============
# 计数统一走 budget.py（原子写入 + 分桶）：
#   · llm    → DeepSeek 生成/结构化分析（本页 DAILY_LIMIT 管这一桶）
#   · polish → 千问润色/审核
#   · embed  → DashScope Embedding 批次（由 rag_engine 记账）
# 这样三类调用各有账本，不再是"只有 DeepSeek 进预算"。
DAILY_LIMIT = int(_get_secret("DAILY_LLM_LIMIT", "100"))
DAILY_POLISH_LIMIT = int(_get_secret("DAILY_POLISH_LIMIT", "200"))
DAILY_EMBED_LIMIT = int(_get_secret("DAILY_EMBED_LIMIT", "600"))


def _read_usage():
    """今日 DeepSeek 逻辑调用次数（重试不重复计数）。"""
    return _budget.read(_budget.BUCKET_LLM)


def _bump_usage():
    _budget.bump(_budget.BUCKET_LLM, 1)

def check_quota():
    """返回剩余额度是否大于 0；用尽时在页面提示并熔断。"""
    if _read_usage() >= DAILY_LIMIT:
        st.error(
            f"⚠️ 今日 LLM 分析额度（{DAILY_LIMIT} 次）已用完，为保护服务资源已暂停生成。"
            "请明日再试或联系管理员。"
        )
        return False
    return True

def _chat_once(model, messages, temperature, json_mode=False):
    """单次（非流式）对话调用。json_mode 时启用服务端 JSON 输出模式。"""
    _kw = {"model": model, "messages": messages, "temperature": temperature, "stream": False}
    if json_mode:
        _kw["response_format"] = {"type": "json_object"}
    return make_client().chat.completions.create(**_kw)


def llm_once(messages, temperature=0.2, action="llm", json_mode=False):
    """非流式调用。带【模型名回退】：旧模型名停用 / 名字写错时自动换名重试。

    额度只在逻辑调用开始时计一次，不因重试而重复计数。
    """
    import time
    _t0 = time.time()
    if not check_quota():
        return ""
    _bump_usage()
    _last = None
    for _m in _model_candidates():
        _r, _err = None, None
        # json_mode 先试带 response_format，服务端不支持时裸调一次同名模型
        for _jm in ([True, False] if json_mode else [False]):
            try:
                _r = _chat_once(_m, messages, temperature, _jm)
                break
            except Exception as _e:
                _err = _e
                if _jm and not _is_model_error(_e):
                    continue
                break
        if _r is not None:
            _model_in_use["name"] = _m
            _out = _r.choices[0].message.content or ""
            log_event(action, ok=True, dur=time.time() - _t0, extra=f"model={_m}")
            return _out
        _last = _err
        if not _is_model_error(_err):
            break  # 与模型名无关（限流/网络/额度）→ 换名没有意义
    log_event(action, ok=False, dur=time.time() - _t0, extra=type(_last).__name__ if _last else "unknown")
    raise _last if _last else RuntimeError("LLM 调用失败（无可用模型名）")


def llm_stream(messages, placeholder, temperature=0.3, action="chat"):
    import time
    _t0 = time.time()
    if not check_quota():
        placeholder.error(f"⚠️ 今日 LLM 分析额度（{DAILY_LIMIT} 次）已用完，为保护服务资源已暂停生成。")
        return ""
    _bump_usage()
    _last = None
    for _m in _model_candidates():
        full = ""
        try:
            resp = make_client().chat.completions.create(
                model=_m, messages=messages, temperature=temperature, stream=True
            )
            for chunk in resp:
                delta = chunk.choices[0].delta.content
                if delta:
                    full += delta
                    placeholder.write(full)
            _model_in_use["name"] = _m
            log_event(action, ok=True, dur=time.time() - _t0, extra=f"model={_m} out_len={len(full)}")
            return full
        except Exception as e:
            _last = e
            if full:
                break  # 已经向页面吐过字了：不能再换名重试，否则内容会出现两份
            if not _is_model_error(e):
                break
    log_event(action, ok=False, dur=time.time() - _t0, extra=type(_last).__name__ if _last else "unknown")
    raise _last if _last else RuntimeError("LLM 调用失败（无可用模型名）")

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


def _latest_period_items(chart_data: list) -> list:
    """从 chart_data 中只挑出最新报告期的条目。

    圆环图表示「同一时点的构成占比」，跨报告期混排（2025年报营收 +
    2026中报净利润……）在语义上不成立，这里按期别前缀分组后取最新一期。
    无期别前缀的条目（如「货币资金」）作为兜底整体返回。
    """
    pat = re.compile(r"(20\d{2})\s*(年报|中报|半年报|一季报|三季报)")
    order = {"一季报": 3, "中报": 6, "半年报": 6, "三季报": 9, "年报": 12}
    best_key, grouped = None, {}
    plain = []
    for d in chart_data or []:
        name = str(d.get("item") or "").replace(" ", "")
        m = pat.search(name)
        if not m:
            plain.append(d)
            continue
        key = (int(m.group(1)), order.get(m.group(2), 0))
        grouped.setdefault(key, []).append(d)
        if best_key is None or key > best_key:
            best_key = key
    if grouped:
        return grouped[best_key]
    return plain


def _trend_chrono(trend_rows: list) -> list:
    """把趋势数据按报告期「由早到晚」排序。

    ⚠️ 关键：`build_annual_trend()` 走东财接口是按 REPORT_DATE **倒序** 返回的
    （2025年报在最前、2021年报在最后）。若直接拿去绘图/解读，会出现三处错误：

      1. X 轴时间倒置 —— 2025 在最左、2021 在最右，与阅读习惯相反；
      2. 「最新一期同比」实际算的是**最早一期**（rev[-1] vs rev[-2]）；
      3. 峰值/谷值、复合增速的起点终点颠倒，文案与图形互相矛盾。

    统一在此处升序排序，绘图与文字解读都从这里取数。
    无法解析出年份的条目保留原相对顺序放在最前。
    """
    def _yr(r):
        m = re.search(r"(20\d{2})", str(r.get("period") or ""))
        return int(m.group(1)) if m else 0

    return sorted(trend_rows or [], key=_yr)


def render_trend_dual_axis(trend_rows: list) -> bytes | None:
    """营收 / 归母净利润趋势（双 Y 轴）。

    营收量级常为净利润的 5~30 倍，单轴折线会把净利线压扁成贴地直线，
    改用左右双轴分别承载两个量级。X 轴按时间由早到晚排列。
    """
    rows = []
    for r in _trend_chrono(trend_rows):
        rev = r.get("revenue") if isinstance(r.get("revenue"), (int, float)) else None
        npf = r.get("net_profit") if isinstance(r.get("net_profit"), (int, float)) else None
        if rev is not None or npf is not None:
            rows.append((str(r.get("period", "")), rev, npf))
    if len(rows) < 2:
        return None
    labels = [r[0] for r in rows]
    x = list(range(len(rows)))
    has_rev = any(r[1] is not None for r in rows)
    has_np = any(r[2] is not None for r in rows)

    fig, ax1 = plt.subplots(figsize=(5.4, 3.2), facecolor=_PAL["card"])
    ax1.set_facecolor(_PAL["card"])
    if has_rev:
        ax1.plot(x, [r[1] for r in rows], color=_PAL["purple"], marker="o",
                 linewidth=2, label="营业收入")
        ax1.set_ylabel("营业收入（亿元）", color=_PAL["purple"], fontsize=9)
        ax1.tick_params(axis="y", labelcolor=_PAL["purple"])
    if has_np:
        ax2 = ax1.twinx() if has_rev else ax1
        ax2.plot(x, [r[2] for r in rows], color=_PAL["gold"], marker="s",
                 linewidth=2, linestyle="--", label="归母净利润")
        if has_rev:
            ax2.set_ylabel("归母净利润（亿元）", color=_PAL["gold"], fontsize=9)
            ax2.tick_params(axis="y", labelcolor=_PAL["gold"])
            ax2.spines["top"].set_visible(False)
        else:
            ax1.set_ylabel("归母净利润（亿元）", color=_PAL["gold"], fontsize=9)

    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, color=_PAL["text"], fontsize=9)
    ax1.spines["top"].set_visible(False)
    ax1.grid(axis="y", color="#E2E8F0", linewidth=0.6, alpha=0.8)
    h1, l1 = ax1.get_legend_handles_labels()
    if has_rev and has_np:
        h2, l2 = ax2.get_legend_handles_labels()
    else:
        h2, l2 = [], []
    ax1.legend(h1 + h2, l1 + l2, loc="upper left", frameon=False,
               labelcolor=_PAL["text"], fontsize=9)
    ax1.set_title("营收与净利润趋势（双轴）", color=_PAL["title"],
                  fontsize=12, fontweight="bold", pad=10)
    return _fig_to_png_bytes(fig)


def _drop_subtotals(chart_data: list) -> list:
    """柱状图取数：剔除与「整体项」重复计量的「部分项」。

    模型抽 chart_data 时常常把「营业总收入」和它的构成「主营业务收入」
    一起塞进同一张柱状图（实测海天味业 288.7 vs 274.0，两根柱子几乎一样高），
    评审看到会直接判定为数据错误。同一张图里若已存在整体项，
    则丢弃其典型分项（主营业务收入 / 「其中…」），保留整体口径。
    仅在剩余条目仍 ≥2 项时才真正丢弃，避免把图削到画不出来。
    """
    items = [d for d in (chart_data or []) if isinstance(d.get("value"), (int, float))]

    def _n(d):
        return str(d.get("item") or "").replace(" ", "")

    _total_marks = ("营业总收入", "总营业收入", "营业收入", "营业总收入")
    has_total = any(
        (any(t in _n(d) for t in _total_marks) and "主营业务收入" not in _n(d))
        for d in items
    )
    if not has_total:
        return items
    _part_marks = ("主营业务收入", "其中")
    parts = [d for d in items if any(t in _n(d) for t in _part_marks)]
    if not parts:
        return items
    _part_ids = {id(d) for d in parts}
    keep = [d for d in items if id(d) not in _part_ids]
    return keep if len(keep) >= 2 else items


def _donut_slice(chart_data: list):
    """圆环图取数：必须是「同一层级、可加总」的一组数，否则占比没有意义。

    返回 (items, title)。优先级：
      1. 分产品/分业务收入构成（名称含"收入"但不是"营业收入/总收入"），≥2 项
         —— 例：酱油收入/蚝油收入/调味酱收入，可加总还原为营收
      2. 最新报告期的同层级条目
    明确排除：
      · 整体项（营业收入/总资产/合计…）与它的分项同时出现 → 会重复计入
      · 利润类（归母净利润/扣非…）与收入类混算 → 不是同一层级的"构成"
    若凑不出一组同层级的数，返回空，宁可不画也不误导。
    """
    items = [d for d in (chart_data or []) if isinstance(d.get("value"), (int, float))]
    if len(items) < 2:
        return [], ""

    def _name(d):
        return str(d.get("item") or "").replace(" ", "")

    def _is_total(n):
        # 只认「总量」概念，避免把「营业成本」这类同层级科目误判为整体项
        if n in ("营收", "收入", "合计", "总计"):
            return True
        return any(t in n for t in ("营业总收入", "营业收入", "总收入", "总营收"))

    def _is_profit(n):
        return any(t in n for t in ("净利润", "净利", "利润总额", "毛利", "扣非"))

    # 1) 分产品 / 分业务收入构成
    parts = [d for d in items if "收入" in _name(d) and not _is_total(_name(d))]
    if len(parts) >= 2:
        return parts, "收入构成占比"

    # 2) 剔除整体项与利润项后，取最新报告期
    rest = [d for d in items if not _is_total(_name(d)) and not _is_profit(_name(d))]
    if len(rest) >= 2:
        picked = _latest_period_items(rest)
        return picked, "指标占比与分布"

    return [], ""


def render_metric_donut(chart_data: list, title: str = "关键财务指标占比") -> bytes | None:
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
    ax.set_title(title, color=_PAL["title"], fontsize=12, pad=10, fontweight="bold")
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

# 【唯一真源】行业基准线直接取 scoring.INDUSTRY_PROFILE ——
# 评分阈值、四象限基准线、页面基准条三处必须同源，否则会出现
# 「基准条写着消费 ROE 18% 优秀，规则却按 15% 打分」的自相矛盾。
# scoring 缺失时退回下面的字面量兜底（与 scoring 内的值保持一致）。
_INDUSTRY_BASE = {
    "通用":   (15.0, 10.0),   # 通用制造/多元
    "消费":   (18.0, 8.0),    # 高端消费 ROE 高、增速稳
    "金融":   (11.0, 6.0),    # 银行/保险：ROE 11% 已属优秀，增速锚定息差
    "新能源": (12.0, 20.0),   # 高增长赛道，增速基准上调
    "医药":   (14.0, 12.0),   # 研发驱动，成长性权重高
    "互联网": (14.0, 15.0),   # 平台型，增长与回报并重
}
if scoring is not None and getattr(scoring, "INDUSTRY_PROFILE", None):
    _INDUSTRY_BASE = {k: tuple(v) for k, v in scoring.INDUSTRY_PROFILE.items()}
    _SCATTER_ROE_LINE, _SCATTER_GROW_LINE = _INDUSTRY_BASE["通用"]


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
        f"• 对标口径：象限基准线随【行业财务基准库】切换（当前「{industry}」= ROE {roe_line:g}% / 增速 {grow_line:g}%）。"
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
    max_name, max_val = rows_sorted[0]
    min_name, min_val = rows_sorted[-1]
    lines = []
    # ⚠️ 占比只在「同一层级、可加总」时才成立。
    #    若图表里既有「营业收入」整体项又有其分项（酱油收入…）+ 利润项，
    #    直接相加会把整体和部分重复计入，占比失去意义 —— 此时只描述量级，不给占比。
    _same_level, _lv_title = _donut_slice(
        [{"item": _n, "value": _v} for _n, _v in rows_sorted]
    )
    _share_ok = False
    if len(_same_level) >= 2:
        _lv_total = sum(float(d.get("value") or 0) for d in _same_level)
        if _lv_total > 0 and str(max_name) in [str(d.get("item")) for d in _same_level]:
            _lv_share = max_val / _lv_total * 100
            lines.append(
                f"• <b>{max_name}</b> 数值最高（<b>{max_val:,.2f}</b>），"
                f"占「{_lv_title}」合计的 <b>{_lv_share:.1f}%</b>，是该公司当前阶段的核心量级。"
            )
            _share_ok = True
    if _share_ok:
        lines.append(
            f"• 最低项 <b>{min_name}</b>（{min_val:,.2f}），"
            f"与最高项相差 <b>{max_val - min_val:,.2f}</b>，金额分布呈明显梯度。"
        )
    else:
        lines.append(
            f"• <b>{max_name}</b> 数值最高（<b>{max_val:,.2f}</b>）；"
            f"<b>{min_name}</b> 最低（{min_val:,.2f}），"
            f"两者相差 <b>{max_val - min_val:,.2f}</b>，指标结构呈明显梯度。"
        )
    # 中位对比
    if len(rows_sorted) >= 3:
        mid = rows_sorted[len(rows_sorted) // 2]
        lines.append(
            f"• 中位项 <b>{mid[0]}</b>（{mid[1]:,.2f}）居于 {len(rows_sorted)} 项中第 "
            f"{len(rows_sorted) // 2 + 1} 位，分布偏度可通过最大/最小比 "
            f"<b>{(max_val / min_val) if min_val else 0:.1f}×</b> 看出。"
        )
    if not _share_ok:
        # 说明文字要贴合图上真实内容，不能一口咬定「含构成项」——
        # 图里可能只有「营业收入 + 归母净利润」这类不同科目，并不存在分项。
        _names_all = [str(_n) for _n, _v in rows_sorted]
        _has_income = any("收入" in _nm for _nm in _names_all)
        _has_profit = any(("净利润" in _nm or "利润" in _nm) for _nm in _names_all)
        if _has_income and _has_profit:
            _note = (
                "• 说明：本图混合了<b>收入类</b>（如营业收入）与<b>利润类</b>"
                "（如归母净利润）科目，仅用于比较量级；"
                "<b>不同科目的金额不可直接相加</b>，占比分析请看右侧圆环图。"
            )
        else:
            _note = (
                "• 说明：本图为不同科目的金额量级对比，"
                "<b>不可直接相加求占比</b>，占比分析请看右侧圆环图。"
            )
        lines.append(_note)
    body = "<br>".join(lines)
    return f'<div class="insight-card">📖 <b>图表解读</b>（自动生成，基于本期数据）<br>{body}</div>'


def build_trend_insight(trend_rows: list) -> str:
    """多年趋势自动解读（涨跌/峰值/谷值/累计涨幅）。

    ⚠️ 必须按时间升序解读，否则「最新一期同比」「复合增速」会把最早一期
    当成最新一期来算（详见 _trend_chrono 的说明）。
    """
    rows = [r for r in _trend_chrono(trend_rows) if isinstance(r.get("revenue"), (int, float))]
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
    """RAG 检索过程可视化：Top-K 章节、相似度、页码、摘要（HTML 字符串）。

    ⚠️ 片段来自外部文档（年报 PDF / 用户上传文件），必须转义后再嵌进 HTML ——
    否则正文里一段 `<img src=x onerror=...>` 就能改变页面结构。
    """
    if not rag_hits:
        return ""
    import html as _html
    rows = []
    for i, h in enumerate(rag_hits[:8], 1):
        # 兼容多种字段命名
        page = h.get("page") or h.get("page_number") or h.get("metadata", {}).get("page") or "?"
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = _html.escape(str(page))
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
        text = _html.escape(text)  # 外部文档内容一律转义，杜绝标签注入
        rows.append(
            f'<div class="rag-hit">'
            f'<span class="rag-rank">#{i}</span>'
            f'<span class="rag-page">📄 年报第 {page} 页</span>'
            f'&nbsp;|&nbsp;<span class="{sim_cls}">相似度 {sim:.2f}</span>'
            f'<br><span style="color:#334155;font-size:12px;line-height:1.6;">{text}</span>'
            f'</div>'
        )
    return (
        '<div style="margin: 10px 0 6px 0;color:#1E3A8A;font-weight:700;font-size:14px;">'
        f'📚 RAG 检索过程（Top-{len(rows)} 相关章节 · 按相似度排序）</div>'
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
        for _k in (f"{key_prefix}_pdf_path", f"{key_prefix}_pdf_year", f"{key_prefix}_pdf_tag",
                   f"{key_prefix}_pdf_title", f"{key_prefix}_rag_status"):
            st.session_state.pop(_k, None)
        if key_prefix == "single":
            st.session_state.pop("report", None)
            st.session_state.pop("analysis", None)
            st.session_state["messages"] = []
        with st.spinner("正在联网获取数据……"):
            hits = ds.search_stock(kw.strip())
            _kw = kw.strip()
            if hits:
                # 精确匹配优先：关键词是 6 位代码、或与名称完全一致 → 置顶，
                # 避免"输入 600519 却因为模糊匹配拿到别的公司"（评审 7.3）
                hits.sort(key=lambda h: 0 if (h.get("code") == _kw or h.get("name") == _kw) else 1)
            if not hits:
                st.error("未找到该股票，请检查代码或名称（支持 A 股 6 位代码）。")
            else:
                code = hits[0]["code"]
                name = hits[0]["name"]
                # 命中多个候选且非精确匹配 → 显式提示其他候选，让用户可确认
                if len(hits) > 1:
                    _others = "、".join(f'{h["name"]}（{h["code"]}）' for h in hits[1:4])
                    st.warning(
                        f"⚠️「{_kw}」匹配到 {len(hits)} 个结果，当前选择 **{name}（{code}）**。"
                        f"其他候选：{_others}。如需切换到其他公司，请直接输入 6 位股票代码后重新获取。"
                    )
                text = ds.build_summary_text(code, name=name, periods=periods)
                if not text:
                    st.error("数据获取失败（可能是网络受限或该股票无公开数据），可改用手动粘贴。")
                else:
                    st.session_state[f"{key_prefix}_online"] = text
                    st.session_state[f"{key_prefix}_online_name"] = name
                    st.session_state[f"{key_prefix}_online_code"] = code
                    st.session_state[f"{key_prefix}_online_org"] = hits[0].get("orgId", "")
                    # 自动匹配行业财务基准库（不再直接修改已渲染的 industry radio，
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
        # 数据来源完整性：若发生过 https→http 降级，如实说明（评审 7.3）
        try:
            _insec = ds.insecure_fallbacks()
        except Exception:
            _insec = []
        if _insec:
            st.caption(
                f"⚠️ 本次取数中有 {len(_insec)} 个接口由 HTTPS 降级为 HTTP（明文连接），"
                "数据来自公开接口，仅供参考；如长期出现请检查网络是否拦截了 HTTPS。"
            )
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
                                # 文档标签：公告日期 + 报告类型。用于给向量集合命名 ——
                                # 只按 code+year 命名会让"同一年披露的年报/半年报/修订版"
                                # 共用同一个集合，检索到的是另一份报告的原文与页码。
                                _tag = rag.doc_tag_for(reports[idx].get("date"), rtype) if rag is not None else ""
                                _pdf_path = Path(tempfile.gettempdir()) / f"{code}_{_yr}_{rtype}.pdf"
                                try:
                                    _pdf_path.write_bytes(pdf)
                                except Exception:
                                    _pdf_path = None
                                st.session_state[f"{key_prefix}_pdftext"] = ptxt
                                st.session_state[f"{key_prefix}_pdf_title"] = reports[idx]["title"]
                                st.session_state[f"{key_prefix}_pdf_year"] = _yr
                                st.session_state[f"{key_prefix}_pdf_tag"] = _tag
                                st.session_state[f"{key_prefix}_pdf_path"] = str(_pdf_path) if _pdf_path else ""
                                st.session_state.pop(f"{key_prefix}_rag_status", None)
                                st.success(f"✅ 已解析《{reports[idx]['title']}》，提取 {len(ptxt)} 字。")
                                # 【RAG 接通关键步】立即按页切片并构建向量索引
                                # ⚠️ 不再依赖"双模型润色"开关：那个开关只决定要不要
                                #    花钱调千问润色；证据检索是**分析链路的一部分**，
                                #    若跟着开关走，会出现"关掉开关 → 结构化分析仍用年报证据、
                                #    研报却不用"的入口不一致。
                                if rag is not None and _pdf_path and code and _yr:
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

                                    _st = rag.build_index_from_pdf(
                                        _pdf_path, code, _yr, progress_cb=_pcb, doc_tag=_tag
                                    )
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
        <h1 style="color:#F8FAFC !important;font-size:30px;font-weight:800;margin:0 0 4px 0;letter-spacing:1px;">📈 金融智能投研 <span class="gold" style="color:#F5C563 !important;">Agent</span></h1>
        <p style="color:#DBEAFE !important;font-size:14px;margin:0;">输入股票代码即可自动取数，一键完成结构化分析、四维评分、趋势图表与研报生成</p>
        <p class="tagline" style="color:#A9C6E8 !important;font-size:12px;margin-top:6px;">在线取数（巨潮+东财）· 年报 PDF 解析 · 双公司对比 · 六大行业财务基准库 · 多轮追问</p>
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
        "<li>选择行业财务基准（同业对标）</li>"
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
        st.caption("📚 RAG 引擎已加载 · 按页检索 · Top-K=8（分析）/ 6（追问）")
    else:
        st.caption("🟡 rag_engine 模块未加载")
    st.markdown("---")
    if st.session_state.get("demo_mode"):
        st.markdown("👤 当前访问：**🎤 评委演示模式（免登录）**")
    else:
        st.markdown(f"👤 当前登录：**{APP_USER}**")
    # ---- 安全提醒：仍在用内置默认凭据时提示运维方改密（不阻断演示）----
    if USING_DEFAULT_CREDS:
        st.markdown(
            '<div class="sb-card" style="border-left:3px solid #D9A441;">'
            '<div class="sb-title" style="color:#B45309 !important;">⚠️ 安全提醒</div>'
            '<div style="font-size:11.5px;line-height:1.7;color:#78350F !important;">'
            '当前使用<b>内置默认凭据</b>。<br>公开部署请在 Secrets 中设置 '
            '<code>APP_USER</code> / <code>APP_PASSWORD</code> / <code>DEMO_KEY</code> '
            '（模板见 <code>.streamlit/secrets.toml.example</code>）。'
            '</div></div>',
            unsafe_allow_html=True,
        )
    # 额度：数字 + 可视化进度条（金色渐变，用满变红）
    _used = _read_usage()
    _ratio = min(_used / max(DAILY_LIMIT, 1), 1.0)
    _bar_color = "linear-gradient(90deg,#E8B84B,#F5D98B)" if _ratio < 0.8 else "linear-gradient(90deg,#D9662F,#E8B84B)"
    st.markdown(
        f'<div class="sb-quota-head">🔋 今日剩余分析额度 <b>{max(DAILY_LIMIT - _used, 0)} / {DAILY_LIMIT}</b></div>'
        f'<div class="sb-quota-bar"><div style="width:{_ratio * 100:.0f}%;background:{_bar_color};"></div></div>',
        unsafe_allow_html=True,
    )
    # ---- 三类调用统一记账（评审 7.2）：DeepSeek / 千问润色 / Embedding 分开统计 ----
    _p_used = _budget.read(_budget.BUCKET_POLISH)
    _e_used = _budget.read(_budget.BUCKET_EMBED)
    st.markdown(
        f'<div class="sb-quota-head" style="margin-top:6px;">📊 今日用量 '
        f'<b>润色 {_p_used}</b> / <b>向量化 {_e_used}</b> 批</div>',
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

# ---- 行业财务基准库：主界面横向按钮（不再藏在侧边栏下拉框里） ----
# ⚠️ 关键：st.radio 一旦指定 key，index 参数在第二次渲染起会被忽略，
#    值完全由 st.session_state[key] 决定。所以「自动匹配行业」不能只靠 index，
#    必须在渲染前显式写 session_state，否则会一直停留在上一只股票的行业
#    （例：先分析招商银行选了"金融"，再分析海天味业时仍停在"金融"，
#      导致评分走银行口径、偿债能力变成"数据不足"）。
_auto_matched = st.session_state.get("single_auto_industry", "")
_manual_pick = st.session_state.get("industry_manual", "")
_applied_auto = st.session_state.get("_industry_auto_applied", "")

if _auto_matched and _auto_matched != _applied_auto:
    # 换公司 → 行业自动跟随（并作废之前的手动选择）
    st.session_state["industry_widget"] = _auto_matched
    st.session_state["_industry_auto_applied"] = _auto_matched
    st.session_state["industry_manual"] = ""
    _manual_pick = ""
elif "industry_widget" not in st.session_state:
    st.session_state["industry_widget"] = (
        _manual_pick if _manual_pick in INDUSTRY_KB else "通用"
    )

_default_industry = st.session_state.get("industry_widget", "通用")
if _default_industry not in INDUSTRY_KB:
    _default_industry = "通用"
    st.session_state["industry_widget"] = "通用"

# 如果已经在线取数，标题直接显示公司名称+代码+匹配行业
_single_name = st.session_state.get("single_online_name", "")
_single_code = st.session_state.get("single_online_code", "")
if _single_name and _single_code:
    st.markdown(
        f"##### 📚 行业财务基准库（同业对标） —— 当前分析对象：**{_single_name}（{_single_code}）** → **{_default_industry}**\n"
        f"*将按该行业基准做差异化评分与象限判定，基准条见下方*"
    )
else:
    st.markdown("##### 📚 行业财务基准库（同业对标） —— 请选择待分析公司所属行业（将按该行业基准做差异化评分）")


def _on_industry_change():
    """用户手动点击才记录为「手动选择」；自动跟随写入的值不算手动。"""
    _v = st.session_state.get("industry_widget", "")
    if _v != st.session_state.get("single_auto_industry"):
        st.session_state["industry_manual"] = _v


industry = st.radio(
    "行业",
    list(INDUSTRY_KB.keys()),
    index=list(INDUSTRY_KB.keys()).index(_default_industry),
    horizontal=True,
    label_visibility="collapsed",
    key="industry_widget",
    on_change=_on_industry_change,
)
# 如果是自动匹配过来的，提示一下
if _auto_matched and industry == _auto_matched:
    st.caption(f"🤖 已根据在线取数结果自动匹配行业：**{industry}**（如需切换，点击上方按钮即可）")
else:
    # 行业细分范围由下方「同业对标基准条」呈现，这里只报当前行业，避免文案重复
    st.caption(f"当前行业：**{industry}**")
    if _auto_matched and industry != _auto_matched:
        st.warning(
            f"⚠️ 系统识别到的行业是 **{_auto_matched}**，当前却是 **{industry}**，"
            f"评分基准与散点图基准会按「{industry}」计算，请确认是否选错。"
        )

# ---- 同业对标基准条：把「对标」这个动作直接摆到用户面前 ----
# 同一组财务数据只有在对标了行业基准后才具备可比性（银行 ROE 11% 已属优秀，
# 白酒 ROE 11% 则严重落后）——「同业对标」是金融分析的规定动作，必须显式标注。
_base_roe, _base_grow = _industry_base(industry)
st.markdown(
    f'<div class="base-bar">'
    f'📐 <b>同业对标基准（{industry}）</b>'
    f' · ROE 优秀线 <span class="bb-num">{_base_roe:g}%</span>'
    f' · 营收增速稳健线 <span class="bb-num">{_base_grow:g}%</span>'
    f'<br><span class="bb-note">四维评分、四象限定位与散点基准线均按该行业口径计算；'
    f'切换行业按钮，全部基准随之调整。</span>'
    f'</div>',
    unsafe_allow_html=True,
)

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
    def _content_hash(text: str) -> str:
        """数据内容指纹（短哈希）。只记"数据变没变"，不落原文，避免日志/状态里囤正文。"""
        import hashlib
        t = (text or "").strip()
        return hashlib.md5(t.encode("utf-8", errors="ignore")).hexdigest()[:10] if t else "-"

    # 【统一清空】根据 公司/代码/报告期数/年报/数据来源/行业/数据内容 生成数据指纹，
    # 指纹变化（切公司、换报告期、换年报、换行业、清空数据、**改了粘贴的正文**）时统一清空旧结果。
    # 只比标签不比内容会漏报：把手写文本里的 ROE 从 5% 改成 20%，标签完全一样，
    # 旧分析与旧研报就不会失效 —— 页面于是显示"基于旧数据"的结论。
    def _data_sig():
        return "|".join([
            src_mode,
            st.session_state.get("single_online_code", ""),
            st.session_state.get("single_online_name", ""),
            str(st.session_state.get("single_periods", "")),
            st.session_state.get("single_pdf_title", ""),
            industry,
            "hasdata" if raw_data else "nodata",
            _content_hash(raw_data),
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
        _tag = st.session_state.get("single_pdf_tag", "")
        if rag is None or not _code or not _year or not st.session_state.get("single_pdftext"):
            return []
        if not rag.has_index(_code, _year, _tag):
            # 兼容老会话：索引可能是按"无 doc_tag"的老命名建的
            _tag = rag.find_index_tag(_code, _year) or _tag
            if not rag.has_index(_code, _year, _tag):
                return []
        try:
            return rag.retrieve(query, _code, _year, top_k=top_k, doc_tag=_tag)
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
                    '  "metrics": [{"item": "指标名（必须带报告期前缀，如「2025年报营业收入」）", "value": 数值, "unit": "单位", "comment": "一句话解读"}],\n'
                    '  "chart_data": [{"item": "指标名（带报告期前缀）", "value": 数值}],\n'
                    '  "trend": [{"period": "2021年报", "revenue": 数值, "net_profit": 数值}],\n'
                    '  "risks": ["风险1", "风险2", "风险3"],\n'
                    '  "highlights": ["亮点1", "亮点2"]\n'
                    "}\n"
                    "⚠️ 口径一致性（最重要）：metrics 与 chart_data 的所有条目**必须取自同一个报告期**，"
                    "优先使用**最近一个年报**（如「2025年报」。若数据中只有中期报告则用最近一期并明确标注）；"
                    "**禁止把年报、一季报、中报、三季报的数据混在同一张表或同一张图里**；\n"
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
                    json_mode=True,  # 服务端 JSON 模式；不支持时自动裸调兜底
                )
                analysis = parse_json_loose(raw_json)

                # 3) 规则引擎评分（固定阈值 → 同样的数据永远得到同样的分数）
                if scoring is not None:
                    _metrics_for_score = {}
                    _score_period = ""
                    # ⚠️ 只有在「在线取数」模式下才允许取在线指标做评分。
                    #    历史 bug：这里直接读会话里的 single_online_code，
                    #    于是"先在线看了公司 A → 切到手动粘贴公司 B"时，
                    #    评分仍去拉 A 的财务指标（甚至包括 A 的年报 ROE），
                    #    页面显示的是 B 的数据、分数却来自 A。
                    _is_online = src_mode.startswith("🌐")
                    _oc = st.session_state.get("single_online_code", "") if _is_online else ""
                    if _oc:
                        try:
                            # 直接只取年报（annual_only=True），而不是"取最近 N 期再筛年报"：
                            # 报告期数选小了（如 3 期）时，最近 3 期可能全是一季报/中报/三季报，
                            # 明明存在年报却筛不到，于是退回中期口径 —— 评分基准期随选择器漂移。
                            _fina = ds.get_financial_indicators(
                                _oc,
                                periods=int(st.session_state.get("single_periods", 4) or 4),
                                annual_only=True,
                            )
                            if not _fina:
                                # 该公司确实还没有年报（如新上市/次新股）→ 退回全部报告期
                                _fina = ds.get_financial_indicators(
                                    _oc, periods=int(st.session_state.get("single_periods", 4) or 4)
                                )
                            if _fina:
                                # ⚠️ 口径统一：一律用【最近一个年报】评分。
                                #    中期报告（一季报/中报/三季报）的金额是年初至今累计值，
                                #    ROE 也是期间值（海天味业中报 ROE 10.07% vs 年报 19.59%），
                                #    与年报不可比。混用会让同一页出现两个 ROE，评分也随之失真。
                                _annual = [r for r in _fina if (r.get("report_type") or "") == "年报"]
                                _pick = _annual[0] if _annual else _fina[0]
                                _metrics_for_score = _pick
                                _score_period = str(_pick.get("report_name") or "")
                                if not _annual:
                                    _score_period += "（无年报记录，暂用中期报告口径）"
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
                                    json_mode=True,
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
                    analysis["score_engine"] = "规则引擎 v2（同业对标 + 覆盖率门槛）"
                    # 覆盖率 / 置信标签 / 说明：低覆盖时不许给"满分综合评价"
                    analysis["score_coverage"] = sc.get("coverage")
                    analysis["score_confidence"] = sc.get("confidence")
                    analysis["score_notes"] = sc.get("notes") or []
                    analysis["score_expected"] = sc.get("expected_metrics")
                    analysis["score_provided"] = sc.get("provided_metrics")
                    analysis["score_industry"] = sc.get("industry")
                    # 保存标准化原始指标（供 ROE×营收增速 散点图取数）
                    analysis["metrics_raw"] = dict(_metrics_for_score or {})
                    analysis["score_period"] = _score_period
                    analysis["score_source"] = (
                        f"在线财务指标（{_score_period}）" if _score_period
                        else ("在线财务指标（最新报告期）" if _oc else "文本抽取")
                    )

                # 4) 严格校验（字段/类型/范围/缺失值），异常项修复或剔除
                analysis, v_issues = validate_analysis(analysis)
                for _iss in v_issues:
                    st.warning(f"输出校验：{_iss}")

                # 4b) 证据核对：把"看起来专业"的数字与页码引用，与真正拿到的来源对一遍。
                #     格式校验只能保证"字段合法"，保证不了"数字有出处"——
                #     一个凭空写出的 99999 亿元营收、一个第 99999 页的引用，
                #     格式上完全合法。这一步专门盯这类问题。
                _pg_count = (st.session_state.get("single_rag_status") or {}).get("pages")
                _pg_range = (1, int(_pg_count)) if isinstance(_pg_count, int) and _pg_count > 0 else None
                for _iss in verify_evidence(analysis, raw_data, page_range=_pg_range):
                    st.warning(f"证据核对：{_iss}")

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
            # 指标覆盖率与置信标签：让"这个分数有多可信"变成页面上的显式信息，
            # 而不是把一个缺数据的 10.0 分当成全面优秀
            _cov = analysis.get("score_coverage")
            if isinstance(_cov, (int, float)):
                _conf = analysis.get("score_confidence") or ""
                _icon = {"充分": "🟢", "局部评价": "🟡", "信息不足": "🔴"}.get(_conf, "⚪")
                st.markdown(
                    f"{_icon} **指标覆盖率 {_cov * 100:.0f}%**"
                    + (f" · 置信标签：**{_conf}**" if _conf else "")
                    + (f" · 评分行业口径：**{analysis.get('score_industry')}**"
                       if analysis.get("score_industry") else "")
                )
            for _nt in (analysis.get("score_notes") or []):
                st.warning(_nt)
            _sp = analysis.get("score_period") or ""
            _mraw_show = analysis.get("metrics_raw") or {}
            if _sp:
                _sp_txt = f"🎯 **评分基准期：{_sp}**"
                if isinstance(_mraw_show.get("roe"), (int, float)):
                    _sp_txt += f" · ROE(加权) {_mraw_show['roe']:g}%"
                if isinstance(_mraw_show.get("revenue_yoy"), (int, float)):
                    _sp_txt += f" · 营收同比 {_mraw_show['revenue_yoy']:g}%"
                _sp_txt += "（一律采用年报口径：中期报告为年初至今累计值，与年报不可比）"
                st.markdown(_sp_txt)
                st.caption(
                    "ℹ️ 下方「指标明细」表由大模型从原始数据中提取，可能与评分基准期不同；"
                    "如两者报告期不一致，以标注的报告期为准。"
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
            # 剔除与整体项重复计量的分项（如「主营业务收入」vs「营业收入」），
            # 否则同一张柱状图会出现两根几乎同高的柱子，看起来像数据错误
            chart_data = _drop_subtotals(analysis.get("chart_data") or [])
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
            # 圆环图（看同一层级可加总的一组数的构成占比）
            # 只取「分产品收入构成」或「最新报告期的同层级条目」，
            # 避免「整体营收 + 它的分项 + 净利润」混算导致占比失去意义
            chart_data_right, _donut_title = _donut_slice(analysis.get("chart_data") or [])
            if len(chart_data_right) >= 2:
                st.markdown(f"##### 🍩 {_donut_title}")
                _donut_png = render_metric_donut(chart_data_right, title=_donut_title)
                if _donut_png:
                    show_png(_donut_png, alt="指标占比圆环图")
                _dn = "、".join(str(d.get("item") or "") for d in chart_data_right)
                st.caption(f"构成项：{_dn}（同一层级、可加总，占比才有意义）")
            else:
                st.caption("ℹ️ 圆环图需要同一层级、可加总的一组数（如各产品收入构成）；"
                           "当前指标含整体项与分项混排，占比无意义，已隐藏。")
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
                _trend_png = render_trend_dual_axis(_trend_annual)
                if _trend_png:
                    show_png(_trend_png, alt="营收与净利润趋势图（双轴）")
                st.markdown(build_trend_insight(_trend_annual), unsafe_allow_html=True)
            else:
                # 手动模式：使用 LLM 抽取的 trend
                trend = analysis.get("trend") or []
                if len(trend) >= 2:
                    _trend_png = render_trend_dual_axis(trend)
                    if _trend_png:
                        show_png(_trend_png, alt="营收与净利润趋势图（双轴）")
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
            _sp_lbl = analysis.get("score_period") or ""
            st.markdown(
                "##### 🎯 ROE × 营收增速 · 象限定位"
                + (f"（{_sp_lbl}口径）" if _sp_lbl else "")
            )
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
                    f'<span style="color:#64748B">切换行业财务基准库按钮，基准线会随之调整。</span>'
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
        # 【RAG 接通】索引已在"下载并解析该报告"时构建完成，
        # 这里按研报主题检索相关章节（含页码），注入 DeepSeek 上下文。
        # ⚠️ 检索【不再绑定】"双模型润色"开关：那个开关只控制千问润色（花钱的步骤），
        #    若把检索也绑上去，会出现"关掉开关后：结构化分析与追问仍在用年报证据、
        #    研报却不用"的入口不一致 —— 同一份报告，证据口径必须一致。
        _rag_hits: list = []
        if rag is not None:
            _code = st.session_state.get("single_online_code", "")
            _yr = st.session_state.get("single_pdf_year", "")
            _tag = st.session_state.get("single_pdf_tag", "")
            _pdf_path = st.session_state.get("single_pdf_path", "")
            if st.session_state.get("single_pdftext") and _code and _yr:
                # 兜底：如果索引还没建（如旧会话数据），现场补建一次
                if not rag.has_index(_code, _yr, _tag) and _pdf_path and Path(_pdf_path).exists():
                    with st.spinner("正在补建年报向量索引……"):
                        _built = rag.build_index_from_pdf(_pdf_path, _code, _yr, doc_tag=_tag)
                        st.session_state["single_rag_status"] = _built
                if rag.has_index(_code, _yr, _tag):
                    with st.spinner("正在从年报全文检索相关章节……"):
                        _rag_hits = rag.retrieve(
                            f"{online_name or '目标公司'} 财务表现 经营讨论 业务亮点 风险因素 未来展望",
                            _code, _yr, top_k=6, doc_tag=_tag,
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
                if _rag_hits:
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
                # 关键：清掉流式初稿占位区，交给下方统一渲染【最终稿】。
                # 否则这次点击里页面显示的是润色前初稿，而下载按钮给的是润色后终稿，
                # 同一个按钮触发的"看到的内容"和"下载的内容"不一致。
                box.empty()
            except Exception as e:
                box.empty()
                st.error(f"研报生成失败：{e}")

    report = st.session_state.get("report")
    if report:
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

    # 【统一清空】对比对象变化时清空旧对比报告与旧评分对照
    # 指纹必须包含**数据内容**：只比公司名/代码的话，把 A 的营收从 100 亿改成 900 亿，
    # 指纹不变 → 旧对比结论继续挂在页面上，与实际输入脱节。
    def _cmp_sig():
        import hashlib
        _h = lambda t: hashlib.md5((t or "").strip().encode("utf-8", errors="ignore")).hexdigest()[:10] if (t or "").strip() else "-"
        return "|".join([
            str(st.session_state.get("name_a", "")),
            str(st.session_state.get("name_b", "")),
            st.session_state.get("cmp_a_online_code", ""),
            st.session_state.get("cmp_b_online_code", ""),
            _h(data_a), _h(data_b),
        ])

    _csig_now = _cmp_sig()
    _csig_old = st.session_state.get("cmp_sig")
    if _csig_old and _csig_old != _csig_now:
        _stale = []
        for _k in ("cmp_report", "cmp_scores"):
            if st.session_state.get(_k):
                st.session_state.pop(_k, None)
                _stale.append(_k)
        if _stale:
            st.info("🔄 对比对象或输入数据已变化，旧的对比报告 / 评分对照已清空，请重新点击「⚖️ 开始对比分析」。")
    st.session_state["cmp_sig"] = _csig_now

    def _cmp_side_metrics(prefix, is_online, text, name):
        """取一侧公司的标准指标（在线优先，且只取年报口径）→ (metrics, 期别标签, 行业)。"""
        _ind = (ds.guess_industry(name) if name else "") or industry
        met, per = {}, ""
        if is_online and scoring is not None:
            _c = st.session_state.get(f"{prefix}_online_code", "")
            if _c:
                try:
                    _f = ds.get_financial_indicators(_c, periods=4, annual_only=True)
                    if not _f:
                        _f = ds.get_financial_indicators(_c, periods=4)
                    if _f:
                        met = _f[0]
                        per = str(_f[0].get("report_name") or "")
                except Exception:
                    met = {}
        if not met and scoring is not None and (text or "").strip():
            met = scoring.extract_metrics_regex(text)
            per = "文本抽取"
        return met, per, _ind

    cmp_btn = st.button("⚖️ 开始对比分析", disabled=not (data_a and data_b), use_container_width=True, type="primary")

    if cmp_btn and data_a and data_b:
        # 【程序算分】对比不该只让模型"读两段文本自由发挥"：
        # 两家公司各自跑一遍固定规则评分，差额与对照表由程序生成，模型只负责解释。
        if scoring is not None:
            with st.spinner("正在计算两家公司的规则评分……"):
                try:
                    _ma, _pa, _ia = _cmp_side_metrics("cmp_a", use_online_a, data_a, name_a)
                    _mb, _pb, _ib = _cmp_side_metrics("cmp_b", use_online_b, data_b, name_b)
                    st.session_state["cmp_scores"] = {
                        "a": {"name": name_a, "scores": scoring.compute_scores(_ma, _ia),
                              "period": _pa, "industry": _ia},
                        "b": {"name": name_b, "scores": scoring.compute_scores(_mb, _ib),
                              "period": _pb, "industry": _ib},
                        "cross_industry": _ia != _ib,
                    }
                except Exception as _e:
                    st.session_state.pop("cmp_scores", None)
                    st.warning(f"规则评分对照计算失败（不影响下方文字对比）：{_e}")
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
                    box.empty()  # 交给下方统一渲染，保证"看到的内容"= "下载的内容"
                except Exception as e:
                    box.empty()
                    st.error(f"对比分析失败：{e}")

    # ---- 规则评分对照（程序计算，可复现；不再是"模型读两段文本自由发挥"） ----
    _cs = st.session_state.get("cmp_scores")
    if _cs:
        _ca, _cb = _cs["a"], _cs["b"]
        _na, _nb = _ca["name"], _cb["name"]
        _colA = f"{_na}｜{_ca['industry']}"
        _colB = f"{_nb}｜{_cb['industry']}"
        _fmt = lambda v: ("—" if v is None else v)
        _rows = []
        for _d in ["盈利能力", "成长性", "偿债能力", "现金流质量"]:
            _rows.append({
                "维度": _d,
                _colA: _fmt(_ca["scores"]["scores"].get(_d)),
                _colB: _fmt(_cb["scores"]["scores"].get(_d)),
            })
        _rows.append({
            "维度": "综合评分",
            _colA: _fmt(_ca["scores"]["total_score"]),
            _colB: _fmt(_cb["scores"]["total_score"]),
        })
        _rows.append({
            "维度": "指标覆盖率（可信度）",
            _colA: f"{_ca['scores']['coverage'] * 100:.0f}%",
            _colB: f"{_cb['scores']['coverage'] * 100:.0f}%",
        })
        st.markdown("##### 📐 规则引擎评分对照（程序计算 · 同一份数据永远同一分数）")
        st.dataframe(pd.DataFrame(_rows).set_index("维度"), use_container_width=True)
        st.caption(
            f"评分基准期：{_na} = {_ca['period'] or '未获取'}；{_nb} = {_cb['period'] or '未获取'}。"
            "总分为四个维度均分；覆盖率低于 50% 时不给综合评分。"
        )
        if _cs.get("cross_industry"):
            st.warning(
                f"⚠️ 两家公司分属不同行业（**{_ca['industry']}** vs **{_cb['industry']}**），"
                "各自按本行业基准线评分 —— **分数不可直接横向比较**，请结合下方文字对比与业务差异一起判断。"
            )

    cmp_report = st.session_state.get("cmp_report")
    if cmp_report:
        st.markdown("#### 📄 对比分析结论")
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

**行业财务基准库（同业对标）：**
本引擎是**面向全行业上市公司的通用财务投研引擎**。页面顶部横向按钮可选择行业
（通用、消费、金融、新能源、医药、互联网），每个行业对应一套**财务口径**的同业对标基准
（ROE 优秀线 / 营收增速稳健线），四维评分、四象限定位与散点基准线全部按当前行业口径计算。

之所以必须做行业对标：银行天然高杠杆、薄利差，ROE 11% 已属优秀；而白酒业 ROE 11% 则严重落后。
若用同一把尺子衡量所有行业，银行会被误判为「瘦狗」、新能源会被误判为「问题型」——
**同业对标才是有效的财务判断**。

页面会实时显示当前基准（如「金融 = ROE 11% / 增速 6%」）。银行/保险等特殊行业另内置专用指标
（不良贷款率、拨备覆盖率、核心一级资本充足率等），自动切换为金融专用评分口径。

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

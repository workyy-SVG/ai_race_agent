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
  5. 多公司横向对比模式（2~5 家，支持在线取数）
  6. 内置行业财务基准库（主界面横向按钮选择；为四维评分/散点/象限提供同业对标基准）
  7. 【修】追问对话：上下文实时刷新、可连续多轮提问；
     渲染顺序为「问在上 → 答在中 → 输入框在最下面」（见 tab1 里那段「顺序很关键」注释）

密钥说明：所有敏感信息从 Streamlit Secrets / 环境变量读取，代码中不含任何 key。
"""

import os
import re
import json
import io
import base64
import math
import tempfile
import datetime
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

# 统一业务流程（页面与离线评测共用同一条链路 · 优化清单 P1-06）：
#   取数(年报口径) → 规则评分 → 输出校验 → 证据核对 → 证据卡 → 引用统计
#
# ⚠️⚠️ 这里**不能**写成 `try: import pipeline / except ImportError: _pipeline = None`。
#    2026-10-15 云端真实故障复盘：pipeline 没能加载，异常被静默吃掉，页面照常渲染、
#    按钮照常可点，直到点下「开始结构化分析」才炸出一句
#        'NoneType' object has no attribute 'analyze_core'
#    —— 既看不出是哪个模块、也看不出为什么，排查只能靠猜（就是这条注释存在的原因）。
#
#    现在的策略是**三级降级**，每一级都留下可读的证据：
#      ① 常规 import；失败时保留真实异常 + 完整 traceback（绝不吞掉）
#      ② 退一步按 app.py 所在目录**以绝对路径加载同名文件**
#         —— 应对 sys.path / 工作目录异常导致的 ModuleNotFoundError：
#            文件明明在仓库里却 import 不到，这是云端部署最常见的坑
#      ③ 仍失败则挂上内置的等价兜底实现（接口与 pipeline 完全一致），
#         保证按钮依然可用，并在页面上**明确标注降级**，不假装一切正常
def _load_pipeline():
    """尽最大努力加载统一流程模块 pipeline，并如实返回失败原因。

    :return: ``{"module", "source", "error", "traceback", "path"}``
        ``source`` ∈ {``module`` 常规导入, ``file`` 按路径加载, ``none`` 都没成功}
    """
    import sys
    import traceback
    import importlib

    _here = Path(__file__).resolve().parent
    _target = _here / "pipeline.py"
    _err, _tb = "", ""

    # ① 常规导入（正常情况走这里）
    try:
        _m = importlib.import_module("pipeline")
        return {
            "module": _m, "source": "module", "error": "", "traceback": "",
            "path": str(getattr(_m, "__file__", "") or _target),
        }
    except Exception as _e:  # noqa: BLE001 —— 要的就是把真实原因完整留下来
        _err = f"{type(_e).__name__}: {_e}"
        _tb = traceback.format_exc()

    # ② 按绝对路径加载同名文件（sys.path 里没本目录时也能救回来）
    try:
        if _target.exists():
            _spec = importlib.util.spec_from_file_location("pipeline", str(_target))
            _m = importlib.util.module_from_spec(_spec)
            sys.modules.setdefault("pipeline", _m)
            _spec.loader.exec_module(_m)
            return {
                "module": _m, "source": "file",
                "error": f"常规 import 失败（{_err}），已按绝对路径加载成功",
                "traceback": _tb, "path": str(_target),
            }
    except Exception as _e2:  # noqa: BLE001
        _err += f" | 按绝对路径加载也失败：{type(_e2).__name__}: {_e2}"
        _tb += "\n---- 按绝对路径加载 ----\n" + traceback.format_exc()

    return {"module": None, "source": "none", "error": _err, "traceback": _tb,
            "path": str(_target)}


_pl = _load_pipeline()
_pipeline = _pl["module"]
_PIPELINE_SOURCE = _pl["source"]        # module / file / none / fallback
_PIPELINE_ERROR = _pl["error"]          # 非空 = 加载过程有异常（真实原因）
_PIPELINE_TRACEBACK = _pl["traceback"]
_PIPELINE_PATH = _pl["path"]            # 期望的 pipeline.py 绝对路径

# 输出校验层（纯函数，页面与离线评测共用同一实现）：
#   validate_analysis / parse_json_loose / _safe_num / verify_evidence
# 独立成模块的意义：eval_cases.py 必须能调用**同一份**校验代码，
# 否则"评测声称走了全流程校验"就是一句无法验证的话。
from analysis_validate import (  # noqa: E402
    validate_analysis, parse_json_loose, _safe_num, verify_evidence,
    evidence_map, citation_accuracy, MIN_SCORE_COVERAGE,
)

# ---- pipeline 加载失败时的等价兜底 ----------------------------------------
# 只在 _load_pipeline() 三级降级都失败时才会被挂上去（见上面的 _PIPELINE_SOURCE）。
# 它不是"第二套算法"：评分仍走 scoring.compute_scores，校验/证据卡/引用统计仍走
# analysis_validate 里的同一批函数 —— 与 pipeline 用的是**完全相同**的实现，
# 这里只是把编排顺序照抄一遍，好让页面在模块缺失时仍然能跑出结果。
# ⚠️ 它一旦被启用就说明部署有问题，页面会明确标注（不静默），
#    并有 eval_pipeline_load.py 做"与真实 pipeline 结果逐字段一致"的等价性测试。
_FALLBACK_NEED_KEYS = (
    "roe", "gross_margin", "net_margin", "revenue_yoy", "net_profit_yoy",
    "debt_ratio", "current_ratio", "ocf_to_revenue",
    "npl_ratio", "core_car", "provision_coverage",
    "nim", "loan_provision_ratio", "solvency_ar", "nbv_rate",
)


class _PipelineFallback:
    """与 pipeline 模块同接口的最小兜底实现（页面用到的 4 个函数 + 1 个常量）。"""

    DEFAULT_PERIODS = 8

    @staticmethod
    def page_range_of(rag_status):
        if not isinstance(rag_status, dict):
            return None
        _n = rag_status.get("pages")
        return (1, int(_n)) if isinstance(_n, int) and _n > 0 else None

    @staticmethod
    def annual_metrics(code, name="", periods=8):
        """取【最近一个年报】的指标 → (metrics, 期别标签)。口径与 pipeline 一致。"""
        if not code:
            return {}, ""
        fina = ds.get_financial_indicators(code, periods=periods, annual_only=True)
        if not fina:
            fina = ds.get_financial_indicators(code, periods=periods)
        if not fina:
            return {}, ""
        _annual = [r for r in fina if (r.get("report_type") or "") == "年报"]
        _pick = _annual[0] if _annual else fina[0]
        _label = str(_pick.get("report_name") or "")
        if not _annual:
            _label += "（无年报记录，暂用中期报告口径）"
        return _pick, _label

    @staticmethod
    def all_annual_rows(code, periods=8):
        if not code:
            return []
        try:
            _rows = ds.get_financial_indicators(code, periods=periods, annual_only=True) or []
            return [r for r in _rows if (r.get("report_type") or "") == "年报"]
        except Exception:
            return []

    @staticmethod
    def year_of(row):
        for _k in ("report_name", "report_date"):
            _m = re.search(r"(20\d{2})", str((row or {}).get(_k) or ""))
            if _m:
                return _m.group(1)
        return ""

    @staticmethod
    def analyze_core(analysis, raw_data, industry="通用", *, code="", periods=8,
                     online=True, finance_kind=None, pdftext="", rag_hits=None,
                     rag_pages=None, metrics_extractor=None):
        """与 pipeline.analyze_core 逐步对应的兜底编排（改一处必须同步改两处）。"""
        # 1) 指标来源：在线优先，且只取年报口径
        metrics, period, source = {}, "", ""
        _oc = code if online else ""
        if _oc:
            try:
                metrics, period = _PipelineFallback.annual_metrics(_oc, periods=periods)
                source = (f"在线财务指标（{period}）" if period
                          else "在线财务指标（最新报告期）")
            except Exception:
                metrics, period, source = {}, "", ""
        if not metrics:
            metrics = scoring.extract_metrics_regex(raw_data)
            _need = [k for k in _FALLBACK_NEED_KEYS if k not in metrics]
            if _need and raw_data and metrics_extractor is not None:
                try:
                    _m = metrics_extractor(raw_data, _need)
                    if isinstance(_m, dict):
                        for _k in _need:
                            if _m.get(_k) is not None:
                                metrics[_k] = _m[_k]
                except Exception:
                    pass
            if str(source).startswith("在线财务指标"):
                source = "文本抽取（在线财务指标不可用，已退回文本）"
            else:
                source = source or "文本抽取"

        # 2) 规则评分（与 pipeline.score_block 字段一一对应）
        _sc = scoring.compute_scores(metrics or {}, industry, finance_kind=finance_kind)
        analysis.update({
            "scores": _sc["scores"],
            "total_score": _sc["total_score"],
            "score_reasons": _sc["reasons"],
            "score_missing": _sc["missing"],
            "score_engine": "规则引擎 v3（同业对标 + 覆盖率门槛 + 金融子行业）",
            "score_coverage": _sc.get("coverage"),
            "score_confidence": _sc.get("confidence"),
            "score_notes": _sc.get("notes") or [],
            "score_expected": _sc.get("expected_metrics"),
            "score_provided": _sc.get("provided_metrics"),
            "score_industry": _sc.get("industry"),
            "score_finance_kind": _sc.get("finance_kind") or "",
            "metrics_raw": dict(metrics or {}),
            "score_period": period,
            "score_source": source or ("文本抽取" if not period else "在线财务指标"),
            "_scoring_result": _sc,
        })

        # 3) 输出校验
        analysis, v_issues = validate_analysis(analysis)

        # 4) 证据核对 + 证据卡 + 引用统计
        _pg = (1, int(rag_pages)) if isinstance(rag_pages, int) and rag_pages > 0 else None
        _sources = []
        if pdftext and len(pdftext) <= 4_000_000:
            _sources.append(pdftext)
        for _h in (rag_hits or []):
            _sources.append(str((_h or {}).get("text") or ""))
        ev_issues = verify_evidence(analysis, raw_data, page_range=_pg, extra_sources=_sources)
        ev_items = evidence_map(analysis, rag_hits, raw_data=raw_data, page_range=_pg)
        cit = citation_accuracy(analysis, _pg)

        _found = [e for e in ev_items if e.get("value") is not None and e.get("found")]
        _checkable = [e for e in ev_items if isinstance(e.get("value"), (int, float))]
        analysis["evidence_items_count"] = len(ev_items)
        analysis["evidence_issues_count"] = len(ev_issues)
        return {
            "analysis": analysis,
            "validation_issues": v_issues,
            "evidence_issues": ev_issues,
            "evidence_items": ev_items,
            "citation": cit,
            "num_trace_rate": (round(len(_found) / len(_checkable), 3) if _checkable else None),
            "score_period": period,
            "score_source": source,
            "metrics": metrics,
        }


if _pipeline is None:
    _pipeline = _PipelineFallback()
    _PIPELINE_SOURCE = "fallback"


def pipeline_diag_markdown() -> str:
    """部署自检：pipeline 从哪来、为什么没来、期望的文件到底在不在。

    这条信息存在的唯一目的，是让「模块没加载」这种故障**一眼可查**，
    而不是像 2026-10-15 那样只丢一句 ``'NoneType' object has no attribute ...``。
    """
    _exists = Path(_PIPELINE_PATH).exists() if _PIPELINE_PATH else False
    _size = (Path(_PIPELINE_PATH).stat().st_size if _exists else 0)
    _icon = {"module": "✅", "file": "🟡", "fallback": "⚠️", "none": "❌"}.get(_PIPELINE_SOURCE, "❔")
    _label = {
        "module": "常规导入成功",
        "file": "常规导入失败，已按**绝对路径**加载成功",
        "fallback": "**未能加载，已启用内置等价兜底**",
        "none": "**未能加载，且兜底也不可用**",
    }.get(_PIPELINE_SOURCE, _PIPELINE_SOURCE)
    _lines = [
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 统一流程模块状态 | {_icon} {_label} |",
        f"| 期望文件路径 | `{_PIPELINE_PATH}` |",
        f"| 该文件是否存在 | {'是（%s 字节）' % _size if _exists else '**否** —— 部署里缺少这个文件'} |",
        f"| app.py 所在目录 | `{Path(__file__).resolve().parent}` |",
    ]
    if _PIPELINE_ERROR:
        _lines.append(f"| 真实异常 | `{_PIPELINE_ERROR}` |")
    else:
        _lines.append("| 真实异常 | 无（正常导入） |")
    return "\n".join(_lines)

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
    /* 横幅内所有文字居中对齐（标题/说明/标签行三段整体居中） */
    text-align: center;
    box-shadow: 0 10px 32px rgba(30, 64, 175, 0.30),
                0 0 0 1px rgba(217, 164, 65, 0.25) inset;
  }
  /* 关掉 Streamlit 自动给标题附加的「跳转到本节」锚点图标：
     该图标由 Streamlit 渲染在标题内部（data-testid="stHeaderActionElements"），
     在深蓝横幅底色上是一个几乎不可见的深色色块，且会破坏居中视觉。 */
  .hero h1 a,
  .hero a.anchor-link,
  .hero [data-testid="stHeaderActionElements"] { display: none !important; }

  /* ===== 紧凑顶栏（替代原来的大横幅）=====
     大横幅占了近 100px 高、信息密度却很低（品牌名 + 一句口号）。
     改成单行：品牌 + 当前分析对象 + 状态徽章，把纵向空间还给工作台。 */
  .topbar {
    display: flex;
    align-items: center;
    gap: 12px;
    background: #FFFFFF;
    border: 1px solid #D6E0EC;
    border-left: 4px solid #1E40AF;
    border-radius: 10px;
    padding: 9px 16px;
    margin-bottom: 10px;
    box-shadow: 0 2px 10px rgba(30, 64, 175, 0.08);
  }
  .tb-brand { font-size: 16px; font-weight: 800; color: #1E3A8A !important; letter-spacing: .5px; }
  .tb-brand b { color: #B45309 !important; }
  .tb-gap { flex: 1 1 auto; }
  .tb-obj { font-size: 12.5px; color: #334155 !important; }
  .tb-obj b { color: #1E3A8A !important; }
  .tb-chip {
    font-size: 11.5px; padding: 2px 10px; border-radius: 999px;
    background: #DBEAFE; color: #1E40AF !important;
    border: 1px solid #BFDBFE; white-space: nowrap;
  }
  .tb-chip.warn { background: #FEF3C7; color: #92400E !important; border-color: #FDE68A; }

  /* ===== 三栏工作台 ===== */
  /* 左列：图标菜单（约 76px）；中列：分析记录（约 290px）；右列：工作台 */
  .rail-cap {
    font-size: 11px; color: #64748B !important; text-align: center;
    margin: 6px 0 2px 0; line-height: 1.3;
  }
  .hist-wrap {
    background: #FFFFFF;
    border: 1px solid #D6E0EC;
    border-radius: 10px;
    padding: 10px 12px;
    /* 独立滚动：记录多了不把整页撑长 */
    max-height: calc(100vh - 220px);
    overflow-y: auto;
  }
  .hist-head {
    font-size: 13px; font-weight: 700; color: #1E3A8A !important;
    padding-bottom: 6px; margin-bottom: 8px; border-bottom: 1px solid #E2E8F0;
  }
  .hist-item {
    border: 1px solid #E2E8F0; border-radius: 8px;
    padding: 7px 9px; margin-bottom: 7px; background: #F8FAFC;
  }
  .hist-item.on { border-color: #1E40AF; background: #EFF6FF; }
  .hist-name { font-size: 12.5px; font-weight: 700; color: #0B1F3A !important; }
  .hist-meta { font-size: 11px; color: #64748B !important; margin-top: 2px; }
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
        <div class="hero" style="text-align:center; max-width:720px; margin:28px auto 14px auto;">
            <h1 style="color:#F8FAFC !important;font-size:30px;font-weight:800;margin:0 0 4px 0;letter-spacing:1px;">📈 金融智能投研 <span class="gold" style="color:#F5C563 !important;">Agent</span></h1>
            <p style="color:#DBEAFE !important;font-size:14px;margin:0;">AI 驱动的财报深度分析 · 结构化评分 · 多公司对比 · 同业对标基准</p>
            <p class="tagline" style="color:#A9C6E8 !important;font-size:12px;margin-top:6px;">数据来自巨潮资讯网 / 东方财富公开接口 · 结论仅供参考</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 左侧：功能展示区；右侧：登录卡片。
    # Streamlit 的 columns 在窄屏（手机）会自动纵向堆叠，因此无需额外媒体查询。
    _lc, _rc = st.columns([1.3, 1], gap="large")

    with _lc:
        st.markdown(
            """
            <div style="background:#FFFFFF;border:1px solid #D6E0EC;border-radius:14px;
                        padding:18px 20px;box-shadow:0 2px 10px rgba(30,64,175,.06);">
              <div style="font-size:15px;font-weight:800;color:#1E3A8A;margin:0 0 10px 0;">
                这套系统能做四件事
              </div>
              <div style="font-size:13px;line-height:2.0;color:#334155;">
                <div><b style="color:#1E3A8A;">① 单公司深度分析</b> —— 联网取数 → 规则引擎四维评分 → 图表与风险提示，<span style="color:#64748B;">分数可复现，大模型只做解读</span></div>
                <div><b style="color:#1E3A8A;">② 多公司横向对比</b> —— 2~5 家公司各自按本行业基准评分，程序生成对照表、差额与对比图，<span style="color:#64748B;">支持 CSV / 报告导出</span></div>
                <div><b style="color:#1E3A8A;">③ 年报原文引用</b> —— 一键解析年报 PDF，按页建向量库，结论标注「见年报第 X 页」，<span style="color:#64748B;">并可回溯核对</span></div>
                <div><b style="color:#1E3A8A;">④ 同业对标基准</b> —— 内置消费 / 金融 / 新能源 / 医药 / 互联网行业财务基准，<span style="color:#64748B;">同一 ROE 在不同行业得分不同</span></div>
              </div>
              <div style="margin-top:14px;padding-top:12px;border-top:1px dashed #D6E0EC;
                          font-size:12px;color:#64748B;line-height:1.9;">
                🟢 演示路径：取数 → 结构化分析 → 年度时间轴 → 生成研报 → 多公司对比<br>
                🟡 页码引用需先在取数面板解析一份年报（约 40–80 秒）
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with _rc:
        st.markdown(
            '<div style="font-size:15px;font-weight:800;color:#1E3A8A;margin:2px 0 8px 0;">🔐 访问登录</div>',
            unsafe_allow_html=True,
        )
        with st.container(border=True):
            with st.form("login_form", clear_on_submit=False):
                user = st.text_input("账号", placeholder="请输入访问账号")
                pw = st.text_input("密码", type="password", placeholder="请输入访问密码")
                submitted = st.form_submit_button("进入系统", use_container_width=True, type="primary")
                if submitted:
                    if user == APP_USER and pw == APP_PASSWORD:
                        st.session_state["authenticated"] = True
                        st.rerun()
                    else:
                        st.error("❌ 账号或密码错误，请检查后重试（区分大小写）。")
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


# ---- 双公司对比图表 ----
# 按「量级相近」分两组，避免把 拨备覆盖率 300% 与 ROE 14% 画在同一根轴上，
# 高的顶天、矮的贴地，等于没画。
_CMP_GROUP_PROFIT = [
    ("roe", "ROE(加权)"), ("gross_margin", "毛利率"), ("net_margin", "净利率"),
    ("revenue_yoy", "营收同比"), ("net_profit_yoy", "净利同比"), ("nim", "净息差"),
]
_CMP_GROUP_RISK = [
    ("debt_ratio", "资产负债率"), ("npl_ratio", "不良率"),
    ("provision_coverage", "拨备覆盖率"), ("core_car", "资本充足率"),
    ("solvency_ar", "偿付能力"), ("ocf_to_revenue", "现金流/营收"),
]


def fig_compare_group(ma: dict, mb: dict, na: str, nb: str,
                      group: list, title: str) -> bytes | None:
    """双公司分组柱状图：只画「至少一侧有值」的指标，柱顶标注数值。"""
    rows = []
    for k, lbl in group:
        va = ma.get(k) if isinstance(ma.get(k), (int, float)) else None
        vb = mb.get(k) if isinstance(mb.get(k), (int, float)) else None
        if va is not None or vb is not None:
            rows.append((lbl, va, vb))
    if not rows:
        return None
    labels = [r[0] for r in rows]
    x = np.arange(len(rows))
    w = 0.38
    fig, ax = plt.subplots(figsize=(6.0, 3.2), facecolor=_PAL["card"])
    ax.set_facecolor(_PAL["card"])

    def _vals(idx):
        return [0.0 if r[idx] is None else float(r[idx]) for r in rows]

    _va, _vb = _vals(1), _vals(2)
    b1 = ax.bar(x - w / 2, _va, w, label=na, color=_PAL["purple"])
    b2 = ax.bar(x + w / 2, _vb, w, label=nb, color=_PAL["gold"])
    # 柱顶标数值（缺失项显式写 N/A，而不是画成 0 —— 0 会被误读为"该项为零"）
    for _bars, _raw in ((b1, [r[1] for r in rows]), (b2, [r[2] for r in rows])):
        for _b, _v in zip(_bars, _raw):
            ax.annotate("N/A" if _v is None else f"{_v:g}",
                        (_b.get_x() + _b.get_width() / 2, _b.get_height()),
                        ha="center", va="bottom", fontsize=8, color=_PAL["text"],
                        xytext=(0, 2), textcoords="offset points")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, color=_PAL["text"], fontsize=9)
    ax.grid(axis="y", color="#E2E8F0", linewidth=0.6, alpha=0.8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(frameon=False, labelcolor=_PAL["text"], fontsize=9, loc="upper right")
    ax.set_title(title, color=_PAL["title"], fontsize=12, fontweight="bold", pad=10)
    return _fig_to_png_bytes(fig)


def fig_compare_dims(scores_a: dict, scores_b: dict, na: str, nb: str) -> bytes | None:
    """四维评分对比：两张并排小图，一眼看出强项/弱项分布。"""
    dims = [d for d in scores_a.keys() if d in scores_b]
    if not dims:
        return None
    va = [scores_a.get(d) if isinstance(scores_a.get(d), (int, float)) else 0.0 for d in dims]
    vb = [scores_b.get(d) if isinstance(scores_b.get(d), (int, float)) else 0.0 for d in dims]
    x = np.arange(len(dims))
    w = 0.38
    fig, ax = plt.subplots(figsize=(6.0, 3.0), facecolor=_PAL["card"])
    ax.set_facecolor(_PAL["card"])
    b1 = ax.bar(x - w / 2, va, w, label=na, color=_PAL["purple"])
    b2 = ax.bar(x + w / 2, vb, w, label=nb, color=_PAL["gold"])
    for _bars, _raw in ((b1, [scores_a.get(d) for d in dims]), (b2, [scores_b.get(d) for d in dims])):
        for _b, _v in zip(_bars, _raw):
            ax.annotate("N/A" if _v is None else f"{_v:g}",
                        (_b.get_x() + _b.get_width() / 2, _b.get_height()),
                        ha="center", va="bottom", fontsize=8, color=_PAL["text"],
                        xytext=(0, 2), textcoords="offset points")
    ax.set_ylim(0, 11)
    ax.set_ylabel("评分（0-10）", color=_PAL["text"], fontsize=9)
    ax.set_xticks(x)
    ax.set_xticklabels(dims, color=_PAL["text"], fontsize=9)
    ax.grid(axis="y", color="#E2E8F0", linewidth=0.6, alpha=0.8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(frameon=False, labelcolor=_PAL["text"], fontsize=9, loc="upper right")
    ax.set_title("四维评分对比（同行业可比；跨行业仅看分布）", color=_PAL["title"],
                 fontsize=12, fontweight="bold", pad=10)
    return _fig_to_png_bytes(fig)


# ---- N 公司对比图表（优化清单 P1-02：至少 2 家 / 3 家 / 5 家都能画）----
_CMP_SERIES_COLORS = ["#1E40AF", "#D9A441", "#0E7490", "#7C3AED", "#B45309"]


def _cmp_series_color(i: int) -> str:
    return _CMP_SERIES_COLORS[i % len(_CMP_SERIES_COLORS)]


def fig_compare_group_n(mets: list, names: list, group: list, title: str) -> bytes | None:
    """N 家公司分组柱状图：只画「至少一家有值」的指标，缺失项柱顶写 N/A（不画 0）。"""
    n = len(names)
    if n < 2 or len(mets) != n:
        return None
    rows = []
    for k, lbl in group:
        vals = [m.get(k) if isinstance((m or {}).get(k), (int, float)) else None for m in mets]
        if any(v is not None for v in vals):
            rows.append((lbl, vals))
    if not rows:
        return None
    labels = [r[0] for r in rows]
    x = np.arange(len(rows))
    total_w = 0.82
    w = total_w / n
    fig, ax = plt.subplots(figsize=(max(6.2, 1.5 * n + 4.0), 3.3), facecolor=_PAL["card"])
    ax.set_facecolor(_PAL["card"])
    for i in range(n):
        offs = x - total_w / 2 + w * (i + 0.5)
        vals = [0.0 if rows[j][1][i] is None else float(rows[j][1][i]) for j in range(len(rows))]
        bars = ax.bar(offs, vals, w * 0.9, label=names[i], color=_cmp_series_color(i))
        for b, raw in zip(bars, [rows[j][1][i] for j in range(len(rows))]):
            ax.annotate("N/A" if raw is None else f"{raw:g}",
                        (b.get_x() + b.get_width() / 2, b.get_height()),
                        ha="center", va="bottom", fontsize=7, color=_PAL["text"],
                        xytext=(0, 2), textcoords="offset points")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, color=_PAL["text"], fontsize=9)
    ax.grid(axis="y", color="#E2E8F0", linewidth=0.6, alpha=0.8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(axis="y", labelcolor=_PAL["text"], labelsize=9)
    ax.legend(frameon=False, labelcolor=_PAL["text"], fontsize=8,
              ncol=min(n, 3), loc="upper right")
    ax.set_title(title, color=_PAL["title"], fontsize=12, fontweight="bold", pad=10)
    return _fig_to_png_bytes(fig)


def fig_compare_dims_n(score_list: list, names: list) -> bytes | None:
    """N 家公司四维评分分组柱状图（维度取并集；缺失维度写 N/A）。"""
    n = len(names)
    if n < 2 or len(score_list) != n:
        return None
    dims = []
    for s in score_list:
        for d in (s or {}).keys():
            if d not in dims:
                dims.append(d)
    if not dims:
        return None
    x = np.arange(len(dims))
    total_w = 0.82
    w = total_w / n
    fig, ax = plt.subplots(figsize=(max(6.2, 1.5 * n + 4.0), 3.1), facecolor=_PAL["card"])
    ax.set_facecolor(_PAL["card"])
    for i in range(n):
        offs = x - total_w / 2 + w * (i + 0.5)
        vals = [(score_list[i] or {}).get(d) for d in dims]
        bars = ax.bar(offs, [0.0 if v is None or not isinstance(v, (int, float)) else float(v)
                             for v in vals],
                      w * 0.9, label=names[i], color=_cmp_series_color(i))
        for b, v in zip(bars, vals):
            ax.annotate("N/A" if not isinstance(v, (int, float)) else f"{v:g}",
                        (b.get_x() + b.get_width() / 2, b.get_height()),
                        ha="center", va="bottom", fontsize=7, color=_PAL["text"],
                        xytext=(0, 2), textcoords="offset points")
    ax.set_ylim(0, 11)
    ax.set_ylabel("评分（0-10）", color=_PAL["text"], fontsize=9)
    ax.set_xticks(x)
    ax.set_xticklabels(dims, color=_PAL["text"], fontsize=9)
    ax.grid(axis="y", color="#E2E8F0", linewidth=0.6, alpha=0.8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(frameon=False, labelcolor=_PAL["text"], fontsize=8,
              ncol=min(n, 3), loc="upper right")
    ax.set_title("四维评分对比（同行业可比；跨行业仅看分布，不作排名依据）",
                 color=_PAL["title"], fontsize=12, fontweight="bold", pad=10)
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


# ============ 年度时间轴（P1-01） ============
# 验收标准逐条对应：
#   · 年份切换后卡片与详情同步 —— 年份选择器同时驱动「卡片区」与「明细表高亮」
#   · 缺失年份不填零 —— 图上只画**真的有数据**的年份；缺哪年用文字列出来，
#     绝不插一根 0 值柱子（0 会被读成"那一年营收为零"）
#   · 比率变化用百分点 —— ROE / 毛利率的同比写「+1.3 pp」，金额类才写百分比
_TIMELINE_METRICS = [
    ("revenue", "营业收入", "亿元", False),
    ("net_profit", "归母净利润", "亿元", False),
    ("roe", "ROE(加权)", "%", True),
    ("gross_margin", "毛利率", "%", True),
]
_TIMELINE_LABELS = {k: lbl for k, lbl, _u, _r in _TIMELINE_METRICS}


def _tl_num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def render_year_metric_chart(rows: list, key: str, label: str, unit: str) -> bytes | None:
    """单指标年度柱状图：X 轴只放**有数据的年份**（缺失年份不补零柱）。"""
    data = []
    for r in rows:
        _v = _tl_num(r.get(key))
        _y = str(r.get("year") or "")
        if _v is not None and _y:
            data.append((_y, _v))
    if not data:
        return None
    data.sort(key=lambda x: x[0])
    labels = [d[0] for d in data]
    vals = [d[1] for d in data]
    fig, ax = plt.subplots(figsize=(6.6, 3.0), facecolor=_PAL["card"])
    ax.set_facecolor(_PAL["card"])
    colors = [_PAL["purple"] if v >= 0 else _PAL["neutral"] for v in vals]
    bars = ax.bar(labels, vals, color=colors, width=0.55)
    for b, v in zip(bars, vals):
        _off = 0.02 * (max(abs(v) for v in vals) or 1)
        ax.annotate(f"{v:g}", (b.get_x() + b.get_width() / 2, v + (v >= 0 and _off or -_off)),
                    ha="center", va="bottom" if v >= 0 else "top",
                    color=_PAL["text"], fontsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#E2E8F0", linewidth=0.6, alpha=0.8)
    ax.axhline(0, color="#CBD5E1", linewidth=0.8)
    ax.tick_params(axis="both", labelcolor=_PAL["text"], labelsize=9)
    ax.set_title(f"{label}（{unit}）· 年报口径", color=_PAL["title"],
                 fontsize=12, fontweight="bold", pad=10)
    return _fig_to_png_bytes(fig)


def render_year_timeline(code: str, name: str = "", years: int = 8) -> None:
    """年度时间轴：筛选年份范围 → 选指标 → 选年份看当年数据 → 导出 CSV。"""
    st.markdown("##### 🗓️ 年度时间轴（近 %d 年年报口径）" % years)
    try:
        rows = ds.build_annual_trend(code, years=years) or []
    except Exception:
        rows = []
    rows = _trend_chrono(rows)
    _seen, _uniq = set(), []
    for r in rows:
        _y = str(r.get("year") or "")
        if _y and _y not in _seen:
            _seen.add(_y)
            _uniq.append(r)
    rows = _uniq
    if len(rows) < 2:
        st.info("年报期数不足（需 ≥2 个年报期才能构成时间轴）。"
                "可在上方「报告期数」调大后重新取数。")
        return

    years_all = [str(r["year"]) for r in rows]
    c1, c2 = st.columns([3, 4])
    with c1:
        _lo, _hi = st.select_slider(
            "年份范围",
            options=years_all,
            value=(years_all[0], years_all[-1]),
            key="timeline_range",
            help="只影响本区块的展示与导出，不改变评分口径（评分固定最近一个年报）。",
        )
    with c2:
        _mkey = st.radio(
            "指标",
            [k for k, _l, _u, _r in _TIMELINE_METRICS],
            format_func=lambda k: _TIMELINE_LABELS[k],
            horizontal=True,
            key="timeline_metric",
        )
    _m_idx = [k for k, _l, _u, _r in _TIMELINE_METRICS].index(_mkey)
    _mlabel, _munit, _is_ratio = _TIMELINE_METRICS[_m_idx][1], _TIMELINE_METRICS[_m_idx][2], _TIMELINE_METRICS[_m_idx][3]

    sel = [r for r in rows if _lo <= str(r.get("year")) <= _hi]
    _present = {str(r["year"]) for r in sel}
    _missing = [str(y) for y in range(int(_lo), int(_hi) + 1) if str(y) not in _present]
    if not sel:
        st.warning("所选年份范围内没有数据，请调整年份范围。")
        return

    _png = render_year_metric_chart(sel, _mkey, _mlabel, _munit)
    if _png:
        show_png(_png, alt=f"{_mlabel}年度趋势")
    st.caption(
        f"📊 图中仅包含**有年报数据**的年份"
        + (f"；缺失年份：{'、'.join(_missing)}（不补零，避免被读成「该年为零」）"
           if _missing else "；所选范围内无缺失年份")
        + f"。数据来源：东方财富财务指标接口（REPORT_TYPE=年报），分析对象：{name or code}。"
    )

    # ---- 点击/选择年份 → 卡片与明细同步 ----
    _years_desc = sorted(_present, reverse=True)
    _prev_map = {}
    _tail = [r for r in rows if str(r.get("year")) <= _hi]
    for _i, _r in enumerate(_tail):
        _y = str(_r.get("year"))
        if _y in _years_desc and _i > 0:
            _prev_map[_y] = _tail[_i - 1]
    _pick = st.selectbox("查看年份", _years_desc, key="timeline_year")
    _row = next((r for r in rows if str(r.get("year")) == _pick), None)
    if _row:
        st.markdown(f"**{_pick} 年年报**")
        _cols = st.columns(len(_TIMELINE_METRICS))
        for _i, (_k, _lbl, _u, _ratio) in enumerate(_TIMELINE_METRICS):
            _v = _tl_num(_row.get(_k))
            _pv = _tl_num((_prev_map.get(_pick) or {}).get(_k))
            with _cols[_i]:
                if _v is None:
                    st.metric(_lbl, "无数据")
                else:
                    _delta = None
                    if _pv is not None:
                        if _ratio:
                            # 比率类必须用**百分点**表示变化（百分比变化的百分比是外行话）
                            _delta = f"{_v - _pv:+.2f} pp"
                        elif _pv:
                            _delta = f"{(_v / _pv - 1) * 100:+.1f}%"
                    st.metric(f"{_lbl}（{_u}）", f"{_v:g}", delta=_delta)
        _src = str(_row.get("report_date") or "") or "—"
        st.caption(
            f"📄 数据来源：东方财富财务指标接口 · 报告期 {_row.get('period') or _pick}"
            f"（披露日期 {_src}）· 同比口径：金额类为同比百分比，比率类为百分点（pp）之差。"
        )

    # ---- 明细表 + 导出 ----
    _tbl = []
    for r in sel:
        _rec = {"年份": str(r.get("year")), "报告期": str(r.get("period") or "")}
        for _k, _lbl, _u, _ratio in _TIMELINE_METRICS:
            _v = _tl_num(r.get(_k))
            _pv = _tl_num((_prev_map.get(str(r.get("year"))) or {}).get(_k))
            _rec[f"{_lbl}({_u})"] = "—" if _v is None else f"{_v:g}"
            if _v is None or _pv is None:
                _rec[f"{_lbl}变化"] = "—"
            elif _ratio:
                _rec[f"{_lbl}变化"] = f"{_v - _pv:+.2f} pp"
            else:
                _rec[f"{_lbl}变化"] = f"{(_v / _pv - 1) * 100:+.1f}%" if _pv else "—"
        _tbl.append(_rec)
    _tl_df = pd.DataFrame(_tbl)
    st.dataframe(_tl_df, use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ 导出年度时间轴（CSV）",
        data=_tl_df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"年度时间轴_{name or code}_{_lo}-{_hi}.csv",
        mime="text/csv",
        key=f"tl_csv_{code}_{_lo}_{_hi}",
    )


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
        # 纯语义相似度（混合重排后 score 已不是余弦值，单独标注避免误解）
        try:
            _cos = float(h.get("cos"))
        except (TypeError, ValueError):
            _cos = None
        if sim >= 0.75:
            sim_cls = "rag-sim-high"
        elif sim >= 0.5:
            sim_cls = "rag-sim-mid"
        else:
            sim_cls = "rag-sim-low"
        text = (h.get("text") or h.get("content") or "").replace("\n", " ").strip()
        text = text[:160] + ("…" if len(text) > 160 else "")
        text = _html.escape(text)  # 外部文档内容一律转义，杜绝标签注入
        _cos_txt = f' · 语义 {_cos:.2f}' if _cos is not None else ""
        rows.append(
            f'<div class="rag-hit">'
            f'<span class="rag-rank">#{i}</span>'
            f'<span class="rag-page">📄 年报第 {page} 页</span>'
            f'&nbsp;|&nbsp;<span class="{sim_cls}">相关度 {sim:.2f}{_cos_txt}</span>'
            f'<br><span style="color:#334155;font-size:12px;line-height:1.6;">{text}</span>'
            f'</div>'
        )
    return (
        '<div style="margin: 10px 0 6px 0;color:#1E3A8A;font-weight:700;font-size:14px;">'
        f'📚 RAG 检索过程（Top-{len(rows)} 相关章节 · 语义+关键词混合重排）</div>'
        + "".join(rows)
    )


# ============ 在线取数：UI 组件（可复用） ============
def _panel_clear(key_prefix: str) -> None:
    """清空某个取数面板派生的全部结果（P0-01：失败 / 换标的时必须让旧结果失效）。

    ⚠️ 只清「数据类」键，**绝不碰控件键**（*_kw / *_pickle / *_periods / *_rtype /
       *_fetch / *_candsel …）。Streamlit 不允许在控件实例化之后 pop 它的 key，
       那样会直接抛 StreamlitAPIException，把页面打崩。
    """
    for _k in ("online", "online_name", "online_code", "online_org",
               "auto_industry", "cands", "pdftext", "pdf_path", "pdf_year",
               "pdf_tag", "pdf_title", "rag_status", "reports"):
        st.session_state.pop(f"{key_prefix}_{_k}", None)
    if key_prefix == "single":
        # 单公司页：旧分析 / 旧研报 / 旧对话 / 旧证据卡都必须一起失效
        for _k in ("analysis", "analysis_data", "report", "messages", "data_sig",
                   "evidence_items_last", "timeline_rows", "timeline_code"):
            st.session_state.pop(_k, None)


def _panel_store(key_prefix: str, code: str, name: str, org: str, periods: int):
    """按**已确认**的代码取数并写入 session_state。返回 (ok, err_msg)。

    与旧实现的区别：不再自行决定"用哪家公司" —— 目标由 data_source.resolve_stock
    精确解析后传入，这里只负责取数与落库。
    """
    if not code:
        return False, "内部错误：目标代码为空，已取消取数。"
    text = ds.build_summary_text(code, name=name, periods=periods)
    if not text:
        return False, (f"未取到 {name}（{code}）的公开财务数据：可能是网络受限、"
                       "或该证券无可用财报，可改用手动粘贴。")
    st.session_state[f"{key_prefix}_online"] = text
    st.session_state[f"{key_prefix}_online_name"] = name
    st.session_state[f"{key_prefix}_online_code"] = code
    st.session_state[f"{key_prefix}_online_org"] = org or ""
    # 自动匹配行业财务基准库（不直接改已渲染的 industry radio，
    # 而是存进 session_state，由顶部 radio 在下一轮渲染时读取）
    gi = ds.guess_industry(name)
    if gi:
        st.session_state[f"{key_prefix}_auto_industry"] = gi
    return True, ""


def _build_online_digest() -> str:
    """把在线取数结果压缩成一份**精简财务摘要**，供「一键填入」使用。

    与 `single_online` 全文的区别：只保留「最近一个年报」的指标明细 + 最近 3 期速览，
    几十行以内 —— 用户拿到手能直接改，不会出现"一屏都翻不完、不知道从哪下手"。
    没有在线数据时返回空串（调用方负责提示先取数）。
    """
    raw = (st.session_state.get("single_online") or "").strip()
    if not raw:
        return ""
    title = ""
    blocks = []          # [{"head": 报告期标题, "rows": [指标行, ...]}]
    cur = None
    for ln in raw.splitlines():
        s = ln.strip()
        if s.startswith("### "):
            cur = {"head": s[4:].strip(), "rows": []}
            blocks.append(cur)
        elif s.startswith("## "):
            cur = None                      # 进入新的一级章节，停止收指标行
        elif s.startswith("# "):
            title = s[2:].strip()
        elif cur is not None and s.startswith("- "):
            cur["rows"].append(s[2:].strip())
    _annual = [b for b in blocks if "年报" in b["head"] and "无年报" not in b["head"]]
    _pick = _annual[0] if _annual else (blocks[0] if blocks else None)
    lines = [f"### {title or '在线财务摘要'}（自动生成 · 可直接编辑）", ""]
    if _pick:
        lines.append(f"**{_pick['head']}**")
        for _r in _pick["rows"]:
            lines.append(f"- {_r}")
        lines.append("")
    _others = [b for b in blocks if b is not _pick][:3]
    if _others:
        lines.append("**近几期报告期速览**")
        for _b in _others:
            _one = "；".join(_b["rows"])[:150]
            lines.append(f"- {_b['head']}：{_one}" if _one else f"- {_b['head']}")
    return "\n".join(lines).strip()


def online_fetch_panel(key_prefix: str, default_name: str = "", compact: bool = False):
    """
    渲染「在线取数」面板，返回 (summary_text, company_name, extra_info)。
    - 输入股票代码/名称 → 自动拉取行情 + 多期财务指标 → 生成分析文本
    - 可从报告列表一键下载年报 PDF 并解析
    结果存入 session_state[key_prefix + "_online"], 避免与手动输入冲突。

    :param compact: 紧凑模式（多公司对比页用，P1-02）。只保留「选代码 + 取数」，
        去掉报告期数选择器、原始数据展开与年报 PDF 解析面板 ——
        否则 3~5 家公司并列时，光取数 UI 就把页面撑得无法使用。
        取数逻辑、防"猜代码"逻辑与完整模式**完全一致**。
    """
    st.markdown(
        "**🌐 在线取数**" if compact
        else "##### 🌐 在线取数（自动从巨潮资讯网 + 东方财富获取）"
    )
    if compact:
        c1, c3 = st.columns([3, 1])
    else:
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
            help="选中后代码只填入一次；之后输入框可自由编辑，不会被下拉框覆盖。",
        )
        _code = next((s[1] for s in _POPULAR_STOCKS if s[0] == _choice), "")
        # 2. 手动输入框
        # ⚠️ 只在「下拉选择真的发生变化」时才写回输入框（原实现每次渲染都无条件覆盖）。
        #    原写法的后果不止是"输入框锁死"：评委改成别的代码后，任何一次 rerun
        #    都会把输入框覆盖回下拉选中那家，于是"输入了错误代码，页面却显示茅台"。
        _kw_key = f"{key_prefix}_kw"
        _last_pick_key = f"{key_prefix}_last_pick"
        st.session_state.setdefault(_kw_key, "")
        if _code and st.session_state.get(_last_pick_key) != _choice:
            st.session_state[_kw_key] = _code
        st.session_state[_last_pick_key] = _choice
        kw = st.text_input(
            "股票代码或名称（可编辑；确认后点右侧 🔍 获取数据）",
            key=_kw_key,
            placeholder="例如：600519 或 贵州茅台",
        )
    periods = 3
    if not compact:
        with c2:
            # 选项改为 1~8 连续：旧实现写死 [1, 2, 3, 4, 6, 8]，用户看到列表里缺 5 和 7，
            # 会误以为功能有缺陷（实际只是当时随手挑了几个值）。
            periods = st.selectbox(
                "报告期数",
                list(range(1, 9)),
                index=2,
                key=f"{key_prefix}_periods",
                help="向东方财富拉取「最近 N 个报告期」的财务数据（含一季报/中报/三季报/年报）。"
                     "它只决定可取用的报告期数量与趋势图范围：N 越大可选期数越多、耗时略增。"
                     "四维评分、柱状图、圆环图一律固定按「最近一个年报」口径计算，与 N 无关。",
            )
    with c3:
        st.write("")
        st.write("")
        do_fetch = st.button("🔍 获取数据", use_container_width=True, key=f"{key_prefix}_fetch")
    if not compact:
        st.caption(
            "ℹ️ **报告期数 = 抓取最近 N 期财务数据**（含季报/中报/年报）；它只影响「可选报告的期数」与趋势范围，"
            "**评分与图表固定按「最近一个年报」口径**计算 —— 换 N 不会让分数漂移。"
        )

    if do_fetch:
        _kw = (kw or "").strip()
        if not _kw:
            st.error("请先输入 6 位股票代码或公司名称，再点击「获取数据」。")
        else:
            # 【证券解析】把输入变成一个**确定**的目标公司。
            # 巨潮搜索是模糊/前缀匹配，直接取第一条会把用户没输入过的公司当成分析对象
            # （实测：'00000' → 平安银行；'abc' → 一只 REIT）。resolve_stock 的原则是
            # 「代码必须精确命中；名称多义交用户确认；绝不猜」。
            with st.spinner("正在核验证券代码……"):
                _res = ds.resolve_stock(_kw)
            if _res.get("ok"):
                _panel_clear(key_prefix)   # 取数成功前先清旧结果，绝不让新旧混显
                with st.spinner("正在联网获取数据……"):
                    _ok, _err = _panel_store(
                        key_prefix, _res["code"], _res["name"], _res.get("orgId", ""), periods
                    )
                if not _ok:
                    _panel_clear(key_prefix)
                    st.error(_err)
            elif _res.get("ambiguous"):
                # 多义名称 → 记下候选，交给下方"候选确认"区，绝不自动采用首项
                _panel_clear(key_prefix)
                st.session_state[f"{key_prefix}_cands"] = _res["candidates"]
                st.warning(f"⚠️ {_res.get('error')} 请在下方候选中确认。")
            else:
                # 失败 → 清空该面板全部旧数据，杜绝"查询失败却仍显示上一家公司"
                _panel_clear(key_prefix)
                st.error(f"❌ {_res.get('error')}")

    # ---- 名称多义时的用户确认（对应验收标准「名称存在多个结果」）----
    _cands = st.session_state.get(f"{key_prefix}_cands") or []
    if _cands:
        st.info(f"请在候选中确认要分析的公司（共 {len(_cands)} 家，不会自动选择第一个）：")
        _c_opt = [f"{c.get('name', '')}（{c.get('code', '')}）" for c in _cands]
        _c_i = st.radio("候选公司", range(len(_c_opt)), format_func=lambda i: _c_opt[i],
                        key=f"{key_prefix}_candsel")
        _cc1, _cc2, _cc3 = st.columns([1, 1, 3])
        with _cc1:
            _c_ok = st.button("✅ 使用该公司", key=f"{key_prefix}_candbtn", use_container_width=True)
        with _cc2:
            _c_no = st.button("✖️ 取消", key=f"{key_prefix}_candcancel", use_container_width=True)
        with _cc3:
            st.caption("提示：直接输入 6 位代码（如 600519）可以免确认，最省事。")
        if _c_no:
            st.session_state.pop(f"{key_prefix}_cands", None)
            st.rerun()
        if _c_ok:
            _pick = _cands[min(_c_i, len(_cands) - 1)]
            _panel_clear(key_prefix)
            with st.spinner(f"正在获取 {_pick.get('name', '')} 的数据……"):
                _ok, _err = _panel_store(
                    key_prefix, _pick.get("code", ""), _pick.get("name", ""),
                    _pick.get("orgId", ""), periods,
                )
            if not _ok:
                _panel_clear(key_prefix)
                st.error(_err)
            else:
                st.rerun()

    text = st.session_state.get(f"{key_prefix}_online", "")
    # 【防御】key_prefix 命名空间里若有同名控件（如某处的复选框也叫 xxx_online），
    # 会话值会被写成 bool/其他类型，len(text) 会抛 TypeError 让整页崩掉。
    # 这里统一降级为 str，保证面板永远可用（P1-02 多公司区曾真实踩到该坑）。
    if not isinstance(text, str):
        text = "" if not text else str(text)
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
        if compact:
            # 紧凑模式到此为止：不铺开原始数据、不提供年报解析面板（对比页只需指标）
            return text, name, ""
        with st.expander("查看获取到的原始数据", expanded=False):
            st.markdown(text)

        # 年报 PDF 一键获取：默认展开，且解析成功后保持展开
        pdf_done = bool(st.session_state.get(f"{key_prefix}_pdftext"))
        _rag_ok = bool(st.session_state.get(f"{key_prefix}_rag_status", {}).get("ok"))
        # 索引还没建 → 默认展开这个面板，把"要页码引用就先解析年报"变成显式引导，
        # 而不是让评委点完「开始分析」才发现结论里没有引用（优化清单：RAG 不是开箱即用）
        with st.expander(
            "📄 一键获取并解析年报 PDF → 启用「见年报第X页」页码引用（约 40-80 秒）",
            expanded=not _rag_ok,
        ):
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

        # RAG 状态提示：明确告诉使用者"现在有没有页码引用能力"
        if _rag_ok:
            _st_ = st.session_state.get(f"{key_prefix}_rag_status", {})
            st.caption(
                f"📚 年报向量库已就绪：{_st_.get('chunks', 0)} chunks · "
                f"{_st_.get('pages', 0)} 页 · 分析/研报/追问将按问题检索相关章节并标注页码"
            )
        else:
            st.caption(
                "ℹ️ **当前未建立年报向量索引** —— 分析结果基于取数文本（含年报开头部分），"
                "**不含「见年报第X页」式页码引用**。若要启用页码引用，请点开上方面板"
                "「📄 一键获取并解析年报 PDF」下载解析一份年报（约 40-80 秒），"
                "建好后本页会自动提示「向量库已就绪」。"
            )

    pdftext = st.session_state.get(f"{key_prefix}_pdftext", "")
    return text, name, pdftext

# ============ 紧凑顶栏 + 侧边栏 ============
# 原来的深蓝大横幅（三行、近 100px 高）换成单行顶栏：
# 品牌 + 当前分析对象 + 状态徽章。信息一样，纵向空间还给工作台。
_tb_name = st.session_state.get("single_online_name", "")
_tb_code = st.session_state.get("single_online_code", "")
_tb_obj = (
    f"当前对象：<b>{_tb_name}（{_tb_code}）</b>"
    if _tb_name and _tb_code else "当前对象：<b>尚未选择</b>"
)
_tb_ragst = st.session_state.get("single_rag_status") or {}
_tb_rag_ok = bool(isinstance(_tb_ragst, dict) and _tb_ragst.get("ok"))
_tb_rag_txt = "📚 年报索引已就绪" if _tb_rag_ok else "📚 未建年报索引"
_tb_who = "🎤 演示模式" if st.session_state.get("demo_mode") else "🔒 已登录"
st.markdown(
    '<div class="topbar">'
    '<span class="tb-brand">📈 金融智能投研 <b>Agent</b></span>'
    '<span class="tb-gap"></span>'
    f'<span class="tb-obj">{_tb_obj}</span>'
    f'<span class="tb-chip">{_tb_who}</span>'
    f'<span class="tb-chip{" warn" if not _tb_rag_ok else ""}">{_tb_rag_txt}</span>'
    "</div>",
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
        '<span class="sb-chip">多公司对比</span>'
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

# ---- 金融子行业口径（优化清单 P1-04：拆分银行 / 保险 / 证券的适用指标）----
# 把「金融」当一个桶是有代价的：银行看净息差 + 不良率 + 资本充足率；
# 险企看新业务价值率 + 偿付能力充足率；而券商的高杠杆是**业务模式**
# （大头是客户保证金），拿通用"资产负债率 >70% 需警惕"去打分是错的。
if industry == "金融":
    _fk = st.radio(
        "金融子行业口径",
        ["🤖 自动识别（按取到的指标判定）", "🏦 银行", "🛡️ 保险", "📈 证券"],
        horizontal=True,
        key="single_finance_kind_widget",
        help="银行：净息差 · 不良率 · 拨备覆盖率 · 核心一级资本充足率；"
             "保险：新业务价值率 · 偿付能力充足率；"
             "证券：ROE · 净利率 + 金融杠杆表（80% 负债率属常态）。",
    )
    _fk_val = {"🏦 银行": "银行", "🛡️ 保险": "保险", "📈 证券": "证券"}.get(_fk, "")
    st.session_state["single_finance_kind"] = _fk_val
    st.caption(
        f"当前金融口径：**{_fk_val or '自动识别'}** —— "
        "四维指标构成与判读标准会随该选择切换（这就是「行业变化影响实际指标与评价依据」）。"
    )
else:
    st.session_state["single_finance_kind"] = ""

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

def system_prompt_with_kb(industry_override: str = ""):
    """拼装系统提示词。可指定行业口径 —— 双公司对比时必须能传「另一家的行业」，
    否则两家不同行业的公司会被套用同一个行业知识库（优化清单：对比共用同一套行业框架）。"""
    _ind = industry_override or industry
    _kb = INDUSTRY_KB.get(_ind) or INDUSTRY_KB.get("通用") or ""
    return BASE_SYSTEM_PROMPT + "\n\n" + _kb

# ---------------------------------------------------------------
# 视图 1：单公司深度分析
# ⚠️ 原来是 `with tab1:`，改成函数：由三栏工作台按左侧图标菜单的选中项调用。
#    函数体缩进本来就是 4，所以这里只改了一行声明，内部逻辑零改动。
# ---------------------------------------------------------------
def _view_single():
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
        # 【补充材料 · 三选一】原来这里直接摊开一块大输入框，看起来像"必须手填"。
        # 在线取数其实已经把行情、多期财务指标、年报检索全部接进来了，手填多数时候是多余的；
        # 但"系统拿不到的材料"（业绩说明会纪要、券商研报结论、自有 Excel）又确实需要入口。
        # 于是改成可选：默认不追加；需要时一键用在线数据生成摘要（完全不用打字）；
        # 只有真要粘第三方材料时，才落到手写框。
        st.markdown("##### ✍️ 补充材料（可选）")
        _extra_mode = st.radio(
            "补充材料方式",
            ["🟢 不追加（推荐）", "⚡ 一键用在线数据生成摘要", "✍️ 手动粘贴自定义内容"],
            horizontal=True,
            label_visibility="collapsed",
            key="single_extra_mode",
            help="在线取数已自动包含：实时行情、多期财务指标、年报检索片段。"
                 "只有当你还想补充系统拿不到的材料时，才需要在这里追加。",
        )
        if _extra_mode.startswith("🟢"):
            data_text = ""
            st.caption("✅ 已选择不追加 —— 本次分析完全使用在线取数结果，无需任何手动输入。")
        elif _extra_mode.startswith("⚡"):
            _ac1, _ac2 = st.columns([1, 3])
            with _ac1:
                if st.button("⬇️ 生成并填入摘要", key="single_autofill", use_container_width=True):
                    _digest = _build_online_digest()
                    if _digest:
                        st.session_state["single_text"] = _digest
                    else:
                        st.warning("还没有在线取数结果，请先点「🔍 获取数据」，或改用「手动粘贴自定义内容」。")
            with _ac2:
                st.caption("点左侧按钮即可用**当前在线数据**自动生成财务摘要填入下方，无需手打；填好后仍可自由编辑。")
            data_text = st.text_area(
                "财务摘要（自动生成，可编辑）",
                height=140,
                key="single_text",
                placeholder="点上方「⬇️ 生成并填入摘要」即可自动填充，无需手打。",
            )
        else:
            data_text = st.text_area(
                "粘贴补充的财报摘要（业绩说明会纪要 / 研报结论 / 自有数据）",
                height=140,
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

    # 【降级提示】pipeline 没加载成功时必须**当场**告诉用户，而不是等点完按钮报一句天书。
    # 正常部署下 _PIPELINE_SOURCE == "module"，这里什么都不显示。
    if _PIPELINE_SOURCE == "fallback":
        with st.expander("⚠️ 统一流程模块 pipeline 未能加载（当前使用内置等价兜底，点击查看原因）",
                         expanded=False):
            st.caption(
                "结果仍然可用，但**页面与离线评测可能不是同一份实现**，"
                "建议修复部署后重新分析。"
            )
            st.markdown(pipeline_diag_markdown())
            if _PIPELINE_TRACEBACK:
                with st.expander("展开完整堆栈"):
                    st.code(_PIPELINE_TRACEBACK, language="text")
    elif _PIPELINE_SOURCE == "file":
        st.caption("ℹ️ 统一流程模块已就绪（常规 import 失败，已按绝对路径加载）。")

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

                # 3) 规则评分 + 输出校验 + 证据核对 / 证据卡
                #    ⚠️ 整段与离线评测共用 pipeline.analyze_core()（P1-06：页面和评测复用同一业务流程）。
                #    各自写一份的后果是"评测报告证明的其实是另一条流水线"——
                #    页面改了阈值或口径，评测脚本不会跟着变，结论自然不可信。
                #
                #    历史 bug（已在本模块内修掉）：评分曾直接读会话里的 single_online_code，
                #    于是"先在线看了公司 A → 切到手动粘贴公司 B"时，评分仍去拉 A 的财务指标。
                #    pipeline 用 online= 显式开关复现这条防线。
                _is_online = src_mode.startswith("🌐")
                _oc = st.session_state.get("single_online_code", "") if _is_online else ""

                def _llm_extract(_text, _need):
                    """手动模式下正则抽不到的指标 → 让模型只抽数字（不打分）。"""
                    _mj = llm_once(
                        [
                            {"role": "system", "content": "你是严谨的财务数据抽取器，只输出 JSON。"},
                            {"role": "user", "content": scoring.METRICS_EXTRACTION_PROMPT + _text[:MAX_CHARS]},
                        ],
                        temperature=0.0,
                        action="metrics_extract",
                        json_mode=True,
                    )
                    return parse_json_loose(_mj) or {}

                _pg_count = (st.session_state.get("single_rag_status") or {}).get("pages")
                _core = _pipeline.analyze_core(
                    analysis,
                    raw_data,
                    industry,
                    code=_oc,
                    periods=int(st.session_state.get("single_periods", 4) or 4),
                    online=bool(_oc),
                    finance_kind=(st.session_state.get("single_finance_kind") or None),
                    pdftext=st.session_state.get("single_pdftext") or "",
                    rag_hits=rag_hits,
                    rag_pages=_pg_count if isinstance(_pg_count, int) else None,
                    metrics_extractor=_llm_extract,
                )
                analysis = _core["analysis"]

                # 4) 输出校验（字段/类型/范围/缺失值）—— 由 pipeline 内部调用，
                #    页面与评测看到的是同一份问题清单。
                for _iss in _core["validation_issues"]:
                    st.warning(f"输出校验：{_iss}")

                # 4b) 证据核对：把"看起来专业"的数字与页码引用，与真正拿到的来源对一遍。
                #     格式校验只能保证"字段合法"，保证不了"数字有出处"——
                #     一个凭空写出的 99999 亿元营收、一个第 99999 页的引用，格式上完全合法。
                #
                #     ⚠️ 证据库必须包含**年报原文**：营业成本 / 管理费用 / 经营活动现金流净额
                #        这类数字往往只出现在年报里（RAG 检索片段或解析出的全文），
                #        并不在 raw_data（在线取数摘要）中。pipeline 已把 pdftext + rag_hits
                #        一起纳入证据库，并对金额单位做换算链比对 —— 否则满屏黄条全是误报。
                _ev_issues = _core["evidence_issues"]
                if _ev_issues:
                    with st.expander(
                        f"🔎 证据核对：{len(_ev_issues)} 条提示（点击展开）", expanded=False
                    ):
                        st.caption(
                            "下列数值未能在「在线取数摘要 / 年报检索片段 / 年报全文」中匹配到出处，"
                            "可能由模型换算或推算而来，建议以年报原文为准。"
                            "（已放宽千分位、单位换算与两位小数舍入，仅保留确实对不上的条目。）"
                        )
                        for _iss in _ev_issues[:12]:
                            st.markdown(f"- {_iss}")
                        if len(_ev_issues) > 12:
                            st.caption(f"（另有 {len(_ev_issues) - 12} 条同类提示，已省略）")
                else:
                    st.caption(
                        "✅ 证据核对通过：指标与图表数值均可在「在线取数摘要 / 年报检索片段」中找到出处。"
                    )

                # 4c) 证据卡：把每个数值**正向**定位回它的出处（页码 + 原文片段）。
                #     P1-05 要求「关键结论可定位到支持的原文」—— 只报"哪些找不到"不够，
                #     还得能指着说"这一条出自第 42 页这一段"，否则"引用核验"无法被核查；
                #     证据不足时也要能明确说出"无法定位，原因是没有建索引"。
                _ev_items = _core["evidence_items"]
                st.session_state["evidence_items_last"] = _ev_items
                _ev_num = [e for e in _ev_items
                           if isinstance(e.get("value"), (int, float))
                           and not isinstance(e.get("value"), bool)]
                _ev_located = [e for e in _ev_num if e.get("found") and e.get("page")]
                if _ev_num:
                    with st.expander(
                        f"🔗 证据卡：{len(_ev_located)}/{len(_ev_num)} 项数值可定位到年报原文",
                        expanded=False,
                    ):
                        if _ev_located:
                            st.caption(
                                "数值与它在年报中的出处（页码 + 原文片段）一一对应；"
                                "未建年报向量索引时只能定位到取数摘要，无法给出页码。"
                            )
                        else:
                            st.caption(
                                "当前**未定位到任何年报页码** —— 多为尚未建立年报向量索引（属"
                                "「证据不足，无法判断」的明确说明，而不是假装引用有效）。"
                                "在上方「📄 一键获取并解析年报 PDF」里下载解析一份年报后再分析，"
                                "即可获得「第X页 + 原文片段」级别的引用核验。"
                            )
                        _ev_df = pd.DataFrame([
                            {
                                "类型": e.get("kind", ""),
                                "指标": e.get("item", ""),
                                "数值": (f"{e['value']:g}"
                                         if isinstance(e.get("value"), (int, float))
                                         and not isinstance(e.get("value"), bool)
                                         else str(e.get("value", ""))),
                                "页码": (f"第 {e['page']} 页" if e.get("page") else "—"),
                                "定位结果": ("✅ " + str(e.get("source", ""))) if e.get("found")
                                            else ("⚠️ " + str(e.get("source", ""))),
                                "原文片段": str(e.get("snippet") or ""),
                            }
                            for e in _ev_items
                        ])
                        st.dataframe(_ev_df, use_container_width=True, hide_index=True)
                _cit = _core.get("citation") or {}
                if _cit.get("rate") is not None:
                    st.caption(
                        f"🧭 页码引用正确率：{_cit.get('ok', 0)}/{_cit.get('refs', 0)}"
                        f"（{_cit['rate'] * 100:.0f}%）"
                        "—— 指正文引用的「第X页」落在年报真实页数范围内的比例。"
                    )

                st.session_state["analysis"] = analysis
                st.session_state["analysis_data"] = raw_data
                # 每次重新分析时清空旧的追问记录与旧的研报，避免上下文/报告串台
                st.session_state["messages"] = []
                st.session_state.pop("report", None)

                # ---- 往「分析记录」列写一条（仅 session_state，不持久化）----
                # 存的是 analysis 的**引用**：回看时直接把它放回 session_state，
                # 中列那条记录就能把当时的评分、图表、证据一起还原出来。
                # ⚠️ 明确不做持久化：Streamlit Cloud 文件系统是临时的，刷新即丢；
                #    真要跨会话恢复，必须外接存储，那是二期工程。
                _hist = st.session_state.get("analysis_history") or []
                _hid = f"{st.session_state.get('single_online_code') or 'x'}-" \
                       f"{len(_hist)}-{id(analysis)}"
                _hist.append(
                    {
                        "id": _hid,
                        "code": st.session_state.get("single_online_code", ""),
                        "name": (analysis.get("company")
                                 or st.session_state.get("single_online_name", "")),
                        "industry": industry,
                        "total": analysis.get("total_score"),
                        "ts": datetime.datetime.now().strftime("%H:%M:%S"),
                        "analysis": analysis,
                    }
                )
                st.session_state["analysis_history"] = _hist[-20:]
                st.session_state["hist_active"] = _hid
            except Exception as e:
                # 报错必须**自带病因**。旧版只写 f"{e}"，于是模块没加载时用户看到的
                # 是一句 `'NoneType' object has no attribute 'analyze_core'` —— 无法行动。
                # 现在：异常类型 + 异常原文 + 模块加载自检表 + 完整堆栈，一次给全。
                st.error(
                    f"结构化分析失败：{type(e).__name__}: {e}"
                    " —— 已保留完整技术细节，展开下方「排查信息」即可定位。"
                )
                with st.expander("🔧 排查信息（模块加载状态 + 完整调用堆栈）", expanded=True):
                    if isinstance(e, AttributeError) and "NoneType" in str(e):
                        st.warning(
                            "典型的「统一流程模块未加载」症状：pipeline 拿不到时，"
                            "它的属性访问就会抛 NoneType 错误。请按下表确认本环境状态。"
                        )
                    st.markdown(pipeline_diag_markdown())
                    import traceback as _tb_dbg
                    st.code(_tb_dbg.format_exc(), language="text")
                st.caption(
                    "可重试；若反复失败，请确认云端仓库里存在 pipeline.py，"
                    "并在 Streamlit Cloud 点一次 Reboot 后重试。"
                )

    analysis = st.session_state.get("analysis")
    if analysis:
        # 兜底默认值：这三个变量原本在 `if analysis.get("score_engine")` 分支里才赋值，
        # 页签化之后读取它们的代码脱离了那个分支（变成独立函数），
        # 历史数据缺 score_engine 字段时会直接 NameError —— 这里先给安全默认值，
        # 后面真正算出来会覆盖。
        _reasons = analysis.get("score_reasons") or {}
        _missing = analysis.get("score_missing") or []
        _suspicious = []
        # 年报趋势：公共头算一次，多个页签共用（概览页补 ROE/增速、依据页画趋势图）。
        # 原来「算趋势」写在雷达图旁边的分支里，页签化后其它页签也要用，提到公共头。
        _oc = st.session_state.get("single_online_code", "")
        _trend_annual = []
        if _oc:
            try:
                _trend_annual = ds.build_annual_trend(_oc, years=5) or []
            except Exception:
                _trend_annual = []
        st.divider()
        st.subheader(f"📊 结构化分析结果：{analysis.get('company', '目标公司')}")
        if analysis.get("summary"):
            st.caption(analysis["summary"])

        scores = analysis.get("scores", {}) or {}
        total = analysis.get("total_score")
        # 维度名随行业切换（金融行业是 盈利能力/成长性/资产质量/资本充足），
        # 所以这里**按 scores 的实际键动态渲染**，不再写死四个名字 ——
        # 否则银行股会永远少一张卡片（优化清单：银行/保险股四维评分会缺一维）。
        _dim_items = [(k, v) for k, v in scores.items()]
        _cols = st.columns(max(len(_dim_items) + 1, 2))
        for _i, (_dn, _dv) in enumerate(_dim_items):
            with _cols[_i]:
                render_score_card(_dn, _dv)
        with _cols[len(_dim_items)]:
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
            # 数值合理性兜底：接口口径变更 / 单位换算写错时，至少让人看见"这个数不对劲"，
            # 而不是静默输出一个看起来正常、其实算错的分数。
            try:
                _suspicious = ds.sanity_check_metrics(_mraw_show)
            except Exception:
                _suspicious = []
        # ---- 工作台四个子页签：把一次分析的结果按「阅读动线」拆开 ----
        # 原来是一条长页面从上往下堆（图表 → 时间轴 → 评分依据 → 研报），
        # 想看年度时间轴要滚很久。现在按使用顺序分四页：
        #   概览看数字 → 时间轴看多年 → 评分与依据看证据 → 研报看结论。
        _w1, _w2, _w3, _w4 = st.tabs(
            ["📊 分析概览", "🗓️ 年度时间轴", "🧮 评分与依据", "📝 研报"]
        )

    # ⚠️ 下面每个 _tab*_ 函数体**一行渲染逻辑都没改**，只是外面套了一层 def。
    #    之所以不把它们塞进 `if analysis:` 里（那需要整体再缩进一级），
    #    是因为批量改缩进最容易引入隐性 bug；套 def 是零改动的等价做法。
    def _tab1_a():
        """分析概览 · 数据合理性提醒。"""
        if _suspicious:
            with st.expander(f"⚠️ 数据合理性提醒（{len(_suspicious)} 项）", expanded=True):
                for _sus in _suspicious:
                    st.warning(_sus)
                st.caption("以上为数值区间异常提示：若确认上游接口口径未变，可忽略；"
                           "否则请勿直接采信本次评分。")
    def _tab3_a():
        """评分与依据 · 每个分数怎么算出来的（规则引擎依据链）。"""
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

    def _tab1_b():
        """分析概览 · 关键财务指标图 + 构成圆环 + 指标明细。"""
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

    def _tab3_g():
        """评分与依据 · 四维评分雷达 + 年报趋势（评分区间与多年走势是打分的直接依据）。"""
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
            # _trend_annual 已在公共头算好（概览页补 ROE/营收增速也要用它），
            # 这里直接复用，避免同一份数据发起两次取数请求。
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

    def _tab1_c():
        """分析概览 · ROE × 营收增速 四象限定位。"""
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

    def _tab2_e():
        """年度时间轴 · 近 N 年年报（独立于 LLM 的确定性区块，可复现）。"""
        # ---- 🗓️ 年度时间轴（优化清单 P1-01）----
        # 独立于 LLM 的确定性区块：数据直接来自财务指标接口的年报记录，
        # 所以年份切换、指标切换、导出都可复现，不受模型输出影响。
        _tl_code = st.session_state.get("single_online_code", "")
        if _tl_code:
            st.divider()
            render_year_timeline(
                _tl_code,
                name=(analysis.get("company")
                      or st.session_state.get("single_online_name", "")),
            )
        else:
            st.caption("ℹ️ 年度时间轴需要在线取数（按股票代码抓取多年年报）；"
                       "手动粘贴模式无此区块。")

    def _tab3_dh():
        """评分与依据 · 风险提示 / 投资亮点 + RAG 检索证据（带页码的原文片段）。"""
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
            with st.expander(f"📚 RAG 检索过程 · Top-{min(len(_last_rag), 8)} 年报相关章节（语义+关键词混合重排，已默认展开）", expanded=True):
                st.caption("以下片段是分析时从年报全文中按问题检索得到的关键章节，AI 回答的页码引用即源于此。"
                           "「相关度」= 语义相似度 + 关键词重叠率的加权结果（避免语义相近但答非所问的段落排到前面）。")
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

    def _tab4():
        """研报 · 完整投研报告正文 + 下载入口。"""
        report = st.session_state.get("report")
        if not report:
            st.caption(
                "还没有研报。点击工作台上方「📝 生成完整研报」，"
                "生成完成后会显示在这里（可下载 Markdown）。"
            )
            return
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
        st.download_button(
            "⬇️ 下载研报（Markdown）",
            data=report,
            file_name="投研分析报告.md",
            mime="text/markdown",
            use_container_width=True,
        )

    # ---- 把四个子页签填上内容 ----
    # 必须放在「研报生成逻辑」之后：点「📝 生成完整研报」时先把 result 落进
    # session_state，再渲染页签，否则本轮看到的还是上一份旧稿。
    if analysis:
        with _w1:
            _tab1_a()
            _tab1_b()
            _tab1_c()
        with _w2:
            _tab2_e()
        with _w3:
            _tab3_a()
            _tab3_g()
            _tab3_dh()
        with _w4:
            _tab4()

    # ---- 追问对话（多轮，上下文实时更新） ----
    # 【顺序很关键 · 为什么这么排】
    # st.chat_input 只在「页面主体」才会钉在屏幕底部；写在 tab 这类容器里是**内联渲染**，
    # 也就是"谁先执行谁在上面"。旧版把 chat_input 写在生成回答**之前**，于是：
    # 提问 → 输入框先冒出来 → 回答又出现在输入框**下面**，读起来像"答在问前面"。
    # 现在改成：历史(含本轮提问) → 本轮回答 → 推荐问题 → 输入框放最后，
    # 即「问在上、答在中、继续追问的输入框在最下面」，和常见对话产品的顺序一致。
    st.divider()
    _chat_h, _chat_c = st.columns([5, 1])
    with _chat_h:
        st.markdown("##### 💬 继续追问")
    if "messages" not in st.session_state:
        st.session_state["messages"] = []
    with _chat_c:
        if st.session_state["messages"]:
            if st.button("🗑️ 清空", key="clear_chat", use_container_width=True):
                st.session_state["messages"] = []
                st.rerun()
    st.caption("基于当前财务数据与已生成的分析继续提问，支持连续多轮对话；新回答会出现在输入框上方。")

    # 渲染历史消息。本轮的提问在上一轮提交时就已入队，因此这里会一起渲染 → 位于输入框上方。
    for m in st.session_state["messages"]:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    # 提交时的校验提示：上一轮暂存，这里渲染（否则会掉到输入框下方）
    _chat_notice = st.session_state.pop("_chat_notice", None)
    if _chat_notice:
        _lvl, _txt = _chat_notice
        (st.warning if _lvl == "warn" else st.error)(_txt)

    # 本轮待回答的问题（由上一轮的输入框/推荐问题入队）
    question = st.session_state.pop("_answer_pending", "")

    # ---- 生成本轮回答 ----
    # ⚠️ 这一整段必须放在「推荐问题」和「输入框」**之前**：
    #    内联模式下渲染顺序就是屏幕顺序，写在这里答案才会落在输入框上方。
    if question:
        # 1) 组装上下文：数据 + 结构化评分 + RAG 检索 + 历史对话
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

        # 历史对话 = 除最后一条（本轮提问，已由上面的历史渲染展示）以外的消息，最多最近 3 轮
        history = st.session_state["messages"][:-1]
        history = history[-6:]
        msgs = [{"role": "system", "content": system_prompt_with_kb()}]
        msgs.append({"role": "user", "content": base_context + "\n\n以上是背景资料，请基于它回答后续问题。"})
        msgs.append({"role": "assistant", "content": "好的，我已了解该公司的财务数据与行业背景，请提问。"})
        msgs.extend(history)
        msgs.append({"role": "user", "content": question})

        # 2) 流式生成回答
        try:
            with st.chat_message("assistant"):
                box = st.empty()
                with st.spinner("正在分析……"):
                    answer = llm_stream(msgs, box, action="followup")
            st.session_state["messages"].append({"role": "assistant", "content": answer})
        except Exception as e:
            st.error(f"调用失败：{e}")

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
        # 首次进入时展开（引导提问）；对话开始后折叠，避免一堆按钮把输入框推得太远
        with st.expander("🎯 智能推荐提问（根据当前公司与最新评分实时生成，点击即问）",
                         expanded=not st.session_state["messages"]):
            _qcols = st.columns(2)
            for _i, _sq in enumerate(_suggestions[:6]):
                with _qcols[_i % 2]:
                    if st.button(_sq, key=f"suggest_q_{_i}", use_container_width=True):
                        # 与输入框提交走**同一条入队逻辑**：先把用户提问写进消息列表，
                        # 再重跑。少任何一步都会出问题 ——
                        #   · 不 append：推荐问题在对话流里"消失"，且下面算历史时
                        #     会误把上一条助手回答当成"本轮提问"而丢掉；
                        #   · 不 rerun：答案会渲染到推荐按钮/输入框下面。
                        st.session_state["messages"].append({"role": "user", "content": _sq})
                        st.session_state["_answer_pending"] = _sq
                        st.rerun()

    # ---- 提问入口：放在最后（内联模式下"最后执行 = 显示在最下面"）----
    # ⚠️ 必须**无条件**实例化 chat_input。写成 `_pending or st.chat_input(...)` 会因
    #    Python 短路求值而在命中上一项时整个输入框当轮消失。
    _typed = st.chat_input("例如：这家公司的毛利率在同行业处于什么水平？", key="chat_q")
    _new_q = (_typed or "").strip()
    if _new_q:
        if len(_new_q) > 500:
            # 【长度保护】超长输入截断到 500 字，防单次调用成本与耗时失控
            _new_q = _new_q[:500]
            st.session_state["_chat_notice"] = ("warn", "⚠️ 问题过长，已截取前 500 字进行回答。")
        if not raw_data:
            st.session_state["_chat_notice"] = ("warn", "请先获取或粘贴财务数据，再提问。")
        elif not DEEPSEEK_API_KEY:
            st.session_state["_chat_notice"] = ("error", "系统未配置 DEEPSEEK_API_KEY，请联系管理员。")
        else:
            # 用户消息先入队，然后立刻重跑：下一轮它由「历史消息」渲染 → 出现在输入框上方
            st.session_state["messages"].append({"role": "user", "content": _new_q})
            st.session_state["_answer_pending"] = _new_q
        st.rerun()

# ---------------------------------------------------------------
# 视图 2：多公司对比（2~5 家）
# ---------------------------------------------------------------
def _view_compare():
    st.markdown(
        "支持 **2~5 家**公司横向对比：在线自动取数或手动输入，"
        "程序生成对照表、差额与对比图（同一份数据永远同一分数）。"
    )

    # ---- 公司数量：动态增删（优化清单 P1-02）----
    _MAX_CMP = 5
    if "cmp_n" not in st.session_state:
        st.session_state["cmp_n"] = 2
    _nb1, _nb2, _nb3 = st.columns([1, 1, 5])
    with _nb1:
        if st.button("➕ 添加公司", key="cmp_add", use_container_width=True,
                     disabled=st.session_state["cmp_n"] >= _MAX_CMP):
            st.session_state["cmp_n"] += 1
            st.rerun()
    with _nb2:
        if st.button("➖ 移除末位", key="cmp_del", use_container_width=True,
                     disabled=st.session_state["cmp_n"] <= 2):
            _drop = st.session_state["cmp_n"] - 1
            # 【关键】移除后必须把这家公司的**数据与派生结果**一起清掉：
            # 只改数量不清理的话，对照表/图表里会继续出现"已经删掉的那家公司"（P1-02 验收）。
            _panel_clear(f"cmp{_drop}")
            st.session_state["cmp_n"] = max(2, st.session_state["cmp_n"] - 1)
            st.session_state.pop("cmp_scores", None)
            st.session_state.pop("cmp_report", None)
            _panel_clear(f"cmp{st.session_state['cmp_n']}")
            st.rerun()
    with _nb3:
        st.caption(
            f"当前对比 **{st.session_state['cmp_n']}** 家（2~{_MAX_CMP} 家）。"
            "点「➕ / ➖」增删输入区域；**移除公司后对照表、图表与报告会同步更新**。"
        )

    _n_cmp = int(st.session_state["cmp_n"])
    _cmp_tags = ["🅰️", "🅱️", "🅲", "🅳", "🅴"]
    _cmp_cols = st.columns(_n_cmp)
    _cmp_side = []          # 每家: dict(i, name, code, online, data, industry, text)

    def _cmp_industry_select(key: str, default_val: str) -> str:
        """每家公司的行业口径（P1-03：各公司独立设置行业）。

        ⚠️ Streamlit 控件一旦有 key，index 参数即失效 —— 必须显式写 session_state。
        否则会出现"公司换成了宁德时代，行业口径却还停在贵州茅台的消费"。
        """
        if key not in st.session_state:
            st.session_state[key] = default_val
        _auto_key = key + "__auto"
        _man_key = key + "__manual"
        # 名称自动识别的行业变了、且用户从未手动改过 → 自动跟随
        if st.session_state.get(_auto_key) != default_val and _man_key not in st.session_state:
            st.session_state[key] = default_val
        st.session_state[_auto_key] = default_val

        def _onch():
            st.session_state[_man_key] = st.session_state[key]

        return st.selectbox("行业口径", list(INDUSTRY_KB.keys()), key=key, on_change=_onch)

    for _i in range(_n_cmp):
        with _cmp_cols[_i]:
            st.markdown(f"##### {_cmp_tags[_i]} 公司 {chr(65 + _i)}")
            # ⚠️ 这个复选框的 key 绝不能叫 f"cmp{_i}_online"：
            #    online_fetch_panel 内部用 `<prefix>_online` 存放"取到的摘要文本"，
            #    同名会让布尔值把文本覆盖掉 → len(bool) 直接抛 TypeError。
            _use_online = st.checkbox("🌐 在线自动取数", key=f"cmp{_i}_use_online", value=True)
            _text_i = ""
            if _use_online:
                _ot, _on, _op = online_fetch_panel(f"cmp{_i}", compact=True)
                _nm = _on or st.text_input("公司名称（可选）", value=f"公司{chr(65 + _i)}",
                                           key=f"cmp{_i}_name")
                _data = (_ot or "").strip()
            else:
                _nm = st.text_input("公司名称", value=f"公司{chr(65 + _i)}", key=f"cmp{_i}_name")
                _text_i = st.text_area("粘贴财报数据", height=120, key=f"cmp{_i}_text")
                _up = st.file_uploader("或上传文件（PDF / txt / md）",
                                       type=["pdf", "txt", "md"], key=f"cmp{_i}_up")
                _file, _err = read_upload(_up)
                if _err:
                    st.warning(_err)
                _data = (_text_i + "\n" + _file).strip()
            _ind_default = ds.guess_industry(_nm) or industry
            _ind_i = _cmp_industry_select(f"cmp{_i}_ind", _ind_default)
            _cmp_side.append({
                "i": _i, "name": _nm,
                "code": st.session_state.get(f"cmp{_i}_online_code", ""),
                "online": _use_online, "data": _data,
                "industry": _ind_i, "text": _text_i,
            })

    # 【统一清空】对比对象变化时清空旧对比报告与旧评分对照。
    # 指纹必须包含**数据内容**：只比公司名/代码的话，把 A 的营收从 100 亿改成 900 亿，
    # 指纹不变 → 旧对比结论继续挂在页面上，与实际输入脱节。
    def _cmp_sig():
        import hashlib
        _h = lambda t: hashlib.md5((t or "").strip().encode("utf-8", errors="ignore")).hexdigest()[:10] if (t or "").strip() else "-"
        _parts = [str(_n_cmp)]
        for _s in _cmp_side:
            _parts += [_s["name"], _s["code"], _s["industry"], _h(_s["data"])]
        return "|".join(_parts)

    _csig_now = _cmp_sig()
    if st.session_state.get("cmp_sig") and st.session_state.get("cmp_sig") != _csig_now:
        _stale = []
        for _k in ("cmp_report", "cmp_scores"):
            if st.session_state.get(_k):
                st.session_state.pop(_k, None)
                _stale.append(_k)
        if _stale:
            st.info("🔄 对比对象、行业口径或输入数据已变化，旧的对比报告 / 评分对照已清空，"
                    "请重新点击「⚖️ 开始对比分析」。")
    st.session_state["cmp_sig"] = _csig_now

    # ---- 共同年报年份对齐（P1-03）----
    # 「默认选共同年报年份」+「不同年度不混比」：
    #   先各自取回全部年报行，求年份交集；交集非空 → 用一个选择器统一基准年份，
    #   所有公司都取这一年；交集为空 → 明确报错而不是硬凑一张跨年份的表。
    _annual_rows = {}
    for _s in _cmp_side:
        _rows = []
        if _s["online"] and _s["code"] and _pipeline is not None:
            try:
                _rows = _pipeline.all_annual_rows(_s["code"], periods=8)
            except Exception:
                _rows = []
        _annual_rows[_s["i"]] = _rows

    def _years_of(i):
        if _pipeline is None:
            return set()
        return {_pipeline.year_of(r) for r in _annual_rows.get(i, []) if _pipeline.year_of(r)}

    _all_online = _n_cmp >= 2 and all(_s["online"] and _s["code"] for _s in _cmp_side)
    _common_years = []
    if _all_online:
        _sets = [_years_of(_s["i"]) for _s in _cmp_side]
        _common_years = sorted([y for y in set.intersection(*_sets) if y], reverse=True)

    _base_year = ""
    if _all_online and _pipeline is not None:
        if _common_years:
            _base_year = st.selectbox(
                "对比基准年份（年报口径）",
                _common_years,
                key="cmp_base_year",
                help="只列出**所有公司都披露过**的年报年份，因此可以同年份对比；"
                     "切换年份后重新点「开始对比分析」即按新年份计算。",
            )
            st.caption(
                f"✅ 共同年报年份：{'、'.join(_common_years)}"
                f"（默认取最近一年 **{_base_year}**）。"
                "所有公司的对照表、差额与图表都按这一年取数 —— **不同年度不混比**。"
            )
        else:
            _avail = "；".join(
                f"{_s['name']}：{'、'.join(sorted(_years_of(_s['i']), reverse=True)) or '无年报数据'}"
                for _s in _cmp_side
            )
            st.error(
                "❌ **无共同年报年份，无法做同年份对比**（这正是「不同年度不混比」的要求，"
                "硬凑一张跨年份的表会得出错误的强弱结论）。"
                f"各公司可用年报年份：{_avail}。"
                "请更换其中一家公司，或把某一家改为「手动粘贴」模式按文本口径对比。"
            )

    def _cmp_side_metrics(side):
        """取一家公司的标准指标（在线优先且只取年报；对齐到基准年份）→ (metrics, 期别, 行业)。"""
        _ind = side["industry"]
        met, per = {}, ""
        if side["online"] and side["code"] and _pipeline is not None:
            _rows = _annual_rows.get(side["i"]) or []
            _row = None
            if _base_year:
                _row = next((r for r in _rows if _pipeline.year_of(r) == _base_year), None)
            if _row is None and _rows:
                _row = _rows[0]     # 非对齐模式（含手动输入）时退回最近一期年报
            if _row is not None:
                met = _row
                per = str(_row.get("report_name") or "")
            else:
                try:
                    met, per = _pipeline.annual_metrics(side["code"], periods=8)
                except Exception:
                    met, per = {}, ""
        if not met and scoring is not None and (side["data"] or "").strip():
            met = scoring.extract_metrics_regex(side["data"])
            per = "文本抽取"
        return met, per, _ind

    # ---- 输入合法性检查（优化清单 P1-02：重复公司 / 无效代码 / 未完成输入）----
    _codes = [_s["code"] for _s in _cmp_side]
    _dup_codes = sorted({c for c in _codes if c and _codes.count(c) > 1})
    _placeholder = {f"公司{chr(65 + i)}" for i in range(_n_cmp)}
    _named = [_s["name"] for _s in _cmp_side if _s["name"] and _s["name"] not in _placeholder]
    _dup_names = sorted({nm for nm in _named if _named.count(nm) > 1})
    _empty = [_s["name"] for _s in _cmp_side if not (_s["data"] or "").strip()]
    _invalid = [_s["name"] for _s in _cmp_side if _s["online"] and not _s["code"]]

    _block = False
    if _dup_codes or _dup_names:
        st.error(
            "❌ 存在**重复公司**（" + "、".join(_dup_names or _dup_codes) + "）——"
            "对比需要互不相同的公司，请至少更换其中一家（同一家公司的两份数据怎么比都是"
            "「两者相当」，纯属浪费一次分析）。"
        )
        _block = True
    if _invalid:
        st.warning(
            "⚠️ 以下公司选了「在线取数」但**尚未成功取到数据**（代码无效或还没点「🔍 获取数据」）："
            + "、".join(_invalid) + "。"
        )
        _block = True
    if _empty:
        st.warning("⚠️ 以下公司的**输入未完成**（没有可分析的数据）：" + "、".join(_empty) + "。")
        _block = True
    if _all_online and not _common_years:
        _block = True

    cmp_btn = st.button(
        "⚖️ 开始对比分析",
        disabled=_block,
        use_container_width=True, type="primary",
    )

    if cmp_btn and not _block:
        # 【程序算分】对比不该只让模型"读几段文本自由发挥"：
        # 每家公司各自跑一遍固定规则评分，对照表与差额由程序生成，模型只负责解释。
        if scoring is not None:
            with st.spinner(f"正在计算 {_n_cmp} 家公司的规则评分……"):
                try:
                    _items = []
                    for _s in _cmp_side:
                        _m, _p, _ind = _cmp_side_metrics(_s)
                        _items.append({
                            "name": _s["name"], "code": _s["code"],
                            "industry": _ind, "period": _p,
                            "scores": scoring.compute_scores(_m, _ind),
                            # 原始标准指标一起存：对比可视化必须与评分表来自**同一份取值**，
                            # 否则可能出现"表说 A 高、图说 B 高"
                            "raw": dict(_m or {}),
                        })
                    _inds_all = {it["industry"] for it in _items}
                    st.session_state["cmp_scores"] = {
                        "items": _items,
                        "cross_industry": len(_inds_all) > 1,
                        "base_year": _base_year,
                        "mode": ("年报同年份对齐" if _base_year else "文本/最近期口径"),
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
                    # 行业口径要**逐家**注入（优化清单：对比共用同一套行业框架）。
                    # 原先无论几家、什么行业，都只注入全局 industry 那一份知识库 ——
                    # 拿茅台（消费）比宁德时代（新能源）时，新能源那侧等于没有行业背景。
                    _cs_saved = st.session_state.get("cmp_scores") or {}
                    _items_saved = _cs_saved.get("items") or []
                    _blocks = []
                    for _it in _items_saved:
                        _s = next((x for x in _cmp_side if x["name"] == _it["name"]), None)
                        _txt = ((_s or {}).get("data") or "")[:MAX_CHARS]
                        _blocks.append(
                            f"【{_it['name']}｜行业：{_it['industry']}｜基准期：{_it['period'] or '未获取'}】\n{_txt}"
                        )
                    _inds_txt = " vs ".join(sorted({_it["industry"] for _it in _items_saved})) or industry
                    cmp_prompt = (
                        f"请对比以下 {len(_items_saved)} 家公司的财务表现：\n\n"
                        + "\n\n".join(_blocks)
                        + "\n\n输出要求（markdown 格式）：\n"
                        "1. 先给出『核心指标对比表』（markdown 表格，**行=公司**，列至少包括：营收、净利润、毛利率、净利率、ROE、资产负债率、现金流，数据缺失的单元格写 N/A）；\n"
                        "2. 逐维度点评谁更强、依据是什么；\n"
                        "3. 【综合结论】分别用一句话总结各家公司的优劣势与适配的投资者类型；\n"
                        "4. 【风险提示】说明对比结论的局限性（数据口径、行业差异等）。\n"
                        "所有数字必须来自提供的数据，禁止编造。\n"
                        + (
                            f"\n注意：这些公司分属不同行业（{_inds_txt}），必须各自按本行业的财务特征判读"
                            "（银行看不良率/资本充足率，消费看毛利率与品牌溢价），"
                            "并明确说明**跨行业不可直接比高低**、分数不得作为排名依据。"
                            if _cs_saved.get("cross_industry") else ""
                        )
                        + (f"\n本次所有公司统一取 **{_base_year} 年年报**数据，可直接同年份比较。"
                           if _cs_saved.get("base_year") else "")
                    )
                    _sys = system_prompt_with_kb(
                        _items_saved[0]["industry"] if _items_saved else industry
                    )
                    _base_ind = _items_saved[0]["industry"] if _items_saved else industry
                    for _it in _items_saved[1:]:
                        if _it["industry"] != _base_ind:
                            _kb = INDUSTRY_KB.get(_it["industry"]) or ""
                            if _kb and _kb not in _sys:
                                _sys += "\n\n" + _kb
                    cmp_result = llm_stream(
                        [{"role": "system", "content": _sys},
                         {"role": "user", "content": cmp_prompt}],
                        box,
                        temperature=0.3,
                        action="compare",
                    )
                    st.session_state["cmp_report"] = cmp_result
                    box.empty()  # 交给下方统一渲染，保证"看到的内容"= "下载的内容"
                except Exception as e:
                    box.empty()
                    st.error(f"对比分析失败：{e}")

    # ---- 规则评分对照（程序计算，可复现；不再是"模型读几段文本自由发挥"） ----
    _cs = st.session_state.get("cmp_scores")
    if _cs and (_cs.get("items") or []):
        _items = _cs["items"]
        _names = [it["name"] for it in _items]
        # 维度名按各家实际打分键的**并集**动态取（金融是 资产质量/资本充足，
        # 通用是 偿债能力/现金流质量），写死会漏维度。
        _dims = []
        for _it in _items:
            for _d in _it["scores"]["scores"].keys():
                if _d not in _dims:
                    _dims.append(_d)

        def _fmt(v):
            return "—" if v is None else str(v)

        _rows = []
        for _d in _dims:
            _rec = {"维度": _d}
            for _it in _items:
                _rec[_it["name"]] = _fmt(_it["scores"]["scores"].get(_d))
            _nums = [v for v in (_it["scores"]["scores"].get(_d) for _it in _items)
                     if isinstance(v, (int, float))]
            _rec["极差"] = f"{max(_nums) - min(_nums):.1f}" if len(_nums) >= 2 else "—"
            _rows.append(_rec)
        _rec = {"维度": "综合评分"}
        for _it in _items:
            _rec[_it["name"]] = _fmt(_it["scores"]["total_score"])
        _tn = [v for v in (_it["scores"]["total_score"] for _it in _items)
               if isinstance(v, (int, float))]
        _rec["极差"] = f"{max(_tn) - min(_tn):.1f}" if len(_tn) >= 2 else "—"
        _rows.append(_rec)
        _rec = {"维度": "指标覆盖率（可信度）"}
        for _it in _items:
            _rec[_it["name"]] = f"{_it['scores']['coverage'] * 100:.0f}%"
        _rec["极差"] = "—"
        _rows.append(_rec)

        st.markdown("##### 📐 规则引擎评分对照（程序计算 · 同一份数据永远同一分数）")
        _cmp_df = pd.DataFrame(_rows).set_index("维度")
        st.dataframe(_cmp_df, use_container_width=True)

        # ---- 对比可视化（N 家；缺哪家就标 N/A，不画 0 误导）----
        st.markdown("##### 📊 对比可视化")
        _mets = [it["raw"] for it in _items]
        _vg1, _vg2 = st.columns(2)
        with _vg1:
            _png = fig_compare_group_n(_mets, _names, _CMP_GROUP_PROFIT, "盈利与成长对比（%）")
            if not show_png(_png, "cmp-profit"):
                st.caption("各公司均未取到可比的盈利类指标（%），暂不绘制。")
        with _vg2:
            _png = fig_compare_group_n(_mets, _names, _CMP_GROUP_RISK, "风险与偿债对比（%）")
            if not show_png(_png, "cmp-risk"):
                st.caption("各公司均未取到可比的风险类指标（%），暂不绘制。")
        _png = fig_compare_dims_n([it["scores"]["scores"] for it in _items], _names)
        if _png:
            show_png(_png, "cmp-dims")

        st.caption(
            "评分基准期："
            + "；".join(f"{it['name']} = {it['period'] or '未获取'}" for it in _items)
            + "。总分为各维度均分；覆盖率低于 50% 时不给综合评分。"
            + (f" 本次统一按 **{_cs.get('base_year')} 年年报**取数（口径：{_cs.get('mode')}）。"
               if _cs.get("base_year") else f"（口径：{_cs.get('mode')}）")
        )
        # 报告期可比性：中期报告是"年初至今累计值"，与年报不可比，
        # 跨期直接比大小会得出错误结论（优化清单 P1-03：不同年度不混比）。
        _per_years = {}
        for _it in _items:
            _m = re.search(r"(20\d{2})", _it["period"] or "")
            if _m:
                _per_years[_it["name"]] = _m.group(1)
        if len(set(_per_years.values())) > 1:
            st.warning(
                "⚠️ 各公司的评分基准期不在同一年（"
                + "、".join(f"{k}:{v}" for k, v in _per_years.items())
                + "），**不同年度的数据不可直接比大小**，请先统一到同一报告期后再比较。"
            )
        if any(("半年报" in (_it["period"] or "")) or ("季" in (_it["period"] or ""))
               for _it in _items):
            st.caption("ℹ️ 有公司使用了中期报告口径（年初至今累计值），与年报不可直接比较。")

        # 对照表导出（优化清单 P1-03：支持 CSV 导出，且导出内容与页面一致）
        st.download_button(
            "⬇️ 导出评分对照表（CSV）",
            data=_cmp_df.to_csv().encode("utf-8-sig"),
            file_name=f"对比评分对照_{'_vs_'.join(_names)}.csv",
            mime="text/csv",
            use_container_width=False,
        )
        if _cs.get("cross_industry"):
            st.warning(
                "⚠️ 本次对比包含**多个行业**（"
                + "、".join(f"{it['name']}：{it['industry']}" for it in _items)
                + "），各自按本行业基准线评分 —— **分数不可直接横向比较，也不得作为排名依据**，"
                "请结合下方文字对比与业务差异一起判断。"
            )

    cmp_report = st.session_state.get("cmp_report")
    if cmp_report:
        st.markdown("#### 📄 对比分析结论")
        st.markdown(cmp_report)
        st.download_button(
            "⬇️ 下载对比报告（Markdown）",
            data=cmp_report,
            file_name="多公司对比报告.md",
            mime="text/markdown",
            use_container_width=True,
        )

# ---------------------------------------------------------------
# 视图 3：使用说明
# ---------------------------------------------------------------
def _view_help():
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
| 💬 多轮追问 | 「单公司深度分析」页底部 | 基于数据与已有结论连续追问（问在上、答在中、**输入框在最下面**）；年报问题实时检索原文片段（带页码） |
| ⚖️ 多公司对比 | 「多公司对比」页 | 2~5 家公司在线取数或手动输入，逐年对齐后生成对照表、差额、对比图与 CSV / 报告导出 |
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

# ---------------------------------------------------------------
# 三栏工作台：左（图标菜单）· 中（分析记录）· 右（工作台）
#
# 为什么这么改：原来三个视图是顶部 st.tabs 横排页签，切一次要横向找，
# 而且主内容一屏塞不下；改成三栏后 —— 导航固定在最左、分析记录常驻中间、
# 工作台占最大面积，符合"研究工作台"的使用习惯。
#
# ⚠️ 实现方式：把原来的 `with tabN:` 改成 `def _view_x():`，函数体缩进不变，
#    所以三个视图内部上千行逻辑一行没动，只是换了调用入口。
# ---------------------------------------------------------------
_NAV_ITEMS = [
    ("🏢", "单公司深度分析"),
    ("⚖️", "多公司对比（2~5 家）"),
    ("📖", "使用说明"),
]


def _render_icon_menu() -> None:
    """最左列：图标菜单（约 76px 宽）。点击切换右侧工作台视图。"""
    _cur = st.session_state.get("main_view", "🏢")
    for _icon, _label in _NAV_ITEMS:
        if st.button(
            _icon,
            key=f"nav_btn_{_icon}",
            help=_label,
            use_container_width=True,
            type="primary" if _icon == _cur else "secondary",
        ):
            st.session_state["main_view"] = _icon
            st.rerun()
    st.markdown('<div class="rail-cap">点击切换<br/>右侧工作台</div>', unsafe_allow_html=True)


def _render_history_column() -> None:
    """中列：本次会话的分析记录。

    ⚠️ 明确边界：**只存 session_state，不做持久化**。
    Streamlit Cloud 的文件系统是临时的，刷新页面即丢；
    真要做"历史可恢复"必须外接存储（Supabase / Sheets / Redis），
    那属于二期工程，这里先把位置与交互留出来。
    """
    st.markdown('<div class="hist-head">🕘 分析记录</div>', unsafe_allow_html=True)
    _hist = st.session_state.get("analysis_history") or []
    if not _hist:
        st.caption("还没有分析记录。完成一次「🚀 开始结构化分析」后，这里会留下一条，可点回查看。")
        st.caption("⚠️ 记录仅存于当前会话内存，刷新页面即清空（暂不做持久化）。")
        return
    st.caption(f"共 {len(_hist)} 条 · 仅本次会话有效")
    for _i in range(len(_hist) - 1, -1, -1):
        _h = _hist[_i]
        _on = st.session_state.get("hist_active") == _h.get("id")
        _mark = "🔵 " if _on else ""
        if st.button(
            f"{_mark}{_h.get('name') or '—'} · {_h.get('code') or '—'}",
            key=f"hist_btn_{_i}",
            help="回看这条分析结果",
            use_container_width=True,
        ):
            st.session_state["analysis"] = _h.get("analysis")
            st.session_state["hist_active"] = _h.get("id")
            st.rerun()
        st.caption(
            f"{_h.get('ts', '')} · {_h.get('industry', '')} · 综合 {_h.get('total', '—')}"
        )


_nav_col, _hist_col, _work_col = st.columns([0.62, 2.0, 9.2], gap="small")

with _nav_col:
    _render_icon_menu()

with _hist_col:
    # 定高容器 → 记录多了中列自己滚动，不把整页撑长
    with st.container(height=460):
        _render_history_column()

with _work_col:
    _v = st.session_state.get("main_view", "🏢")
    if _v == "⚖️":
        _view_compare()
    elif _v == "📖":
        _view_help()
    else:
        _view_single()

st.divider()
st.markdown(
    '<p class="footer-note">💡 <b>金融智能投研 Agent</b> · 第二届全国人工智能应用创新大赛参赛作品 · 分析结果仅供参考，不构成投资建议</p>',
    unsafe_allow_html=True,
)

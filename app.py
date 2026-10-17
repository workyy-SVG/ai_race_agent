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
from contextlib import contextmanager
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
except Exception:
    rag = None  # 模块可选缺失, 走兜底
# 【v13 · 用户「为什么没给我建立年报索引」】
# 旧写法只捕获 ImportError 且没有第二梯队：rag_engine 顶层 import chromadb /
# dashscope 一失败 → rag = None → 面板里 `if rag is not None` 永远不成立 →
# 点了「下载并解析」也建不出索引，页面永远挂着"未建立年报向量索引"且无任何报错。
# 现在 rag_engine 已把硬依赖改成可选（自带 rag_lite 零依赖后端），
# 这里再补一层：万一 rag_engine 仍不可用，直接把 rag_lite 顶上作为 rag，
# 保证「解析了年报就一定能拿到页码引用」在任何环境下都成立。
if rag is None:
    try:
        import rag_lite as rag
    except Exception:
        rag = None
try:
    import llm_qwen as lq
except ImportError:
    lq = None

# ---- Agent 执行轨迹（工具调用链）----
# 赛题是「让 AI Agent 赋能千行百业」，但页面上原本只呈现**最终结果**：
# Agent 自己调用了哪些工具、按什么顺序、哪一步走了降级，评委看不见。
# agent_trace 把这些真实发生的步骤落成台账，让「Agent 做了什么」从代码事实变成页面事实。
# ⚠️ 它是**观测层**：模块缺失或记录异常都不允许反过来拖垮分析，所以给了空实现兜底。
try:
    import agent_trace
except ImportError:
    agent_trace = None


class _NullTracer:
    """agent_trace 缺失时的空实现（与 rag / lq 同一套「可选模块」思路）。"""

    class _S:
        out = ""
        note = ""
        status = "ok"

        def finish(self):
            pass

    @contextmanager
    def span(self, *a, **k):
        yield self._S()

    def hit(self, *a, **k):
        return self._S()

    def rows(self):
        return []

    def summary(self):
        return {}

    def clear(self):
        pass

    @property
    def steps(self):
        return []


_NULL_TRACER = _NullTracer()

# 轨迹里「动作」的中文名：直接把 action= 打出来太工程化，评委看不懂
_ACTION_LABEL = {
    "analysis": "结构化财务抽取",
    "metrics_extract": "指标兜底抽取",
    "chat": "追问回答（带年报检索）",
    "report": "研报生成",
    "polish": "千问润色校验",
    "llm": "通用对话",
}


def get_tracer():
    """取当前会话的轨迹记录器。

    拿不到 session_state（离线评测、_import 期）时返回一个一次性实例 ——
    记录自然被丢弃，但调用方代码不用写任何分支，业务链路保持干净。
    """
    if agent_trace is None:
        return _NULL_TRACER
    try:
        _tr = st.session_state.get("agent_tracer")
        if _tr is None:
            _tr = agent_trace.Tracer()
            st.session_state["agent_tracer"] = _tr
        return _tr
    except Exception:
        return agent_trace.Tracer()

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

# ============ 全局样式（金融主题：浅冷灰底 + 单一品牌蓝） ============
CUSTOM_CSS = """
<style>
  /* ==========================================================
     金融智能投研 Agent · 深蓝侧栏 + 浅蓝灰工作台 (v7)
     v3 → v4：颜色太多（到处蓝字、金蓝撞色）→ 收敛成单一结构色 + 深灰文字
     v4 → v5：结构"一坨" → 页面大标题 + 一步一张白卡
     v5 → v6：结构对了但"整体还是白的" → 深蓝侧栏 + 浅蓝灰底 + 多栏网格
     v6 → v7：用户四条新反馈，逐条对应：
       ① **字体偏小** → config.toml 抬高根字号 baseFontSize=17（原生组件整体放大）
          + 自定义类的 px 字号一律上调 1~3px（px 不随根字号变，必须手改）
       ② **有些字看不清** → 主区辅色 #64748B 在浅蓝灰底上只有 3.77:1（不合格），
          统一换成 #475569（6.0:1），卡片说明再提到 #3B4A5F（7.14:1）；
          深蓝侧栏文字整体提亮：#E8F0FF→#EAF4FF（7.69:1）、
          #B7CAEA→#AFD2F5（5.44:1）、强调青 #67E8F9→#7FE9FF（6.11:1）
       ③ **界面太空旷** → 补纯 CSS/SVG 图形：页面标题右侧科技徽记、
          卡片编号发光方块、侧栏底部网格光环、未取数时的空状态插图；
          登录页补左右两侧悬浮数据面板 + 旋转光环 + 扫描光 + 节点网络
       ④ **侧栏选中项"点了会不会变全白"** → 不再依赖 `button[kind=primary]`
          （styled-components v6 不再把未知属性写到 DOM，这条规则可能整条失效），
          改用**应用自己控制的 key 类名** `st-key-nav_on_*`：
          选中态的白底与深蓝字写在**同一条规则里**，不可能只生效一半。
     正文三级： 主 #0B1F3A (16.5:1) / 次 #334155 (10.4:1) / 辅 #3B4A5F (9.0:1)
     深蓝侧栏（#0E2A5E→#16489E）实测对比度（以最亮端 #16489E 为最坏情况）：
        #EAF4FF 7.69:1 / #AFD2F5 5.44:1 / 强调青 #7FE9FF 6.11:1（均 ≥4.5）
        选中态 = 白底 #14377A 字（11.31:1）
     ========================================================== */

  /* ===== 全局背景：浅蓝灰（v6 再降一档）=====
     v4「白到近白」、v5「浅冷灰」都还不够 —— 用户反馈"整体背景还是白色"。
     v6 直接压到明显带蓝的浅蓝灰（#EDF3FC→#DAE6F6）：白卡才真正"浮"在底上，
     侧边栏的深蓝也才有对比。底色只负责分区，正文一律落在白卡里，
     所以字号与对比度不受影响（#334155 压在 #DAE6F6 上仍有 8.3:1）。 */
  .stApp {
    background: linear-gradient(180deg, #EDF3FC 0%, #E3EDF9 48%, #DAE6F6 100%);
    background-attachment: fixed;
  }
  header[data-testid="stHeader"] { background: transparent; }
  /* 限宽：卡片是整行白底，超过 ~1300px 会拉成"长条白板"，一行文字也过长难读 */
  .block-container { padding-top: 1.2rem; max-width: 1320px; color: #334155; }

  /* ===== 深色大横幅（.hero）已整体下线 =====
     主页面改用紧凑顶栏 .topbar；登录页改用「全屏沉浸式」深蓝渐变 + 居中白卡
     （相关样式在 LOGIN_CSS，只在未登录分支注入）。 */

  /* ===== 紧凑顶栏 =====
     单行：品牌 + 当前分析对象 + 状态徽章。
     配色收敛：品牌名走深墨色（#0B1F3A），只有 "Agent" 两个字用品牌蓝，
     不再用金色 —— 金字压在浅底上只有 2.25:1，是这个页面里最典型的"看不清"。 */
  .topbar {
    display: flex;
    align-items: center;
    gap: 12px;
    background: #FFFFFF;
    border: 1px solid #E4EAF2;
    border-left: 4px solid #1D4ED8;
    border-radius: 10px;
    padding: 9px 16px;
    margin-bottom: 14px;
    box-shadow: 0 2px 10px rgba(15, 35, 70, 0.05);
  }
  /* v7：顶栏左侧加一颗纯 CSS 六边形徽标（.tb-logo）——
     原来顶栏只有一个 emoji 📊，在深色侧栏旁边显得"没有品牌感、整条空"。
     六边形用 clip-path 画，不用任何图片素材（少一次 IO、少一份资源）。 */
  .tb-logo {
    flex: 0 0 auto;
    width: 26px; height: 29px;
    clip-path: polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%);
    background: linear-gradient(160deg, #38BDF8 0%, #1D4ED8 100%);
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 900; color: #FFFFFF !important;
    box-shadow: 0 2px 10px rgba(29, 78, 216, 0.35);
  }
  .tb-brand { font-size: 17.5px; font-weight: 800; color: #0B1F3A !important; letter-spacing: .5px; }
  .tb-brand b { color: #1D4ED8 !important; }
  .tb-gap { flex: 1 1 auto; }
  .tb-obj { font-size: 13.5px; color: #3B4A5F !important; }
  .tb-obj b { color: #0B1F3A !important; }
  .tb-chip {
    font-size: 12.5px; padding: 2px 11px; border-radius: 999px;
    background: #EEF3FE; color: #1D4ED8 !important;
    border: 1px solid #D8E3FA; white-space: nowrap;
  }
  .tb-chip.warn { background: #FEF3C7; color: #92400E !important; border-color: #FDE68A; }

  /* ===== 卡片容器（st.container(key="kcard-*")）=====
     为什么要卡片：v4 的每个步骤是"一条白底标题条 + 直接摊在页面上的控件"，
     控件之间没有边界，一屏十来个控件读起来依然"到处都是东西"（用户：主界面很乱）。
     v5 给每一步一个**真正的卡片**：白底 + 圆角 + 细边 + 浅投影；
     标题条降级成"卡片内的表头"（去掉底色，只留一条分隔线）→ 不再盒子里套盒子。

     ⚠️ 选择器为什么是 `st-key-` 前缀类名：
        Streamlit 1.57 会给**带 key 的容器**自动加 `st-key-<key>` 类
        （前端 DO()/OO() 从元素 id `$$ID-<hash>-<user_key>` 里还原用户 key），
        这是 1.57 里唯一稳定的容器挂钩点 —— 旧教程里常用的
        stVerticalBlockBorderWrapper 已经在本版本消失（实测 grep 前端 bundle 命中 0），
        照抄旧写法会写出一条永不生效的规则。
  ⚠️ 卡片 key 一律以 `kcard-` 开头，且**任何控件 key 都不得以 kcard- 开头**：
     `[class*=...]` 是子串匹配，否则控件自己也会被刷成一张卡片。
  ⚠️ 多栏网格：`card(..., col=_grid[0])` 会把卡片挂到某一列里
     （内部用 `col.container(key=...)`，等价于 `with col: with st.container(...)`），
     这样**不用给已有代码加缩进**就能参与多栏排布 —— 少缩进 = 少踩坑。 */
  [class*="st-key-kcard-"] {
    background: #FFFFFF;
    border: 1px solid #DCE6F3;
    border-radius: 16px;
    padding: 15px 18px 6px 18px;
    box-shadow: 0 4px 16px rgba(15, 35, 70, 0.06);
    margin-bottom: 14px;
  }

  /* ===== 页面大标题（每个视图顶部一条）=====
     原来是"进来就是一堆编号方块"，没有一句"这是什么页面"。
     现在每个视图先给一个大标题 + 一句说明 + 一条蓝色短横。
     v7：右侧补一块**科技徽记**（同心环 + 六边形 + 迷你折线），
         解决"页面顶部一大片空白、看着太空旷"。 */
  .page-head {
    display: flex; align-items: flex-start; gap: 18px;
    margin: 4px 0 18px 0;
    padding-bottom: 14px;
    border-bottom: 1px solid #E1EAF6;
  }
  .page-head .ph-text { flex: 1 1 auto; min-width: 0; }
  .page-head .ph-deco { flex: 0 0 auto; opacity: .9; margin-top: 2px; }
  .page-head .ph-title {
    font-size: 30px; font-weight: 800; color: #0B1F3A !important;
    letter-spacing: .6px; line-height: 1.28;
  }
  .page-head .ph-title em { color: #1D4ED8 !important; font-style: normal; }
  .page-head .ph-rule {
    display: block; width: 52px; height: 3px; border-radius: 2px;
    background: #1D4ED8; margin: 11px 0 10px 0;
  }
  .page-head .ph-note {
    display: block; font-size: 14px; color: #3B4A5F !important; line-height: 1.9;
  }

  /* ===== 小节标题（多公司对比：1. 对比公司管理 / 2. 开始对比分析）=====
     与卡片内表头（.step-head）刻意拉开一档：小节标题 21px，
     用于"页面级分段"，卡片表头只用于"卡片内分区"。 */
  .sec-head {
    display: flex; align-items: baseline; flex-wrap: wrap; gap: 10px;
    margin: 22px 0 13px 0; padding-left: 12px;
    border-left: 4px solid #1D4ED8;
  }
  .sec-head .sec-num { font-size: 21px; font-weight: 800; color: #1D4ED8 !important; }
  .sec-head .sec-title { font-size: 21px; font-weight: 800; color: #0B1F3A !important; letter-spacing: .3px; }
  .sec-head .sec-note { font-size: 13.5px; color: #3B4A5F !important; }
  .sec-head .sec-tag {
    font-size: 12px; padding: 1px 10px; border-radius: 999px;
    background: #EEF3FE; color: #1D4ED8 !important; border: 1px solid #D8E3FA;
    align-self: center;
  }

  /* ===== 卡片内小标题（紧凑取数面板等窄容器里用）===== */
  .mini-head { font-size: 15px; font-weight: 800; color: #0B1F3A !important; margin: 2px 0 9px 0; }

  /* ===== 取数成功状态块（v10-2 · 用户第 8a 条）=====
     多公司对比页每家公司挤在 1/5 宽的窄卡里，原来的 st.success 小字会折成
     3~4 行、字号被容器压到几乎看不清（"贵州茅台下面那行字看不清"）。
     改成自定义块：公司名单独一行 17.5px 深墨字，状态另起一行 14.5px 深绿字，
     白底浅绿 + 左侧绿色竖条，对比度 8:1 以上，窄卡里也不塌。 */
  .cmp-ok {
    background: linear-gradient(96deg, #E6F8EF 0%, #F3FBFF 100%);
    border: 1px solid #A7E3C4; border-left: 4px solid #16A34A;
    border-radius: 12px; padding: 10px 13px 11px 13px; margin: 8px 0 6px 0;
  }
  .cmp-ok-name {
    font-size: 17.5px; font-weight: 800; color: #0F2419 !important;
    line-height: 1.45; letter-spacing: .3px; word-break: break-all;
  }
  .cmp-ok-sub {
    font-size: 14.5px; font-weight: 600; color: #1F5138 !important;
    margin-top: 5px; line-height: 1.55;
  }
  /* 尚未取数时的占位块：同样用大字号，避免"一行浅灰小字贴在卡底没人看见" */
  .cmp-wait {
    background: #F4F7FB; border: 1px dashed #B9C7DC; border-radius: 12px;
    padding: 10px 13px; margin: 8px 0 6px 0;
  }
  .cmp-wait-t {
    font-size: 15.5px; font-weight: 700; color: #33455F !important; line-height: 1.5;
  }

  /* ===== 统一分区标题（卡片内表头）=====
     编号方块 + 深色短标题 + 灰色小字说明；无色块、只留一条分隔线。
     标题用 #0B1F3A（16.5:1）、说明用 #3B4A5F（9.0:1），
     不再出现"一片中蓝色字挤在一起"。
     v7：编号方块改成**青蓝渐变 + 外发光**（原来是一块死蓝底），
         并把编号从 20px 提到 23px、标题从 15px 提到 17px。 */
  .step-head {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 10px;
    background: transparent;
    border: none;
    border-bottom: 1px solid #EDF2F8;
    border-radius: 0;
    padding: 0 0 11px 0;
    margin: 0 0 15px 0;
  }
  .step-head .sh-num {
    flex: 0 0 auto;
    width: 23px; height: 23px; border-radius: 7px;
    background: linear-gradient(145deg, #38BDF8 0%, #1D4ED8 100%);
    color: #FFFFFF !important;
    font-size: 13px; font-weight: 800;
    display: inline-flex; align-items: center; justify-content: center;
    align-self: center;
    box-shadow: 0 2px 9px rgba(29, 78, 216, 0.32);
  }
  .step-head .sh-title { font-size: 17px; font-weight: 800; color: #0B1F3A !important; letter-spacing: .2px; }
  .step-head .sh-tag {
    font-size: 12px; padding: 1px 9px; border-radius: 999px;
    background: #EEF3FE; color: #1D4ED8 !important; border: 1px solid #D8E3FA;
    align-self: center;
  }
  .step-head .sh-note { font-size: 13px; color: #3B4A5F !important; }

  /* 内容区小标题（h4/h5）：全站统一同一档字号 + 左竖条 + 统一间距
     原来 h4/h5 混用、margin 走 Streamlit 默认值，导致区块之间忽宽忽窄，
     观感上就是"排版乱"。现在一次锁死，全部 markdown 标题自动对齐。 */
  h3 {
    color: #0B1F3A !important;
    font-size: 20px !important;
    font-weight: 800 !important;
    margin: 20px 0 10px 0 !important;
    line-height: 1.45;
  }
  h4, h5 {
    color: #0B1F3A !important;
    font-size: 15.5px !important;
    font-weight: 800 !important;
    margin: 20px 0 9px 0 !important;
    padding-left: 9px;
    border-left: 3px solid #93B4E8;
    letter-spacing: .2px;
    line-height: 1.5;
  }

  /* ===== 侧边栏：导航 + 分析记录 =====
     导航菜单与分析记录都住在侧边栏里（主区原本的「图标列 + 记录列」已取消）。
     v6 侧栏改深蓝后，这里的分组小标题必须走浅色（#E8F0FF 6.8:1），
     分隔线也要换成半透明白线，否则深底上"黑线 + 黑字"整片糊掉。 */
  .hist-head {
    font-size: 14.5px; font-weight: 800; color: #FFFFFF !important;
    margin-top: 9px; padding-top: 11px;
    border-top: 1px solid rgba(255, 255, 255, 0.16);
    letter-spacing: .3px;
  }

  /* ===== 指标卡：白底 + 浅边 ===== */
  div[data-testid="stMetric"] {
    background: #FFFFFF;
    border: 1px solid #E4EAF2;
    border-radius: 12px;
    padding: 13px 16px 8px 16px;
    box-shadow: 0 2px 8px rgba(15, 35, 70, 0.05);
  }
  div[data-testid="stMetric"] label {
    color: #3B4A5F !important;
    font-weight: 600;
  }
  div[data-testid="stMetric"] div {
    color: #0B1F3A !important;
    font-weight: 800;
  }
  /* 原来这里给指标数字做了「蓝→深蓝渐变文字」（background-clip:text）。
     深底浅字时它还算好看，但压在近白底上会掉进"一片蓝字"的坑，
     且渐变中段对比度最低。v4 直接用纯深墨色：16.5:1，最清楚。 */
  div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
    color: #0B1F3A !important;
    -webkit-text-fill-color: #0B1F3A !important;
  }
  div[data-testid="stMetricDelta"] svg { fill: #1D4ED8; }

  /* ===== Expander / 区块卡片化 ===== */
  div[data-testid="stExpander"] {
    background: #FFFFFF;
    border: 1px solid #E4EAF2;
    border-radius: 12px;
    box-shadow: 0 2px 8px rgba(15, 35, 70, 0.05);
    margin-bottom: 4px;
  }
  div[data-testid="stExpander"] details { background: transparent; border: none; }
  div[data-testid="stExpander"] summary {
    font-weight: 700;
    color: #0B1F3A !important;
    padding: 10px 4px;
  }
  div[data-testid="stExpander"] summary * { color: #0B1F3A !important; }
  div[data-testid="stExpander"] summary:hover { color: #1D4ED8 !important; }

  /* ===== 药丸式选择（行业 / 数据来源 / 补充材料） =====
     选中态原来是"蓝底 + 金色文字"——金色压蓝底确实够（4.61:1），但全站到处
     是金蓝撞色，用户反馈"颜色太多太乱"。v4 统一成蓝底白字（6.7:1）。 */
  div[role="radiogroup"] label {
    background: #FFFFFF;
    border: 1px solid #D6E0EC;
    border-radius: 999px;
    padding: 6px 14px;
    margin: 3px 3px;
    transition: all .18s ease;
    color: #334155 !important;
  }
  div[role="radiogroup"] label:hover {
    border-color: #1D4ED8;
    box-shadow: 0 0 10px rgba(29, 78, 216, 0.15);
  }
  div[role="radiogroup"] label:has(input:checked) {
    background: #1D4ED8;
    border-color: #1D4ED8;
    box-shadow: 0 2px 10px rgba(29, 78, 216, 0.30);
  }
  div[role="radiogroup"] label:has(input:checked) div,
  div[role="radiogroup"] label:has(input:checked) div *,
  div[role="radiogroup"] label:has(input:checked) p,
  div[role="radiogroup"] label:has(input:checked) span {
    color: #FFFFFF !important; font-weight: 700;
  }

  /* ===== 按钮：主按钮实心品牌蓝 =====
     原来是「蓝渐变 + 强光晕」，堆叠在一起像一排发光的牌子；
     现在压平成一个实色块，页面立刻安静。 */
  /* 【v14 · 用户反馈「开始结构化分析字看不清」】根因：1.57 前端不会把 kind="primary"
     写进 DOM（本项目 1018 行注释里早就记录过这个坑），于是下面这条白字规则整条失效，
     按钮文字只剩 `.stButton > button { color:#334155 }` 的深灰 —— 深灰字压深蓝底。
     补上 stBaseButton-primary 这个**稳定挂钩**（与侧栏 1019 行同一写法），
     并把按钮内所有子元素（文字在 <p> 里）一并锁白。 */
  .stButton > button[kind="primary"],
  .stButton > [data-testid="stBaseButton-primary"] {
    background: #1D4ED8;
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    font-weight: 700;
    box-shadow: 0 2px 8px rgba(29, 78, 216, 0.24);
  }
  .stButton > button[kind="primary"]:hover,
  .stButton > [data-testid="stBaseButton-primary"]:hover {
    background: #1E40AF;
    color: #FFFFFF;
    box-shadow: 0 3px 12px rgba(29, 78, 216, 0.34);
  }
  /* 主区 primary 按钮的文字（含 emoji 外层 <p>）全部锁白，不再依赖 kind 属性 */
  .stButton > button[kind="primary"] p,
  .stButton > [data-testid="stBaseButton-primary"] p,
  .stButton > [data-testid="stBaseButton-primary"] * {
    color: #FFFFFF !important;
  }
  .stButton > button { color: #334155; }

  /* ===== 标签页 ===== */
  .stTabs [data-baseweb="tab-list"] { gap: 8px; }
  .stTabs [data-baseweb="tab"] {
    background-color: #FFFFFF;
    border-radius: 10px 10px 0 0;
    padding: 8px 20px;
    font-weight: 600;
    color: #475569;
    border: 1px solid #E4EAF2;
  }
  .stTabs [aria-selected="true"] {
    background: #1D4ED8 !important;
    color: #FFFFFF !important;
    border-color: #1D4ED8 !important;
    box-shadow: none;
  }
  /* 页签内容区 = 一块卡片（与下面的白色页签连成一体）：
     结果区原来直接摊在浅灰底上，和输入区的卡片语言不统一。
     用 CSS 给面板补上底色/描边/圆角，**不用**再包一层 st.container
     —— 少一层容器 = 少一处缩进改动，也就少一处隐性 bug。 */
  .stTabs [data-baseweb="tab-panel"] {
    background: #FFFFFF;
    border: 1px solid #E4EAF2;
    border-top: none;
    border-radius: 0 0 12px 12px;
    padding: 16px 18px 10px 18px;
    box-shadow: 0 2px 10px rgba(15, 35, 70, 0.045);
  }

  /* ===== 侧边栏：深蓝渐变（v6 主视觉，v7 提亮文字）=====
     模板（用户手绘稿）里最显眼的一块就是"深蓝导航条 + 白色亮块选中项"：
     深色侧栏一下子把"这是工作台"和"这是内容"分开，比在浅色里描边有效得多。
     深底带来的唯一风险是**文字对比度**，所以下面每一条文字都显式上色并量化
     （以渐变最亮的一端 #16489E 为最坏情况）：
       #EAF4FF 正文 7.69:1 / #AFD2F5 说明 5.44:1 / #7FE9FF 强调 6.11:1
     全部 ≥4.5:1，在 17px 根字号下正文已经是"亮青白"，不会再"看着发灰"。 */
  section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0E2A5E 0%, #123A80 46%, #16489E 100%);
    border-right: none;
    box-shadow: 2px 0 18px rgba(10, 28, 60, 0.16);
  }
  /* 侧栏内部容器一律透明，别把渐变盖住（Streamlit 默认给内层上了主题底色） */
  section[data-testid="stSidebar"] [data-testid="stSidebarContent"],
  section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"],
  section[data-testid="stSidebar"] [data-testid="stSidebarHeader"] {
    background: transparent;
  }
  section[data-testid="stSidebar"] hr {
    border-top: 1px solid rgba(255, 255, 255, 0.16);
  }
  /* 折叠按钮的图标在深底上是深色 → 显式改浅 */
  section[data-testid="stSidebar"] [data-testid="stSidebarCollapseButton"] svg {
    fill: #C7D7F0 !important;
    color: #C7D7F0 !important;
  }
  /* ⚠️ 这里原来是 `section[data-testid="stSidebar"] * { color:#1E3A5F !important }`
     —— 通配符 + !important 的特异性(0,1,1) 高于 `.sb-brand`(0,1,0)，
     会把品牌卡里本该是白色的标题刷成深蓝 #1E3A5F，压在蓝色渐变卡上
     实测对比度只有 1.83:1（截图里几乎看不见）。
     改为只给 Streamlit 原生文本上色，自定义组件各自保留自己的配色。
     ⚠️ **绝不能把 `span` 加进这个列表** —— 选中态导航按钮是"白底深蓝字"，
     一旦通配到 span 就会被刷成浅色，变成白底白字（又一次"字没了"）。 */
  section[data-testid="stSidebar"] p,
  section[data-testid="stSidebar"] li,
  section[data-testid="stSidebar"] label,
  section[data-testid="stSidebar"] small,
  section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
  section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] *,
  section[data-testid="stSidebar"] [data-testid="stWidgetLabel"],
  section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] * {
    color: #EAF4FF !important;
  }
  /* 说明类文字（caption / 额度行）压暗一档，但仍要 ≥4.5:1 */
  section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
  section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] * {
    color: #AFD2F5 !important;
  }
  section[data-testid="stSidebar"] code {
    background: rgba(255, 255, 255, 0.16);
    color: #7FE9FF;
  }
  section[data-testid="stSidebar"] h3 {
    border-left: 3px solid #7FE9FF;
    padding-left: 8px;
    color: #FFFFFF !important;
  }
  /* 导航按钮：未选中 = 半透明白底 + 浅字；选中 = **白底深蓝字**（模板里的亮块）
     左对齐是因为模板里的按钮是"图标 + 文字"从左边起的，居中的话不整齐。 */
  section[data-testid="stSidebar"] .stButton > button {
    background: rgba(255, 255, 255, 0.10);
    border: 1px solid rgba(255, 255, 255, 0.20);
    color: #EAF4FF !important;
    border-radius: 9px;
    font-weight: 600;
    justify-content: flex-start;
    padding-left: 13px;
  }
  section[data-testid="stSidebar"] .stButton > button:hover {
    background: rgba(255, 255, 255, 0.20);
    border-color: #7FE9FF;
    color: #FFFFFF !important;
  }
  /* ---- 选中态导航按钮：**不依赖任何 Streamlit 内部属性** ----
     用户担心"点了之后是不是会变成全白、看不清"。这个担心在 v6 是有现实可能的：
       · v6 用 `button[kind="primary"]` 表达选中 —— 而 1.57 前端的按钮是
         styled-components v6 渲染的，未知属性（kind）**不一定**会写到 DOM 上，
         这条规则可能整条不生效（实测同类选择器在页面里挑不到元素）；
       · 一旦底色规则失效而文字规则生效，就正好是"浅字压深底"或"白底白字"。
     修法：改用**应用自己控制的 key 类名** `st-key-nav_on_*`
     （前端会为带 key 的元素生成 `st-key-<key>` 类，这是 1.57 里唯一稳定的挂钩点）。
     同时把"白底"和"深蓝字"写在**同一条规则组里**，且文字规则带上按钮内所有子元素
     （`button *`）—— 布局与配色同一来源，不存在只生效一半的可能。 */
  section[data-testid="stSidebar"] [class*="st-key-nav_on_"] button {
    background: #FFFFFF !important;
    border: 1px solid #FFFFFF !important;
    border-left: 4px solid #0EA5E9 !important;
    box-shadow: 0 4px 16px rgba(2, 10, 26, 0.34) !important;
    font-weight: 800 !important;
  }
  section[data-testid="stSidebar"] [class*="st-key-nav_on_"] button,
  section[data-testid="stSidebar"] [class*="st-key-nav_on_"] button * {
    color: #14377A !important;   /* 白底上的深蓝字：11.31:1 */
  }
  section[data-testid="stSidebar"] [class*="st-key-nav_on_"] button:hover {
    background: #F4F8FF !important;
    border-left-color: #0284C7 !important;
  }
  /* 兼容兜底：若该版本确实把 kind 写进了 DOM，也一并按同样配色处理
     （与上面同色，不会互相覆盖；且**只在侧栏作用域内**，不影响主区主按钮） */
  section[data-testid="stSidebar"] .stButton > button[kind="primary"],
  section[data-testid="stSidebar"] [data-testid="stBaseButton-primary"] {
    background: #FFFFFF !important;
    border: none !important;
    color: #14377A !important;
    font-weight: 800 !important;
    box-shadow: 0 4px 16px rgba(2, 10, 26, 0.34) !important;
  }
  /* 双模型开关（st.toggle）在深蓝侧栏上原本是"深蓝轨道压深蓝底"，看不出开关状态：
     用 :has() 直接判里面的 switch 选没选中，把整颗开关放进一块可见的小底板里
     （与上面药丸选择器的 `label:has(input:checked)` 是同一套写法）。
     ⚠️ 只挂在侧边栏作用域内，不影响主区的开关。 */
  section[data-testid="stSidebar"] label:has([role="switch"]) {
    background: rgba(255, 255, 255, 0.10);
    border: 1px solid rgba(255, 255, 255, 0.20);
    border-radius: 10px;
    padding: 7px 9px;
  }
  section[data-testid="stSidebar"] label:has([role="switch"]:checked) {
    background: rgba(34, 211, 238, 0.16);
    border-color: rgba(127, 233, 255, 0.60);
  }
  /* 分析记录：深底上单独给一个半透明凹槽，记录多了自己滚动 */
  [class*="st-key-khist-scroll"] {
    background: rgba(0, 0, 0, 0.16);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 12px;
  }
  /* 侧边栏品牌头：保留**白底卡**（模板里左上角就是一块白卡压深蓝栏），
     左侧竖条换成青色提亮 —— 深蓝底上的白卡本身就是最强的视觉锚点。
     对比度沿用白底深字：标题 #0B1F3A 16.5:1 / 副说明 #475569 7.58:1。 */
  .sb-brand {
    background: #FFFFFF;
    border: 1px solid #E4EAF2;
    border-left: 4px solid #22D3EE;
    border-radius: 10px;
    padding: 12px 14px;
    font-weight: 800;
    font-size: 16.5px;
    letter-spacing: 0.3px;
    box-shadow: 0 4px 16px rgba(4, 16, 40, 0.22);
  }
  /* 深色文字同时作用于子元素 —— 只写 .sb-brand 时，任何作用到子元素的
     通配符规则都会把文字压成别的颜色（v3 事故的根因），故在此加固。
     一律走深墨色：即使被全局规则改写，落在白底上依然清晰可读。 */
  .sb-brand, .sb-brand * { color: #0B1F3A !important; }
  .sb-brand .sb-sub, .sb-brand .sb-sub * {
    display: block;
    color: #475569 !important;
    font-size: 12px;
    font-weight: 600;
    margin-top: 5px;
    letter-spacing: 0.2px;
  }
  /* 侧边栏内容卡片（安全提醒）：同样走白底卡 + 深字（深蓝栏上的白块） */
  .sb-card {
    background: #FFFFFF;
    border: 1px solid #E4EAF2;
    border-radius: 10px;
    padding: 11px 13px;
    margin: 8px 0;
    box-shadow: 0 4px 16px rgba(4, 16, 40, 0.18);
  }
  /* ⚠️ `.sb-title` 有两个落点，必须分开处理：
       ① 直接压在深蓝栏上的小标题（🧭 工作台导航 / 🤖 双模型协同）→ 浅色
       ② 白卡里的小标题（⚠️ 安全提醒）→ 深色（且该处有 inline !important 兜底）
     把它写成同一个颜色，就一定会有一处看不清 —— 这正是 v3 那次翻车的模式。 */
  .sb-title {
    color: #EAF4FF !important;
    font-weight: 700;
    font-size: 14.5px;
    margin-bottom: 7px;
    letter-spacing: 0.3px;
  }
  .sb-card .sb-title {
    color: #0B1F3A !important;
  }
  .sb-card ol, .sb-card ul {
    margin: 0;
    padding-left: 18px;
    color: #334155 !important;
    font-size: 13px;
    line-height: 1.85;
  }
  .sb-chip {
    display: inline-block;
    background: rgba(255, 255, 255, 0.16);
    border: 1px solid rgba(255, 255, 255, 0.24);
    color: #7FE9FF !important;
    border-radius: 999px;
    padding: 1px 10px;
    font-size: 12px;
    margin: 2px 2px;
    white-space: nowrap;
  }
  .sb-quota-head { font-size: 13px; margin-bottom: 5px; color: #AFD2F5 !important; }
  .sb-quota-head b { color: #FBBF24 !important; }
  .sb-quota-bar {
    height: 7px;
    background: rgba(255, 255, 255, 0.20);
    border-radius: 999px;
    overflow: hidden;
  }
  .sb-quota-bar div { height: 100%; border-radius: 999px; }
  /* 侧栏内的页脚（主区也有 .footer-note，但那边是浅底深字，故只做作用域覆盖） */
  section[data-testid="stSidebar"] .footer-note { color: #AFD2F5 !important; }

  /* ===== 侧栏底部科技装饰（v7 新增，纯 CSS）=====
     侧栏下半部分原来是"一片纯深蓝"，用户反馈太空旷。
     这里用三圈同心圆 + 一圈虚线刻度环 + 一条渐变扫描线做底纹，
     全部 `pointer-events:none` 且透明度 ≤0.2，绝不干扰点击与阅读。
     放在侧栏**最底部**（在代码里最后注入），且 absolute 定位不占布局高度。 */
  .sb-deco {
    position: relative; height: 96px; margin: 10px 0 -6px 0;
    pointer-events: none; overflow: hidden; opacity: .9;
  }
  .sb-deco i {
    position: absolute; left: 50%; bottom: -46px; transform: translateX(-50%);
    border-radius: 50%; border: 1px solid rgba(127, 233, 255, 0.22);
  }
  .sb-deco i:nth-child(1) { width: 190px; height: 190px; }
  .sb-deco i:nth-child(2) { width: 132px; height: 132px; border-style: dashed;
                            border-color: rgba(127, 233, 255, 0.28); }
  .sb-deco i:nth-child(3) { width: 74px; height: 74px; border-color: rgba(255, 255, 255, 0.26); }
  .sb-deco b {
    position: absolute; left: 50%; bottom: 0; transform: translateX(-50%);
    width: 150px; height: 1px;
    background: linear-gradient(90deg, transparent, rgba(127, 233, 255, 0.55), transparent);
  }

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
    background-color: rgba(29, 78, 216, 0.07);
    border-left: 4px solid #1D4ED8;
    color: #0B1F3A !important;
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
    color: #3B4A5F;
    font-size: 13px;
    padding: 8px;
  }
  .footer-note b { color: #B45309; }

  /* ===== 双模型徽章 ===== */
  /* 渐变右端原来是 #93B5E0（浅蓝），白字压上去只有 2.2:1 看不清；
     改为深蓝渐变右端 #2563EB，白字对比度 5.2:1 */
  .dual-badge {
    display: inline-block;
    background: linear-gradient(90deg, #1E3A8A 0%, #2563EB 100%);
    color: #FFFFFF !important;
    padding: 4px 13px;
    border-radius: 999px;
    font-size: 13px;
    font-weight: 700;
    margin: 6px 0;
    box-shadow: 0 4px 14px rgba(30, 64, 175, 0.40);
  }

  /* ===== 区块小标题：统一深墨色 =====
     原来 h4/h5 混用 + 中蓝色 #1E40AF，一屏里出现七八种蓝，
     用户反馈"一片蓝字看不清"。现在标题只有一档深墨色。
     ⚠️ 这里**不要再写 font-weight / font-size** —— 上面已经按档位锁死，
        在下面重复声明只会把上面的 800 覆盖成 700（同特异性、后写的赢）。 */
  h3, h4, h5 { color: #0B1F3A !important; }
  h3, h4 { letter-spacing: 0.3px; }
  p, li, span, div { color: #334155; }
  /* v7：说明类文字从 #475569 提到 #3B4A5F ——
     #475569 压在浅蓝灰画布 #DAE6F6 上只有 6.0:1，字号小的时候明显"发灰看不清"；
     #3B4A5F 是 7.14:1，白卡上是 9.0:1，两级都稳。 */
  small, .stCaption, [data-testid="stCaptionContainer"],
  [data-testid="stCaptionContainer"] * { color: #3B4A5F !important; }
  code { background: #EEF3FE; color: #1D4ED8; padding: 1px 6px; border-radius: 4px; }

  /* ===== 空状态插图（v7 新增）=====
     未取数时结果区只有两行小字，正是用户说的"太空旷"。
     这里给一块科技风占位：同心环 + 六边形 + 折线 + "待取数"提示。
     纯内联 SVG，无图片素材；深色描边压浅底，对比度足够但不抢视线。 */
  .empty-art {
    text-align: center; padding: 26px 12px 22px 12px;
    border: 1px dashed #C9DAF0; border-radius: 14px;
    background: linear-gradient(180deg, #FBFDFF 0%, #F3F8FE 100%);
    margin: 6px 0 4px 0;
  }
  .empty-art svg { display: block; margin: 0 auto 12px auto; }
  .empty-art .ea-title { font-size: 16px; font-weight: 800; color: #0B1F3A !important; }
  .empty-art .ea-note {
    font-size: 13.5px; color: #3B4A5F !important; margin-top: 6px; line-height: 1.85;
  }
  a { color: #1D4ED8 !important; }

  /* ===== RAG 检索结果卡 ===== */
  .rag-hit {
    background: #FFFFFF;
    border-left: 3px solid #1D4ED8;
    border-top: 1px solid #E4EAF2;
    border-right: 1px solid #E4EAF2;
    border-bottom: 1px solid #E4EAF2;
    border-radius: 8px;
    padding: 10px 12px;
    margin: 6px 0;
    color: #334155;
    font-size: 13.5px;
    line-height: 1.75;
  }
  .rag-hit .rag-rank {
    color: #1D4ED8;
    font-weight: 700;
    margin-right: 8px;
  }
  .rag-hit .rag-page {
    color: #0B1F3A;
    font-weight: 700;
  }
  .rag-hit .rag-sim-high { color: #15803D; font-weight: 700; }
  .rag-hit .rag-sim-mid { color: #B45309; font-weight: 700; }
  /* v7：低相似度原来是 #64748B —— 压在白卡上只有 4.76:1（勉强），
     压到浅蓝灰画布上只剩 3.77:1（不合格，就是用户说的"看不清"）。
     统一收到 #475569（白卡 7.58:1 / 画布 6.0:1）。 */
  .rag-hit .rag-sim-low { color: #475569; font-weight: 700; }

  /* ===== 图表解读卡 ===== */
  .insight-card {
    background: #F8FAFC;
    border: 1px solid #E4EAF2;
    border-left: 3px solid #93B4E8;
    border-radius: 10px;
    padding: 11px 15px;
    margin: 8px 0 14px 0;
    color: #334155;
    font-size: 14px;
    line-height: 1.8;
  }
  .insight-card b { color: #0B1F3A; }
  .insight-card .up { color: #DC2626; font-weight: 700; }
  .insight-card .down { color: #16A34A; font-weight: 700; }
  .insight-card .neutral { color: #475569; }

  /* ===== 分割线 ===== */
  hr { border: none; border-top: 1px solid #E4EAF2; }

  /* ===== 散点图四象限说明卡 ===== */
  .quad-note {
    background: #FFFFFF;
    border: 1px solid #E4EAF2;
    border-radius: 10px;
    padding: 11px 15px;
    margin: 6px 0 12px 0;
    font-size: 14.5px;
    line-height: 1.9;
    color: #334155;
  }
  /* v10-2（用户第 6 条）：象限名中英文并列 —— 只写「■ 明星」时，
     评审不知道 ■ 是什么、明星又对应图里哪个象限。*/
  .quad-note .q-star,
  .quad-note .q-cash,
  .quad-note .q-quest,
  .quad-note .q-dog { font-weight: 800; font-size: 15px; }
  .quad-note .q-star { color: #DC2626; }
  .quad-note .q-cash { color: #B45309; }
  .quad-note .q-quest { color: #1D4ED8; }
  .quad-note .q-dog { color: #475569; }
  .quad-note .q-en { font-size: 12.5px; font-weight: 600; color: #475569 !important; }

  /* ===== 同业对标基准条（行业选择器下方） =====
     说明: 只用类选择器, 不用通配符 + !important
     (通配符特异性 (0,1,1) 会压掉组件自身配色, 见 MEMORY 设计约定 16) */
  .base-bar {
    background: #F8FAFC;
    border: 1px solid #E4EAF2;
    border-left: 3px solid #1D4ED8;
    border-radius: 8px;
    padding: 10px 15px;
    margin: 4px 0 12px 0;
    font-size: 14px;
    line-height: 1.8;
    color: #334155;
    text-align: left;
  }
  .base-bar b { color: #0B1F3A; }
  .base-bar .bb-num { color: #B45309; font-weight: 700; }
  .base-bar .bb-note { color: #3B4A5F; }

  /* ===== v10 全局补丁 ===== */

  /* 1) 页头那排纯图形按钮（Share / 星标 / 链接 / 白方块）是 Streamlit 自带工具栏，
        没有文字说明、颜色和深蓝背景融在一起，用户看不懂也不需要。
        参赛作品用不上"分享/部署"入口，整排隐藏最干净。 */
  header[data-testid="stHeader"] [data-testid="stToolbar"],
  header[data-testid="stHeader"] [data-testid="stStatusWidget"],
  header[data-testid="stHeader"] [data-testid="stLogo"],
  header[data-testid="stHeader"] [data-testid="stDecorationContainer"] {
    display: none !important;
  }

  /* 2) 表单输入框右下的 "Press Enter to submit form" 是英文系统提示，
        登录页出现了两次，用户明确要求去掉。 */
  [data-testid="InputInstructions"] { display: none !important; }

  /* 3) 侧边栏「分析记录」：时间 / 行业 / 评分这些 caption 在深蓝底上
        默认是暗灰字，实测几乎看不清（用户截图点名）。统一提亮 + 加大。 */
  [data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {
    color: #C9DAF6 !important;
    font-size: 14.5px !important;
  }
  .st-key-khist-scroll [data-testid="stCaptionContainer"] p {
    color: #DCE8FB !important;
    font-size: 15px !important;
  }

  /* 4) v11（用户阶段三第 ② 条）：历史按钮**悬停**时出现的说明浮层
        （Streamlit 的 help tooltip）默认是主题蓝字，压在深蓝侧栏上看不清；
        按钮本身 hover 时文字也会被主题染成蓝紫。统一改白 + 加粗。 */
  /* 历史记录按钮未悬停时也提亮一档（默认 secondary 按钮字偏灰）。
     ⚠️ 必须写在 hover 规则**之前**：两条特异性相同，后者胜 ——
     顺序反了 hover 的纯白会被这条 #EAF2FF 盖掉。 */
  [data-testid="stSidebar"] [class*="st-key-khist-scroll"] button p {
    color: #EAF2FF !important;
    font-weight: 600;
  }
  [data-testid="stSidebar"] button:hover p,
  [data-testid="stSidebar"] button:focus p {
    color: #FFFFFF !important;
    font-weight: 700;
  }
  [data-testid="stSidebar"] [data-testid="stTooltipContent"],
  [data-testid="stSidebar"] [data-testid="stTooltipContent"] *,
  [data-testid="stSidebar"] [data-testid="stTooltip"] * {
    color: #FFFFFF !important;
    font-size: 14.5px !important;
    font-weight: 600;
  }
</style>
"""

# ============ 页面与登录 ============
# ⚠️ initial_sidebar_state 必须是 expanded：导航菜单与分析记录都住在侧边栏里，
#    默认折叠会让用户进来看到一片空白的主区（找不到切视图的入口）。
st.set_page_config(
    page_title="金融智能投研 Agent",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------
# 登录页（v6：深蓝科技版，按用户给的登录页模板）
#
# ⚠️ v4 曾经**故意放弃**深底方案，理由是：深底要求"每一个文字元素都显式上色且带
#    !important"，漏一个就会掉进全站的 `p,li,span,div{color:#334155}` 变成深字压深底
#    （1.0:1），用户看到的就是"深蓝底上一片看不清的字"。
#    v6 用户明确要求换回深蓝模板 —— 那就把这条风险**逐条堵死**：
#      · 登录页的每一类文字（标题/副标题/说明/标签/占位符/列表/页脚）都单列规则
#      · 每条规则都用 !important，并在注释里写上算好的对比度
#      · 深底是最深的一档（#061428→#0F3272），最亮端 L=0.036，
#        白 12.2:1 / #EAF2FF 11.3:1 / #A9C0E6 6.6:1 / #8FA9D4 5.1:1 / 青 #67E8F9 8.4:1
#        青色按钮上用深字 #052438：8.8:1（青底最亮端）
#
# 表单结构沿用上一轮用户的要求：
#   · 「账号 / 密码」作为**外置标签**（14px 加粗浅色），不塞进框里
#   · 框内放**小号英文**占位符 Account / Password（12px），比外标签小一档
#
# ⚠️ LOGIN_CSS 只在"未登录"分支注入（见 check_login）：登录成功后这段 <style>
#    不再输出，深蓝背景与深色卡片自动消失，不需要任何反向恢复代码。
# ---------------------------------------------------------------
LOGIN_CSS = """
<style>
  /* 登录背景：深蓝科技渐变 + 两处极淡的青蓝辉光（模拟模板里的光雾）。
     辉光透明度控制在 0.16 以内，保证最亮处的文字仍 ≥4.5:1。
     ⚠️ 必须同时压 .stApp / stAppViewContainer：CUSTOM_CSS 里给 .stApp 上的是
        浅蓝灰渐变（!important 级别），只压内层会从边缘漏出浅色。 */
  .stApp,
  [data-testid="stApp"],
  [data-testid="stAppViewContainer"] {
    background:
      radial-gradient(circle at 20% 26%, rgba(34, 211, 238, 0.16), transparent 42%),
      radial-gradient(circle at 78% 74%, rgba(56, 189, 248, 0.13), transparent 46%),
      linear-gradient(160deg, #061428 0%, #0A2450 46%, #0F3272 100%) !important;
    background-attachment: fixed !important;
  }
  [data-testid="stMain"], [data-testid="stMainBlockContainer"] { background: transparent !important; }
  /* ⚠️ v7 关键：把主内容抬到装饰层之上。
     下面新增的 `.lb-deco` 是 fixed 定位的装饰层 —— fixed 元素默认会**盖在**
     普通静态内容上面（定位元素优先绘制）。虽然它带 pointer-events:none 不会
     挡点击，但网格线/光环仍会压到文字上。所以这里给主内容一个更高的层叠级别，
     让"装饰永远在文字下面"，一劳永逸。 */
  [data-testid="stMainBlockContainer"] { position: relative; z-index: 3; }
  /* v8：登录页要"**横向**变大，不要竖着变大"（用户原话）。
     病根不在字号 —— 是全站 `.block-container` 上限 1320px 对登录页太窄，
     说明文字反复折行（"…CSV 导 / 出"、"…不构成投资建 / 议"），
     卡片就长成又高又瘦的一条。登录页单独放宽到 1560px + 中列加宽，
     文案改成横向铺开 → 视觉变宽，行数变少，而不是整体拔高。
     ⚠️ 这段 CSS 只在未登录分支注入，工作台宽度不受影响。 */
  .block-container { max-width: 1800px !important; }
  header[data-testid="stHeader"] { background: transparent !important; }

  /* ---- v9 修正：右上角工具栏（Share / 星标 / 菜单）终于看得清了 ----
     ⚠️ 原来那条规则写的是 [data-testid=stAppToolbar] svg —— **选择器是错的**：
     在 1.57 里 stAppHeader / stAppToolbar 是 **className**，
     真正的 testid 是 stHeader / stToolbar（已在前端包里逐个核对过）：
         <header className="stAppHeader" data-testid="stHeader">
           <div className="stAppToolbar" data-testid="stToolbar">
             … <div className="stToolbarActionButton" data-testid="stToolbarActionButton">
                   <span data-testid="stToolbarActionButtonLabel">Share</span>
     （上面这段注释刻意用不带引号的写法，免得把字面量写进源码、污染回归里的
      `SRC` 断言 —— 这是本仓库踩过的老坑。）
     所以那条规则**从来没生效过** → 深色图标压在深蓝底上，
     用户反馈"share 什么什么那些根本就看不清"。改成按真实 testid 命中，
     并且顺带把字号提上去、加一层浅色药丸底（深底上白字才立得住）。
     ⚠️ 主菜单展开后的下拉是**浅色弹层**，绝不能把里面的字也刷浅（白底白字）
     → 下面专门写了"弹层还原成深字"的一组规则，放在最后覆盖。 */
  [data-testid="stToolbar"], .stAppToolbar { color: #EAF2FF !important; }
  [data-testid="stToolbar"] button,
  [data-testid="stToolbar"] button *,
  [data-testid="stToolbar"] a,
  [data-testid="stToolbar"] a * {
    color: #EAF2FF !important;
    font-size: 15.5px !important;
    font-weight: 700 !important;
    letter-spacing: .3px;
  }
  [data-testid="stToolbar"] svg,
  [data-testid="stToolbar"] svg * { fill: #EAF2FF !important; color: #EAF2FF !important; }
  /* Share / 星标 / 菜单触发器：浅色药丸底（半透明白 + 青边），深底上不糊 */
  [data-testid="stToolbarActionButton"] button,
  [data-testid="stAppDeployButton"] button,
  [data-testid="stMainMenuButton"],
  [data-testid="stMainMenu"] button {
    background: rgba(12, 34, 70, 0.62) !important;
    border: 1px solid rgba(150, 190, 240, 0.42) !important;
    border-radius: 999px !important;
    padding: 6px 14px !important;
  }
  [data-testid="stToolbarActionButton"] button:hover,
  [data-testid="stAppDeployButton"] button:hover,
  [data-testid="stMainMenuButton"]:hover,
  [data-testid="stMainMenu"] button:hover {
    background: rgba(34, 211, 238, 0.22) !important;
    border-color: rgba(103, 232, 249, 0.75) !important;
  }
  [data-testid="stStatusWidget"], [data-testid="stStatusWidget"] * {
    color: #EAF2FF !important; font-size: 15px !important;
  }
  /* ⚠️ 把"工具栏"的浅色规则**收回**到弹层里：主菜单下拉是**浅底弹层**
     （真 testid：stMainMenuPopover / stMainMenuList / stMainMenuItem-*），
     里面的字必须回到深色，否则就是我们自己造出来的"白底白字"。
     ⚠️ 这条必须放在上面那些浅色规则**之后**（同优先级时后者胜）。 */
  [data-testid="stMainMenuPopover"],
  [data-testid="stMainMenuList"],
  [data-baseweb="popover"] [data-baseweb="menu"],
  [data-baseweb="popover"] ul { background: #FFFFFF !important; }
  [data-testid^="stMainMenuItem"],
  [data-testid="stMainMenuItemLabel"],
  [data-testid="stMainMenuList"] *,
  [data-baseweb="popover"] [role="menuitem"],
  [data-baseweb="popover"] [role="menuitemradio"],
  [data-baseweb="popover"] [role="menuitemcheckbox"],
  [data-baseweb="popover"] [role="menuitem"] *,
  [data-baseweb="popover"] [role="menuitemradio"] *,
  [data-baseweb="popover"] [role="menuitemcheckbox"] * {
    color: #0B1F3A !important; fill: #334155 !important;
    font-size: 15px !important; font-weight: 600 !important;
  }
  /* 登录前不显示侧边栏：导航 / 额度 / 退出登录都不该出现 */
  section[data-testid="stSidebar"] { display: none !important; }

  /* 登录卡 = 表单本体（st.container(border=True) 在 1.57 没有稳定 testid 可挂钩）
     —— 深色玻璃卡：半透明深底 + 青色描边 + 内高光。
     v9：与两侧面板换成**同一套玻璃材质**（底色 / 描边 / 圆角 / 投影 / 毛玻璃一致），
     三块东西才像"同一个控制台里的三件仪表"，而不是三张各画各的贴纸 ——
     用户原话"整体都变大，并且结合在一起、融合在一起"。 */
  [data-testid="stForm"] {
    background: rgba(9, 24, 52, 0.72) !important;
    border: 1px solid rgba(103, 232, 249, 0.30) !important;
    border-top: 4px solid #22D3EE !important;
    border-radius: 18px !important;
    padding: 30px 34px 26px 34px !important;
    box-shadow: 0 20px 48px rgba(2, 10, 26, 0.50),
                inset 0 1px 0 rgba(255, 255, 255, 0.07) !important;
    backdrop-filter: blur(8px);
  }

  /* 外置标签：账号 / 密码 —— 浅色加粗（比框内占位符大一档） */
  [data-testid="stForm"] [data-testid="stWidgetLabel"] p,
  [data-testid="stForm"] [data-testid="stWidgetLabel"] label,
  [data-testid="stForm"] label[data-testid="stWidgetLabel"] p {
    color: #EAF2FF !important;
    font-size: 18.5px !important;
    font-weight: 700 !important;
    letter-spacing: .5px;
  }

  /* 输入框：**深色实底** + 亮描边；框内是「小号英文」提示。
     v11（用户阶段三第 ① 条）：原来是 rgba(255,255,255,.07) 半透明白底，
     压在浅色极光/玻璃卡上等于一片白 —— 白字打上去"全是白色的根本看不清"，
     尤其点击输入（focus）后浏览器还会把底色提亮一档。改成深墨蓝实底：
     白字压深底对比度 12:1 以上，聚焦、输入中、选中文本全都清楚。 */
  [data-testid="stForm"] input {
    background: #06142E !important;
    border: 1px solid rgba(150, 190, 240, 0.45) !important;
    border-radius: 10px !important;
    color: #FFFFFF !important;
    font-size: 18.5px !important;
    padding: 13px 17px !important;
    caret-color: #67E8F9 !important;   /* 光标也换亮青色，白色光标在深底上才看得见 */
  }
  /* Chrome/Edge 的账号密码自动填充会强制白底黄字 —— 用 inset 阴影盖回深底白字 */
  [data-testid="stForm"] input:-webkit-autofill,
  [data-testid="stForm"] input:-webkit-autofill:hover,
  [data-testid="stForm"] input:-webkit-autofill:focus {
    -webkit-box-shadow: 0 0 0 1000px #06142E inset !important;
    -webkit-text-fill-color: #FFFFFF !important;
    caret-color: #67E8F9 !important;
    transition: background-color 99999s;
  }
  /* 选中文本高亮：默认浅蓝底+白字几乎看不见，改成青底深字 */
  [data-testid="stForm"] input::selection {
    background: rgba(34, 211, 238, 0.55) !important;
    color: #04101F !important;
  }
  [data-testid="stForm"] input:focus {
    border-color: #22D3EE !important;
    box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.18) !important;
  }
  [data-testid="stForm"] input::placeholder {
    color: #A9C0E6 !important;
    font-size: 15px !important;
    letter-spacing: .3px;
  }
  /* 密码框的「显示/隐藏」眼睛图标 */
  [data-testid="stForm"] [data-testid="stTextInputRootElement"] button svg {
    fill: #A9C0E6 !important; color: #A9C0E6 !important;
  }

  /* 登录按钮：青色渐变 + 深蓝字（模板里的「安全登录」就是这个形态） */
  [data-testid="stFormSubmitButton"] button {
    background: linear-gradient(90deg, #22D3EE 0%, #38BDF8 100%) !important;
    color: #052438 !important;
    border: none !important;
    border-radius: 11px !important;
    padding: 15px 20px !important;
    font-size: 21px !important;
    font-weight: 800 !important;
    letter-spacing: 1.8px;
    box-shadow: 0 8px 22px rgba(34, 211, 238, 0.30) !important;
  }
  [data-testid="stFormSubmitButton"] button:hover {
    background: linear-gradient(90deg, #67E8F9 0%, #7DD3FC 100%) !important;
    color: #04202F !important;
  }

  /* 卡内文字统一浅色（深底上必然可读） */
  [data-testid="stForm"] p,
  [data-testid="stForm"] li,
  [data-testid="stForm"] small { color: #CBDCF6 !important; }
  [data-testid="stForm"] .stMarkdown * { color: #CBDCF6; }
  /* 卡内报错（账号或密码错误）也要看得见：浅底红字在深色卡上完全糊掉 */
  [data-testid="stForm"] [data-testid="stAlert"] {
    background: rgba(220, 38, 38, 0.18) !important;
    border-left: 4px solid #F87171 !important;
  }
  [data-testid="stForm"] [data-testid="stAlert"] * { color: #FECACA !important; }

  /* ---- 登录页品牌区（深底浅字，全部用类选择器 + !important） ---- */
  /* 中央「AI 六边形徽标」：CSS 画的发光六边形，对应模板里那颗 AI 球。
     v7：外面再套两圈旋转光环（.lb-ring），把"中间一个孤零零的六边形"变成
     "一颗悬浮在环形轨道上的核心"，是整页最省力也最见效的科技感来源。 */
  .login-brand .lb-orb {
    width: 118px; height: 133px; margin: 0 auto 22px auto;
    clip-path: polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%);
    background: linear-gradient(160deg, #22D3EE 0%, #3B82F6 100%);
    box-shadow: 0 0 46px rgba(34, 211, 238, 0.55);
    display: flex; align-items: center; justify-content: center;
    font-size: 40px; font-weight: 900; letter-spacing: 1px;
    color: #052438 !important;
  }
  .login-brand .lb-badge {
    display: inline-block; font-size: 16.5px; letter-spacing: 2.4px;
    color: #7FE9FF !important; background: rgba(34, 211, 238, 0.10);
    border: 1px solid rgba(103, 232, 249, 0.42); border-radius: 999px;
    padding: 7px 24px; margin-bottom: 18px;
  }
  /* ⚠️ 这里刻意用 <div> 而不是 <h1>：Streamlit 会给 markdown 标题自动塞一个
     深色的「跳转到本节」锚点图标，压在任何底上都是一块脏点；
     用 div 就完全不会生成锚点，也顺带躲开了全站 h3/h4/h5 的样式覆盖。 */
  .login-brand .lb-title {
    color: #FFFFFF !important; font-size: 48px; font-weight: 800;
    margin: 0 0 12px 0; letter-spacing: 2.4px;
    text-shadow: 0 2px 20px rgba(34, 211, 238, 0.30);
  }
  .login-brand .lb-title em { color: #7FE9FF !important; font-style: normal; }
  .login-brand .lb-sub { color: #CBDCF6 !important; font-size: 21px; margin: 0; letter-spacing: .5px; }
  .login-brand .lb-note { color: #A9C0E6 !important; font-size: 17.5px; margin-top: 11px; letter-spacing: .3px; }
  .login-foot { text-align: center; font-size: 17px; color: #A9C0E6 !important; margin: 20px 0 0 0; letter-spacing: .2px; }

  /* ==========================================================
     v7 新增：登录页科技装饰层（用户："不要只给我一个登录的账号，
     里面有很多图形，给我放一点在上面，要不然给人的感觉太空旷"）
     全部纯 CSS，没有任何图片素材；
       ① .lb-deco  —— fixed 全屏装饰层（pointer-events:none，绝不挡点击）
       ② .lb-ring  —— 品牌区背后的两圈光环（虚线刻度环 + 实线内环）
       ③ .lb-scan  —— 缓慢旋转的锥形扫描光（conic-gradient）
       ④ .lb-grid    —— 底部透视网格（渐隐遮罩，像"地面"）
       ⑤ .lb-halo    —— 中央一枚大范围柔和辉光（把左/中/右"焊"成同一块控制台）
       ⑥ .lb-side-col —— 左右两侧的数据面板（v9 起住进**真实列**，不再绝对定位）
     ⚠️ 这些元素都压在主内容**下面**（主内容 z-index:3，见文件上方），
        所以即使重叠也不会糊住文字；< 1280px 时两侧整列隐藏。

     v8 修复（用户："给我放这两个上去是什么意思？代码有点让人感觉不舒服"）：
        v7 把这段装饰 HTML 写成了**带缩进、中间夹空行的多行字符串**，
        markdown-it 遇到空行就结束 HTML 块，剩下的行按 markdown 解析；
        而 Streamlit 会对整段做"去公共缩进"，剩下的行正好还剩 4 空格缩进
        → 触发 markdown 的**缩进代码块**，登录页上直接打印出一大坨
        `div.lb-panel` / `rect` / `circle` 源码（6 块面板只有 2 块渲染成功，
        另外 4 块变成了代码块）。
      → 结论：**注入 HTML 一律写成"一整行、无缩进、无空行"的字符串**
        （见文件下方的 LOGIN_DECO / LOGIN_BRAND 常量），
        这样整段就是一个 HTML 块，永远不会掉进代码块分支。 */
  .lb-deco { position: fixed; inset: 0; overflow: hidden; pointer-events: none; z-index: 0; }
  .lb-ring {
    position: absolute; left: 50%; top: 5vh; transform: translateX(-50%);
    width: 420px; height: 420px; border-radius: 50%;
    border: 1px dashed rgba(103, 232, 249, 0.20);
  }
  .lb-ring::after {
    content: ""; position: absolute; inset: 38px; border-radius: 50%;
    border: 1px solid rgba(56, 189, 248, 0.16);
  }
  .lb-scan {
    position: absolute; left: 50%; top: 0; width: 760px; height: 760px;
    margin-left: -380px; border-radius: 50%;
    background: conic-gradient(from 0deg, rgba(34, 211, 238, 0.09), transparent 26%);
    animation: lb-spin 18s linear infinite;
  }
  @keyframes lb-spin { to { transform: rotate(360deg); } }
  .lb-grid {
    position: absolute; left: 0; right: 0; bottom: 0; height: 24vh;
    background-image:
      linear-gradient(rgba(103, 232, 249, 0.14) 1px, transparent 1px),
      linear-gradient(90deg, rgba(103, 232, 249, 0.14) 1px, transparent 1px);
    background-size: 48px 48px;
    -webkit-mask-image: linear-gradient(to top, rgba(0,0,0,.9), transparent);
    mask-image: linear-gradient(to top, rgba(0,0,0,.9), transparent);
  }
  /* ---- ⑤ v9：两侧数据面板不再"绝对定位浮在大屏两边"，而是**住进真实的 st.columns**
     （左列 / 登录卡 / 右列 三列同排）。这样它们和登录卡是同一个网格里的兄弟节点：
     宽度由布局决定（不会重叠、更不会压住输入框），并且三块东西同材质 → 视觉上"融合"。
     用户原话："左边也有图形内容…右边也有图形和内容…但是还是太小了，能不能再大一点？
     整体都变大，并且结合在一起、融合在一起"。 */
  .lb-halo {
    position: absolute; left: 50%; top: 46%;
    width: min(1560px, 96vw); height: min(780px, 76vh);
    transform: translate(-50%, -50%); border-radius: 50%;
    background: radial-gradient(ellipse at 50% 50%,
      rgba(34, 211, 238, 0.12), rgba(34, 211, 238, 0.045) 46%, transparent 74%);
  }
  .lb-side-col { display: flex; flex-direction: column; gap: 20px; }
  /* 面板组在列里竖直居中：st.columns 会把列拉到与登录卡等高，
     面板少一半高度也不至于"上重下轻"。
     ⚠️ 这里的 testid 是**逐个核过的**（1.57 前端包）：
        · `stColumn` 确实是 data-testid（别被 `goToFirstColumn` 那种字符串骗了，
          直接搜 "stColumn" 会命中 DataFrame 的键盘快捷键名，是假阳性）
        · `stVerticalBlock` 是**动态拼**出来的：`data-testid: SO(border_type)`，
          函数体 `e===HORIZONTAL ? 'stHorizontalBlock' : 'stVerticalBlock'`，
          所以字面量搜不到，但属性确实存在
        · `stAppHeader` / `stAppToolbar` 则是 **className**（不是 testid）。 */
  [data-testid="stColumn"]:has(.lb-side-col) { display: flex; flex-direction: column; }
  [data-testid="stColumn"]:has(.lb-side-col) > [data-testid="stVerticalBlock"] {
    flex: 1 1 auto; justify-content: center;
  }
  .lb-panel {
    width: 100%;
    background: rgba(9, 24, 52, 0.72);            /* 与登录卡同一套玻璃材质 */
    border: 1px solid rgba(103, 232, 249, 0.30);
    border-top: 4px solid rgba(103, 232, 249, 0.62);
    border-radius: 18px;
    padding: 19px 22px 20px 22px;
    box-shadow: 0 20px 48px rgba(2, 10, 26, 0.50),
                inset 0 1px 0 rgba(255, 255, 255, 0.07);
    backdrop-filter: blur(8px);
  }
  /* v10-2（用户第 6 条）：原 lp-head 是 #7FE9FF 青字压在半透明深蓝玻璃上，
     背景极光一动，青字和青色光晕直接融成一片（"字体颜色与背景融为一体"）。
     现在：纯白 22px/800 字重 + 深色文字底片 + 左侧青色竖条 + 投影，
     任何背景亮度下都读得清；右侧再挂一行英文小字，图形看不懂也能靠文字认。 */
  .lb-panel .lp-head {
    font-size: 22px; font-weight: 800; letter-spacing: 1.1px;
    color: #FFFFFF !important; margin-bottom: 13px;
    display: block; padding: 6px 12px;
    border-radius: 10px;
    background: rgba(5, 15, 34, 0.62);
    border-left: 4px solid #67E8F9;
    text-shadow: 0 1px 3px rgba(1, 6, 16, 0.6);
  }
  .lb-panel .lp-head .lp-en {
    font-size: 12.5px; font-weight: 700; letter-spacing: .7px;
    color: #9FE6FF !important; margin-left: 9px;
    text-shadow: none;
  }
  .lb-panel .lp-row {
    display: flex; justify-content: space-between; gap: 12px;
    font-size: 18.5px; color: #EAF4FF !important; line-height: 1.95;
  }
  /* 图形说明文字（图下面那行"这图画的是什么"） */
  .lb-panel .lp-cap {
    font-size: 13px; font-weight: 600; color: #A9D8F5 !important;
    line-height: 1.6; margin-top: 9px;
    text-shadow: 0 1px 2px rgba(1, 6, 16, 0.5);
  }
  /* ⚠️ 必须**显式**给 span 上色：全站有一条 `p, li, span, div { color: #334155 }`，
     它是直接规则，会盖掉从 .lp-row 继承下来的浅色 → 深灰字压深蓝底，
     用户看到的就是"指标名那一列看不清"（v8 实际事故：面板里标签列几乎隐形）。 */
  .lb-panel .lp-row span { color: #EFF6FF !important; font-size: 17px; }
  .lb-panel .lp-row b { color: #FFFFFF !important; font-size: 21px; font-weight: 800; }
  .lb-panel .lp-bar {
    height: 13px; border-radius: 999px; margin-top: 12px;
    background: linear-gradient(90deg, #22D3EE, #3B82F6);
  }
  /* SVG 铺满面板宽度 + 等比缩放（viewBox 提供长宽比）→ 面板变宽，图也跟着变大 */
  .lb-panel svg { display: block; width: 100%; height: auto; }
  /* 窄屏：三列挤成一列很难看 → 把两侧装饰整体撤掉。
     `:has()` 命中时整列隐藏并让登录卡占满宽度；万一浏览器不支持 `:has()`，
     至少内容也隐藏掉，不至于挤成一条看不清的窄柱。 */
  @media (max-width: 1280px) {
    .lb-side-col { display: none !important; }
    [data-testid="stColumn"]:has(.lb-side-col) { display: none !important; }
    [data-testid="stHorizontalBlock"]:has(.lb-side-col)
      > [data-testid="stColumn"]:nth-child(2) {
      width: 100% !important; flex: 1 1 100% !important;
    }
  }
  @media (max-width: 900px) { .lb-ring, .lb-scan { display: none; } }

  /* ==========================================================
     v10 登录页四连修（用户第 1/4/7/11 条反馈）：
       ① 隐藏 "Press Enter to submit form"（全局补丁里已做，这里兜底再写一条）
       ② 密码输入后看不清 → 输入文字改纯白、加大、加字距（密码圆点更醒目）
       ③ 「安全登录」按钮字和青色底融在一起 → 换深蓝渐变底 + 纯白粗体字，
          对比度一步拉满
       ④ 背景要"动态" → 三团极光色块缓慢漂移（纯 CSS keyframes，
          pointer-events:none 压在最底层，不挡任何点击）
     ========================================================== */
  [data-testid="stForm"] [data-testid="InputInstructions"] { display: none !important; }

  /* ---------- ① 大字体（用户第 4 条：登录界面字体较小） ----------
     账号框 22px、密码框 26px：密码框单独再大一档，因为圆点「•」本身字形小，
     跟账号同字号时视觉上会小一半，看起来就像没输进去。 */
  [data-testid="stForm"] input,
  [data-testid="stForm"] input[type="password"] {
    color: #FFFFFF !important;
    font-size: 22px !important;
    font-weight: 600 !important;
    letter-spacing: 1.5px;
    padding: 16px 18px !important;
  }
  /* 密码：字距再拉开到 6px，圆点一颗一颗分得开，位数一眼能数清 */
  [data-testid="stForm"] input[type="password"] {
    font-size: 26px !important;
    letter-spacing: 6px;
    /* 右padding留位置给眼睛按钮，否则末位圆点会被眼睛图标压住 */
    padding-right: 52px !important;
  }
  [data-testid="stForm"] [data-testid="stWidgetLabel"] p,
  [data-testid="stForm"] label[data-testid="stWidgetLabel"] p {
    font-size: 20px !important;
    font-weight: 700 !important;
    color: #EAF2FF !important;
  }
  [data-testid="stForm"] input::placeholder { font-size: 16.5px !important; }

  /* ---------- ② 密码可见（任务1 新增；用户第 11 条「输入密码后看不清」） ----------
     Streamlit 的密码框自带右侧「眼睛」按钮，但默认图标是 #A9C0E6 淡蓝、
     18px，压在半透明输入框里几乎看不见 —— 用户根本不知道能点。
     这里把图标提到近白 + 放大到 24px + hover 提亮，让它第一眼就被看见。 */
  [data-testid="stForm"] [data-testid="stTextInputRootElement"] button svg {
    fill: #EAF2FF !important; color: #EAF2FF !important;
    width: 24px; height: 24px;
  }
  [data-testid="stForm"] [data-testid="stTextInputRootElement"] button:hover svg {
    fill: #FFFFFF !important; color: #FFFFFF !important;
  }
  /* 明文态（点了眼睛之后 input 变 type=text）：同样保证白字大号 */
  [data-testid="stForm"] input[type="text"] { letter-spacing: 2px; }
  /* 密码框下方那行「可以点眼睛看明文」的说明。
     ⚠️ 必须写在 [data-testid="stForm"] p/li/small 统一样式**之后**，
        否则会被那条 color:#CBDCF6 直接盖掉（同特异性、后者胜）。 */
  [data-testid="stForm"] .login-pw-tip,
  [data-testid="stForm"] .login-pw-tip * {
    color: #9FB8DC !important;
    font-size: 14px !important;
    font-weight: 600 !important;
    letter-spacing: .2px;
    margin: -4px 0 2px 2px;
  }
  [data-testid="stForm"] .login-pw-tip b { color: #CFE3FF !important; }

  [data-testid="stFormSubmitButton"] button {
    background: linear-gradient(92deg, #1D4ED8 0%, #2563EB 55%, #0EA5E9 100%) !important;
    color: #FFFFFF !important;
    font-size: 24px !important;
    font-weight: 900 !important;
    letter-spacing: 3.5px;
    padding: 18px 22px !important;
    border: 1px solid rgba(255, 255, 255, 0.35) !important;
    box-shadow: 0 10px 26px rgba(29, 78, 216, 0.45) !important;
    text-shadow: 0 1px 3px rgba(1, 12, 34, 0.45);
  }
  [data-testid="stFormSubmitButton"] button:hover {
    background: linear-gradient(92deg, #2563EB 0%, #3B82F6 55%, #38BDF8 100%) !important;
    color: #FFFFFF !important;
  }

  /* 动态极光背景：三团色块各自漂移 + 呼吸，动得慢、不刺眼 */
  @keyframes lbAurora1 {
    0%   { transform: translate(-8%, -4%) scale(1); opacity: .55; }
    50%  { transform: translate(10%, 6%) scale(1.18); opacity: .8; }
    100% { transform: translate(-8%, -4%) scale(1); opacity: .55; }
  }
  @keyframes lbAurora2 {
    0%   { transform: translate(6%, 10%) scale(1.1); opacity: .4; }
    50%  { transform: translate(-10%, -6%) scale(0.92); opacity: .65; }
    100% { transform: translate(6%, 10%) scale(1.1); opacity: .4; }
  }
  @keyframes lbAurora3 {
    0%   { transform: translate(0%, 6%) scale(0.95) rotate(0deg); opacity: .35; }
    50%  { transform: translate(-4%, -8%) scale(1.15) rotate(8deg); opacity: .6; }
    100% { transform: translate(0%, 6%) scale(0.95) rotate(0deg); opacity: .35; }
  }
  .lb-aurora { position: fixed; inset: 0; z-index: 0; pointer-events: none; overflow: hidden; }
  .lb-aurora i {
    position: absolute; display: block; border-radius: 50%;
    filter: blur(90px); mix-blend-mode: screen; will-change: transform, opacity;
  }
  .lb-aurora i:nth-child(1) {
    width: 46vw; height: 46vw; left: -8vw; top: -10vw;
    background: radial-gradient(circle, rgba(34,211,238,.34) 0%, rgba(34,211,238,0) 68%);
    animation: lbAurora1 17s ease-in-out infinite;
  }
  .lb-aurora i:nth-child(2) {
    width: 52vw; height: 52vw; right: -12vw; top: 22vh;
    background: radial-gradient(circle, rgba(59,130,246,.32) 0%, rgba(59,130,246,0) 68%);
    animation: lbAurora2 21s ease-in-out infinite;
  }
  .lb-aurora i:nth-child(3) {
    width: 40vw; height: 40vw; left: 28vw; bottom: -16vw;
    background: radial-gradient(circle, rgba(14,165,233,.28) 0%, rgba(14,165,233,0) 70%);
    animation: lbAurora3 25s ease-in-out infinite;
  }
  /* ---------- ③ 图也要动（用户第 4 条：背景 /**图** 做成动态的） ----------
     背景有极光了，但六块面板里的图全是"死图"：走势线不会走、柱子不长、
     环形不转、网络连线不动 —— 一屏科技感的东西全是静止的，看着像截图。
     这里给每张图补一条 CSS 动画，周期 2.2~6s（跟极光同频），慢到不晃眼。 */
  /* 走势主线：dash 描线，线自己"画"出来 */
  @keyframes lbDraw { 0% { stroke-dashoffset: 320; } 60%, 100% { stroke-dashoffset: 0; } }
  .lb-panel .tr-main { stroke-dasharray: 320; animation: lbDraw 4.2s ease-in-out infinite; }
  /* 均线：轻微明暗呼吸，作底不抢戏 */
  @keyframes lbFade { 0%, 100% { opacity: .35; } 50% { opacity: .8; } }
  .lb-panel .tr-sub { animation: lbFade 3.4s ease-in-out infinite; }
  /* 最新价光点：缩放脉冲 */
  @keyframes lbPulse {
    0%, 100% { opacity: .45; transform: scale(.85); }
    50%      { opacity: 1;   transform: scale(1.3); }
  }
  .lb-panel .tr-dot {
    transform-box: fill-box; transform-origin: center;
    animation: lbPulse 2.2s ease-in-out infinite;
  }
  /* 财务指标那条进度条：宽度微微涨落，像实时刷新 */
  @keyframes lbBarW { 0%, 100% { width: 74%; } 50% { width: 83%; } }
  .lb-panel .lp-bar { animation: lbBarW 3.6s ease-in-out infinite; }
  /* 关系网络：连线上跑流动虚线，节点逐个亮 */
  @keyframes lbFlow { to { stroke-dashoffset: -24; } }
  .lb-panel .nw-link { stroke-dasharray: 5 7; animation: lbFlow 1.8s linear infinite; }
  @keyframes lbNode { 0%, 100% { opacity: .55; } 50% { opacity: 1; } }
  .lb-panel .nw-node { animation: lbNode 2.4s ease-in-out infinite; }
  .lb-panel .nw-node:nth-of-type(2) { animation-delay: .4s; }
  .lb-panel .nw-node:nth-of-type(3) { animation-delay: .8s; }
  .lb-panel .nw-node:nth-of-type(4) { animation-delay: 1.2s; }
  .lb-panel .nw-node:nth-of-type(5) { animation-delay: 1.6s; }
  /* 四维评分柱：从下往上长，逐根错开（transform-origin 必须是底边） */
  @keyframes lbGrow {
    0%, 100% { transform: scaleY(.55); }
    50%      { transform: scaleY(1); }
  }
  .lb-panel .sc-bar {
    transform-box: fill-box; transform-origin: bottom;
    animation: lbGrow 2.8s ease-in-out infinite;
  }
  .lb-panel .sc-bar:nth-of-type(2) { animation-delay: .35s; }
  .lb-panel .sc-bar:nth-of-type(3) { animation-delay: .7s; }
  .lb-panel .sc-bar:nth-of-type(4) { animation-delay: 1.05s; }
  /* 风险环：前景弧线匀速绕圈，像雷达扫描（分数本身不变，只是扫描感） */
  /* ⚠️ 弧线本身靠 transform="rotate(-90 cx cy)" 把起点摆到 12 点钟；
     CSS 的 transform 会**覆盖** SVG 的 transform 属性（presentation attribute
     优先级低于 CSS），所以 -90° 这个起始角必须写进 keyframes，否则弧线会
     瞬间跳回 3 点钟方向再开始转。 */
  @keyframes lbRingSpin {
    from { transform: rotate(-90deg); }
    to   { transform: rotate(270deg); }
  }
  .lb-panel .rk-arc {
    transform-box: fill-box; transform-origin: center;
    animation: lbRingSpin 5.5s linear infinite;
  }
  .lb-panel .rk-arc-b { animation-duration: 7s; }
  /* 无障碍：系统开了"减少动态效果"就全部静止 */
  @media (prefers-reduced-motion: reduce) {
    .lb-aurora i,
    .lb-panel .tr-main, .lb-panel .tr-sub, .lb-panel .tr-dot,
    .lb-panel .lp-bar, .lb-panel .nw-link, .lb-panel .nw-node,
    .lb-panel .sc-bar, .lb-panel .rk-arc { animation: none !important; }
  }
</style>
"""


# ============================================================================
# 登录页的注入 HTML（v8 起：一律单行、无缩进、无空行）
#
# 为什么要写成这种"丑"的一长串？——因为 markdown-it 的 HTML 块**遇到空行就结束**，
# 之后的行会退回 markdown 解析；Streamlit 又会先对整段做去公共缩进，
# 剩下的行若仍有 ≥4 空格缩进，就会被判定为**缩进代码块**，
# 于是页面上直接打印出 `div.lb-panel`、`rect`、`circle` 这些源码
# （v7 实际事故：登录页 6 块装饰面板只有 2 块渲染，另外 4 块变成代码块）。
# 单行 = 一个 HTML 块，任何情况下都不会掉进代码块分支。
# ⚠️ 改这几段时**不要**为了好看而换行/缩进/加空行。
#
# v9：拆成三段 —— 背景装饰（LOGIN_DECO）/ 左列面板（LOGIN_PANELS_L）/
#     右列面板（LOGIN_PANELS_R）。面板从"fixed 覆盖层"搬进真实的 st.columns，
#     于是它们能跟着布局一起变大，也不会再和登录卡抢位置。
# ============================================================================
LOGIN_DECO = (
    '<div class="lb-aurora" aria-hidden="true"><i></i><i></i><i></i></div>'
    '<div class="lb-deco" aria-hidden="true">'
    '<div class="lb-halo"></div>'
    '<div class="lb-ring"></div><div class="lb-scan"></div><div class="lb-grid"></div>'
    '</div>'
)

# v10-2（用户第 6 条）：面板表头原来一律是「■ + 中文」，■ 是纯装饰字符、
# 图里也没有任何文字 → 评委看不出这几块画的是什么。现在：
#   ① 去掉 ■，表头改成「中文 + 英文」双行式（中英文都看得懂）；
#   ② 每张图下面补一行 .lp-cap 说明（这图画的是什么）；
#   ③ 图里关键节点直接打中文标签（供应商 / 核心企业 / 客户 …），
#      不再是"几个彩色圆点连几根线"。
LOGIN_PANELS_L = (
    '<div class="lb-side-col">'
    '<div class="lb-panel">'
    '<div class="lp-head">走势曲线 · K 线结构<span class="lp-en">Price Trend / K-Line</span></div>'
    '<svg width="272" height="92" viewBox="0 0 212 76" fill="none" '
    'xmlns="http://www.w3.org/2000/svg">'
    '<line x1="0" y1="46" x2="212" y2="46" stroke="rgba(103,232,249,.16)"/>'
    '<line x1="0" y1="26" x2="212" y2="26" stroke="rgba(103,232,249,.16)"/>'
    '<polyline class="tr-main" points="2,44 28,30 54,37 80,18 106,27 132,12 158,23 184,7 210,17" '
    'stroke="#22D3EE" stroke-width="2" fill="none" '
    'stroke-linejoin="round" stroke-linecap="round"/>'
    '<polyline class="tr-sub" points="2,50 28,42 54,46 80,34 106,40 132,30 158,36 184,24 210,31" '
    'stroke="rgba(56,189,248,.55)" stroke-width="1.6" fill="none" '
    'stroke-linejoin="round" stroke-linecap="round"/>'
    '<circle class="tr-dot" cx="184" cy="7" r="3" fill="#7FE9FF"/>'
    '<text x="0" y="9" fill="#EAF6FF" font-size="10" font-weight="700">股价 Price</text>'
    '<text x="146" y="62" fill="rgba(207,233,255,.85)" font-size="9">日线 · 60 日</text>'
    '<text x="0" y="73" fill="rgba(207,233,255,.70)" font-size="9">均线 MA</text>'
    '</svg>'
    '<div class="lp-row"><span>区间振幅 Range</span><b>+18.6%</b></div>'
    '<div class="lp-cap">折线＝股价走势，浅色线＝均线 MA；用于判断趋势与波动幅度。</div>'
    '</div>'
    '<div class="lb-panel">'
    '<div class="lp-head">财务指标<span class="lp-en">Financial Indicators</span></div>'
    '<div class="lp-row"><span>营业收入 Revenue</span><b>1,742.6 亿</b></div>'
    '<div class="lp-row"><span>净利润 Net Profit</span><b>862.3 亿</b></div>'
    '<div class="lp-row"><span>净资产收益率 ROE</span><b>34.2%</b></div>'
    '<div class="lp-bar" style="width:78%;"></div>'
    '<div class="lp-cap">进度条＝ROE 在同行业中的相对位置（78% 分位）。</div>'
    '</div>'
    '<div class="lb-panel">'
    '<div class="lp-head">公司关系网络<span class="lp-en">Supply Chain Network</span></div>'
    '<svg width="272" height="122" viewBox="0 0 212 96" fill="none" '
    'xmlns="http://www.w3.org/2000/svg">'
    '<line class="nw-link" x1="34" y1="24" x2="106" y2="44" stroke="rgba(103,232,249,.35)"/>'
    '<line class="nw-link" x1="34" y1="62" x2="106" y2="44" stroke="rgba(103,232,249,.35)"/>'
    '<line class="nw-link" x1="106" y1="44" x2="178" y2="24" stroke="rgba(103,232,249,.35)"/>'
    '<line class="nw-link" x1="106" y1="44" x2="178" y2="62" stroke="rgba(103,232,249,.35)"/>'
    '<line class="nw-link" x1="34" y1="24" x2="178" y2="24" stroke="rgba(56,189,248,.20)"/>'
    '<circle class="nw-node" cx="34" cy="24" r="5" fill="#22D3EE"/>'
    '<circle class="nw-node" cx="34" cy="62" r="5" fill="#3B82F6"/>'
    '<circle class="nw-node" cx="106" cy="44" r="7" fill="#7FE9FF"/>'
    '<circle class="nw-node" cx="178" cy="24" r="5" fill="#3B82F6"/>'
    '<circle class="nw-node" cx="178" cy="62" r="5" fill="#22D3EE"/>'
    '<text x="34" y="14" text-anchor="middle" fill="#EAF6FF" font-size="9.5" font-weight="700">供应商</text>'
    '<text x="34" y="80" text-anchor="middle" fill="#EAF6FF" font-size="9.5" font-weight="700">供应商B</text>'
    '<text x="106" y="64" text-anchor="middle" fill="#FFFFFF" font-size="10" font-weight="800">核心企业</text>'
    '<text x="178" y="14" text-anchor="middle" fill="#EAF6FF" font-size="9.5" font-weight="700">客户</text>'
    '<text x="178" y="80" text-anchor="middle" fill="#EAF6FF" font-size="9.5" font-weight="700">经销商</text>'
    '</svg>'
    '<div class="lp-cap">上游供应商 → 核心企业 → 下游客户／经销商的产业链图谱。</div>'
    '</div>'
    '</div>'
)

LOGIN_PANELS_R = (
    '<div class="lb-side-col">'
    '<div class="lb-panel">'
    '<div class="lp-head">指标摘要<span class="lp-en">Key Metrics</span></div>'
    '<div class="lp-row"><span>市盈率 PE-TTM</span><b>21.4</b></div>'
    '<div class="lp-row"><span>市净率 PB</span><b>5.86</b></div>'
    '<div class="lp-row"><span>毛利率 Gross Margin</span><b>91.3%</b></div>'
    '<div class="lp-row"><span>资产负债率 Debt Ratio</span><b>16.8%</b></div>'
    '<div class="lp-cap">估值（PE / PB）＋ 盈利质量（毛利率）＋ 负债水平，四项核心指标。</div>'
    '</div>'
    '<div class="lb-panel">'
    '<div class="lp-head">财务评分 · 四维<span class="lp-en">4-Dimension Score</span></div>'
    '<svg width="272" height="112" viewBox="0 0 212 92" fill="none" '
    'xmlns="http://www.w3.org/2000/svg">'
    '<rect class="sc-bar" x="16"  y="30" width="34" height="34" rx="4" fill="#22D3EE"/>'
    '<rect class="sc-bar" x="66"  y="14" width="34" height="50" rx="4" fill="#38BDF8"/>'
    '<rect class="sc-bar" x="116" y="38" width="34" height="26" rx="4" fill="#3B82F6"/>'
    '<rect class="sc-bar" x="166" y="22" width="34" height="42" rx="4" fill="#7FE9FF"/>'
    '<line x1="0" y1="68" x2="212" y2="68" stroke="rgba(103,232,249,.30)"/>'
    '<text x="33" y="20" text-anchor="middle" fill="#EAF6FF" font-size="10" font-weight="700">8.5</text>'
    '<text x="83" y="10" text-anchor="middle" fill="#EAF6FF" font-size="10" font-weight="700">9.2</text>'
    '<text x="133" y="32" text-anchor="middle" fill="#EAF6FF" font-size="10" font-weight="700">6.8</text>'
    '<text x="183" y="16" text-anchor="middle" fill="#EAF6FF" font-size="10" font-weight="700">8.0</text>'
    '<text x="33" y="82" text-anchor="middle" fill="#EAF6FF" font-size="10.5" font-weight="700">盈利</text>'
    '<text x="83" y="82" text-anchor="middle" fill="#EAF6FF" font-size="10.5" font-weight="700">成长</text>'
    '<text x="133" y="82" text-anchor="middle" fill="#EAF6FF" font-size="10.5" font-weight="700">偿债</text>'
    '<text x="183" y="82" text-anchor="middle" fill="#EAF6FF" font-size="10.5" font-weight="700">营运</text>'
    '</svg>'
    '<div class="lp-cap">四维各 10 分：盈利能力 / 成长能力 / 偿债能力 / 营运能力。</div>'
    '</div>'
    '<div class="lb-panel">'
    '<div class="lp-head">风险评分<span class="lp-en">Risk Score</span></div>'
    '<svg width="272" height="112" viewBox="0 0 212 98" fill="none" '
    'xmlns="http://www.w3.org/2000/svg">'
    '<circle cx="56" cy="42" r="26" stroke="rgba(103,232,249,.22)" '
    'stroke-width="8" fill="none"/>'
    '<circle class="rk-arc" cx="56" cy="42" r="26" stroke="#22D3EE" stroke-width="8" '
    'fill="none" stroke-dasharray="82 164" stroke-linecap="round" '
    'transform="rotate(-90 56 42)"/>'
    '<text x="56" y="49" text-anchor="middle" fill="#EAF2FF" '
    'font-size="19" font-weight="700">4.2</text>'
    '<text x="56" y="88" text-anchor="middle" fill="#EAF6FF" '
    'font-size="11" font-weight="700">财务风险（越低越好）</text>'
    '<circle cx="156" cy="42" r="26" stroke="rgba(103,232,249,.22)" '
    'stroke-width="8" fill="none"/>'
    '<circle class="rk-arc rk-arc-b" cx="156" cy="42" r="26" stroke="#7FE9FF" stroke-width="8" '
    'fill="none" stroke-dasharray="131 164" stroke-linecap="round" '
    'transform="rotate(-90 156 42)"/>'
    '<text x="156" y="49" text-anchor="middle" fill="#EAF2FF" '
    'font-size="19" font-weight="700">7.8</text>'
    '<text x="156" y="88" text-anchor="middle" fill="#EAF6FF" '
    'font-size="11" font-weight="700">经营稳健（越高越好）</text>'
    '</svg>'
    '<div class="lp-cap">圆环＝得分 / 10：左侧财务风险越低越好，右侧经营稳健度越高越好。</div>'
    '</div>'
    '</div>'
)


LOGIN_BRAND = (
    '<div class="login-brand" style="text-align:center;max-width:1080px;'
    'margin:2.5vh auto 18px auto;">'
    '<div class="lb-orb">AI</div>'
    '<div class="lb-badge">第二届全国人工智能应用创新大赛 · 参赛作品</div>'
    '<div class="lb-title">📈 金融智能投研 <em>Agent</em></div>'
    '<p class="lb-sub">AI 驱动的财报深度分析 · 结构化评分 · 多公司对比 · 同业对标基准</p>'
    '<p class="lb-note">交易所公开披露数据 · 上市公司年报 · 财务指标库</p>'
    '</div>'
)


def check_login():
    if st.session_state.get("authenticated", False):
        return True

    # ---- 方案1: 免登录链接 (URL 携带 ?key=xxx) ----
    _qp_key = st.query_params.get("key", "")
    if _qp_key and _qp_key in _demo_keys():
        st.session_state["authenticated"] = True
        st.session_state["demo_mode"] = True
        st.rerun()

    # ---- 登录页（浅色近白）：品牌区 + 居中白卡 ----
    # 侧边栏与主工作台在此分支根本不渲染（check_login 返回 False → st.stop()），
    # LOGIN_CSS 再把侧边栏兜底隐藏，确保"登录前只有这一屏"。
    st.markdown(LOGIN_CSS, unsafe_allow_html=True)

    # ---- v7/v9：科技装饰层（纯 CSS + 内联 SVG，无任何图片素材）----
    # 用户反馈"不要只给我一个登录的账号，里面有很多图形，给我放一点在上面，
    # 要不然给人的感觉太空旷"。这里按参考图补上：
    #   左：走势曲线 / 财务指标 / 公司关系网络   右：指标摘要 / 财务评分 / 风险评分
    # 背景那层（光环 / 扫描光 / 网格 / 中央辉光）装在一个 fixed + pointer-events:none
    # 的 `.lb-deco` 里，且主内容 z-index:3（见 LOGIN_CSS），永远不会挡住表单与文字。
    # ⚠️ v8：HTML 常量见文件上方（必须单行，否则会被 markdown 当代码块打出来）。
    st.markdown(LOGIN_DECO, unsafe_allow_html=True)

    # 品牌区（压在深蓝底上 → 全部显式浅色，见 LOGIN_CSS 的对比度清单）
    # max-width 1080px：让"AI 驱动的财报深度分析 · … · 同业对标基准"一行铺开，
    # 而不是在窄条里折成两行（用户要求"横向变大，不要竖着变大"）。
    st.markdown(LOGIN_BRAND, unsafe_allow_html=True)

    # v9：登录页改成**真三列**（左装饰 | 登录卡 | 右装饰，比例 1.3 : 2 : 1.3）。
    # 面板住进真实列之后：① 宽度由布局给（1800px 容器下每列 ≈ 500px，
    # 比旧的 clamp(270~350px) 大一截）；② 与登录卡同高、同材质 → "融合在一起"；
    # ③ 结构上不可能压住输入框（旧版是 fixed 覆盖层，全靠 media query 兜）。
    _cl, _mid, _cr = st.columns([1.3, 2, 1.3], gap="large")
    with _cl:
        st.markdown(LOGIN_PANELS_L, unsafe_allow_html=True)
    with _mid:
        with st.form("login_form", clear_on_submit=False):
            st.markdown(
                '<div style="text-align:center;margin:0 0 22px 0;">'
                '<div style="font-size:30px;font-weight:800;color:#FFFFFF !important;'
                'letter-spacing:1px;">账号登录</div>'
                '<div style="font-size:19px;color:#A9C0E6 !important;margin-top:8px;'
                'letter-spacing:.3px;">'
                '输入账号与密码进入投研工作台</div>'
                "</div>",
                unsafe_allow_html=True,
            )
            # 标签外置（账号 / 密码），框内只放小号英文占位符
            # ⚠️ 必须带 key（v10）：无 key 控件在登录后会被 Streamlit 清理状态，
            #    而 AppTest 回归的旧树里仍残留这两个节点 → get_widget_states 直接
            #    KeyError；带 key 后值挂在 user key 上，状态生命周期由我们掌控。
            user = st.text_input("账号", key="login_user", placeholder="Account")
            pw = st.text_input("密码", type="password", key="login_pw", placeholder="Password")
            # 任务1「密码可见」：光有眼睛图标没用 —— 图标太淡时用户根本不知道能点。
            # 这行说明把"可以明文核对"这件事说出来；明文态的字体由 LOGIN_CSS 统一放大。
            st.markdown(
                '<div class="login-pw-tip">👁 点右侧 <b>眼睛图标</b> 可切换明文，核对后再登录</div>',
                unsafe_allow_html=True,
            )
            submitted = st.form_submit_button("安全登录", use_container_width=True, type="primary")
            if submitted:
                if user == APP_USER and pw == APP_PASSWORD:
                    st.session_state["authenticated"] = True
                    st.rerun()
                else:
                    st.error("账号或密码错误，请检查后重试（区分大小写）。")
            # v10：删除了原来的「① 单公司深度分析…④ 同业对标基准 + 演示路径」四条简介。
            # 用户明确要求登录页只留登录本身 —— 功能介绍在使用说明页里都有，
            # 登录卡塞满文字反而挤占输入区、拉低首屏质感。
        st.markdown(
            '<p class="login-foot">🔒 本作品仅供大赛评审访问 · 分析结果仅供参考，不构成投资建议</p>',
            unsafe_allow_html=True,
        )
    with _cr:
        st.markdown(LOGIN_PANELS_R, unsafe_allow_html=True)
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
    # 【Agent 轨迹】这是全站唯一的非流式模型出口，在这里插一次桩就能覆盖
    # 结构化分析 / 指标抽取 / 研报 / 润色等全部调用，不必逐处埋点；
    # 模型名回退、JSON 模式裸调等细节都会落到这一步的「产出/备注」里。
    with get_tracer().span(
        "大模型", _ACTION_LABEL.get(action, action),
        inp=f"temperature={temperature}" + (" · JSON 模式" if json_mode else ""),
    ) as _s:
        if not check_quota():
            _s.status = "failed"
            _s.out = "今日额度已用尽，未发起调用"
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
                _s.out = f"model={_m} · 返回 {len(_out)} 字符"
                if _m != _model_candidates()[0]:
                    # 换名才成功说明默认模型名不可用 —— 属于「降级但产出了结果」
                    _s.status = "degraded"
                    _s.note = "默认模型名不可用，已自动回退到兼容模型名"
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



def _htm(s) -> str:
    """把外部文本（公司名 / 代码 / 行业）插进 unsafe_allow_html 片段前先转义。

    公司名来自接口返回，理论上可控，但万一某家简称里带 `<` 或 `&`，
    整块 HTML 会被截断（v10-2 加，用于对比页的取数状态块）。
    """
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_score_card(label, value):
    if value is None:
        st.metric(label, "数据不足")
    else:
        st.metric(label, f"{value} / 10")
        st.progress(min(max(value / 10.0, 0.0), 1.0))


# ============ 统一分区标题（「步骤条」） ============
# 为什么要有这个东西：原来每个区块的标题都是 `##### 长标题 —— 一句很长的解释`，
# 有的用 h4、有的用 h5、有的加粗，字号/颜色/间距全不一致；长解释跟标题挤在一行里
# 会折行，视觉上就变成"一堆颜色差不多的蓝字堆在一起"（用户反馈：排版乱、看不清）。
# 现在统一成：编号方块 + 短标题（深色加粗） + 小号灰色说明（不抢视线），
# 每个区块长得一样，从上到下就是一条清晰的步骤线。
def step_head(num: str, title: str, note: str = "", tag: str = "") -> None:
    # num 允许为空：使用说明页的卡片是"分类"而不是"步骤"，不该硬编号
    _num = f'<span class="sh-num">{num}</span>' if num else ""
    _tag = f'<span class="sh-tag">{tag}</span>' if tag else ""
    _note = f'<span class="sh-note">{note}</span>' if note else ""
    st.markdown(
        f'<div class="step-head">{_num}'
        f'<span class="sh-title">{title}</span>{_tag}{_note}</div>',
        unsafe_allow_html=True,
    )


# ============ 页面大标题 / 小节标题 / 卡片容器（v5 结构层） ============
# 这三个组件解决的是同一个问题：**读者不知道"我在哪、这堆东西是什么"**。
#   page_head → 我在哪个视图（"单公司深度分析"）
#   sec_head  → 我在这个视图的第几段（"1. 对比公司管理"）
#   card      → 这一段的内容从哪到哪（白卡边界）
def page_head(title: str, note: str = "", tag: str = "") -> None:
    """视图顶部的大标题（30px 深墨色）+ 副说明 + 品牌蓝短横 + 右侧科技徽记。

    v7 加的 `.ph-deco` 是右侧那块"同心环 + 六边形 + 迷你折线"的 SVG：
    用户反馈"页面太空旷"，而纯文字标题旁边正好有一大片空白。
    全部用内联 SVG 画（不引图片素材），透明度压到 0.9 以内不抢视线。
    """
    _tag = f' <span class="sh-tag">{tag}</span>' if tag else ""
    _note = f'<span class="ph-note">{note}</span>' if note else ""
    st.markdown(
        f'<div class="page-head">'
        f'<div class="ph-text"><div class="ph-title">{title}{_tag}</div>'
        f'<span class="ph-rule"></span>{_note}</div>'
        f'<svg class="ph-deco" width="168" height="94" viewBox="0 0 168 94" fill="none" '
        f'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
        f'<circle cx="84" cy="47" r="43" stroke="#C3D7F2" stroke-width="1" stroke-dasharray="5 7"/>'
        f'<circle cx="84" cy="47" r="29" stroke="#D5E3F7" stroke-width="1"/>'
        f'<path d="M84 18 L106 31 L106 63 L84 76 L62 63 L62 31 Z" stroke="#93B4E8" stroke-width="1.5"/>'
        f'<path d="M66 58 L75 45 L83 51 L93 34 L103 41" stroke="#1D4ED8" stroke-width="2.2" '
        f'stroke-linecap="round" stroke-linejoin="round" opacity="0.75"/>'
        f'<circle cx="75" cy="45" r="2.8" fill="#38BDF8"/>'
        f'<circle cx="93" cy="34" r="2.8" fill="#38BDF8"/>'
        f'<circle cx="62" cy="31" r="2.2" fill="#93B4E8"/>'
        f'<circle cx="106" cy="63" r="2.2" fill="#93B4E8"/>'
        f'<circle cx="84" cy="76" r="2.2" fill="#93B4E8"/>'
        f'</svg>'
        f'</div>',
        unsafe_allow_html=True,
    )


def sec_head(num: str, title: str, note: str = "", tag: str = "") -> None:
    """页面级小节标题（19px + 左侧蓝色竖条），用于"第 1 段 / 第 2 段"这种大分段。"""
    _tag = f'<span class="sec-tag">{tag}</span>' if tag else ""
    _note = f'<span class="sec-note">{note}</span>' if note else ""
    _num = f'<span class="sec-num">{num}.</span>' if num else ""
    st.markdown(
        f'<div class="sec-head">{_num}<span class="sec-title">{title}</span>{_tag}{_note}</div>',
        unsafe_allow_html=True,
    )


@contextmanager
def card(key: str, num: str = "", title: str = "", note: str = "", tag: str = "",
         col=None):
    """一步一张白卡（样式挂在 CSS 的 `[class*="st-key-kcard-"]` 上）。

    用法：
        with card("src", "2", "数据来源", "在线取数已包含行情与财务指标", "二选一"):
            st.radio(...)

        # 参与多栏网格：把卡片挂到某一列里
        with card("src", "2", "数据来源", col=_grid[0]):
            st.radio(...)

    `col` 的实现在最后一行：走 `col.container(key=...)`，等价于
    `with col: with st.container(key=...)`。这样**已有代码一行都不用加缩进**
    就能排进多栏 —— 缩进改动是这类重构最大的事故来源（改一处、漏一处就报
    IndentationError，而且和业务逻辑的错误混在一起很难定位）。

    ⚠️ key 必须以 `kcard-` 开头（实际类名是 `st-key-kcard-<key>`），
       且**不能再有别的控件用同样前缀**，否则会被子串匹配连带刷成卡片。
    ⚠️ 追问区的 st.chat_input **绝不能**放进卡片里 —— 父容器一变，
       它就从"钉在视口底部"变成"内联渲染"，用户要的钉底效果会丢
       （详见 eval_chat_order.py 的两形态断言）。
       ⚠️ 注意：放进 st.columns 的**列**里同样会变成 inline。所以多栏网格只能装
          1~5 步的输入卡，追问区必须留在页面根层。
    """
    _ctx = col.container(key=f"kcard-{key}") if col is not None else st.container(key=f"kcard-{key}")
    with _ctx:
        if title:
            step_head(num, title, note, tag)
        yield


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


def fig_compare_radar(score_list: list, names: list) -> bytes | None:
    """N 家公司四维评分**雷达对比图**（v10 新增 · 用户第 3 条反馈）。

    柱状图擅长逐维度比高低，雷达图擅长一眼看出"谁强在哪些维度、短板在哪"，
    两种视图互补。维度取各家评分键的并集（与柱状图同一并集逻辑），
    缺失维度不连点（画 0 会把"缺数据"伪装成"得 0 分"）。
    """
    n = len(names)
    if n < 2 or len(score_list) != n:
        return None
    dims = []
    for s in score_list:
        for d in (s or {}).keys():
            if d not in dims:
                dims.append(d)
    if len(dims) < 3:
        return None   # 雷达至少要 3 个维度，否则画出来是没意义的三角形

    N = len(dims)
    angles = np.linspace(0, 2 * np.pi, N, endpoint=False).tolist()
    angles += angles[:1]
    fig = plt.figure(figsize=(5.6, 4.6), facecolor=_PAL["card"])
    ax = fig.add_subplot(111, polar=True)
    ax.set_facecolor(_PAL["card"])
    for i, s in enumerate(score_list):
        vals = [(s or {}).get(d) for d in dims]
        nums = [float(v) if isinstance(v, (int, float)) else None for v in vals]
        xs, ys = [], []
        for a, v in zip(angles[:-1], nums):
            if v is not None:
                xs.append(a)
                ys.append(v)
        # 首尾闭合：只有全部维度都有值才闭合，否则只画散点
        if len(xs) == N:
            xs.append(xs[0])
            ys.append(ys[0])
        if xs:
            if len(xs) == N + 1:
                ax.plot(xs, ys, linewidth=1.8, label=names[i], color=_cmp_series_color(i))
                ax.fill(xs, ys, alpha=0.10, color=_cmp_series_color(i))
            ax.scatter(xs, ys, s=16, color=_cmp_series_color(i), zorder=3)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(dims, color=_PAL["text"], fontsize=9)
    ax.set_ylim(0, 10)
    ax.set_yticks([2, 4, 6, 8, 10])
    ax.tick_params(axis="y", labelcolor=_PAL["text"], labelsize=7.5)
    ax.grid(color="#E2E8F0", alpha=0.8)
    ax.set_title("四维评分雷达对比（0-10 分）", color=_PAL["title"],
                 fontsize=12, fontweight="bold", pad=16)
    ax.legend(frameon=False, labelcolor=_PAL["text"], fontsize=8,
              loc="upper right", bbox_to_anchor=(1.28, 1.10))
    fig.tight_layout()
    return _fig_to_png_bytes(fig)


def fig_compare_scale_n(mets: list, names: list) -> bytes | None:
    """N 家公司**规模对比**（营收 / 归母净利润，绝对额亿元；v10 新增）。

    盈利/风险两张图都是百分比口径，缺一张"公司体量"的绝对值视角：
    30 亿营收净利率 20% 与 3000 亿营收净利率 15%，只看百分比会误判强弱。
    """
    n = len(names)
    if n < 2 or len(mets) != n:
        return None
    rows = []
    for k, lbl in (("revenue", "营业收入（亿元）"), ("net_profit", "归母净利润（亿元）")):
        vals = [m.get(k) if isinstance((m or {}).get(k), (int, float)) else None for m in mets]
        if any(v is not None for v in vals):
            rows.append((lbl, vals))
    if not rows:
        return None
    labels = [r[0] for r in rows]
    x = np.arange(len(rows))
    total_w = 0.5
    w = total_w / n
    fig, ax = plt.subplots(figsize=(max(6.2, 1.5 * n + 4.0), 3.3), facecolor=_PAL["card"])
    ax.set_facecolor(_PAL["card"])
    for i in range(n):
        offs = x - total_w / 2 + w * (i + 0.5)
        vals = [0.0 if rows[j][1][i] is None else float(rows[j][1][i]) for j in range(len(rows))]
        bars = ax.bar(offs, vals, w * 0.9, label=names[i], color=_cmp_series_color(i))
        for b, raw in zip(bars, [rows[j][1][i] for j in range(len(rows))]):
            ax.annotate("N/A" if raw is None else f"{raw:,.1f}",
                        (b.get_x() + b.get_width() / 2, b.get_height()),
                        ha="center", va="bottom", fontsize=7.5, color=_PAL["text"],
                        xytext=(0, 2), textcoords="offset points")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, color=_PAL["text"], fontsize=9)
    ax.grid(axis="y", color="#E2E8F0", linewidth=0.6, alpha=0.8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(axis="y", labelcolor=_PAL["text"], labelsize=9)
    ax.legend(frameon=False, labelcolor=_PAL["text"], fontsize=8,
              ncol=min(n, 3), loc="upper right")
    ax.set_title("公司规模对比（绝对额 · 缺失写 N/A，不画 0）",
                 color=_PAL["title"], fontsize=12, fontweight="bold", pad=10)
    return _fig_to_png_bytes(fig)


def fig_compare_years_n(rows_map: dict, names: list, pipeline,
                        mkey: str, mlabel: str) -> bytes | None:
    """N 家公司**逐年年报对比**分组柱状（v11 新增 · 用户第 12/13/⑧ 条）。

    之前对比只有"单一基准年份"一张快照，用户明确要"每一年的都想看看"。
    这张图 X 轴 = 各家披露过的年报年份并集（升序），每个年份下并排 n 家；
    某家某年没披露 → 柱顶标 N/A，**绝不画 0**（缺数据不能伪装成零营收）。
    年份跨度大时柱会很密：柱宽与画布宽度都按 年份数×家数 自适应。
    """
    n = len(names)
    if n < 2 or pipeline is None:
        return None
    years = sorted({pipeline.year_of(r)
                    for rows in rows_map.values() for r in (rows or [])
                    if pipeline.year_of(r)})
    if not years:
        return None
    x = np.arange(len(years))
    total_w = 0.82
    w = total_w / n
    fig, ax = plt.subplots(figsize=(max(6.2, 0.62 * len(years) * n + 3.2), 3.3),
                           facecolor=_PAL["card"])
    ax.set_facecolor(_PAL["card"])
    _any = False
    for i, _nm in enumerate(names):
        rows = rows_map.get(i) or []
        by_year = {pipeline.year_of(r): r for r in rows if pipeline.year_of(r)}
        offs = x - total_w / 2 + w * (i + 0.5)
        raws = []
        for y in years:
            r = by_year.get(y)
            v = r.get(mkey) if isinstance(r, dict) and isinstance(r.get(mkey), (int, float)) else None
            raws.append(v)
            if v is not None:
                _any = True
        bars = ax.bar(offs, [0.0 if v is None else float(v) for v in raws],
                      w * 0.9, label=_nm, color=_cmp_series_color(i))
        for b, raw in zip(bars, raws):
            ax.annotate("N/A" if raw is None else f"{raw:,.1f}",
                        (b.get_x() + b.get_width() / 2, b.get_height()),
                        ha="center", va="bottom", fontsize=6.5, color=_PAL["text"],
                        xytext=(0, 2), textcoords="offset points", rotation=0)
    if not _any:
        plt.close(fig)
        return None
    ax.set_xticks(x)
    ax.set_xticklabels(years, color=_PAL["text"], fontsize=9)
    ax.grid(axis="y", color="#E2E8F0", linewidth=0.6, alpha=0.8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(axis="y", labelcolor=_PAL["text"], labelsize=9)
    ax.legend(frameon=False, labelcolor=_PAL["text"], fontsize=8,
              ncol=min(n, 3), loc="upper right")
    ax.set_title(f"{mlabel} · 逐年年报对比（缺披露年份标 N/A）",
                 color=_PAL["title"], fontsize=12, fontweight="bold", pad=10)
    return _fig_to_png_bytes(fig)


# ============ 追问区的图表辅助 / 自动对比取数（v10 · 用户第 5、13 条反馈） ============
def _chat_viz_png(kind: str):
    """按 kind 生成追问回答的辅助图表（bytes 或 None）。

    为什么只做两种图：追问场景下用户问的是"营收趋势/哪个维度强"这类
    已有数据能直接回答的问题 —— 趋势图（营收+净利润双轴）和雷达图
    （四维评分形状）覆盖了绝大多数追问；为每轮回答临时编新图反而慢。
    数据一律取自 session 里**已生成的分析结果**，与页面显示同源。
    """
    _a = st.session_state.get("analysis")
    if not isinstance(_a, dict):
        return None
    if kind == "radar":
        _sc = _a.get("scores")
        if isinstance(_sc, dict) and _sc:
            return render_score_radar(_sc, _a.get("total_score"))
        return None
    _trend = _a.get("trend")
    if isinstance(_trend, list) and _trend:
        return render_trend_dual_axis(_trend_chrono(_trend))
    return None


def _chat_viz_kind_for(question: str) -> str:
    q = question or ""
    if re.search(r"评分|得分|雷达|哪项|优势|短板|强在哪|弱在哪|强项|劣势", q):
        return "radar"
    return "trend"


def _chat_auto_compare(question: str):
    """追问里出现"和 XX 对比"时，**自动联网取 XX 的财务数据**注入上下文。

    原来的行为：用户在追问框问"它和万科比怎么样"，模型只能答"我拿不到
    对方数据，请你自己先取"—— 一次很扫兴的拒答（用户第 13 条反馈原话）。
    现在：Agent 自己识别对比意图 → 解析出对方公司 → 联网取数 → 注入上下文，
    仍是"会自己动手的 Agent"，而不是把活儿推回给用户。

    识别方式（宁可少抓、不可错抓）：
      · 必须出现 对比/比较/相比/比一比/PK/vs 等意图词；
      · 对方公司 = 问题里的 6 位代码，或股票池里完整出现的公司名（≥3 字）；
      · 排除当前正在分析的公司本身；单轮最多注入 2 家，防止上下文爆炸。
    返回 (数据块列表, 已获取的公司名列表)。
    """
    q = question or ""
    if not re.search(r"对比|比较|相比|比一比|PK|pk|\bvs\b|VS", q):
        return [], []
    _cache = st.session_state.setdefault("chat_cmp_cache", {})
    _cur = st.session_state.get("single_online_code", "")
    _blocks, _got = [], []

    _cands = []   # [(代码, 名称)]
    for _c in dict.fromkeys(re.findall(r"(?<!\d)[0369]\d{5}(?!\d)", q)):
        if _c != _cur:
            _cands.append((_c, ""))
    for s in POPULAR_STOCKS:
        _nm = s[0].split(" (")[0]
        if s[2] and s[1] != _cur and len(_nm) >= 3 and _nm in q:
            _cands.append((s[1], _nm))

    for _cd, _nm in _cands[:2]:
        if _cd in _cache:
            _blocks.append(_cache[_cd])
            _got.append(_nm or _cd)
            continue
        _res = ds.resolve_stock(_cd)
        if not _res.get("ok"):
            continue
        with get_tracer().span("在线取数", "追问自动取对比公司数据",
                               inp=f"{_res['name']}（{_cd}）") as _s:
            _t = ds.build_summary_text(_cd, name=_res["name"], periods=3)
            if _t:
                _s.out = f"取到 {len(_t)} 字符摘要"
                _cache[_cd] = _t
                _blocks.append(_t)
                _got.append(_res["name"])
    return _blocks, _got


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
    # v11（用户阶段三第 11 条）：年份范围只选 1~2 年时，柱子按默认宽度 0.55
    # 会把整幅图撑满，看着"像把全部都遮住了"。柱宽随柱数自适应：
    # 1 根 0.26（约一半）、2 根 0.40、3 根及以上 0.55 —— 多年对比时依旧饱满。
    _nbar = max(len(labels), 1)
    _bw = 0.26 if _nbar == 1 else (0.40 if _nbar == 2 else 0.55)
    bars = ax.bar(labels, vals, color=colors, width=_bw)
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
def _report_year(date_str: str, report_type: str) -> str:
    """把「公告日期 + 报告类型」换算成**报告期所属年度**。

    这是个真实的口径 bug：年报是在**次年**披露的（2025 年年报在 2026-04 才公告），
    直接取公告日期的年份会得到 2026 —— 标注成"2026 年年报"是错的，
    而且一旦分析侧按 2025 取数、索引侧按 2026 命名，两边就对不上。
    换算规则：年报且公告月份 ≤6 → 上一年；其余（含半年报/季报）→ 公告年。
    """
    _d = re.sub(r"\D", "", date_str or "")
    if len(_d) < 4:
        return ""
    _y = int(_d[:4])
    _m = int(_d[4:6]) if len(_d) >= 6 else 1
    if (report_type or "").strip() == "年报" and _m <= 6:
        _y -= 1
    return str(_y)


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
    # 【Agent 轨迹】这是全站唯一的在线取数落库入口，在这里插桩即可覆盖
    # 单公司页与多公司对比页的全部取数动作（两者共用本函数）。
    with get_tracer().span("在线取数", "巨潮资讯网 + 东方财富公开接口",
                           inp=f"{name or code}（{code}）· 请求 {periods} 期") as _s:
        text = ds.build_summary_text(code, name=name, periods=periods)
        if not text:
            _s.status = "failed"
            _s.out = "接口未返回数据"
            return False, (f"未取到 {name}（{code}）的公开财务数据：可能是网络受限、"
                           "或该证券无可用财报，可改用手动粘贴。")
        _s.out = f"取到财务摘要 {len(text)} 字符"
    st.session_state[f"{key_prefix}_online"] = text
    st.session_state[f"{key_prefix}_online_name"] = name
    st.session_state[f"{key_prefix}_online_code"] = code
    st.session_state[f"{key_prefix}_online_org"] = org or ""
    # 自动匹配行业财务基准库（不直接改已渲染的 industry radio，
    # 而是存进 session_state，由顶部 radio 在下一轮渲染时读取）
    # 【Agent 轨迹 · 决策点】"按公司名猜行业"是 Agent 自己做的判断，
    # 猜中要说明命中了哪套规则，猜不中要如实说退回通用框架 —— 不能假装很确定。
    gi = ds.guess_industry(name)
    if gi:
        st.session_state[f"{key_prefix}_auto_industry"] = gi
        get_tracer().hit("行业识别", "自动匹配财务基准库", inp=f"公司名：{name}",
                         out=f"命中「{gi}」行业规则", kind="decision")
    else:
        get_tracer().hit("行业识别", "自动匹配财务基准库", inp=f"公司名：{name}",
                         out="未命中，回退「通用」四维框架", status="degraded",
                         kind="decision")
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


# ============ 热门股票池（模块级 · v10 扩充并带行业标签） ============
# 三元组：(显示名, 代码, 行业)。行业必须取自 INDUSTRY_KB 的 key
#（通用/消费/金融/新能源/医药/互联网），这样「按行业过滤热门股票」才有依据：
# 用户点了「消费」，下拉框里就不该再出现招商银行 —— 这是第 14 条反馈的原话。
# 挑选原则：沪深 A 股、流动性好、财报披露规范、六个行业各有 6~8 家代表公司。
POPULAR_STOCKS = [
    ("— 手动输入 —", "", ""),
    # ---- 消费（白酒/食品/家电/农牧 · 33 家）----
    ("贵州茅台 (600519)", "600519", "消费"),
    ("五粮液 (000858)", "000858", "消费"),
    ("泸州老窖 (000568)", "000568", "消费"),
    ("山西汾酒 (600809)", "600809", "消费"),
    ("洋河股份 (002304)", "002304", "消费"),
    ("古井贡酒 (000596)", "000596", "消费"),
    ("迎驾贡酒 (603198)", "603198", "消费"),
    ("今世缘 (603369)", "603369", "消费"),
    ("伊利股份 (600887)", "600887", "消费"),
    ("海天味业 (603288)", "603288", "消费"),
    ("中国中免 (601888)", "601888", "消费"),
    ("东鹏饮料 (605499)", "605499", "消费"),
    ("青岛啤酒 (600600)", "600600", "消费"),
    ("双汇发展 (000895)", "000895", "消费"),
    ("金龙鱼 (300998)", "300998", "消费"),
    ("安井食品 (603345)", "603345", "消费"),
    ("绝味食品 (603517)", "603517", "消费"),
    ("涪陵榨菜 (002507)", "002507", "消费"),
    ("安琪酵母 (600298)", "600298", "消费"),
    ("格力电器 (000651)", "000651", "消费"),
    ("美的集团 (000333)", "000333", "消费"),
    ("海尔智家 (600690)", "600690", "消费"),
    ("苏泊尔 (002032)", "002032", "消费"),
    ("老板电器 (002508)", "002508", "消费"),
    ("牧原股份 (002714)", "002714", "消费"),
    ("温氏股份 (300498)", "300498", "消费"),
    ("新希望 (000876)", "000876", "消费"),
    ("百润股份 (002568)", "002568", "消费"),
    # 【v14 · 用户要求再增 20 家】消费 +5
    ("汤臣倍健 (300146)", "300146", "消费"),
    ("洽洽食品 (002557)", "002557", "消费"),
    ("三只松鼠 (300783)", "300783", "消费"),
    ("燕京啤酒 (000729)", "000729", "消费"),
    ("中炬高新 (600872)", "600872", "消费"),
    # ---- 金融（银行/券商/保险 · 27 家）----
    ("招商银行 (600036)", "600036", "金融"),
    ("工商银行 (601398)", "601398", "金融"),
    ("建设银行 (601939)", "601939", "金融"),
    ("农业银行 (601288)", "601288", "金融"),
    ("中国银行 (601988)", "601988", "金融"),
    ("交通银行 (601328)", "601328", "金融"),
    ("邮储银行 (601658)", "601658", "金融"),
    ("平安银行 (000001)", "000001", "金融"),
    ("兴业银行 (601166)", "601166", "金融"),
    ("江苏银行 (600919)", "600919", "金融"),
    ("宁波银行 (002142)", "002142", "金融"),
    ("杭州银行 (600926)", "600926", "金融"),
    ("成都银行 (601838)", "601838", "金融"),
    ("南京银行 (601009)", "601009", "金融"),
    ("中信证券 (600030)", "600030", "金融"),
    ("华泰证券 (601688)", "601688", "金融"),
    ("国泰君安 (601211)", "601211", "金融"),
    ("东方财富 (300059)", "300059", "金融"),
    ("中国平安 (601318)", "601318", "金融"),
    ("中国人寿 (601628)", "601628", "金融"),
    ("新华保险 (601336)", "601336", "金融"),
    ("中国人保 (601319)", "601319", "金融"),
    ("中国太保 (601601)", "601601", "金融"),
    ("同花顺 (300033)", "300033", "金融"),
    # 【v14 · 用户要求再增 20 家】金融 +3
    ("东方证券 (600958)", "600958", "金融"),
    ("光大证券 (601788)", "601788", "金融"),
    ("中信建投 (601066)", "601066", "金融"),
    # ---- 新能源（锂电/光伏/风电/核电/水电 · 26 家）----
    ("宁德时代 (300750)", "300750", "新能源"),
    ("比亚迪 (002594)", "002594", "新能源"),
    ("隆基绿能 (601012)", "601012", "新能源"),
    ("阳光电源 (300274)", "300274", "新能源"),
    ("通威股份 (600438)", "600438", "新能源"),
    ("亿纬锂能 (300014)", "300014", "新能源"),
    ("国轩高科 (002074)", "002074", "新能源"),
    ("欣旺达 (300207)", "300207", "新能源"),
    ("德方纳米 (300769)", "300769", "新能源"),
    ("天合光能 (688599)", "688599", "新能源"),
    ("晶澳科技 (002459)", "002459", "新能源"),
    ("福斯特 (603806)", "603806", "新能源"),
    ("福莱特 (601865)", "601865", "新能源"),
    ("恩捷股份 (002812)", "002812", "新能源"),
    ("璞泰来 (603659)", "603659", "新能源"),
    ("格林美 (002340)", "002340", "新能源"),
    ("金风科技 (002202)", "002202", "新能源"),
    ("明阳智能 (601615)", "601615", "新能源"),
    ("中国核电 (601985)", "601985", "新能源"),
    ("三峡能源 (600905)", "600905", "新能源"),
    ("长江电力 (600900)", "600900", "新能源"),
    ("华能水电 (600025)", "600025", "新能源"),
    # 【v14 · 用户要求再增 20 家】新能源 +4
    ("许继电气 (000400)", "000400", "新能源"),
    ("国电南瑞 (600406)", "600406", "新能源"),
    ("中伟新材 (300919)", "300919", "新能源"),
    ("当升科技 (300073)", "300073", "新能源"),
    # ---- 医药（创新药/器械/中药/CXO · 26 家）----
    ("恒瑞医药 (600276)", "600276", "医药"),
    ("迈瑞医疗 (300760)", "300760", "医药"),
    ("药明康德 (603259)", "603259", "医药"),
    ("爱尔眼科 (300015)", "300015", "医药"),
    ("片仔癀 (600436)", "600436", "医药"),
    ("长春高新 (000661)", "000661", "医药"),
    ("智飞生物 (300122)", "300122", "医药"),
    ("沃森生物 (300142)", "300142", "医药"),
    ("康泰生物 (300601)", "300601", "医药"),
    ("华东医药 (000963)", "000963", "医药"),
    ("通化东宝 (600867)", "600867", "医药"),
    ("云南白药 (000538)", "000538", "医药"),
    ("白云山 (600332)", "600332", "医药"),
    ("复星医药 (600196)", "600196", "医药"),
    ("百济神州 (688235)", "688235", "医药"),
    ("君实生物 (688180)", "688180", "医药"),
    ("联影医疗 (688271)", "688271", "医药"),
    ("惠泰医疗 (688617)", "688617", "医药"),
    ("奕瑞科技 (688301)", "688301", "医药"),
    ("华大智造 (688114)", "688114", "医药"),
    ("凯莱英 (002821)", "002821", "医药"),
    ("泰格医药 (300347)", "300347", "医药"),
    # 【v14 · 用户要求再增 20 家】医药 +4
    ("同仁堂 (600085)", "600085", "医药"),
    ("华润三九 (000999)", "000999", "医药"),
    ("万泰生物 (603392)", "603392", "医药"),
    ("华兰生物 (002007)", "002007", "医药"),
    # ---- 互联网 / 科技（AI/半导体/消费电子 · 30 家）----
    ("海康威视 (002415)", "002415", "互联网"),
    ("科大讯飞 (002230)", "002230", "互联网"),
    ("金山办公 (688111)", "688111", "互联网"),
    ("中际旭创 (300308)", "300308", "互联网"),
    ("新易盛 (300502)", "300502", "互联网"),
    ("天孚通信 (300394)", "300394", "互联网"),
    ("立讯精密 (002475)", "002475", "互联网"),
    ("京东方A (000725)", "000725", "互联网"),
    ("中兴通讯 (000063)", "000063", "互联网"),
    ("紫光股份 (000938)", "000938", "互联网"),
    ("浪潮信息 (000977)", "000977", "互联网"),
    ("中科曙光 (603019)", "603019", "互联网"),
    ("韦尔股份 (603501)", "603501", "互联网"),
    ("兆易创新 (603986)", "603986", "互联网"),
    ("北方华创 (002371)", "002371", "互联网"),
    ("中微公司 (688012)", "688012", "互联网"),
    ("澜起科技 (688008)", "688008", "互联网"),
    ("圣邦股份 (300661)", "300661", "互联网"),
    ("卓胜微 (300782)", "300782", "互联网"),
    ("深南电路 (002916)", "002916", "互联网"),
    ("生益科技 (600183)", "600183", "互联网"),
    ("歌尔股份 (002241)", "002241", "互联网"),
    ("蓝思科技 (300433)", "300433", "互联网"),
    ("工业富联 (601138)", "601138", "互联网"),
    # 【v14 · 用户要求再增 20 家】互联网/科技 +4
    ("石头科技 (688169)", "688169", "互联网"),
    ("传音控股 (688036)", "688036", "互联网"),
    ("中芯国际 (688981)", "688981", "互联网"),
    ("紫光国微 (002049)", "002049", "互联网"),
    ("汇川技术 (300124)", "300124", "互联网"),
    ("大华股份 (002236)", "002236", "互联网"),
    # ---- 通用（能源/化工/基建/交运/装备 · 24 家）----
    ("中国神华 (601088)", "601088", "通用"),
    ("陕西煤业 (601225)", "601225", "通用"),
    ("兖矿能源 (600188)", "600188", "通用"),
    ("中国石油 (601857)", "601857", "通用"),
    ("中国石化 (600028)", "600028", "通用"),
    ("中国海油 (600938)", "600938", "通用"),
    ("万华化学 (600309)", "600309", "通用"),
    ("宝丰能源 (600989)", "600989", "通用"),
    ("华鲁恒升 (600426)", "600426", "通用"),
    ("三一重工 (600031)", "600031", "通用"),
    ("恒立液压 (601100)", "601100", "通用"),
    ("中国中车 (601766)", "601766", "通用"),
    ("中国建筑 (601668)", "601668", "通用"),
    ("中国中铁 (601390)", "601390", "通用"),
    ("福耀玻璃 (600660)", "600660", "通用"),
    ("潍柴动力 (000338)", "000338", "通用"),
    ("顺丰控股 (002352)", "002352", "通用"),
    ("中远海控 (601919)", "601919", "通用"),
    ("上海机场 (600009)", "600009", "通用"),
    ("大秦铁路 (601006)", "601006", "通用"),
    ("京沪高铁 (601816)", "601816", "通用"),
    ("宁沪高速 (600377)", "600377", "通用"),
    ("宝钢股份 (600019)", "600019", "通用"),
]


def _popular_options_for(ind_name: str) -> tuple:
    """按行业上下文过滤热门股票下拉选项。

    返回 (options 列表, 是否真的过滤了)。规则：
      · 行业 = 通用 / 未识别 → 全量展示（不过滤）
      · 行业命中 → 只展示「— 手动输入 —」+ 该行业公司
      · 会话里已选中的旧选项若被过滤掉，**追加回列表末尾**：
        selectbox 有 key 时，session 值不在 options 里会直接抛异常，
        兜底追加既保住过滤语义又保证控件不崩。
    """
    _all = [s[0] for s in POPULAR_STOCKS]
    if not ind_name or ind_name not in INDUSTRY_KB or ind_name == "通用":
        return _all, False
    _picked = [s[0] for s in POPULAR_STOCKS if s[2] == ind_name]
    _opts = ["— 手动输入 —"] + _picked
    return _opts, True


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
    # BUG-02（外部测试报告）：报错后输入框残留旧值，用户接着输入会拼接在一起
    # （自动化实测出现「999999999600000」）。失败路径置 _clear_kw 标志并 rerun，
    # 这里在**控件实例化之前**消费标志清空输入框 —— 控件实例化之后再改它的
    # session key 会抛 StreamlitAPIException，所以必须放在函数最顶部。
    if st.session_state.pop(f"{key_prefix}_clear_kw", False):
        st.session_state[f"{key_prefix}_kw"] = ""
    if compact:
        st.markdown('<div class="mini-head">🌐 在线取数</div>', unsafe_allow_html=True)
    else:
        step_head("3", "在线取数",
                  "从交易所公开披露数据自动获取（行情 · 多期财务指标 · 年报原文）",
                  tag="推荐")
    if compact:
        c1, c3 = st.columns([3, 1])
    else:
        c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        # 热门股票下拉框：不用记代码，直接选；也支持手动输入
        # v10：改为「按行业上下文过滤」—— 顶部行业选了「消费」，
        # 下拉框里就只出消费类公司，不再混着招商银行（第 14 条反馈）。
        # 行业上下文：单公司页 = 模块级 industry；对比页 = 每家面板自己的 cmp*_ind。
        _pop_ind = ""
        if key_prefix.startswith("cmp"):
            _pop_ind = str(st.session_state.get(f"{key_prefix}_ind", "") or "")
        else:
            _pop_ind = str(globals().get("industry", "") or "")
        _pop_opts, _pop_filtered = _popular_options_for(_pop_ind)
        # 旧选中项被过滤掉 → 兜底追加，保证 selectbox 不因值不在 options 里而报错
        _pop_prev = st.session_state.get(f"{key_prefix}_pickle")
        if _pop_prev and _pop_prev not in _pop_opts:
            _pop_opts = _pop_opts + [_pop_prev]
        _POPULAR_STOCKS = POPULAR_STOCKS
        # 1. 热门股票下拉框
        # v11（用户阶段三第 ⑩b 条）：选中热门股票后**立即自动取数**，不再要求
        # 用户"先选下拉 → 再点获取"两步走 —— 用户原话"这两个地方多此一举"。
        # 手动输入框保留（用于股票池外的公司），但热门公司一次点击直接出数据。
        # 多公司对比页受全局开关 cmp_auto_fetch 控制（用户第 ⑤ 条：选一家自动拉一家）。
        def _on_pick():
            _auto_all = key_prefix.startswith("cmp") and not bool(
                st.session_state.get("cmp_auto_fetch"))
            if not _auto_all:
                st.session_state[f"{key_prefix}_auto_fetch"] = True
        _choice = st.selectbox(
            "📋 热门股票（点选无需查代码）",
            options=_pop_opts,
            index=0,
            key=f"{key_prefix}_pickle",
            on_change=_on_pick,
            help="选中后立即自动获取数据；股票池外的公司用下方输入框手动输入。"
                 + (f"当前已按行业「{_pop_ind}」过滤。" if _pop_filtered else ""),
        )
        if _pop_filtered:
            st.caption(f"🔎 已按行业「**{_pop_ind}**」过滤股票池；要选其他行业的公司，直接在下方输入代码/名称即可。")
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
            max_chars=20,   # BUG-03：A 股代码 6 位 / 简称 ≤8 字，20 字上限防超长输入撑破布局
            placeholder="例如：600519 或 贵州茅台",
        )
    periods = 3
    if not compact:
        with c2:
            # v11（用户阶段三第 14 条）：原来最多 8 期 ≈ 只能看到 2 个年度年报，
            # 用户反馈"报告期数只有 8 期太少了，想自己选"。东财接口的 pageSize
            # 本身没有上限 → 放开到 1~24 期（年报口径约 6 个年度）。
            periods = st.selectbox(
                "报告期数",
                list(range(1, 25)),
                index=2,
                key=f"{key_prefix}_periods",
                help="向东方财富拉取「最近 N 个报告期」的财务数据（含一季报/中报/三季报/年报）。"
                     "N=24 时年报口径约可覆盖最近 6 个年度。四维评分、柱状图、圆环图"
                     "一律固定按「最近一个年报」口径计算，与 N 无关。",
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

    # ---- 取数主流程（手动按钮与热门下拉自动触发共用，v11 抽成函数）----
    def _fail_fetch(msg: str) -> None:
        """取数失败的统一出口：清空输入框 + 持久化错误信息 + rerun。
        （BUG-02/03：旧实现只打一条 st.error 就结束，输入框残留旧值，
        用户接着输入会拼接；超长输入还会把 alert 撑破布局。）"""
        st.session_state[f"{key_prefix}_last_err"] = msg
        st.session_state[f"{key_prefix}_clear_kw"] = True
        st.rerun()

    def _fetch_by_kw(kw_val: str, p: int) -> None:
        """【证券解析】把输入变成一个**确定**的目标公司。
        巨潮搜索是模糊/前缀匹配，直接取第一条会把用户没输入过的公司当成分析对象
        （实测：'00000' → 平安银行；'abc' → 一只 REIT）。resolve_stock 的原则是
        「代码必须精确命中；名称多义交用户确认；绝不猜」。"""
        with st.spinner("正在核验证券代码……"):
            _res = ds.resolve_stock(kw_val)
        if _res.get("ok"):
            _panel_clear(key_prefix)   # 取数成功前先清旧结果，绝不让新旧混显
            with st.spinner("正在联网获取数据……"):
                _ok, _err = _panel_store(
                    key_prefix, _res["code"], _res["name"], _res.get("orgId", ""), p
                )
            if not _ok:
                _fail_fetch(_err or "取数失败")
            else:
                st.session_state.pop(f"{key_prefix}_last_err", None)
        elif _res.get("ambiguous"):
            # 多义名称 → 记下候选，交给下方"候选确认"区，绝不自动采用首项
            _panel_clear(key_prefix)
            st.session_state[f"{key_prefix}_cands"] = _res["candidates"]
            st.warning(f"⚠️ {_res.get('error')} 请在下方候选中确认。")
        else:
            # 失败 → 清空该面板全部旧数据，杜绝"查询失败却仍显示上一家公司"
            _panel_clear(key_prefix)
            # BUG-02：报错后清空输入框并 rerun，避免旧输入残留（用户原话场景：
            # 输错 999999999 报错后改输 600000，残值拼接成 999999999600000 仍失败）。
            # 错误信息存 session，rerun 后照常显示（rerun 会冲掉本轮直接打印的 error）。
            _fail_fetch(str(_res.get("error") or "取数失败"))

    _auto_fetch = st.session_state.pop(f"{key_prefix}_auto_fetch", False)
    if _auto_fetch and not do_fetch:
        # v11（⑩b/⑤）：热门下拉选中 → 自动取数。优先用下拉对应的精确代码，
        # 下拉没命中（手动输入 —）时退回输入框内容。
        _auto_kw = _code or (kw or "").strip()
        if _auto_kw:
            _fetch_by_kw(_auto_kw, periods)
        else:
            st.warning("请先在热门股票里选一家，或在输入框填写 6 位代码。")

    if do_fetch:
        _kw = (kw or "").strip()
        if not _kw:
            st.error("请先输入 6 位股票代码或公司名称，再点击「获取数据」。")
        else:
            _fetch_by_kw(_kw, periods)

    # BUG-02：rerun 后在这里回放上一轮失败的错误信息（输入框已被清空，可直接重输）
    _last_err = st.session_state.pop(f"{key_prefix}_last_err", "")
    if _last_err:
        st.error(f"❌ {_last_err}")

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
        if compact:
            # v10-2（用户第 8a 条）：对比页 2~5 家公司各自挤在 1/5 宽的窄卡里，
            # 原来那行 st.success 小字在窄容器里会折成 3~4 行、字号被压到几乎看不清
            # （"贵州茅台下面那行字看不清"）。这里换成自定义高对比块：
            # 公司名单独一行 17.5px 深墨字，状态信息另起一行 14.5px 深绿字，
            # 字号不随容器变窄而缩水，白底浅绿 + 左侧绿条，对比度 8:1 以上。
            _sub = f"财务数据 {len(text)} 字已取到"
            if auto_ind:
                _sub += f" · 建议行业：{auto_ind}"
            st.markdown(
                '<div class="cmp-ok">'
                f'<div class="cmp-ok-name">✅ {_htm(name)}（{_htm(code)}）</div>'
                f'<div class="cmp-ok-sub">{_htm(_sub)}</div>'
                '</div>',
                unsafe_allow_html=True,
            )
        else:
            tip = f"✅ 已获取 **{name}（{code}）** 数据，共 {len(text)} 字"
            if auto_ind:
                tip += f" · 建议行业：**{auto_ind}**"
            st.success(tip)
        # 数据来源完整性：若发生过 https→http 降级，如实说明（评审 7.3）
        # 紧凑模式下不显示：窄卡里再塞一行小字，等于又造一个"看不清"。
        try:
            _insec = ds.insecure_fallbacks()
        except Exception:
            _insec = []
        if _insec and not compact:
            st.caption(
                f"⚠️ 本次取数中有 {len(_insec)} 个接口由 HTTPS 降级为 HTTP（明文连接），"
                "数据来自公开接口，仅供参考；如长期出现请检查网络是否拦截了 HTTPS。"
            )
        if compact:
            # 紧凑模式到此为止：不铺开原始数据、不提供年报解析面板（对比页只需指标）
            return text, name, ""
    elif compact:
        # 没取到数据时也要有"进度"：不然卡片下半截是空的，
        # 用户不知道这家到底取没取（v10-2 · 用户第 8a 条）。
        st.markdown(
            '<div class="cmp-wait"><div class="cmp-wait-t">'
            '⏳ 尚未取数 —— 选好上方股票，再点「⚡ 一键获取全部公司数据」'
            '</div></div>',
            unsafe_allow_html=True,
        )
        return text, name, ""

    # ------------------ 单公司完整模式才走到这里 ------------------
    # 【v12 修复 · 用户阶段四第 ①/③ 条】「查看原始数据 + 年报 PDF 一键解析 + RAG 状态」
    # 这一整块原来被错误地缩进在 `elif compact:` 分支里，后果是：
    #   · 单公司页（非 compact）**永远渲染不出**「📄 一键获取并解析年报 PDF」面板；
    #   · 年报向量索引因此无法建立 → 右上角永远显示「页码引用未启用」；
    #   · 面板反而只在"对比页且未取数"这个根本用不了的场合出现。
    # 现在提升到函数级：单公司页取数后即可一键下载解析年报、建立向量索引。
    with st.expander("查看获取到的原始数据", expanded=False):
        st.markdown(text)

    # 年报 PDF 一键获取：默认展开，且解析成功后保持展开
    _rag_ok = bool(st.session_state.get(f"{key_prefix}_rag_status", {}).get("ok"))
    # 索引还没建 → 默认展开这个面板，把"要页码引用就先解析年报"变成显式引导，
    # 而不是让评委点完「开始分析」才发现结论里没有引用（优化清单：RAG 不是开箱即用）
    with st.expander(
        "📄 一键获取并解析年报 PDF → 启用「见年报第X页」页码引用（约 40-80 秒）",
        expanded=not _rag_ok,
    ):
        if not code:
            st.info("请先在上方取数成功，这里才能下载并解析年报 PDF。")
        else:
            rtype = st.radio("报告类型", ["年报", "半年报", "一季报", "三季报"], horizontal=True, key=f"{key_prefix}_rtype")

            # ---- 下载 → 解析 → 建索引（一键自动 / 手动选报告两条路径共用）----
            def _build_rag_from_report(_r: dict, _rtype: str):
                """下载年报 → 解析 → 建立页码索引，返回 (ok, 说明文案)。

                【v13】以前「下载解析」和「建索引」是两段割裂的代码，且建索引被
                `if rag is not None` 挡着 —— rag 为 None 时（chromadb 缺失）
                点了就只是"已解析 XX 字"，索引永远不会建，页面永远提示"未建立"。
                现在合并成一条必走链路，且**任何一步失败都返回可见原因**。
                """
                if rag is None:
                    return False, "RAG 模块不可用（rag_engine 与 rag_lite 均未能加载）"
                with st.spinner(f"正在下载《{_r.get('title', '')}》（大文件约 10-30 秒）……"):
                    try:
                        pdf = ds.download_pdf(_r["url"])
                    except Exception as _e:
                        pdf = None
                if not pdf:
                    return False, "PDF 下载失败（可能是网络或公告链接失效），请稍后重试或改用手动上传。"
                ptxt = extract_pdf_bytes(pdf)
                if not ptxt or len(ptxt) < 200:
                    return False, "该 PDF 提取不到文字（疑似扫描件/图片型），页码引用无法建立。"

                # ⚠️ 用**报告期所属年度**而非公告年份：2025 年年报是 2026-04 公告的，
                # 取 [:4] 会得到 2026（错）。详见 _report_year()。
                _yr = _report_year(_r.get("date") or "", _rtype)
                # 文档标签：公告日期 + 报告类型。用于区分"同一年披露的年报/半年报/修订版"，
                # 否则它们会共用同一份索引，检索到的是另一份报告的原文与页码。
                try:
                    _tag = rag.doc_tag_for(_r.get("date"), _rtype)
                except Exception:
                    _tag = ""
                _pdf_path = Path(tempfile.gettempdir()) / f"{code}_{_yr}_{_rtype}.pdf"
                try:
                    _pdf_path.write_bytes(pdf)
                except Exception:
                    return False, "PDF 保存到临时目录失败（磁盘不可写）。"

                st.session_state[f"{key_prefix}_pdftext"] = ptxt
                st.session_state[f"{key_prefix}_pdf_title"] = _r.get("title", "")
                st.session_state[f"{key_prefix}_pdf_year"] = _yr
                st.session_state[f"{key_prefix}_pdf_tag"] = _tag
                st.session_state[f"{key_prefix}_pdf_path"] = str(_pdf_path)
                st.session_state.pop(f"{key_prefix}_rag_status", None)

                if not code or not _yr:
                    return False, f"无法从公告日期解析出年报年份（date={_r.get('date')}）。"

                # 【RAG 接通关键步】立即按页切片并建立索引
                # ⚠️ 不再依赖"双模型润色"开关：那个开关只决定要不要花钱调千问润色；
                #    证据检索是**分析链路的一部分**，跟着开关走会出现入口不一致。
                import time as _time
                _t0 = _time.time()
                _pbar = st.progress(0.0, text="正在建立年报页码索引……")

                def _pcb(done: int, total: int):
                    _pbar.progress(
                        min(done / max(total, 1), 1.0),
                        text=f"正在建立年报页码索引… 批次 {done}/{total}"
                             f"（完整年报约需 40-80 秒，请勿关闭页面）",
                    )

                try:
                    _st = rag.build_index_from_pdf(
                        _pdf_path, code, _yr, progress_cb=_pcb, doc_tag=_tag
                    )
                except Exception as _e:
                    _st = {"ok": False, "chunks": 0, "pages": 0,
                           "msg": f"{type(_e).__name__}: {_e}"}
                _pbar.empty()
                log_event(
                    "rag_build", ok=bool(_st.get("ok")),
                    dur=_time.time() - _t0,
                    extra=f"chunks={_st.get('chunks', 0)};backend={_st.get('backend', '?')}",
                )
                st.session_state[f"{key_prefix}_rag_status"] = _st
                if _st.get("ok"):
                    _be = "语义向量" if _st.get("backend") == "chroma" else "本地页级"
                    return True, (
                        f"✅ 已解析《{_r.get('title', '')}》并建立**{_be}索引**："
                        f"{_st.get('chunks', 0)} 个文本块 · 覆盖 {_st.get('pages', 0)} 页；"
                        f"后续分析 / 研报 / 追问将自动检索原文并标注「见年报第X页」。"
                        + (f"（{_st.get('msg', '')}）" if "⚠️" in str(_st.get("msg", "")) else "")
                    )
                return False, f"索引未建立：{_st.get('msg') or '未知原因'}"

            # ---- ⚡ 一键自动建立：无需手动选报告，自动取最新一份完整年报 ----
            # 用户的原话是"你没有给我建立年报索引" —— 他要的是系统自己建，
            # 而不是让他去点「获取报告列表 → 选报告 → 下载并解析」三步。
            _auto_key = f"{key_prefix}_rag_auto"
            _tried_key = f"{key_prefix}_rag_autotried"
            _c1, _c2 = st.columns([1.25, 1])
            with _c1:
                _auto_btn = st.button(
                    "⚡ 一键自动建立页码索引（自动取最新年报）",
                    key=f"{key_prefix}_autobtn", type="primary", use_container_width=True,
                )
            with _c2:
                st.toggle(
                    "🚀 取数成功后自动建立",
                    key=_auto_key,
                    value=bool(st.session_state.get(_auto_key, True)),
                    help="开启后，每次取数成功会自动下载并解析一份最新年报建立页码索引（约 40-80 秒）。"
                         "关闭则只在手动点击时才建立。",
                )

            # 自动触发条件：开关开 + 当前确实没索引 + 这家公司还没尝试过（避免每轮 rerun 都卡 40 秒）
            _need_auto = (
                bool(st.session_state.get(_auto_key, True))
                and not _rag_ok
                and st.session_state.get(_tried_key) != code
            )
            if _auto_btn or _need_auto:
                st.session_state[_tried_key] = code      # 先记账，失败也不再反复重试
                with st.spinner("正在查找该公司最新年报公告……"):
                    try:
                        _auto_reports = ds.list_reports(code, org, rtype, limit=3)
                    except Exception:
                        _auto_reports = []
                if not _auto_reports:
                    st.warning(
                        f"未找到「{rtype}」类型的完整版报告。可换一个报告类型，"
                        f"或改用下方手动选择 / 手动上传 PDF。"
                    )
                else:
                    _ok, _msg = _build_rag_from_report(_auto_reports[0], rtype)
                    if _ok:
                        st.success(_msg)
                    else:
                        st.warning(_msg)
                    st.session_state[f"{key_prefix}_reports"] = _auto_reports

            st.divider()
            st.caption("—— 或手动指定某一份报告 ——")
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
                    _ok, _msg = _build_rag_from_report(reports[idx], rtype)
                    if _ok:
                        st.success(_msg)
                    else:
                        st.error(_msg)

    # RAG 状态提示：明确告诉使用者"现在有没有页码引用能力"
    # （重新读一次：向量索引可能就在本轮刚构建完成，不能用构建前的旧值）
    _rag_ok = bool(st.session_state.get(f"{key_prefix}_rag_status", {}).get("ok"))
    if _rag_ok:
        _st_ = st.session_state.get(f"{key_prefix}_rag_status", {})
        _be_ = "语义向量" if _st_.get("backend") == "chroma" else "本地页级"
        st.caption(
            f"📚 **年报索引已就绪（{_be_}）**：{_st_.get('chunks', 0)} 个文本块 · "
            f"覆盖 {_st_.get('pages', 0)} 页 · 分析/研报/追问将按问题检索相关章节"
            f"并标注「见年报第X页」。"
        )
    else:
        _be_now = ""
        try:
            _be_now = rag.backend_label() if rag is not None else ""
        except Exception:
            _be_now = ""
        _why = (
            f"（当前检索后端：{_be_now}）" if _be_now else
            "（⚠️ RAG 模块未加载：rag_engine 与 rag_lite 均不可用，页码引用功能不可用）"
        )
        st.caption(
            f"ℹ️ **当前未建立年报索引** {_why}—— 分析结果基于取数文本（含年报开头部分），"
            "**不含「见年报第X页」式页码引用**。点开上方面板的"
            "「⚡ 一键自动建立页码索引」即可自动下载解析最新年报并建立索引"
            "（约 40-80 秒），建好后这里会自动变成「年报索引已就绪」。"
        )

    pdftext = st.session_state.get(f"{key_prefix}_pdftext", "")
    return text, name, pdftext

# ============ 紧凑顶栏 + 侧边栏 ============
# 原来的深蓝大横幅（三行、近 100px 高）换成单行顶栏：
# 品牌 + 当前分析对象 + 状态徽章。信息一样，纵向空间还给工作台。
#
# 【v12 修 · 外部测试报告 BUG-06】顶栏原来只在模块级渲染一次，而「当前对象 /
# 页码引用」这些 session 值要等到文件末尾的视图函数里（取数面板 / RAG 建库）
# 才会被更新 —— 于是取数成功后顶栏仍显示「尚未选择 / 未启用」，滞后一整轮。
# 修法：顶栏画进 st.empty() 槽位，脚本**末尾**（视图全部执行完）按最新
# session_state 重画一次。st.empty() 覆盖写入不改变位置，视觉仍在页面最上方。
_topbar_slot = st.empty()


def _topbar_html() -> str:
    """按当前 session_state 生成顶栏 HTML（渲染两次：占位 + 末尾刷新）。"""
    _tb_name = st.session_state.get("single_online_name", "")
    _tb_code = st.session_state.get("single_online_code", "")
    _tb_obj = (
        f"当前对象：<b>{_tb_name}（{_tb_code}）</b>"
        if _tb_name and _tb_code else "当前对象：<b>尚未选择</b>"
    )
    _tb_ragst = st.session_state.get("single_rag_status") or {}
    _tb_rag_ok = bool(isinstance(_tb_ragst, dict) and _tb_ragst.get("ok"))
    # v11（用户阶段三第 ⑨ 条）：右上角"未建年报索引"让用户困惑"这是什么问题"。
    # ① 文案改成人话：它不是报错，只是"还没有解析过年报 PDF，所以没有页码引用"；
    # ② 悬停出完整解释（HTML title），把"影响什么、怎么启用"一次说清。
    _tb_rag_txt = "📚 页码引用已启用" if _tb_rag_ok else "📚 页码引用未启用（非报错）"
    _tb_rag_tip = (
        "年报向量索引已建立：分析/研报/追问会检索年报原文，结论标注「见年报第X页」。"
        if _tb_rag_ok else
        "这是什么：还没有解析过年报 PDF，所以无法给出「见年报第X页」式页码引用 —— "
        "不是报错，分析照常可用（基于在线取数的财务文本）。"
        "想启用：在单公司「🌐 在线取数」面板里点「⚡ 一键自动建立页码索引」"
        "（自动取最新年报，约 40-80 秒）；或勾选「🚀 取数成功后自动建立」，"
        "以后每次取数会自动建好，这个徽章会自动变化。"
    )
    _tb_who = "🎤 演示模式" if st.session_state.get("demo_mode") else "🔒 已登录"
    return (
        '<div class="topbar">'
        '<span class="tb-logo">AI</span>'
        '<span class="tb-brand">金融智能投研 <b>Agent</b></span>'
        '<span class="tb-gap"></span>'
        f'<span class="tb-obj">{_tb_obj}</span>'
        f'<span class="tb-chip">{_tb_who}</span>'
        f'<span class="tb-chip{" warn" if not _tb_rag_ok else ""}" title="{_tb_rag_tip}">{_tb_rag_txt}</span>'
        "</div>"
    )


_topbar_slot.markdown(_topbar_html(), unsafe_allow_html=True)

# ---------------------------------------------------------------
# 侧边栏导航（图标 + 文字）· 分析记录
#
# 为什么搬到侧边栏：导航与「分析记录」是**每一轮都要用的工具**，
# 常驻在最左侧的侧边栏里比在主区占两列更省地方 —— 主区的横向与纵向
# 都还给工作台，且侧边栏本来就有「常驻工具区」的语义。
# 同时把纯图标换成「图标 + 文字」：只看 🏢 / ⚖️ / 📖 三个图形，
# 没人知道哪个是「多公司对比」。
#
# ⚠️ 这两个函数必须定义在 `with st.sidebar:` **之前**：
#    Streamlit 是按代码**执行顺序**往容器里追加元素的，
#    把定义留在文件末尾、在模块级直接调用 → 先执行调用再定义 → NameError。
# ---------------------------------------------------------------
_NAV_ITEMS = [
    ("🏢", "单公司深度分析"),
    ("⚖️", "多公司对比"),
    ("📖", "使用说明"),
]


# ---- 分析记录本地持久化（v10 · 用户第 8 条反馈：刷新就被清空） ----
# 方案：写本地 JSON + 2 小时 TTL。
#   · 刷新页面 / 重开浏览器 → session_state 丢了，但文件还在 → 下次渲染时自动恢复；
#   · 2 小时后过期 → 不会无限堆积，也不会把"三天前的旧分数"当最新结果展示；
#   · 只存分析结果 dict（数字/字符串），不存财报原文，文件体量在几十 KB 量级；
#   · 任何读写失败都静默降级回"仅会话内存"模式，绝不让侧栏崩掉。
_HIST_TTL_SEC = 2 * 3600
# 路径可用环境变量 ANALYSIS_HISTORY_CACHE 覆盖（回归测试用它指向临时文件，
# 避免测试数据与真实记录互相污染；部署环境不设即用默认路径）
_HIST_FILE = Path(_os.environ.get("ANALYSIS_HISTORY_CACHE")
                  or ".streamlit/_analysis_history_cache.json")


def _hist_load() -> list:
    try:
        if not _HIST_FILE.exists():
            return []
        data = json.loads(_HIST_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        import time as _time
        _now = _time.time()
        return [h for h in data
                if isinstance(h, dict)
                and isinstance(h.get("_ts"), (int, float))
                and _now - h["_ts"] <= _HIST_TTL_SEC]
    except Exception:
        return []


def _hist_save(lst: list) -> None:
    try:
        import time as _time
        _now = _time.time()
        _keep = [h for h in lst
                 if isinstance(h, dict)
                 and isinstance(h.get("_ts"), (int, float))
                 and _now - h["_ts"] <= _HIST_TTL_SEC][-20:]
        _HIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HIST_FILE.write_text(
            json.dumps(_keep, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except Exception:
        pass


def _render_icon_menu() -> None:
    """侧边栏导航菜单（图标 + 文字）。点击切换右侧工作台视图。

    选中态怎么表达 —— 这是 v7 修掉的一个**真隐患**：
      v6 用 `type="primary"` 表达"当前选中"，CSS 里靠 `button[kind="primary"]`
      把它刷成"白底 + 深蓝字"。但 1.57 前端的按钮由 styled-components v6 渲染，
      它**不保证**把 `kind` 这种非标准属性写到 DOM 上 —— 一旦这条选择器整条失效，
      侧栏上就只剩"浅色字压深蓝底"（虽然还看得清，但**看不出哪个是选中的**），
      而如果只失效一半（底色规则没了、文字规则还在）就会更糟。
    修法：给选中项换一个**应用自己控制的 key**（`nav_on_*` / `nav_off_*`），
      前端会给带 key 的元素加 `st-key-<key>` 类，CSS 直接钩这个类 ——
      不依赖任何 Streamlit 内部属性，"白底 + 深蓝字"写在同一条规则组里，
      结构与配色的来源是同一个，不可能只生效一半。
    """
    _cur = st.session_state.get("main_view", "🏢")
    for _icon, _label in _NAV_ITEMS:
        _on = (_icon == _cur)
        # key 里带 on/off：key 一变就是"另一个控件"，Streamlit 不会复用旧状态
        # （按钮本来就是无状态的，这样改没有任何副作用）
        if st.button(
            f"{_icon} {_label}",
            key=f"nav_{'on' if _on else 'off'}_{_icon}",
            use_container_width=True,
            type="primary" if _on else "secondary",
        ):
            st.session_state["main_view"] = _icon
            st.rerun()


def _render_history_list() -> None:
    """侧边栏：分析记录（v10 起**本地持久化 2 小时**）。

    ⚠️ v10 之前只存 session_state，刷新页面即丢 —— 用户反馈"刷新就没了"。
    现在写入本地 JSON 缓存（`.streamlit/_analysis_history_cache.json`），
    带 2 小时 TTL：刷新 / 重开会话都能恢复；超过 2 小时自动过期，
    不会无限堆积旧数据，也不依赖任何外部存储（Streamlit Cloud 同样可用）。
    """
    st.markdown('<div class="hist-head">🕘 分析记录</div>', unsafe_allow_html=True)
    _hist = st.session_state.get("analysis_history")
    if not _hist:
        _hist = _hist_load()
        if _hist:
            st.session_state["analysis_history"] = _hist
    if not _hist:
        st.caption("还没有分析记录。完成一次「🚀 开始结构化分析」后，这里会留下一条，可点回查看。")
        st.markdown(
            # v11（用户阶段三第 ⑩ 条）：深蓝侧栏上红字 #FF9D9D 实测看不清，
            # 用户点名"换成加粗加厚的白色或黄色" → 琥珀黄 15.5px/800 + 深色文字底片。
            '<div style="background:rgba(67,44,6,.55);border-left:4px solid #FFC53D;'
            'border-radius:10px;padding:9px 12px;margin:6px 0;">'
            '<div style="color:#FFE066 !important;font-size:15.5px;font-weight:800;'
            'line-height:1.7;">⚠️ 记录保留 2 小时：刷新页面不丢；超过 2 小时自动清除。</div></div>',
            unsafe_allow_html=True,
        )
        return
    st.caption(f"共 {len(_hist)} 条 · 本地保留 2 小时（刷新不丢）")
    # 定高容器 → 记录多了这一段自己滚动，不把侧边栏撑得老长。
    # v6 加了 key：侧栏改深蓝后，这个凹槽要单独刷成"深底半透明槽 + 浅色描边"
    # （没有 key 就没有 `st-key-*` 类名可挂钩，样式只能靠猜 testid —— 而 1.57 已经没有
    #  旧的 stVerticalBlockBorderWrapper 了，见 MEMORY 设计约定）。
    with st.container(height=210, key="khist-scroll"):
        for _i in range(len(_hist) - 1, -1, -1):
            _h = _hist[_i]
            _on = st.session_state.get("hist_active") == _h.get("id")
            _mark = "🔵 " if _on else ""
            if st.button(
                f"{_mark}{_h.get('name') or '—'} · {_h.get('code') or '—'}",
                key=f"hist_btn_{_i}",
                help="回看这条分析结果（一键还原当时的取数数据、评分、研报与追问对话，无需重新分析）",
                use_container_width=True,
            ):
                # v11（用户阶段三第 ④ 条）：回看 = 整页恢复到当时的状态。
                # 关键是把 data_sig 置 None：主区的"数据版本变化清空"逻辑见到
                # _sig_old 为 None 会直接跳过 —— 否则恢复宁德时代的取数文本后，
                # 指纹与当前（比亚迪）不一致，刚恢复的 analysis 会被立刻清掉。
                _snap = _h.get("snapshot") or {}
                st.session_state["analysis"] = _h.get("analysis")
                if _snap:
                    st.session_state["single_src"] = "🌐 在线自动取数（推荐）"
                    st.session_state["single_online"] = _snap.get("online", "")
                    st.session_state["single_online_code"] = _snap.get("code", "")
                    st.session_state["single_online_name"] = _snap.get("name", "")
                    st.session_state["single_online_org"] = _snap.get("org", "")
                    st.session_state["single_periods"] = _snap.get("periods", 3)
                    st.session_state["data_sig"] = None       # 见上：跳过当轮清空
                    st.session_state["messages"] = _snap.get("messages") or []
                    if _snap.get("report"):
                        st.session_state["report"] = _snap["report"]
                    else:
                        st.session_state.pop("report", None)
                st.session_state["hist_active"] = _h.get("id")
                st.rerun()
            st.caption(
                f"{_h.get('ts', '')} · {_h.get('industry', '')} · 综合 {_h.get('total', '—')}"
            )


with st.sidebar:
    # 品牌头（金色描边卡片，替代裸标题）
    st.markdown(
        '<div class="sb-brand">📊 金融智能投研 Agent'
        '<span class="sb-sub">规则评分引擎 · RAG 按页检索 · 双模型协同</span></div>',
        unsafe_allow_html=True,
    )

    # ---- 工作台导航 + 分析记录（原来在主区占两列，见文件末尾的说明）----
    st.markdown(
        '<div class="sb-title" style="margin:10px 0 2px 2px;">🧭 工作台导航</div>',
        unsafe_allow_html=True,
    )
    _render_icon_menu()
    _render_history_list()
    st.markdown("---")

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
            '<div style="font-size:12.5px;line-height:1.75;color:#78350F !important;">'
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

    # ---- v7：侧栏底部科技装饰（纯 CSS，解决"下半截一片纯深蓝太空旷"）----
    # 三圈同心圆 + 虚线刻度环 + 一条渐变扫描线，全部 pointer-events:none，
    # 不占布局高度（absolute 定位），也不会挡住任何控件与文字。
    st.markdown(
        '<div class="sb-deco" aria-hidden="true"><i></i><i></i><i></i><b></b></div>',
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

# ⚠️ 视图分发写在文件末尾，但这里必须**提前**知道当前视图：
#    「单公司行业基准库」整块（行业按钮 + 金融子行业口径 + 同业对标基准条）
#    只属于「🏢 单公司深度分析」。v3 把它写在模块级 → 切到「⚖️ 多公司对比」
#    和「📖 使用说明」时，页面顶部还顶着一个"请选择待分析公司所属行业"的
#    单公司选择器 + 基准条，是"主界面看着乱"最直接的一个原因。
_cur_view = st.session_state.get("main_view", "🏢")

# 如果已经在线取数，标题直接显示公司名称+代码+匹配行业
_single_name = st.session_state.get("single_online_name", "")
_single_code = st.session_state.get("single_online_code", "")

# ---------------------------------------------------------------
# 单公司视图的「页面大标题 + 两行网格」必须在这里（模块级）先建立
#
# 为什么要抢在 _view_single() 之前：
#   Streamlit 是按**执行顺序**往容器里追加元素的。第 1 步「行业财务基准库」
#   是在模块级渲染的（它决定全站评分口径，多公司对比还要复用 industry 变量），
#   而 _view_single() 要到文件末尾才被调用 —— 于是真实渲染顺序变成
#   「顶栏 → 卡 1 → 大标题『单公司深度分析』→ 卡 2…」，
#   标题被第一张卡压在下面，整页看上去仍然是"一堆卡片摊着"（用户：布局没变）。
#   把标题与网格提到这里，顺序才对：顶栏 → 大标题 → 两行卡片 → 结果区 → 追问区。
#
# v7 为什么从"三栏"改成"两行"（用户："他什么行业财务基准库在线取数……这些地方，
# 是不是排版不合理啊，感觉从左往右乱乱的"）：
#   三栏版的实际读序是 左列(①②) / 中列(③④) / 右列(⑤) —— 编号确实是从左到右，
#   但每列卡片张数与高度都不一样，第 ④ 张卡会"掉到 ⑤ 的左边偏下"，
#   眼睛扫过去就是"忽高忽低、不知道下一张在哪"。
# v10 为什么从"两行网格"改成"一行一个"（用户："直接一横一横来，
#      不要从左到右再从左到右"）：
#   两行网格的读序是 ① ② ③ / ④ ⑤，但每列卡片高度不同、内部又各自分栏，
#   视线要"横跳 + 换行"才能读完；用户要的是最朴素的纵向流：
#   ① → ② → ③ → ④ → ⑤，每张卡占满整行，从上到下一口气读完。
#   实现：把 st.columns 换成三个**顺序创建的纵向容器**——Streamlit 按
#   创建顺序排版，卡片挂进哪个容器就落在哪一行（原 columns 的视觉先后
#   语义不变，只是从"横排"变"竖排"，所以 card(col=...) 的调用点一行都不用动）。
# ⚠️ 第 2 行的两个容器仍然必须在 ③ 渲染完之后创建（在 _view_single 里），
#    否则会插到 ③ 上边去。见 _view_single 里 `_single_grid_r2`。
# ⚠️ 追问区（st.chat_input）**不进这些容器**：一旦进了容器，父容器就从
#    "页面根层"变成嵌套层，Streamlit 会把它从"钉视口底部"降级成内联渲染
#    （见 chat.py 判定）。
# ---------------------------------------------------------------
_single_grid = None        # ① ② ③（模块级建，纵向三连容器）
_single_grid_r2 = None     # ④ ⑤（在 _view_single 里、③ 渲染完之后创建）
if _cur_view == "🏢":
    page_head(
        "单公司深度分析",
        "输入一家上市公司 → 在线取数（行情 / 多期财务指标 / 年报原文）→ 规则引擎四维评分 → "
        "年报按页引用 → 一键生成研报。五个步骤自上而下逐行操作即可。",
    )
    _single_grid = [
        st.container(key="sg-r1"),
        st.container(key="sg-r2"),
        st.container(key="sg-r3"),
    ]

if _cur_view == "🏢":
    # 第 1 步：行业财务基准库（整块现在住在一张白卡里 —— 卡片表头就是原来的步骤条）
    _kb_note = (
        f"{_single_name}（{_single_code}）→ 已按该行业基准计算"
        if (_single_name and _single_code)
        else "四维评分、四象限定位与散点基准线都按所选行业计算"
    )
    with card("kb", "1", "行业财务基准库", _kb_note, tag="同业对标",
              col=_single_grid[0]):

        def _on_industry_change():
            """用户手动点击才记录为「手动选择」；自动跟随写入的值不算手动。"""
            _v = st.session_state.get("industry_widget", "")
            if _v != st.session_state.get("single_auto_industry"):
                st.session_state["industry_manual"] = _v

        industry = st.radio(
            "行业",
            list(INDUSTRY_KB.keys()),
            # 【v12 · BUG-01 加固】key 一旦存在，index 在二次渲染起就会被忽略；
            # 两者混用在不同 Streamlit 版本上还可能触发"控件锁死"（外部测试报告
            # 复现的"点行业没反应"）。这里去掉 index，默认值完全由上方
            # session_state["industry_widget"] 初始化逻辑决定 —— 这是官方推荐的
            # 唯一写法，任何版本下行为一致。
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
                    f"⚠️ 自动识别到的行业是 **{_auto_matched}**，当前却是 **{industry}**，"
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
            f'切换上方行业按钮，全部基准随之调整。</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
else:
    # 非单公司视图：不渲染行业选择器与基准条（它们只对单公司分析有意义），
    # 沿用上次选择的行业作为「多公司对比」的默认口径。
    industry = st.session_state.get("industry_widget", "通用")
    if industry not in INDUSTRY_KB:
        industry = "通用"
    if industry != "金融":
        st.session_state["single_finance_kind"] = ""

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
    # ⚠️ 页面大标题放在模块级（本函数之外）渲染：第 1 步「行业财务基准库」在模块级
    #    先执行，标题写在这里会落到第一张卡**下面**（详见模块级的说明）。
    #    这里只负责 ②~⑤ 的卡片，全部挂进模块级 / 本函数内建好的**两行网格**：
    #      第 1 行 _single_grid    （模块级建）：① ② ③
    #      第 2 行 _single_grid_r2 （本函数内、③ 之后建）：④ ⑤

    # ---- 数据来源选择（第 1 行第 2 栏） ----
    with card("src", "2", "数据来源",
              "在线取数已包含实时行情、多期财务指标与年报检索片段",
              tag="二选一", col=_single_grid[1]):
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
        # 第 3 步的卡片表头由 panel 内部的 step_head("3", ...) 渲染，
        # 这里只开卡片、不再传 title —— 否则一张卡上会出现两条表头。
        with card("fetch", col=_single_grid[2]):
            online_text, online_name, online_pdftext = online_fetch_panel("single")
    else:
        with card("manual", "3", "手动输入 / 上传",
                  "可粘贴财报摘要，或上传 PDF / txt / md（上传的年报会按页进入检索库）",
                  tag="二选一", col=_single_grid[2]):
            # ⚠️ 这一栏只有主区的 ~1/3 宽 —— 再分成左右两栏会把输入框挤成
            #    ~180px，所以两个控件改成上下堆叠。
            data_text = st.text_area(
                "① 粘贴财报摘要（营收、净利润、毛利率、负债等）",
                height=200,
                key="single_text",
            )
            up = st.file_uploader("② 或上传财报文件（支持 PDF / txt / md）", type=["pdf", "txt", "md"], key="single_upload")
            uploaded_text, upload_err = read_upload(up)
            if upload_err:
                st.warning(upload_err)

    # ---- ④ ⑤ 纵向容器（v10：一行一个板块）----
    # ⚠️ 必须**在这里**创建：容器的创建顺序就是显示顺序。提前建会让
    #    ④ ⑤ 跑到 ③ 的上边去；而 ③ 又在 if/else 两个分支里，
    #    所以这两个容器只能落在"两分支都结束之后"。
    _single_grid_r2 = [st.container(key="sg-r4"), st.container(key="sg-r5")]

    if src_mode.startswith("🌐"):
        # 【补充材料 · 三选一】原来这里直接摊开一块大输入框，看起来像"必须手填"。
        # 在线取数其实已经把行情、多期财务指标、年报检索全部接进来了，手填多数时候是多余的；
        # 但"系统拿不到的材料"（业绩说明会纪要、券商研报结论、自有 Excel）又确实需要入口。
        # 于是改成可选：默认不追加；需要时一键用在线数据生成摘要（完全不用打字）；
        # 只有真要粘第三方材料时，才落到手写框。
        with card("extra", "4", "补充材料",
                  "在线取数已够用，仅在需要第三方材料时追加",
                  tag="可选", col=_single_grid_r2[0]):
            _extra_mode = st.radio(
                "补充材料方式",
                ["🟢 不追加（推荐）", "⚡ 一键用在线数据生成摘要", "✍️ 手动粘贴自定义内容"],
                horizontal=True,
                label_visibility="collapsed",
                key="single_extra_mode",
                help="在线取数已自动包含：实时行情、多期财务指标、年报检索片段。"
                     "只有当你还想补充自动取数拿不到的材料时，才需要在这里追加。",
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

    # ⑤ 运行分析落哪一栏：
    #   在线模式 → 第 2 行第 2 栏（④ 补充材料在左、⑤ 在右，Z 字读序）
    #   手动模式 → 没有 ④，⑤ 直接占第 2 行左侧那一栏（宽 2/3），
    #              否则会出现在"右侧 1/3、左边一大片空白"的怪位置。
    _run_col = _single_grid_r2[1] if src_mode.startswith("🌐") else _single_grid_r2[0]
    with card("run", "5", "运行分析",
              "规则引擎固定评分 + 千问润色 + 年报按页引用",
              tag="出结果", col=_run_col):
        # ⚠️ 这一栏只有 1/3 宽（在线模式），两个按钮并排会各自挤到 ~180px、
        #    按钮文案会折行，所以改成上下两个整宽按钮（主操作在上）。
        run_btn = st.button("🚀 开始结构化分析", disabled=not raw_data, use_container_width=True, type="primary")
        st.caption("先看分数：四维评分、图表、风险与亮点（结果按四个页签展开）。")
        report_btn = st.button("📝 生成完整研报", disabled=not raw_data, use_container_width=True)
        st.caption("直接出稿：把上面的结果整理成一份可下载的 Markdown 研报。")

        if not raw_data:
            # v7：未取数时不再只丢一行 info —— 这里放一块科技风空状态插图，
            # 把"接下来会发生什么"画出来（同心环 + 六边形 + 折线），
            # 解决"卡片里空空如也、整页看着太空"。（用户明确要求"多放点图形"）
            st.markdown(
                '<div class="empty-art">'
                '<svg width="150" height="84" viewBox="0 0 150 84" fill="none" '
                'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
                '<circle cx="75" cy="42" r="38" stroke="#D5E3F7" stroke-width="1" stroke-dasharray="4 6"/>'
                '<circle cx="75" cy="42" r="25" stroke="#E1EBF8" stroke-width="1"/>'
                '<path d="M75 18 L94 29 L94 55 L75 66 L56 55 L56 29 Z" stroke="#93B4E8" stroke-width="1.4"/>'
                '<polyline points="60 52 68 41 75 46 84 32 92 38" stroke="#1D4ED8" stroke-width="2" '
                'stroke-linecap="round" stroke-linejoin="round" opacity="0.7"/>'
                '<circle cx="68" cy="41" r="2.4" fill="#38BDF8"/>'
                '<circle cx="84" cy="32" r="2.4" fill="#38BDF8"/>'
                '<circle cx="75" cy="66" r="2" fill="#C3D7F2"/>'
                '</svg>'
                '<div class="ea-title">等待数据 · 还没有可分析的内容</div>'
                '<div class="ea-note">先在上面「🌐 在线自动取数」里检索一家上市公司，<br>'
                '或切到「✍️ 手动粘贴 / 上传」贴一段财报摘要 —— 之后这里会展开 '
                '四维评分、年度时间轴与按页引用。</div>'
                '</div>',
                unsafe_allow_html=True,
            )
        elif not DEEPSEEK_API_KEY and (run_btn or report_btn):
            st.error("未配置 DEEPSEEK_API_KEY，请联系管理员。")

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
        _tr = get_tracer()
        if rag is None or not _code or not _year or not st.session_state.get("single_pdftext"):
            # 【Agent 轨迹】没建索引也要如实记一步「未执行」：
            # 台账里凭空少一步，比记录"这步没做"更容易让人误以为检索过了。
            _tr.hit("年报 RAG", "检索年报相关章节", inp=str(query)[:36],
                    out="未执行", note="尚未解析年报 PDF，无向量索引", status="skipped")
            return []
        if not rag.has_index(_code, _year, _tag):
            # 兼容老会话：索引可能是按"无 doc_tag"的老命名建的
            _tag = rag.find_index_tag(_code, _year) or _tag
            if not rag.has_index(_code, _year, _tag):
                _tr.hit("年报 RAG", "检索年报相关章节", inp=str(query)[:36],
                        out="未执行", note=f"{_code} {_year} 年报索引不存在", status="skipped")
                return []
        try:
            with _tr.span("年报 RAG", "检索年报相关章节",
                          inp=f"top_k={top_k} · {str(query)[:36]}") as _s:
                _hits = rag.retrieve(query, _code, _year, top_k=top_k, doc_tag=_tag)
                _pgs = sorted({int(h.get("page") or 0) for h in (_hits or [])
                               if str(h.get("page") or "").isdigit()})
                _s.out = f"命中 {len(_hits or [])} 个章节" + (
                    f" · 页码 {_pgs[:6]}" if _pgs else "")
            return _hits
        except Exception as e:
            _tr.hit("年报 RAG", "检索年报相关章节", out="检索失败", status="failed",
                    note=f"{type(e).__name__}: {e}")
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
                    "不要输出任何评分（scores 由规则引擎计算）；数据不足的字段填 null。\n\n"
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

                # 【Agent 轨迹】规则评分 / 输出校验 / 证据核对都在 pipeline 内部完成
                # （页面与离线评测共用同一条链路），所以只能在调用**之后**回填台账。
                # 附加好处：台账里每个数字都取自 _core 本身，与页面上显示的评分、
                # 黄条提示**同源** —— 不会出现"页面显示一套、轨迹记录另一套"。
                _agent_tr = get_tracer()
                _agent_tr.hit(
                    "口径控制", "锁定最近一个年报口径",
                    inp=("在线接口" if _is_online else "手动粘贴文本")
                         + f" · 请求 {int(st.session_state.get('single_periods', 4) or 4)} 期",
                    out=(f"采用 {_core.get('score_period')}"
                         if _core.get("score_period")
                         else "无年报记录 → 退回最近报告期，并在页面显式标注口径已退化"),
                    status=("ok" if _core.get("score_period") else "degraded"),
                    kind="decision",
                )
                _agent_tr.hit(
                    "规则引擎", "四维评分（固定阈值，可复现）",
                    inp=f"行业={industry} · 来源={_core.get('score_source') or '—'}",
                    out=(f"{analysis.get('total_score')} / 10"
                         if analysis.get("total_score") is not None else "数据不足 → 明确不给分"),
                    note=(f"指标覆盖率 {int(round((analysis.get('score_coverage') or 0) * 100))}%"
                          if isinstance(analysis.get("score_coverage"), (int, float)) else ""),
                    kind="check",
                )
                _v_iss = _core.get("validation_issues") or []
                _agent_tr.hit(
                    "输出校验", "字段 / 类型 / 范围校验",
                    inp="validate_analysis（页面与评测同一实现）",
                    out=("全部通过" if not _v_iss else f"{len(_v_iss)} 项已修正或提示"),
                    status=("ok" if not _v_iss else "degraded"),
                    kind="check",
                )
                _ev_items_now = _core.get("evidence_items") or []
                _ev_num_now = [e for e in _ev_items_now
                               if isinstance(e.get("value"), (int, float))
                               and not isinstance(e.get("value"), bool)]
                _ev_loc_now = [e for e in _ev_num_now if e.get("found") and e.get("page")]
                _agent_tr.hit(
                    "证据核对", "数值溯源 + 页码定位",
                    inp="证据库：年报全文 + 检索片段 + 取数摘要",
                    out=f"可核验数值 {len(_ev_num_now)} 项 · 定位到年报原文 {len(_ev_loc_now)} 项"
                        + (f" · 存疑 {len(_core.get('evidence_issues') or [])} 条"
                           if _core.get("evidence_issues") else ""),
                    note=("未建年报索引时只能定位到取数摘要，无法给出页码"
                          if not _ev_loc_now and _ev_num_now else ""),
                    kind="check",
                )
                _cit_now = _core.get("citation") or {}
                if _cit_now.get("rate") is not None:
                    _agent_tr.hit(
                        "引用核验", "正文页码引用正确率",
                        inp=(f"合法页码区间 1~{_pg_count}"
                             if isinstance(_pg_count, int) and _pg_count > 0
                             else "无年报索引，无法核验"),
                        out=f"{_cit_now.get('ok', 0)}/{_cit_now.get('refs', 0)}"
                            f"（{_cit_now['rate'] * 100:.0f}%）",
                        kind="check",
                    )

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

                # ---- 往「分析记录」写一条（session_state + 本地文件，2 小时 TTL）----
                # 存的是 analysis 的**引用**：回看时直接把它放回 session_state，
                # 侧栏那条记录就能把当时的评分、图表、证据一起还原出来。
                # v10：追加写本地缓存（_hist_save），刷新页面 / 重开会话可恢复。
                # v11（用户阶段三第 ④ 条）：升级为**完整状态快照** —— 用户实测
                # "测完宁德时代再测比亚迪，回看宁德时代时数据都在，却要重新点
                # 「运行分析」"。原因：旧快照只存 analysis，不存当时的取数文本，
                # 回看后数据指纹对不上、上下文还是另一家公司的。现在把取数文本、
                # 公司三要素、报告期、研报、追问对话一起存进去，回看=整页时光倒流。
                import time as _time_hist
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
                        "_ts": _time_hist.time(),
                        "analysis": analysis,
                        "snapshot": {
                            "online": (raw_data or "")[:60000],
                            "code": st.session_state.get("single_online_code", ""),
                            "name": st.session_state.get("single_online_name", ""),
                            "org": st.session_state.get("single_online_org", ""),
                            "periods": st.session_state.get("single_periods", 3),
                            "report": (st.session_state.get("report") or "")[:60000],
                            "messages": (st.session_state.get("messages") or [])[-12:],
                        },
                    }
                )
                st.session_state["analysis_history"] = _hist[-20:]
                _hist_save(_hist[-20:])
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
        # 【新增第 5 个页签】把 Agent 的工具调用链摊开。
        # 赛题是「AI Agent」，但前四个页签呈现的都是**结果**；评委想判断"这是
        # 不是真 Agent"，看的是它自主调了哪些工具、按什么顺序、失败后怎么换路。
        _w1, _w2, _w3, _w4, _w5 = st.tabs(
            ["📊 分析概览", "🗓️ 年度时间轴", "🧮 评分与依据", "📝 研报", "🤖 Agent 轨迹"]
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

    def _tab5_agent_trace():
        """Agent 轨迹 · 工具调用链与自主决策点。

        为什么单独占一个页签：
          前四个页签回答「分析出了什么」，这一页回答「Agent 是怎么做出来的」。
          同样调一次接口，会不会自己判断「这是中报，不能当年报用」、
          「这个字段缺了，不能硬凑分」，才是 Agent 与普通脚本的分水岭 ——
          而这个区别如果只写在代码注释里，评委翻到页面是看不到的。

        ⚠️ 这里展示的是**真实调用记录**：由代码中各调用点实时写入，
        不是事后整理的说明文字；每一步的产出数字都与页面显示同源。
        """
        _tr = get_tracer()
        _rows = _tr.rows()
        if not _rows:
            st.info(
                "暂无轨迹记录。完成一次「🌐 在线取数 → 🚀 运行分析」后，"
                "本页会按真实发生顺序列出 Agent 调用的每一步工具、耗时与是否降级。"
            )
            return

        _sum = _tr.summary()
        _c1, _c2, _c3, _c4 = st.columns(4)
        with _c1:
            st.metric("工具调用", f"{_sum.get('tools', 0)} 次")
        with _c2:
            st.metric("自主决策", f"{_sum.get('decisions', 0)} 处")
        with _c3:
            st.metric("自检环节", f"{_sum.get('checks', 0)} 项")
        with _c4:
            st.metric("降级 / 失败",
                      f"{_sum.get('degraded', 0)} / {_sum.get('failed', 0)}")
        st.caption(
            f"本次会话累计 {_sum.get('total', 0)} 步 · 总耗时 {_sum.get('elapsed_ms', 0)} ms"
            " —— 降级项表示「Agent 自己换了条路但仍产出了结果」，"
            "失败项表示这一步确实没做成；两者都在页面上如实呈现，不做静默处理。"
        )

        # 内部字段（_kind / _status）不展示，只留给程序判断，避免表格出现两列噪音
        _df = pd.DataFrame([
            {k: v for k, v in _r.items() if not str(k).startswith("_")}
            for _r in _rows
        ])
        st.dataframe(_df, use_container_width=True, hide_index=True)

        if agent_trace is not None:
            _dec = agent_trace.decisions_of(_tr)
            if _dec:
                with st.expander("🧭 自主决策点（Agent 自己判断了什么）", expanded=True):
                    st.caption(
                        "工具调用表回答「做了什么」，这里回答「为什么这么做」。"
                        "这几处判断是本作品与「直接把财报丢给大模型」的关键差异。"
                    )
                    st.dataframe(pd.DataFrame(_dec), use_container_width=True,
                                 hide_index=True)

        st.divider()
        st.markdown("**这条链路上的每一步，分别回答什么问题**")
        st.markdown(
            "- 🌐 **在线取数**：自己去公开接口拿数据，而不是等人把财报粘进来\n"
            "- 📄 **年报解析 / RAG 检索**：自己决定去年报的哪一页找依据，而不是读开头几千字\n"
            "- 🤖 **大模型抽取**：只让它做擅长的「读文本、抽指标、写解读」，不让它打分\n"
            "- 🧮 **规则引擎评分**：分数由固定阈值算出，同一份数据必然同一分数（可复现）\n"
            "- ✅ **输出校验 / 证据核对 / 引用核验**：自己检查自己的输出，发现问题显式提示"
        )
        if st.button("🧹 清空本次轨迹记录", key="btn_clear_agent_trace"):
            _tr.clear()
            st.rerun()

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
                    f'<span class="q-star">■ 明星 <span class="q-en">Star</span></span>'
                    f'　ROE≥{_rl:g}% 且 增速≥{_gl:g}%（高盈利 · 高成长）<br>'
                    f'<span class="q-cash">■ 现金牛 <span class="q-en">Cash Cow</span></span>'
                    f'　ROE≥{_rl:g}% 且 增速&lt;{_gl:g}%（高盈利 · 低成长）<br>'
                    f'<span class="q-quest">■ 问题 <span class="q-en">Question</span></span>'
                    f'　ROE&lt;{_rl:g}% 且 增速≥{_gl:g}%（低盈利 · 高成长）<br>'
                    f'<span class="q-dog">■ 瘦狗 <span class="q-en">Dog</span></span>'
                    f'　ROE&lt;{_rl:g}% 且 增速&lt;{_gl:g}%（低盈利 · 低成长）<br>'
                    f'<span style="color:#475569">切换行业财务基准库按钮，基准线会随之调整。</span>'
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
    # 【v10】研报页签（_tab4）里也可以直接触发生成：点页签里的按钮 → 置位
    # `_gen_report_from_tab` → rerun → 走到这里同一条生成链路。这样用户
    # 在"研报"页签发现没研报时，不用再滚回上方「运行分析」卡里找按钮
    # （用户第 12 条反馈：生成研报入口离查看入口太远）。
    _gen_report_from_tab = st.session_state.pop("_gen_report_from_tab", False)
    if (report_btn or _gen_report_from_tab) and raw_data and DEEPSEEK_API_KEY:
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
            # 【v10】生成入口搬到页签里：查看在哪里，生成就在哪里（用户第 12 条反馈）。
            st.caption("还没有研报。点下方按钮**直接在这里生成**，完成后自动显示（可下载 Markdown）。")
            if st.button("📝 生成完整研报并在此查看",
                         disabled=not raw_data,
                         use_container_width=True, type="primary",
                         key="gen_report_in_tab"):
                st.session_state["_gen_report_from_tab"] = True
                st.rerun()
            if not raw_data:
                st.caption("（先完成「🌐 在线取数」或手动输入，才有内容可生成研报。）")
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
        with _w5:
            _tab5_agent_trace()

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
            # 【v10 · 第 5 条】带 viz 标记的助手回答，重渲染时把辅助图一并还原
            if m.get("role") == "assistant" and m.get("viz"):
                _vz_png = _chat_viz_png(m["viz"])
                if _vz_png:
                    with st.expander("📈 图表辅助 · 一图看懂这轮回答", expanded=False):
                        st.image(_vz_png)

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

        # 【v10 · 第 13 条】追问里要和别家公司对比 → Agent 自动取对方数据注入上下文
        _cmp_blocks, _cmp_got = _chat_auto_compare(question)
        if _cmp_blocks:
            base_context += (
                "\n\n【对比公司财务数据 · 系统自动联网获取】\n"
                + "\n\n".join(_cmp_blocks)
                + "\n（以上为对方公司的公开财报数据，可直接用于对比分析；"
                  "对比结论请标注双方口径与报告期。）"
            )
            st.caption(
                "🔀 检测到对比意图，已自动获取 "
                + "、".join(_cmp_got) + " 的财务数据并加入上下文，无需手动取数。"
            )

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
                # 【v10 · 第 5 条】回答下方直接挂辅助图表，答案不再只有干巴巴的文字
                _viz_kind = _chat_viz_kind_for(question)
                _viz_png = _chat_viz_png(_viz_kind)
                if _viz_png:
                    with st.expander("📈 图表辅助 · 一图看懂这轮回答", expanded=True):
                        st.image(_viz_png)
            st.session_state["messages"].append(
                {"role": "assistant", "content": answer,
                 "viz": _viz_kind if _viz_png else ""}
            )
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
            st.session_state["_chat_notice"] = ("error", "未配置 DEEPSEEK_API_KEY，请联系管理员。")
        else:
            # 用户消息先入队，然后立刻重跑：下一轮它由「历史消息」渲染 → 出现在输入框上方
            st.session_state["messages"].append({"role": "user", "content": _new_q})
            st.session_state["_answer_pending"] = _new_q
        st.rerun()

# ---------------------------------------------------------------
# 视图 2：多公司对比（2~5 家）
# ---------------------------------------------------------------
def _view_compare():
    page_head(
        "多公司对比",
        "2~5 家公司各自在线取数（或手动输入财报），按**各自所属行业**的基准分别评分，"
        "由程序生成对照表、差额与对比图（同一份数据永远同一分数），可导出 CSV 与对比报告。",
    )

    # ===== 第 1 段：对比公司管理（增删公司 + 每家一张卡片）=====
    sec_head("1", "对比公司管理",
             "在线取数或手动输入 · 每家按各自所属行业基准评分", tag="横向对比")

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

    # ⚠️ 控件定义顺序：toggle / 期数选择器必须放在「一键取数」按钮**之前** ——
    # button 点击分支同轮就会用到 _cmp_periods，定义放后面直接 NameError。
    _cmp_periods = int(st.session_state.get("cmp_periods", 8) or 8)
    _cmp_auto_on = bool(st.session_state.get("cmp_auto_fetch", True))

    # ---- ⚡ 一键批量取数（v10 · 用户第 3 条反馈）----
    # 原来 2~5 家公司要逐家点「🔍 获取数据」，一次点 5 下、每下还各自转圈。
    # 现在填好各家的代码/名称后，这里一次取完全部：内部仍然逐家调用
    # resolve_stock + _panel_store（与单家点按钮**同一条链路**，轨迹、清空、
    # 校验逻辑一行没改），只是把"点 N 次"折叠成"点 1 次"。
    # v11（用户第 ⑤ 条）：再加「自动合并取数」开关 —— 打开后在任意一家的
    # 热门下拉里选公司，**这一家立即自动取数**，先选 A 再选 B 也一样，
    # 全程不用碰「获取数据」按钮；开关关闭则退回手动点按钮。
    _bf1, _bf2, _bf3 = st.columns([1, 1, 2])
    with _bf1:
        if st.button("⚡ 一键获取全部公司数据", key="cmp_batch_fetch",
                     use_container_width=True):
            _bf_results = []
            with st.spinner("正在逐家联网取数（巨潮 + 东方财富）……"):
                for _bi in range(_n_cmp):
                    _bw = str(st.session_state.get(f"cmp{_bi}_kw", "") or "").strip()
                    if not _bw:
                        _bf_results.append((chr(65 + _bi), "", False, "未填写代码/名称，已跳过"))
                        continue
                    _bres = ds.resolve_stock(_bw)
                    if not _bres.get("ok"):
                        _bf_results.append((chr(65 + _bi), _bw, False,
                                            _bres.get("error") or "证券解析失败"))
                        continue
                    _bok, _berr = _panel_store(
                        f"cmp{_bi}", _bres["code"], _bres["name"],
                        _bres.get("orgId", ""), _cmp_periods,
                    )
                    _bf_results.append((chr(65 + _bi),
                                        f'{_bres["name"]}（{_bres["code"]}）', _bok, _berr))
            _bf_ok = [r for r in _bf_results if r[2]]
            _bf_bad = [r for r in _bf_results if not r[2]]
            if _bf_ok:
                get_tracer().hit("批量取数", "一键获取全部对比公司",
                                 inp=f"{_n_cmp} 家 · {_cmp_periods} 期",
                                 out=f"成功 {len(_bf_ok)} 家")
                st.success("✅ 已获取：" + "、".join(r[1] for r in _bf_ok))
            for r in _bf_bad:
                st.warning(f"公司 {r[0]}：{r[3]}")
            if _bf_ok:
                st.rerun()
    with _bf2:
        st.toggle(
            "🚀 自动合并取数", key="cmp_auto_fetch", value=True,
            help="开启后：在任意公司的「热门股票」下拉里选一家，这一家**立即自动取数**"
                 "（先选公司A、再选公司B也一样，一次点击出一次数据，不用再点「获取数据」）。"
                 "关闭后恢复手动点按钮取数。",
        )
    with _bf3:
        # 真正的控件渲染；取值已在上方提前读入 _cmp_periods（按钮分支要用）
        st.selectbox(
            "报告期数（作用于全部对比公司）",
            [3, 4, 6, 8, 12, 16, 20, 24],
            index=3, key="cmp_periods",
            help="每家公司向东方财富拉取「最近 N 个报告期」。N 越大，可参与对比的"
                 "年报年份越多（N=24 时年报口径约 6 个年度）——想比 2023 / 2024 年就调大它。",
        )
        st.caption(
            "填好每家公司的代码 / 名称后点「⚡ 一键获取」，**一次性取完全部**；"
            "或开着「🚀 自动合并取数」逐家选公司，**选一家自动拉一家**。"
        )

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
            # 每家一张白卡：三家公司并列时，卡片边界就是"这家 / 那家"的边界
            with card(f"cmp{_i}"):
                st.markdown(f'<div class="mini-head">{_cmp_tags[_i]} 公司 {chr(65 + _i)}</div>',
                            unsafe_allow_html=True)
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

    # ---- 共同年报年份对齐（P1-03）+ 对比口径选择（v11 · 用户第 12/13 条）----
    # 「默认选共同年报年份」+「不同年度不混比」：
    #   先各自取回全部年报行，求年份交集；交集非空 → 用一个选择器统一基准年份，
    #   所有公司都取这一年；交集为空 → 明确报错而不是硬凑一张跨年份的表。
    # v11：期数跟随「报告期数」选择器（默认 8 → 最大 24，年报口径约 6 个年度），
    # 之前写死 8 期 ≈ 只有 2 个年报，用户"只有 2025 年可比"的根因就在这。
    # 同时新增「各自最新年报」口径：两家公司年报年份对不齐时，也允许各家用
    # 自己的最新年报比（期别分别标注，不再一刀切拒绝）。
    _annual_rows = {}
    for _s in _cmp_side:
        _rows = []
        if _s["online"] and _s["code"] and _pipeline is not None:
            try:
                # 【v12 · 用户阶段四第 ④ 条】年报行固定取满 24 期（≈6 个年报年度），
                # **不再跟随报告期数选择器** —— 之前用 max(_cmp_periods, 8)，
                # 默认 8 期只够 2 个年报，用户选 2022 年时基准年份下拉里根本没有
                # 2022，追问问 2022 也只会得到"没有该年数据"。年报行只是一次
                # pageSize=24 的接口调用，成本极低，放开取满才能支撑跨年对比。
                _rows = _pipeline.all_annual_rows(_s["code"], periods=24)
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

    _MODE_SAME = "📅 同年份对比（默认 · 不同年度不混比）"
    _MODE_OWN = "🆚 各自最新年报（每家用自己的最新年报，期别分别标注）"
    _base_year = ""
    _year_mode = _MODE_SAME
    if _all_online and _pipeline is not None:
        _year_mode = st.radio(
            "对比口径（年份怎么对齐）",
            [_MODE_SAME, _MODE_OWN],
            key="cmp_year_mode",
            help="同年份对比：所有公司都取同一年年报，绝对可比；"
                 "各自最新年报：两家公司年报披露节奏不同（如一家只有 2025、另一家最新是 2024）"
                 "时，各家用各自的最新年报对比 —— 数值仍可横向看强弱，但**期别不同**，"
                 "页面会在对照表里分别标注每家公司用的是哪一年。",
        )
    if _all_online and _pipeline is not None and _year_mode == _MODE_SAME:
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
            st.warning(
                "⚠️ **两家公司没有共同披露的年报年份**，同年份对比做不了 ——"
                f"各公司可用年报年份：{_avail}。"
                "可改用上方「🆚 各自最新年报」口径继续对比（期别分别标注），"
                "或把报告期数调大到 16~24 期后再试（年报年份覆盖更多）。"
            )

    def _annual_table_txt(rows) -> str:
        """把一家公司的多年年报行压成「年份: 指标」文本，供 LLM 直接引用。
        【v12 · 用户阶段四第 ④ 条】对比提示词 / 追问上下文原来只带**取数文本**
        （最近 N 期）与最新一期指标 —— 用户选 2022 年或追问 2022 年时，
        模型手里根本没有 2022 的数字，只能回答"无 2022 年报数据"。
        现在把年报行全量注入（与评分/图表同一份取值），选了哪年就有哪年的数。"""
        _out = []
        for _r in (rows or []):
            _y = _pipeline.year_of(_r) if _pipeline is not None else ""
            if not _y:
                continue
            _kv = []
            for _k, _lab in (
                ("revenue", "营收(亿)"), ("net_profit", "归母净利(亿)"),
                ("revenue_yoy", "营收同比%"), ("net_profit_yoy", "净利同比%"),
                ("roe", "ROE%"), ("gross_margin", "毛利率%"), ("net_margin", "净利率%"),
                ("debt_ratio", "资产负债率%"), ("ocf_to_revenue", "经营现金流/营收%"),
            ):
                _v = (_r or {}).get(_k)
                if isinstance(_v, (int, float)):
                    _kv.append(f"{_lab}={_v:g}")
            if _kv:
                _out.append((int(_y), f"{_y}年年报： " + "；".join(_kv)))
        _out.sort(key=lambda t: t[0], reverse=True)
        return "\n".join(t[1] for t in _out)

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
                    met, per = _pipeline.annual_metrics(
                        side["code"], periods=24)
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

    # ===== 第 2 段：开始对比分析（校验信息 + 主按钮住同一张卡）=====
    sec_head("2", "开始对比分析",
             "所有公司取数完成后点击 —— 生成对照表 / 差额 / 对比图 / CSV")
    with card("cmp-run"):
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
        if _all_online and not _common_years and _year_mode == _MODE_SAME:
            # 同年份口径下没有共同年报年 → 阻断；各自最新口径放行（期别分别标注）
            _block = True
        if not _block:
            if _all_online and _year_mode == _MODE_OWN:
                st.caption(
                    "✅ 已就绪（**各自最新年报口径**）："
                    + "、".join(
                        f"{_s['name']} → "
                        + (sorted(_years_of(_s['i']), reverse=True) or ['无'])[0]
                        for _s in _cmp_side)
                    + "。每家公司用自己的最新年报，期别已在对照表中标注。")
            else:
                st.caption(
                    f"✅ 已就绪：{_n_cmp} 家公司"
                    + (f" · 对比基准年份 **{_base_year}**（年报口径，不同年度不混比）" if _base_year else "")
                )

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
                            # v11：多年年报行快照 —— 「逐年年报对比」图的数据源
                            # （与评分同一批取数，不二次请求接口）
                            "annual": list(_annual_rows.get(_s["i"]) or []),
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
            st.error("未配置 DEEPSEEK_API_KEY，请联系管理员。")
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
                        _blk = (
                            f"【{_it['name']}｜行业：{_it['industry']}｜基准期：{_it['period'] or '未获取'}】\n{_txt}"
                        )
                        # 【v12 · 用户阶段四第 ④ 条】逐年年报指标表 —— 用户选哪一年
                        # （含 2022 这类历史年份）模型就必须有哪一年的数字可引用。
                        _at = _annual_table_txt(_it.get("annual"))
                        if _at:
                            _blk += "\n【逐年年报指标（程序联网取数，与评分同源，可直接引用）】\n" + _at
                        _blocks.append(_blk)
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
                        + ("\n用户问某一年份时，优先引用「逐年年报指标」中该年的数据；"
                           "该年不在列表里就如实说明，并明确列出**可用的年份范围**，禁止编造或用其它年份冒充。")
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
        # v10：从"只有柱状图"扩成三种视角（用户第 3 条反馈）：
        #   ① 分组柱状（百分比口径：盈利成长 / 风险偿债）
        #   ② 雷达图（一眼看出各家强弱维度的形状差异）
        #   ③ 规模柱状（绝对额：营收 / 净利润，百分比看不到"体量"）
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
        _png = fig_compare_radar([it["scores"]["scores"] for it in _items], _names)
        if _png:
            show_png(_png, "cmp-radar")
        _png = fig_compare_scale_n(_mets, _names)
        if _png:
            show_png(_png, "cmp-scale")

        # ---- v11 新增两张（用户第 ⑧/12/13 条）----
        # ① 逐年年报对比：想看哪一年就看哪一年，不再只有"单一基准年份"一张快照
        _rows_map = {_idx: (it.get("annual") or []) for _idx, it in enumerate(_items)}
        _yg1, _yg2 = st.columns(2)
        with _yg1:
            _png = fig_compare_years_n(_rows_map, _names, _pipeline,
                                       "revenue", "营业收入（亿元）")
            if not show_png(_png, "cmp-years-rev"):
                st.caption("各公司均未取到多年年报营收数据，暂不绘制。")
        with _yg2:
            _png = fig_compare_years_n(_rows_map, _names, _pipeline,
                                       "net_profit", "归母净利润（亿元）")
            if not show_png(_png, "cmp-years-np"):
                st.caption("各公司均未取到多年年报净利润数据，暂不绘制。")
        # ② 估值对比（PE-TTM / PB）：行情走四级兜底链（东财→K线→新浪→腾讯），
        #    ds.get_quote 自带本地缓存，同一家公司一轮只打一次接口。
        _val_mets = []
        for _it in _items:
            _q = None
            try:
                _q = ds.get_quote(_it.get("code") or "") if _it.get("code") else None
            except Exception:
                _q = None
            _val_mets.append({"pe_ttm": (_q or {}).get("pe_ttm"),
                              "pb": (_q or {}).get("pb")})
        _png = fig_compare_group_n(
            _val_mets, _names,
            [("pe_ttm", "市盈率 PE-TTM"), ("pb", "市净率 PB")],
            "估值对比（PE / PB · 实时行情）")
        if _png:
            show_png(_png, "cmp-valuation")

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

    # ---- 💬 对比追问（v11 · 用户第 ⑥/13 条）----
    # 之前「继续追问」只在单公司页有：对比完想追问"哪家更值得投"，
    # 只能切回单公司页 —— 上下文也跟着丢。这里在对比结果下方直接给一个追问区：
    # 上下文自动携带**全部对比公司**的指标与评分，推荐问题按当前对比公司动态生成。
    _cs_chat = st.session_state.get("cmp_scores")
    if _cs_chat and (_cs_chat.get("items") or []):
        _citems = _cs_chat["items"]
        _cnames = [it["name"] for it in _citems]
        st.markdown("##### 💬 对比追问（基于上方全部对比公司的数据继续提问）")

        def _cmp_context() -> str:
            """把每家公司的指标与评分压成一段可引用的上下文（与评分表同一份取值）。
            【v12 · 用户阶段四第 ④ 条】追加「逐年年报指标」：追问"2022 年谁更强"
            这类跨年问题，模型必须能直接拿到对应年份的数字，而不是回答"没有该年数据"。"""
            _parts = []
            for it in _citems:
                _rows = [f"{k}={v}" for k, v in (it.get("raw") or {}).items()
                         if isinstance(v, (int, float))]
                _sc = it.get("scores") or {}
                _at = _annual_table_txt(it.get("annual"))
                _years = sorted({(_pipeline.year_of(r) or "") for r in (it.get("annual") or [])
                                 if _pipeline is not None and _pipeline.year_of(r)}, reverse=True)
                _blk = (
                    f"【{it['name']}（{it.get('code') or '—'}）· {it.get('industry') or '—'} · "
                    f"基准期 {it.get('period') or '—'} · 综合评分 {_sc.get('total_score', '—')}】\n"
                    + "；".join(_rows[:24])
                )
                if _at:
                    _blk += "\n【逐年年报指标（可直接引用，缺失年份不得编造）】\n" + _at
                if _years:
                    _blk += f"\n（可用年报年份：{'、'.join(_years)}）"
                _parts.append(_blk)
            return "\n\n".join(_parts)

        if "cmp_messages" not in st.session_state:
            st.session_state["cmp_messages"] = []

        # 1) 历史消息（问在上）
        for _m in st.session_state["cmp_messages"]:
            with st.chat_message(_m["role"]):
                st.markdown(_m["content"])

        # 2) 本轮回答（答在中）
        _cmp_pending = st.session_state.pop("_cmp_answer_pending", None)
        if _cmp_pending:
            with st.chat_message("assistant"):
                box = st.empty()
                with st.spinner("正在结合全部对比公司数据分析……"):
                    try:
                        _sys = system_prompt_with_kb(_citems[0].get("industry") or industry)
                        _prompt = (
                            "以下是本次对比的全部公司数据（系统联网取数 + 规则评分，可直接引用）：\n\n"
                            + _cmp_context()
                            + "\n\n请基于以上数据回答用户问题。涉及公司间比较时，"
                              "注明每家公司使用的报告期与行业口径；数据中没有的不要编造。"
                              "用户问某一年份（如 2022 年）时，优先引用「逐年年报指标」中"
                              "该年的数据；该年不在可用年份里就如实说明并给出可用年份范围。"
                        )
                        _msgs = [{"role": "system", "content": _sys},
                                 {"role": "user", "content": _prompt},
                                 {"role": "assistant", "content": "好的，我已掌握全部对比公司数据，请提问。"}]
                        _msgs.extend(st.session_state["cmp_messages"][:-1][-6:])
                        _msgs.append({"role": "user", "content": _cmp_pending})
                        _ans = llm_stream(_msgs, box, temperature=0.3, action="compare")
                    except Exception as e:
                        box.empty()
                        st.error(f"调用失败：{e}")
                        _ans = ""
                if _ans:
                    st.session_state["cmp_messages"].append(
                        {"role": "assistant", "content": _ans})

        # 3) 推荐问题（按当前对比公司自动生成）
        _cmp_sugs = []
        if len(_cnames) >= 2:
            _na, _nb = _cnames[0], _cnames[1]
            _cmp_sugs = [
                f"{_na} 和 {_nb} 哪家更值得投资？请从盈利、成长、偿债、营运四个维度比较后给出结论。",
                f"对比 {_na} 与 {_nb} 的营收与净利润，谁的增长质量更高？",
                f"{_na} 与 {_nb} 目前的估值（PE/PB）谁更便宜？结合基本面说明是否合理。",
                f"如果只能投资一家，你选 {_na} 还是 {_nb}？请给出理由与主要风险。",
                f"{_na} 相对 {_nb} 的主要优势与劣势分别是什么？",
                f"两家公司对比，哪个财务指标差异最大？这个差异说明什么？",
            ]
        if _cmp_sugs:
            with st.expander("🎯 推荐问题（按当前对比公司自动生成，点击即问；也可手动输入）",
                             expanded=not st.session_state["cmp_messages"]):
                _qc = st.columns(2)
                for _i, _sq in enumerate(_cmp_sugs[:6]):
                    with _qc[_i % 2]:
                        if st.button(_sq, key=f"cmp_suggest_{_i}", use_container_width=True):
                            # 与输入框同一条入队逻辑：先 append 再 rerun（见单公司页注释）
                            st.session_state["cmp_messages"].append({"role": "user", "content": _sq})
                            st.session_state["_cmp_answer_pending"] = _sq
                            st.rerun()

        # 4) 输入框（最下）
        _cmp_q = st.chat_input("例如：这几家公司哪一家更值得投资？", key="cmp_chat_q")
        _cmp_new = (_cmp_q or "").strip()
        if _cmp_new:
            if not DEEPSEEK_API_KEY:
                st.error("未配置 DEEPSEEK_API_KEY，请联系管理员。")
            else:
                st.session_state["cmp_messages"].append({"role": "user", "content": _cmp_new})
                st.session_state["_cmp_answer_pending"] = _cmp_new
            st.rerun()

# ---------------------------------------------------------------
# 视图 3：使用说明
# ---------------------------------------------------------------
def _view_help():
    page_head(
        "使用说明",
        "这套工作台一共三个入口：单公司深度分析 / 多公司对比 / 使用说明（都在左侧边栏）。"
        "第一次使用，照着下面第 1 张卡片的 4 步走一遍即可。",
    )

    # 使用说明页的卡片是"分类"而不是"步骤"，所以不编号，只用卡片表头 + 一条分隔线。
    # 【v10 · 第 9 条】先给一张总流程图：全部文字说明太密，一张图先建立全局印象，
    # 后面逐卡片的文字说明就有了"挂靠点"。
    # 为什么是内联 SVG 而不是 st.graphviz_chart：graphviz 渲染依赖系统 dot 可执行文件
    # （本地实测 ExecutableNotFound，Streamlit Cloud 容器同样不带），SVG 零依赖、
    # 部署到哪都能渲染，且与本页其他科技风插图同一套画法。
    with card("help-flow", "", "🗺️ 全流程一图看懂", "先看图，再往下读逐项说明"):

        def _flow_node(x: int, y: int, w: int, h: int, title: str, sub: str = "",
                       fill: str = "#EDF3FC", stroke: str = "#1E40AF") -> str:
            """一个流程节点：圆角矩形 + 居中标题 + 可选小字副题。"""
            _cx = x + w / 2
            _lines = [title] + ([sub] if sub else [])
            _tspans = ""
            _base_y = y + (h / 2 - 6 if len(_lines) == 2 else h / 2 + 5)
            for _i, _ln in enumerate(_lines):
                _sz = 15 if _i == 0 else 11.5
                _fill = "#0B1F3A" if _i == 0 else "#5B6B82"
                _dy = 0 if _i == 0 else 20
                _tspans += (f'<tspan x="{_cx}" y="{_base_y + _dy}" font-size="{_sz}" '
                            f'fill="{_fill}" font-weight="{700 if _i == 0 else 400}">'
                            f"{_ln}</tspan>")
            return (
                f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="1.3"/>'
                f'<text text-anchor="middle">{_tspans}</text>'
            )

        def _arrow(x1: int, y: int, x2: int, dashed: bool = False,
                   color: str = "#1E40AF") -> str:
            _mid = (x1 + x2) / 2
            return (
                f'<line x1="{x1}" y1="{y}" x2="{_mid - 3}" y2="{y}" '
                f'stroke="{color}" stroke-width="1.6"'
                + (' stroke-dasharray="5 4"' if dashed else "")
                + '/>'
                f'<path d="M{_mid - 2} {y - 4} L{_mid + 5} {y} L{_mid - 2} {y + 4} Z" '
                f'fill="{color}"/>'
                f'<line x1="{_mid + 6}" y1="{y}" x2="{x2}" y2="{y}" '
                f'stroke="{color}" stroke-width="1.6"'
                + (' stroke-dasharray="5 4"' if dashed else "")
                + '/>'
            )

        _NW, _NH, _GAP, _X0 = 142, 64, 36, 14
        _Y1 = 52
        # —— 单公司主线（7 节点）——
        _s1 = [("① 选行业基准", "同业对标", "#EDF3FC", "#1E40AF"),
               ("② 在线取数", "巨潮+东财+新浪兜底", "#EDF3FC", "#1E40AF"),
               ("③ 解析年报 PDF", "按页建向量库（可选）", "#FFF7E6", "#B45309"),
               ("④ 结构化分析", "规则引擎四维评分", "#E7F6EC", "#16A34A"),
               ("⑤ 看结果", "概览/时间轴/评分/研报/轨迹", "#EDF3FC", "#1E40AF"),
               ("⑥ 继续追问", "自动检索原文·自动取对比", "#FFF7E6", "#B45309"),
               ("⑦ 生成研报", "Markdown 可下载", "#E7F6EC", "#16A34A")]
        _svg_row1 = ""
        for _i, (_tt, _ss, _ff, _cc) in enumerate(_s1):
            _x = _X0 + _i * (_NW + _GAP)
            _svg_row1 += _flow_node(_x, _Y1, _NW, _NH, _tt, _ss, _ff, _cc)
            if _i < len(_s1) - 1:
                _svg_row1 += _arrow(_x + _NW, _Y1 + _NH / 2, _x + _NW + _GAP)
        # —— 多公司支线（3 节点，虚线汇入 ⑦）——
        _Y2 = 178
        _c1 = [("⚖️ 多公司对比", "填 2~5 家公司代码", "#F3E8FD", "#7C3AED"),
               ("⚡ 一键批量取数", "或逐家获取", "#F3E8FD", "#7C3AED"),
               ("开始对比分析", "对照表/雷达/规模图/CSV", "#E7F6EC", "#16A34A")]
        _svg_row2 = ""
        for _i, (_tt, _ss, _ff, _cc) in enumerate(_c1):
            _x = _X0 + _i * (_NW + _GAP)
            _svg_row2 += _flow_node(_x, _Y2, _NW, _NH, _tt, _ss, _ff, _cc)
            if _i < len(_c1) - 1:
                _svg_row2 += _arrow(_x + _NW, _Y2 + _NH / 2, _x + _NW + _GAP)
        # 虚线：从「开始对比分析」右端 → 拐向右上 → 汇入 ⑦ 底边
        _c3x = _X0 + 2 * (_NW + _GAP) + _NW
        _s7x = _X0 + 6 * (_NW + _GAP) + _NW / 2
        _svg_row2 += (
            f'<path d="M{_c3x} {_Y2 + _NH / 2} H{_c3x + 26} V{_Y1 + _NH} '
            f'H{_s7x}" fill="none" stroke="#94A3B8" stroke-width="1.4" '
            'stroke-dasharray="5 4"/>'
            f'<path d="M{_s7x - 5} {_Y1 + _NH - 6} L{_s7x} {_Y1 + _NH + 1} '
            f'L{_s7x + 5} {_Y1 + _NH - 6} Z" fill="#94A3B8"/>'
            f'<text x="{_c3x + 40}" y="{_Y1 + _NH + 22}" font-size="11" '
            'fill="#64748B">对比报告同样可导出</text>'
        )
        st.markdown(
            '<svg viewBox="0 0 1240 268" width="100%" style="min-width:720px;" '
            'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="使用流程图">'
            '<text x="14" y="30" font-size="15" font-weight="700" fill="#0B1F3A">'
            "🏢 单公司深度分析主线</text>"
            + _svg_row1
            + '<text x="14" y="164" font-size="15" font-weight="700" fill="#0B1F3A">'
            "⚖️ 多公司对比支线</text>"
            + _svg_row2
            + "</svg>",
            unsafe_allow_html=True,
        )
        st.caption(
            "橙色 = 可选步骤；绿色 = 出结果的步骤；紫色 = 多公司支线；虚线 = 对比支线汇入研报导出。"
            "左侧导航切换「🏢 单公司 / ⚖️ 多公司 / 📖 使用说明」。"
        )

    # 【v11 · 第 ⑦ 条】用户觉得线性流程图"太单调" → 补一张**分层架构图**：
    # 数据层（四级免费数据源）→ 引擎层（解析/管道/评分/RAG/双模型/轨迹）→ 展示层。
    # 每层一个带左侧色条的圆角容器，层间大箭头标数据流；仍是零依赖内联 SVG。
    with card("help-arch", "", "🏛️ 系统架构图", "三层数据流：免费数据源 → 分析引擎 → 工作台展示"):

        def _arch_layer(y: int, h: int, name: str, accent: str, bg: str, note: str) -> str:
            return (
                f'<rect x="16" y="{y}" width="1208" height="{h}" rx="14" '
                f'fill="{bg}" stroke="{accent}" stroke-opacity="0.30" stroke-width="1.2"/>'
                f'<rect x="16" y="{y}" width="6" height="{h}" rx="3" fill="{accent}"/>'
                f'<text x="36" y="{y + 27}" font-size="15" font-weight="700" fill="{accent}">{name}</text>'
                f'<text x="1216" y="{y + 27}" text-anchor="end" font-size="11.5" fill="#64748B">{note}</text>'
            )

        def _arch_box(x: int, y: int, w: int, h: int, icon: str, title: str,
                      sub: str, accent: str) -> str:
            return (
                f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="#FFFFFF" '
                f'stroke="{accent}" stroke-opacity="0.55" stroke-width="1.2"/>'
                f'<text x="{x + 12}" y="{y + 24}" font-size="13" font-weight="700" fill="#0B1F3A">'
                f'{icon} {title}</text>'
                f'<text x="{x + 12}" y="{y + 44}" font-size="10.5" fill="#5B6B82">{sub}</text>'
            )

        def _arch_flow(y1: int, y2: int, label: str) -> str:
            _cx = 620
            return (
                f'<line x1="{_cx}" y1="{y1}" x2="{_cx}" y2="{y2 - 8}" stroke="#94A3B8" stroke-width="2"/>'
                f'<path d="M{_cx - 6} {y2 - 9} L{_cx} {y2} L{_cx + 6} {y2 - 9} Z" fill="#94A3B8"/>'
                f'<text x="{_cx - 12}" y="{(y1 + y2) / 2 + 4}" text-anchor="end" '
                f'font-size="11.5" fill="#64748B">{label}</text>'
            )

        _A = ""   # 拼接器
        # —— 数据层 ——
        _A += _arch_layer(14, 112, "① 数据层 · 四级免费公开数据源", "#2563EB", "#F0F6FF",
                          "全部无需密钥 · 逐级兜底")
        _dsrc = [("🏛️", "巨潮资讯网", "公告检索 · 年报 PDF 下载"),
                 ("📈", "东方财富", "实时行情 · 多期财务指标"),
                 ("🐦", "新浪财经", "行情 / 指标兜底 ③"),
                 ("🐧", "腾讯财经", "行情兜底 ④（v11 新增）")]
        for _i, (_ic, _tt, _ss) in enumerate(_dsrc):
            _A += _arch_box(40 + _i * 294, 52, 274, 62, _ic, _tt, _ss, "#2563EB")
        _A += _arch_flow(130, 172, "取数 · REST（失败自动换下一级）")
        # —— 引擎层 ——
        _A += _arch_layer(176, 112, "② 引擎层 · 取数管道与规则评分", "#7C3AED", "#F7F2FE",
                          "确定性优先 · 大模型只解读不打分")
        _eng = [("🎯", "证券解析", "代码精确命中 · 多义交确认"),
                ("⚙️", "取数管道", "指标标准化 · 亿元口径"),
                ("📐", "规则评分引擎", "固定阈值 · 同数同分"),
                ("📚", "RAG 年报检索", "按页切片 · 页码引用"),
                ("🤖", "双模型协同", "DeepSeek+千问"),
                ("🧾", "Agent 轨迹", "每步工具调用台账")]
        for _i, (_ic, _tt, _ss) in enumerate(_eng):
            _A += _arch_box(40 + _i * 199, 214, 187, 62, _ic, _tt, _ss, "#7C3AED")
        _A += _arch_flow(292, 334, "评分 + 解读 · 结构化结果")
        # —— 展示层 ——
        _A += _arch_layer(338, 112, "③ 展示层 · 投研工作台", "#16A34A", "#F0FBF4",
                          "问在上 · 答在中 · 输入框在最下")
        _ui = [("🏢", "单公司五页签", "概览/时间轴/评分/研报/轨迹"),
               ("🗓️", "年度时间轴", "缺年不补零 · pp 口径"),
               ("⚖️", "多公司对比", "对照表/雷达/规模/逐年/估值"),
               ("💬", "追问对话", "推荐问题 · 图表辅助"),
               ("📝", "研报导出", "Markdown · CSV")]
        for _i, (_ic, _tt, _ss) in enumerate(_ui):
            _A += _arch_box(40 + _i * 238, 376, 226, 62, _ic, _tt, _ss, "#16A34A")
        st.markdown(
            '<svg viewBox="0 0 1240 470" width="100%" style="min-width:760px;" '
            'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="系统架构图">'
            + _A + "</svg>",
            unsafe_allow_html=True,
        )
        st.caption(
            "蓝 = 免费数据源（任一接口被限流自动换下一级）；紫 = 确定性引擎（评分由固定规则计算，可复现）；"
            "绿 = 工作台展示。评分只发生在引擎层，展示层从不自行算分 —— 同一份数据在任何页面分数一致。"
        )

    with card("help-quick", "", "🚀 快速上手（4 步）", "从零到一份可下载的研报"):
        st.markdown(
            """
1. 左侧边栏选「🏢 单公司深度分析」，先在**第 1 步**选好行业财务基准（同业对标）
2. **第 2~3 步**输入股票代码（如 `600519`）或从热门股票里选，点「🔍 获取数据」自动取数
3. **第 5 步**点「🚀 开始结构化分析」，结果分五个页签：📊 分析概览 / 🗓️ 年度时间轴 / 🧮 评分与依据 / 📝 研报 / 🤖 Agent 轨迹
4. 页面底部可直接**继续追问**；需要成稿就在「📝 研报」页签里一键生成
"""
        )

    with card("help-cap", "", "🧩 主要能力", "每个能力的入口与作用"):
        st.markdown(
            """
| 功能 | 入口 | 说明 |
|---|---|---|
| 🌐 在线自动取数 | 「🏢 单公司深度分析」第 3 步 | 输入股票代码/名称，自动获取行情、多期财务指标、年报 PDF（东财为主，新浪财经兜底），无需自己找财报 |
| 📄 年报 PDF 云端解析 + RAG | 取数面板内 | 一键下载年报 → 按页切片构建向量索引；分析与追问自动检索相关章节，结论标注「见年报第X页」 |
| 📐 规则引擎评分 | 「🚀 开始结构化分析」 | 四维评分由固定阈值规则计算（同一数据必然同一分数），页面可展开查看每个分数的完整依据链 |
| 🏢 单公司深度分析 | 左侧边栏 | 一键得到四维评分、指标图表、年报口径趋势、风险与亮点 |
| 💬 多轮追问 | 「🏢 单公司深度分析」页底 | 基于数据与已有结论连续追问（问在上、答在中、**输入框在最下面**）；年报问题实时检索原文片段（带页码），回答自动附图表；问"和 XX 对比"会**自动取对方数据** |
| ⚖️ 多公司对比 | 左侧边栏 | 2~5 家公司，**一键批量取数**，逐年对齐后生成对照表、柱状/雷达/规模图与 CSV / 报告导出 |
| 🕘 分析记录 | 左侧边栏 | 本地持久化 2 小时：刷新页面不丢，点一下即可回看当时的评分、图表与证据 |
| 📝 研报导出 | 各页的下载按钮 | 分析结果可保存为 Markdown 报告（双模型润色）；「📝 研报」页签内可直接生成 |
"""
        )

    with card("help-kb", "", "📐 行业财务基准库（同业对标）", "同一个分数，只有在行业里比才有意义"):
        st.markdown(
            """
本引擎是**面向全行业上市公司的通用财务投研引擎**。第 1 步的横向按钮可选择行业
（通用、消费、金融、新能源、医药、互联网），每个行业对应一套**财务口径**的同业对标基准
（ROE 优秀线 / 营收增速稳健线），四维评分、四象限定位与散点基准线全部按当前行业口径计算。

之所以必须做行业对标：银行天然高杠杆、薄利差，ROE 11% 已属优秀；而白酒业 ROE 11% 则严重落后。
若用同一把尺子衡量所有行业，银行会被误判为「瘦狗」、新能源会被误判为「问题型」——
**同业对标才是有效的财务判断**。

页面会实时显示当前基准（如「金融 = ROE 11% / 增速 6%」）。银行/保险等特殊行业另内置专用指标
（不良贷款率、拨备覆盖率、核心一级资本充足率等），自动切换为金融专用评分口径。
"""
        )

    with card("help-src", "", "🔗 数据来源与使用边界", "对外统一口径 + 具体实现，一一对应"):
        st.markdown(
            """
**对外统一口径：** 交易所公开披露数据 · 上市公司年报 · 财务指标库

| 数据源类型 | 具体实现 |
|---|---|
| 公告与年报原文（交易所指定披露平台） | 巨潮资讯网 |
| 行情与财务指标（公开财经数据接口） | 东方财富 |
| 大模型 | DeepSeek（推理与生成）· 千问（润色 / 向量化） |

**使用边界：**
- 在线取数会自动带入多个报告期，可进一步上传年报 PDF 补充细节；
- 扫描版 PDF 无法提取文字，请改用在线取数或手动粘贴；
- 分析结果仅供参考，不构成投资建议。
"""
        )

    with card("help-tech", "", "🛠️ 技术说明", "可复现、可核查的实现细节"):
        st.markdown(
            """
- 应用框架：Streamlit · 部署：Streamlit Community Cloud
- PDF 解析：pypdf（按页解析，保留页码）· 向量检索：ChromaDB + DashScope Embedding
- 评分：`scoring.py` 固定规则引擎（可复现、附依据链）· 输出校验：字段/类型/范围自动校验
- 统一流程：`pipeline.py`（页面与离线评测共用**同一条**链路，避免"评测分数好看但页面不是这套"）
- 对照实验：仓库内 `eval_cases.py` + `docs/对照实验报告.md`（Agent vs 直接大模型）
"""
        )

# ---------------------------------------------------------------
# 工作台调度：侧边栏导航决定渲染哪个视图
#
# 导航菜单与分析记录已经搬进侧边栏（定义在 `with st.sidebar:` 之前），
# 所以这里只剩「按选中项调视图函数」一件事 —— 主区因此拿到完整宽度。
#
# ⚠️ 实现方式：三个视图是把原来的 `with tabN:` 改成 `def _view_x():`，
#    函数体缩进不变，上千行内部逻辑一行没动，只是换了调用入口。
# ---------------------------------------------------------------
_v = st.session_state.get("main_view", "🏢")
if _v == "⚖️":
    _view_compare()
elif _v == "📖":
    _view_help()
else:
    _view_single()

# 【v12 修 · BUG-06】视图函数里发生的取数 / RAG 建库都已写入 session_state，
# 在这里按**最新**状态重画顶栏（st.empty() 覆盖写入，位置仍在页面最上方），
# 消除"取数成功后顶栏还显示『尚未选择 / 页码引用未启用』"的滞后。
_topbar_slot.markdown(_topbar_html(), unsafe_allow_html=True)

st.divider()
st.markdown(
    '<p class="footer-note">💡 <b>金融智能投研 Agent</b> · 第二届全国人工智能应用创新大赛参赛作品 · 分析结果仅供参考，不构成投资建议</p>',
    unsafe_allow_html=True,
)

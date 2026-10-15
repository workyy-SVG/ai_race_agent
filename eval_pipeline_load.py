# -*- coding: utf-8 -*-
"""pipeline 加载健壮性 + 兜底等价性测试（无浏览器 · 不联网 · 固定样本）。

背景（2026-10-15 云端线上故障）：
    页面报 `结构化分析失败：'NoneType' object has no attribute 'analyze_core'`。
    病因是 app.py 里那句

        try:
            import pipeline as _pipeline
        except ImportError:
            _pipeline = None

    把 pipeline 加载失败的**真实原因静默吞掉了**。页面照常渲染、按钮照常可点，
    直到点下「开始结构化分析」才炸出一句既不知道哪个模块、也不知道为什么的报错。

本脚本守住四件事：
  1. app.py 不再静默吞掉 pipeline 的导入异常（源码级断言，防止被改回去）
  2. 三级加载真的能降级：常规 import → 绝对路径加载 → 内置兜底，逐级实测
  3. 兜底实现与真实 pipeline **逐字段完全等价**（同一固定样本，不联网）
  4. 正常部署下页面**不会误报**降级，且启动无异常

运行（不联网、不需要 key）：
    python eval_pipeline_load.py
退出码 0 = 全部通过；1 = 有失败项（失败项会打印在最后）。
"""
import sys
import io
import os
import importlib
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
os.chdir(_HERE)

import pipeline as _pl  # noqa: E402

APP = os.path.join(_HERE, "app.py")
SRC = io.open(APP, encoding="utf-8").read()
FAILS = []


def _check(name, cond, extra=""):
    print(("  OK  " if cond else "  FAIL") + " " + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


# ============================================================================
print("=== 1) 源码级：绝不能再静默吞掉 pipeline 的加载异常 ===")
_check("已删除 `except ImportError: _pipeline = None` 这种无信息量写法",
       "except ImportError:\n    _pipeline = None" not in SRC)
_check("新增三级加载器 _load_pipeline()", "def _load_pipeline()" in SRC)
_check("支持按绝对路径加载（应对 sys.path / 工作目录异常）",
       "spec_from_file_location" in SRC)
_check("失败原因被保留（真实异常 + traceback 都留痕）",
       "_PIPELINE_ERROR" in SRC and "_PIPELINE_TRACEBACK" in SRC)
_check("报错文案带上异常类型（不再是干巴巴的 f\"{e}\"）",
       '结构化分析失败：{type(e).__name__}: {e}' in SRC)
_check("报错处提供部署自检信息", "pipeline_diag_markdown()" in SRC)
_check("页面在降级时显式提示（不静默）",
       '_PIPELINE_SOURCE == "fallback"' in SRC)


# ============================================================================
print()
print("=== 2) 页面级：正常部署不误报降级，且启动无异常 ===")
# ⚠️ 本段**必须**跑在 `import app` 之前。
#    直接 import app.py 等于在 Streamlit 运行时之外把脚本执行一遍，会污染全局的
#    表单/脚本上下文，导致随后的 AppTest 一起来就报
#        StreamlitAPIException: `st.button()` can't be used in an `st.form()`
#    —— 那是测试自己的污染，不是应用的问题（已在纯净进程里单独验证：启动 0 异常）。
try:
    from streamlit.testing.v1 import AppTest

    _at = AppTest.from_file(APP, default_timeout=240)
    _at.run()
    _at.session_state["authenticated"] = True
    _at.run()
    _exc = [str(e.value)[:160] for e in _at.exception]
    _check("页面启动无异常（认证后）", not _exc, str(_exc))
    _body = " ".join(
        [str(m.value) for m in _at.markdown]
        + [str(c.value) for c in _at.caption]
    )
    _check("正常部署下不出现「未能加载」的降级提示", "未能加载" not in _body)
    _check("按钮仍在（页面结构未被改坏）",
           any("开始结构化分析" in str(b.label) for b in _at.get("button")))
except Exception as _e:  # noqa: BLE001
    _check("页面级检查可运行", False, f"{type(_e).__name__}: {_e}")


# ============================================================================
print()
print("=== 3) 三级加载：逐级实测降级行为 ===")
import app as _app  # noqa: E402  （刻意放在页面级检查之后，避免污染 Streamlit 上下文）

# ① 常规导入
_check("正常环境下走常规 import（source == module）",
       _app._PIPELINE_SOURCE == "module",
       f"source={_app._PIPELINE_SOURCE}")
_check("正常环境下 _pipeline 就是真的 pipeline 模块",
       getattr(_app._pipeline, "__name__", "") == "pipeline")

# ② 常规 import 失败 → 按绝对路径加载
_real_import = importlib.import_module


def _boom(name, *a, **k):
    if name == "pipeline":
        raise ImportError("ModuleNotFoundError: No module named 'pipeline'（模拟 sys.path 异常）")
    return _real_import(name, *a, **k)


importlib.import_module = _boom
try:
    _r2 = _app._load_pipeline()
finally:
    importlib.import_module = _real_import
_check("常规 import 被挡掉时，按绝对路径加载成功（source == file）",
       _r2["source"] == "file", f"source={_r2['source']} err={_r2['error'][:60]}")
_check("降级到 file 时仍能拿到可用的 analyze_core",
       callable(getattr(_r2["module"], "analyze_core", None)))
_check("降级原因被如实记录（不是空字符串）", bool(_r2["error"]))

# ③ 连文件都不在（模拟云端仓库缺 pipeline.py）→ 明确报「文件不存在」
_real_exists = Path.exists


def _no_pipeline_file(self):
    if self.name == "pipeline.py":
        return False
    return _real_exists(self)


importlib.import_module = _boom
Path.exists = _no_pipeline_file
try:
    _r3 = _app._load_pipeline()
finally:
    importlib.import_module = _real_import
    Path.exists = _real_exists
_check("文件真的不存在时返回 source == none（不假装成功）", _r3["source"] == "none")
_check("诊断信息里能指出「文件不存在」+ 期望路径",
       "pipeline.py" in _r3["path"] and bool(_r3["error"]))

# 自检文案必须把这三种状态都讲清楚
_diag_ok = _app.pipeline_diag_markdown()
_check("自检表含统一流程模块状态行", "统一流程模块状态" in _diag_ok)
_check("自检表含期望文件路径与实际存在性", "期望文件路径" in _diag_ok and "该文件是否存在" in _diag_ok)
_check("自检表含真实异常行", "真实异常" in _diag_ok)


# ============================================================================
print()
print("=== 4) 兜底实现与真实 pipeline 逐字段等价（固定样本，不联网）===")
_SAMPLE_TEXT = (
    "## 财务分析\n本期营业收入 1741.00 亿元，净利润 862.00 亿元，"
    "ROE 34.00%，毛利率 91.50%。\n## 风险\n原材料价格波动风险。\n## 投资亮点\n品牌护城河深厚。\n"
)
_BASE = {
    "company": "贵州茅台",
    "summary": "见年报第 42 页，营业收入 1741 亿元，毛利率 91.50%。",
    "metrics": [
        {"item": "营业收入", "value": 1741.0, "unit": "亿元"},
        {"item": "净利润", "value": 862.0, "unit": "亿元"},
        {"item": "净资产收益率", "value": 34.0, "unit": "%"},
    ],
    "risks": ["原材料价格波动风险。"],
    "highlights": ["品牌护城河深厚。"],
    "chart_data": [{"name": "营业收入", "value": 1741.0}],
}
_HITS = [{"page": 42, "text": "本期营业收入 174,100,000,000.00 元，毛利率 91.50%。"}]
_KW = dict(industry="消费", online=False, rag_hits=_HITS, rag_pages=220)

_CORE_REAL = _pl.analyze_core(dict(_BASE), _SAMPLE_TEXT, **_KW)
_CORE_FB = _app._PipelineFallback.analyze_core(dict(_BASE), _SAMPLE_TEXT, **_KW)

_check("总分一致", _CORE_REAL["analysis"].get("total_score") == _CORE_FB["analysis"].get("total_score"),
       f"{_CORE_REAL['analysis'].get('total_score')} vs {_CORE_FB['analysis'].get('total_score')}")
_check("四维分数一致",
       (_CORE_REAL["analysis"].get("scores") or {}) == (_CORE_FB["analysis"].get("scores") or {}))
_check("评分依据（reasons）一致",
       (_CORE_REAL["analysis"].get("score_reasons") or {}) == (_CORE_FB["analysis"].get("score_reasons") or {}))
_check("覆盖率与置信标签一致",
       (_CORE_REAL["analysis"].get("score_coverage") == _CORE_FB["analysis"].get("score_coverage")
        and _CORE_REAL["analysis"].get("score_confidence") == _CORE_FB["analysis"].get("score_confidence")))
_check("数据来源标注一致（含「退回文本」的如实改写）",
       _CORE_REAL["score_source"] == _CORE_FB["score_source"],
       f"{_CORE_REAL['score_source']} vs {_CORE_FB['score_source']}")
_check("校验问题清单一致",
       (_CORE_REAL["validation_issues"] or []) == (_CORE_FB["validation_issues"] or []))
_check("证据核对问题清单一致",
       (_CORE_REAL["evidence_issues"] or []) == (_CORE_FB["evidence_issues"] or []))
_check("证据卡（数值→页码→原文）逐条一致",
       (_CORE_REAL["evidence_items"] or []) == (_CORE_FB["evidence_items"] or []))
_check("页码引用正确率一致",
       _CORE_REAL["citation"] == _CORE_FB["citation"],
       f"{_CORE_REAL['citation']} vs {_CORE_FB['citation']}")
_check("数值可定位率一致", _CORE_REAL["num_trace_rate"] == _CORE_FB["num_trace_rate"])
_check("**整份 analysis 完全相等**（最强断言）",
       _CORE_REAL["analysis"] == _CORE_FB["analysis"],
       "" if _CORE_REAL["analysis"] == _CORE_FB["analysis"] else
       str(set(_CORE_REAL["analysis"]) ^ set(_CORE_FB["analysis"]))[:120])

# 兜底也要能处理「取数失败必须如实标注来源」（P1-06 的历史防线）
_check("兜底同样会让 score_source 说真话（不冒充在线接口）",
       "在线财务指标" not in str(_CORE_FB["score_source"])
       or str(_CORE_FB["score_source"]).startswith("文本抽取"),
       str(_CORE_FB["score_source"]))
_check("兜底暴露的接口与 pipeline 对齐（4 个函数 + 常量）",
       all(hasattr(_app._PipelineFallback, _n)
           for _n in ("analyze_core", "annual_metrics", "all_annual_rows", "year_of",
                      "page_range_of", "DEFAULT_PERIODS")))


# ============================================================================
print()
if FAILS:
    print(f"FAILED  {len(FAILS)} 项：")
    for _f in FAILS:
        print("   -", _f)
    sys.exit(1)
print("ALL_PIPELINE_LOAD_TESTS_PASSED")
sys.exit(0)

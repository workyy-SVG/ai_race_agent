# -*- coding: utf-8 -*-
"""
对照实验脚本（大赛评审要点：证明本 Agent 比直接使用大模型更有价值）
====================================================================
对同一输入分别运行两种模式，输出可直接放入答辩材料的对比数据：

  A 组（基线）：直接大模型
      · 仅把财务摘要文本直接喂给 DeepSeek，无规则评分、无 RAG、无校验
  B 组（本 Agent）：**与页面同一条链路**
      · 在线结构化取数（东财财务指标接口，年报口径）
      · 固定规则引擎评分（走 pipeline.py —— 页面与评测唯一实现，可复现、同业对标）
      · 年报 RAG 检索（带页码引用，需已构建索引）
      · 输出严格校验（analysis_validate.validate_analysis —— 与页面同一实现）
      · 证据核对（数值/页码与来源对账）
      · 证据卡（每个数值 → 年报页码 + 原文片段，正向可定位）
      · 双模型协同（DeepSeek 分析 + 千问润色，若配置）

⚠️ 不要在评测脚本里"另写一套评分/校验" —— 那会让"评测结论"和"线上行为"各说各话。
   需要打分/校验/对账时统一 `pipeline.analyze_core()`（优化清单 P1-06）。

评测维度（自动项，均可复算）：
  1. 可复现性     —— 同一输入重复运行，评分是否完全一致
  2. 数值可回溯率 —— 正文数字能在来源文本中字面找到的比例（粗口径，用于对照）
  3. 页码可核验率 —— B 组引用的页码中，有多少来自实际检索到的片段
  4. 阶段耗时     —— 取数 / 评分 / 检索 / 生成 / 润色 分段计时（口径一致）
  5. 校验与证据   —— 输出校验与证据核对识别出的问题条数

  人工核对项（脚本不代替）：数值与官方财报的一致性、结论是否被证据支持。

用法（需已配置 DEEPSEEK_API_KEY / QWEN_API_KEY 环境变量或 .streamlit/secrets.toml）：
  python eval_cases.py 600519 贵州 4
  python eval_cases.py            # 默认跑 3 个案例

输出：eval_results/ 目录下的 markdown 报告与原始 JSON。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Any, List

OUT_DIR = Path(__file__).parent / "eval_results"
try:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # 只读/受限环境（沙箱、只读容器）下建不了目录时退到临时目录。
    # 评测本身照常跑完，不至于因为"写不出报告"就把整份实验作废。
    import tempfile
    OUT_DIR = Path(tempfile.gettempdir()) / "ai_race_eval_results"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CASES = [
    {"code": "600519", "kw": "贵州茅台", "periods": 4},
    {"code": "600036", "kw": "招商银行", "periods": 4},
    {"code": "300750", "kw": "宁德时代", "periods": 4},
]


def _get_secret(key: str, default=None):
    try:
        # Streamlit secrets（若本地安装了 streamlit）
        import streamlit as st
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)


def _llm(messages, temperature=0.2):
    """直接调 DeepSeek（不经过 app.py 的熔断/会话逻辑，保证实验可独立运行）。"""
    from openai import OpenAI
    client = OpenAI(
        api_key=_get_secret("DEEPSEEK_API_KEY", ""),
        base_url=_get_secret("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    resp = client.chat.completions.create(
        model=_get_secret("DEEPSEEK_MODEL", "deepseek-chat"),
        messages=messages,
        temperature=temperature,
        stream=False,
    )
    return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# A 组：直接大模型（基线）
# ---------------------------------------------------------------------------
BASELINE_PROMPT = (
    "你是一名金融投研分析师。请基于下面的财务数据，对公司做结构化分析："
    "给出盈利能力、成长性、偿债能力、现金流质量四个维度的评分（1-10 分）与总分，"
    "以及主要风险与投资建议。\n\n财务数据：\n"
)


def _num_traceability(out_text: str, source_text: str) -> Dict[str, Any]:
    """数值字面可回溯率（粗口径）：正文里出现的数字，有多少能在来源文本里找到。

    只做字面比对，因此：
      · 换算过的数字（亿元 vs 元）会算"找不到"，属于误报方向；
      · 凭空编造的数字则一定找不到。
    所以它只能作为"是否大面积脱离来源"的粗筛指标，不能当作准确率 ——
    真实准确性仍需人工逐项核对（见文末"人工核对项"）。
    """
    nums = re.findall(r"\d+(?:\.\d+)?", out_text or "")
    _years = {f"20{y:02d}" for y in range(10, 30)}
    nums = [n for n in nums if len(n) >= 2 and n not in _years]
    if not nums:
        return {"rate": None, "n": 0}
    hit = sum(1 for n in nums if n in (source_text or ""))
    return {"rate": round(hit / len(nums), 3), "n": len(nums)}


def run_baseline(text: str) -> Dict[str, Any]:
    t0 = time.time()
    out = _llm(
        [
            {"role": "system", "content": "你是专业的金融投研分析师。"},
            {"role": "user", "content": BASELINE_PROMPT + text[:12000]},
        ],
        temperature=0.3,
    )
    t1 = time.time()
    has_page_ref = bool(re.search(r"第\s*\d+\s*页", out))
    return {
        "output": out,
        "seconds": round(t1 - t0, 1),          # 只含生成阶段（与 B 组同口径分别报告）
        "page_referenced": has_page_ref,
        "score_extracted": _extract_scores_from_text(out),
        "num_trace": _num_traceability(out, text),
    }


def _extract_scores_from_text(text: str) -> Dict[str, Any]:
    """从自由文本里尽力抽取 A 组的分数（用于一致性对比）。"""
    scores = {}
    for dim in ["盈利能力", "成长性", "偿债能力", "现金流质量"]:
        m = re.search(dim + r"[^\d]{0,12}(\d+(?:\.\d+)?)\s*(?:/|分)", text)
        if m:
            scores[dim] = float(m.group(1))
    m = re.search(r"总分[^\d]{0,12}(\d+(?:\.\d+)?)", text)
    total = float(m.group(1)) if m else None
    return {"scores": scores, "total": total}


# ---------------------------------------------------------------------------
# B 组：本 Agent 流程
# ---------------------------------------------------------------------------
def run_agent(code: str, kw: str, periods: int, industry: str = "通用") -> Dict[str, Any]:
    """B 组：与页面同一条链路（取数 → RAG → 生成 → 润色 → **pipeline** 评分/校验/证据核对）。"""
    import data_source as ds
    import pipeline

    steps: List[str] = []
    timing: Dict[str, float] = {}

    # 1) 在线结构化取数（阶段计时：取数不计入生成耗时，避免与 A 组口径打架）
    _t = time.time()
    text = ds.build_summary_text(code, name=kw, periods=periods) or ""
    timing["取数"] = round(time.time() - _t, 1)
    steps.append("在线取数(巨潮+东财)")

    # 2) 确认在线财务指标可用（报告期用于后面定位年报索引）。
    #    ⚠️ 评分本身**不在这里算** —— 统一交给 pipeline.analyze_core（第 6 步），
    #    保证评测与页面是同一份实现。旧版此处本地调 scoring.compute_scores，
    #    与页面各写一套，出现"评测结论与线上分数不一致"时无法归因。
    _t = time.time()
    fina = ds.get_financial_indicators(code, periods=periods, annual_only=True)
    if not fina:
        fina = ds.get_financial_indicators(code, periods=periods)
    timing["取数2"] = round(time.time() - _t, 1)
    steps.append("在线财务指标(年报口径)")

    # 3) RAG：只有该文档索引已建好才检索（脚本不自动建库 —— 建库要付 Embedding 费用，
    #    且线上是"用户下载年报时顺带建库"。这一限制必须写在结论里，不能含糊）。
    rag_hits = []
    _t = time.time()
    try:
        import rag_engine as rag
        year = ""
        if fina:
            year = re.sub(r"\D", "", str(fina[0].get("report_date") or ""))[:4]
        if year:
            tag = rag.find_index_tag(code, year) or ""
            if rag.has_index(code, year, tag):
                rag_hits = rag.retrieve(f"{kw} 财务表现 风险因素", code, year, top_k=6, doc_tag=tag)
                steps.append(f"RAG检索({len(rag_hits)}片段,含页码)")
            else:
                steps.append(f"RAG未命中(未建 {code}/{year} 索引)")
    except Exception as _e:
        steps.append(f"RAG异常({type(_e).__name__})")
    timing["检索"] = round(time.time() - _t, 1)
    # 页码上限：pipeline 拿它判断正文引用的页码有没有越界（第 6 步用）
    _page_max = max([h.get("page") or 0 for h in rag_hits], default=0)

    # 4) LLM 分析（带检索上下文）
    ctx = (
        "请基于以下财务数据输出结构化分析（公司概况/财务分析/风险/建议），"
        "所有数字必须来自数据，禁止编造。\n\n"
        + text[:12000]
    )
    if rag_hits:
        try:
            import rag_engine
            ctx += "\n\n【年报检索片段】\n" + rag_engine.format_citations(rag_hits)
        except Exception:
            pass
    _t = time.time()
    llm_out = _llm(
        [{"role": "system", "content": "你是专业的金融投研分析师。"},
         {"role": "user", "content": ctx}],
        temperature=0.3,
    )
    timing["生成"] = round(time.time() - _t, 1)
    steps.append("LLM分析")

    # 5) 千问润色（若配置）—— 必须带上检索片段作为事实校验素材：
    #    空上下文调用润色时，它只能改文字，无法核对任何事实（旧版就是传了 None）。
    refined = None
    if llm_out:
        _t = time.time()
        try:
            import llm_qwen as lq
            if lq.is_available():
                _ctx_txt = None
                if rag_hits:
                    try:
                        import rag_engine
                        _ctx_txt = rag_engine.format_citations(rag_hits)
                    except Exception:
                        _ctx_txt = None
                refined = lq.refine_answer(llm_out, _ctx_txt)
                if refined:
                    steps.append("千问润色(带证据素材)")
        except Exception:
            pass
        timing["润色"] = round(time.time() - _t, 1)

    final_text = refined or llm_out

    # 6) 评分 + 输出校验 + 证据核对 + 证据卡 + 引用统计 —— 全部走 pipeline，
    #    与页面完全同一条链路（优化清单 P1-06：页面与评测复用同一业务流程）。
    _t = time.time()
    _parsed = _parse_llm_analysis(final_text)
    _core = pipeline.analyze_core(
        _parsed,
        raw_data=text,
        industry=industry,
        code=code,
        periods=periods,
        online=True,
        rag_hits=rag_hits,
        rag_pages=_page_max or None,
    )
    _sc = ((_core["analysis"].get("_scoring_result")) or {})
    if not _sc:  # 极端情况下 _scoring_result 被 validate 重筛掉时的兜底
        _sc = {
            "scores": _core["analysis"].get("scores") or {},
            "total_score": _core["analysis"].get("total_score"),
            "coverage": _core["analysis"].get("score_coverage", 0.0),
            "confidence": _core["analysis"].get("score_confidence", ""),
        }
    timing["评分校验"] = round(time.time() - _t, 1)
    steps.append(
        f"pipeline评分+校验(覆盖率{float(_sc.get('coverage') or 0) * 100:.0f}%)"
        if float(_sc.get("coverage") or 0) > 0 else "pipeline评分+校验"
    )

    # 7) 页码可核验率：引用的页码中有多少来自实际检索到的片段
    _cite = {int(x) for x in re.findall(r"第\s*(\d+)\s*页", final_text)}
    _hit_pages = {int(h.get("page") or 0) for h in rag_hits}
    _page_ok = (round(len(_cite & _hit_pages) / len(_cite), 3) if _cite else None)

    return {
        "output": final_text,
        "seconds": round(sum(timing.values()), 1),
        "timing": timing,
        "page_referenced": bool(re.search(r"第\s*\d+\s*页", final_text)),
        "page_verifiable_rate": _page_ok,
        "scores": _sc.get("scores") or {},
        "total": _sc.get("total_score"),
        "coverage": _sc.get("coverage", 0.0),
        "confidence": _sc.get("confidence", ""),
        "evidence_issues": len(_core["evidence_issues"]),
        "validation_issues": len(_core["validation_issues"]),
        "num_trace": _num_traceability(final_text, text),
        # ↓ 新增：与页面同源的可核验指标
        "num_trace_rate": _core["num_trace_rate"],
        "citation_rate": (_core["citation"] or {}).get("rate"),
        "evidence_items_count": _core["analysis"].get("evidence_items_count", 0),
        "score_period": _core["score_period"],
        "score_source": _core["score_source"],
        "finance_kind": _core["analysis"].get("score_finance_kind") or "",
        "steps": steps,
        "metrics_source": (
            f"{_core['score_source']}（{_core['score_period'] or '报告期未标注'}）"
        ),
        "validated": True,   # 与页面共用 analysis_validate + pipeline 的实现
    }


def _parse_llm_analysis(text: str) -> Dict[str, Any]:
    """把 LLM 的自由文本整理成 pipeline 能消费的最小结构。

    只做**保守抽取**：抽不到就留空，宁可让 pipeline 走"文本抽取"兜底，
    也不要为了凑字段而臆造数值。
    """
    _t = text or ""
    metrics: List[Dict[str, Any]] = []
    _pat = re.compile(
        r"(营业收入|净利润|净资产收益率|ROE|毛利率|净利率|资产负债率)"
        r"[^\d\-+]{0,14}(-?\d+(?:\.\d+)?)\s*(亿元|万元|%|元)"
    )
    for _m in _pat.finditer(_t):
        metrics.append({"item": _m.group(1), "value": float(_m.group(2)), "unit": _m.group(3)})
    risks, highs = [], []
    _sec = None
    for _line in _t.splitlines():
        _s = _line.strip(" -*#")
        if not _s:
            continue
        if "风险" in _s[:8]:
            _sec = "risk"
            continue
        if any(k in _s[:8] for k in ("亮点", "优势", "投资亮点")):
            _sec = "high"
            continue
        if len(_s) >= 6:  # < 6 字的多为标题碎屑，不值得当结论引用
            (risks if _sec == "risk" else highs if _sec == "high" else []).append(_s[:200])
    return {
        "company": "",
        "summary": _t[:4000],
        "metrics": metrics[:40],
        "risks": risks[:12],
        "highlights": highs[:12],
    }


def consistency_check(code: str, kw: str, periods: int, industry: str = "通用", n: int = 3):
    """B 组可复现性：同一条 pipeline 跑 n 次，评分结果必须完全一致；A 组无法保证（自由生成）。

    ⚠️ 必须走与页面相同的 pipeline（内部仍用年报口径 annual_only=True），
    否则"评测里测的可复现"和"线上跑的那套"根本不是一个东西。
    """
    import data_source as ds
    import pipeline
    text = ds.build_summary_text(code, name=kw, periods=periods) or ""
    results = []
    for _ in range(n):
        _c = pipeline.analyze_core(
            {}, raw_data=text, industry=industry, code=code, periods=periods, online=True,
        )
        results.append(json.dumps(
            _c["analysis"].get("scores"), ensure_ascii=False, sort_keys=True,
        ))
    return len(set(results)) == 1


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    import sys
    cases = DEFAULT_CASES
    if len(sys.argv) >= 3:
        cases = [{"code": sys.argv[1], "kw": sys.argv[2], "periods": int(sys.argv[3]) if len(sys.argv) > 3 else 4}]

    import data_source as ds
    report_lines = [
        "# 对照实验结果（自动生成）",
        "",
        "> A 组：直接大模型（纯文本输入，自由打分）",
        "> B 组：本 Agent（结构化取数 + 年报口径规则评分 + RAG + 输出校验 + 证据核对）",
        ">",
        "> **口径说明（务必保留）**：耗时按阶段分别计时，A 组只统计生成阶段、B 组各阶段分列，",
        "> 不把「取数时间」混进生成时间；数值可回溯率为**字面比对**的粗口径（换算过的数字会误报为找不到），",
        "> 不能当作准确率，真实准确性仍需人工逐项核对；B 组的 RAG 仅在索引已存在时生效（脚本不自动建库）。",
        "",
    ]

    for c in cases:
        code, kw, periods = c["code"], c["kw"], c["periods"]
        industry = ds.guess_industry(kw) or "通用"
        print(f"\n===== 案例：{kw}（{code}）=====")

        text = ds.build_summary_text(code, name=kw, periods=periods) or ""
        if not text:
            print("  取数失败，跳过")
            continue

        print("  [A] 直接大模型运行中……")
        A = run_baseline(text)
        print(f"      耗时 {A['seconds']}s · 页码引用: {'有' if A['page_referenced'] else '无'}")

        print("  [B] Agent 流程运行中……")
        B = run_agent(code, kw, periods, industry)
        print(f"      耗时 {B['seconds']}s · 步骤: {' → '.join(B['steps'])}")

        repro = consistency_check(code, kw, periods, industry)
        print(f"      评分可复现性: {'✅ 3/3 完全一致' if repro else '❌'}")

        (OUT_DIR / f"A_{code}.md").write_text(A["output"], encoding="utf-8")
        (OUT_DIR / f"B_{code}.md").write_text(B["output"], encoding="utf-8")

        repro_txt = "✅ 可复现" if repro else "❌"
        _at = A.get("num_trace") or {}
        _bt = B.get("num_trace") or {}
        _fmt_rate = lambda d: (f"{d['rate'] * 100:.0f}%（n={d['n']}）" if d.get("rate") is not None else "—")
        _b_total = "未达覆盖率门槛（不提供综合评价）" if B["total"] is None else B["total"]
        report_lines += [
            f"## {kw}（{code}）· 行业：{industry}",
            "",
            "| 维度 | A 组：直接大模型 | B 组：本 Agent |",
            "|---|---|---|",
            f"| 生成阶段耗时 | {A['seconds']}s | {B['timing'].get('生成', 0)}s |",
            f"| 全链路耗时（分段） | — | " + " / ".join(f"{k} {v}s" for k, v in B["timing"].items()) + " |",
            f"| 页码引用（有/无） | {'✅' if A['page_referenced'] else '❌ 无'} | {'✅' if B['page_referenced'] else '❌ 无'} |",
            f"| 页码可核验率 | 不可核验（无检索来源） | "
            + (f"{B['page_verifiable_rate'] * 100:.0f}%" if B.get("page_verifiable_rate") is not None else "—")
            + " |",
            f"| 数值可回溯率（粗口径） | {_fmt_rate(_at)} | {_fmt_rate(_bt)} |",
            f"| 数值可定位率（pipeline · 单位链换算后） | — | "
            + (f"{B['num_trace_rate'] * 100:.0f}%"
               if B.get("num_trace_rate") is not None else "无待核数值") + " |",
            f"| 页码引用正确率（pipeline · 越界即判失败） | — | "
            + (f"{B['citation_rate'] * 100:.0f}%（{B.get('citation_rate')}）"
               if B.get("citation_rate") is not None else "正文未引用页码") + " |",
            f"| 输出字段校验 | 无 | 已启用，发现 {B.get('validation_issues', 0)} 个字段问题 |",
            f"| 输出校验 | 无 | 已启用（与页面同一实现） |",
            f"| 证据核对疑点 | 无此环节 | {B.get('evidence_issues', 0)} 条 |",
            f"| 评分来源 | 模型自由生成（每次可能不同） | 固定规则引擎（{repro_txt}） |",
            f"| 评分结果 | {json.dumps(A['score_extracted'], ensure_ascii=False)} | "
            f"总分 {_b_total} · {json.dumps(B['scores'], ensure_ascii=False)} · 覆盖率 {B['coverage'] * 100:.0f}%（{B['confidence']}） |",
            f"| 数据来源 | 用户手动提供的文本 | {B['metrics_source']}"
            + (f"（金融子行业：{B['finance_kind']}）" if B.get("finance_kind") else "") + " |",
            f"| 流水线 | 单次 LLM 调用 | {' → '.join(B['steps'])} |",
            "",
            f"- A 组输出：`eval_results/A_{code}.md`",
            f"- B 组输出：`eval_results/B_{code}.md`",
            "",
        ]

    report_lines += [
        "---",
        "",
        "## 仍需人工核对的部分（脚本不作数）",
        "",
        "1. **数值准确性**：关键指标与官方财报/年报原文是否一致（脚本只能做字面回溯，不能判断口径）。",
        "2. **结论支持度**：每条结论能否在其引用的证据里找到支撑；证据不足时是否如实说明。",
        "3. **期间与单位**：年报/中报口径是否标注清楚，亿元/元换算是否正确。",
        "4. **规则阈值合理性**：行业基准线（ROE 优秀线 / 增速稳健线）是否符合专业判断 ——",
        "   可复现性只能证明「算得一致」，不能证明「算得对」。",
        "",
        "## 运行前提示",
        "",
        "- B 组的 RAG 只在索引已存在时生效；本脚本**不自动建库**（建库会产生 Embedding 费用，",
        "  线上是「用户下载年报时顺带建库」）。若需对照 RAG 增益，请先在页面里下载并解析该年报。",
        "- 同一份结果重复运行应完全相同（规则评分可复现）；若分数变化，说明取数源数据发生了变化，",
        "  应先固定数据快照再比较。",
        "",
    ]
    (OUT_DIR / "对照实验结果.md").write_text("\n".join(report_lines), encoding="utf-8")
    print(f"\n✅ 报告已生成：{OUT_DIR / '对照实验结果.md'}")


if __name__ == "__main__":
    main()

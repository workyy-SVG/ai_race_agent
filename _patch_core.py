# -*- coding: utf-8 -*-
"""一次性补丁：把 app.py 里内联的「评分 + 校验 + 证据核对」整段替换为 pipeline.analyze_core()。

只做一次，成功后可删。用法: python _patch_core.py
"""
import io
import sys

PATH = "app.py"

NEW_BLOCK = '''                # 3) 规则评分 + 输出校验 + 证据核对 / 证据卡
                #    ⚠️ 整段与离线评测共用 pipeline.analyze_core()（P1-06：页面和评测复用同一业务流程）。
                #    各自写一份的后果是"评测报告证明的其实是另一条流水线"——
                #    页面改了阈值或口径，评测脚本不会跟着变，结论自然不可信。
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
                for _iss in _core["validation_issues"]:
                    st.warning(f"输出校验：{_iss}")

                # 4) 证据核对：把"看起来专业"的数字与页码引用，与真正拿到的来源对一遍。
                #    格式校验只能保证"字段合法"，保证不了"数字有出处"——
                #    一个凭空写出的 99999 亿元营收、一个第 99999 页的引用，格式上完全合法。
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

                # 4b) 证据卡：把每个数值**正向**定位回它的出处（页码 + 原文片段）。
                #     P1-05 要求「关键结论可定位到支持的原文」—— 只报"哪些找不到"是不够的，
                #     还得能指着说"这一条出自第 42 页这一段"。
                st.session_state["evidence_items_last"] = _core["evidence_items"]
                _ev_items = _core["evidence_items"]
                _ev_located = [e for e in _ev_items if e.get("found") and e.get("page")]
                _ev_num = [e for e in _ev_items if isinstance(e.get("value"), (int, float))
                           and not isinstance(e.get("value"), bool)]
                if _ev_num:
                    _ev_title = f"🔗 证据卡：{len(_ev_located)}/{len(_ev_num)} 项数值可定位到年报原文"
                    with st.expander(_ev_title, expanded=False):
                        _ev_df = pd.DataFrame([
                            {
                                "类型": e.get("kind", ""),
                                "指标": e.get("item", ""),
                                "数值": (f"{e['value']:g}"
                                         if isinstance(e.get("value"), (int, float))
                                         and not isinstance(e.get("value"), bool) else str(e.get("value", ""))),
                                "页码": (f"第 {e['page']} 页" if e.get("page") else "—"),
                                "定位结果": "✅ " + str(e.get("source", "")) if e.get("found")
                                            else "⚠️ " + str(e.get("source", "")),
                                "原文片段": str(e.get("snippet") or ""),
                            }
                            for e in _ev_items
                        ])
                        st.dataframe(_ev_df, use_container_width=True, hide_index=True)
                        st.caption(
                            "定位规则：数值能在「年报检索片段 / 在线取数摘要」中找到出处即算命中"
                            "（已容忍千分位、单位换算与两位小数舍入）。"
                            "若未建立年报向量索引，则无法定位到页码，只能定位到取数摘要。"
                        )
                _cit = _core.get("citation") or {}
                if _cit.get("rate") is not None:
                    st.caption(
                        f"🧭 页码引用正确率：{_cit.get('ok', 0)}/{_cit.get('refs', 0)}"
                        f"（{_cit['rate'] * 100:.0f}%）—— 指正文引用的「第X页」落在年报真实页数范围内的比例。"
                    )
'''


def main():
    src = io.open(PATH, encoding="utf-8").read()
    lines = src.splitlines(keepends=True)
    start = None
    end = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("# 3) 规则引擎评分"):
            start = i
        if start is not None and ln.strip().startswith('st.session_state["analysis"] = analysis'):
            end = i
            break
    if start is None or end is None:
        print("FAIL: 未找到目标代码块", start, end)
        return 1
    out = lines[:start] + [NEW_BLOCK] + lines[end:]
    io.open(PATH, "w", encoding="utf-8").write("".join(out))
    print(f"OK: 替换第 {start + 1} ~ {end} 行（共 {end - start} 行 → {len(NEW_BLOCK.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

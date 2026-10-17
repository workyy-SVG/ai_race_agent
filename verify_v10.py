# -*- coding: utf-8 -*-
"""v10 改动回归验证：登录页 / 布局 / 对比批量取数 / 股票池过滤 / 帮助流程图 / 研报页签 / 持久化。"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = r"C:/Users/admin/ai_race_agent"
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

# 【v11 修】持久化缓存必须在 import app **之前**重定向到临时目录：
#   ① 沙箱/CI 环境可能拦 .streamlit/ 目录写入 → _hist_save 静默失败 →
#      "写入后能读回" 这组断言假阴性（v11 回归真实踩过）；
#   ② 真实浏览器会话留下的缓存文件也会污染
#      "过期条目被过滤（当前仅 1 条测试记录）" 这条断言 —— 它要求干净环境。
# app.py 读 ANALYSIS_HISTORY_CACHE 环境变量决定缓存路径，这里指向 tempfile。
import tempfile
_TF = tempfile.mkdtemp(prefix="hist_v10_")
os.environ["ANALYSIS_HISTORY_CACHE"] = os.path.join(_TF, "_hist_cache.json")

from streamlit.testing.v1 import AppTest

FAIL = []


def ck(cond, label, detail=""):
    print(("  OK   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAIL.append(label)


at = AppTest.from_file("app.py", default_timeout=60)
at.session_state["authenticated"] = True
at.run()
ck(not at.exception, "登录态下应用无异常", at.exception)

# ---- 1) 登录页简介已删除（未登录时不应再出现「演示路径」）----
at2 = AppTest.from_file("app.py", default_timeout=60)
at2.run()
ck(not at2.exception, "未登录分支无异常", at2.exception)
_all_md = " ".join(str(getattr(m, "value", "")) for m in at2.markdown)
ck("演示路径" not in _all_md, "登录页已删除「演示路径 + 四条简介」")
ck("账号登录" in _all_md, "登录卡仍正常渲染")

# ---- 2) 热门股票：行业过滤函数 ----
import app as _app
_opts, _f = _app._popular_options_for("消费")
ck(_f and not any("招商银行" in o for o in _opts), "行业=消费 时过滤掉金融公司", _opts[:5])
ck(any("贵州茅台" in o for o in _opts), "行业=消费 时包含消费公司")
_opts2, _f2 = _app._popular_options_for("通用")
ck((not _f2) and len(_opts2) > 30, "行业=通用 时展示全量股票池", len(_opts2))

# ---- 3) 新浪兜底数据源 ----
import data_source as ds
_q = ds.sina_quote("600519")
ck(bool(_q and _q.get("price")), "sina_quote 返回茅台实时价", _q)
_rows = ds.sina_financial_indicators("600519")
ck(bool(_rows) and all(k in _rows[0] for k in ("report_name", "roe", "debt_ratio")),
   "sina 财务指标兜底可用", _rows[:1])

# ---- 4) 分析记录持久化函数 ----
ck(hasattr(_app, "_hist_save") and hasattr(_app, "_hist_load"), "持久化函数存在")
_app._hist_save([{"id": "t1", "_ts": __import__("time").time(), "name": "测试"}])
ck(bool(_app._hist_load()), "写入后能读回（TTL 内）")
ck(_app._hist_load()[0].get("name") == "测试", "读回内容一致")
ck(len(_app._hist_load()) == 1, "过期条目被过滤（当前仅 1 条测试记录）")

# ---- 5) 图表函数 ----
_scores = [{"盈利能力": 8, "成长能力": 6, "偿债能力": 7, "现金流质量": 5}] * 2
ck(_app.fig_compare_radar(_scores, ["甲公司", "乙公司"]) is not None, "对比雷达图可生成")
_mets = [{"revenue": 100, "net_profit": 20}, {"revenue": 300, "net_profit": 40}]
ck(_app.fig_compare_scale_n(_mets, ["甲公司", "乙公司"]) is not None, "规模对比图可生成")
ck(_app._chat_viz_kind_for("评分哪项强") == "radar", "追问图表类型识别（评分→雷达）")
ck(_app._chat_viz_kind_for("营收趋势如何") == "trend", "追问图表类型识别（默认→趋势）")
_b, _g = _app._chat_auto_compare("茅台和五粮液相比谁更强？")
ck(isinstance(_b, list) and isinstance(_g, list), "追问自动对比函数可运行", (_b, _g))

# ---- 6) 单公司布局：纵向容器 ----
_src = open("app.py", encoding="utf-8").read()
ck('st.container(key="sg-r1")' in _src and "st.columns([1, 1, 1.15]" not in _src,
   "单公司步骤已改为纵向逐行容器")

print()
if FAIL:
    print(f"FAILED: {len(FAIL)} -> {FAIL}")
    sys.exit(1)
print("ALL_V10_TESTS_PASSED")

# -*- coding: utf-8 -*-
"""验证「继续追问」的渲染顺序：问在上 → 答在中 → **输入框在最下面**。

背景（这是个真实踩过的坑）：
``st.chat_input`` 只在「页面主体」才会钉在屏幕底部；写在 ``st.tabs`` 这类容器里
是**内联渲染**（streamlit 内部按 ``ancestor_block_types`` 判断 position="inline"），
也就是说"谁先执行谁在上面"。旧版把 chat_input 写在生成回答**之前**，于是
提问后输入框先冒出来、回答又出现在输入框下面，读起来像"答在问前面"。
现在把顺序改成：历史(含本轮提问) → 本轮回答 → 推荐问题 → 输入框。

不联网、不需要 API Key：把 ``DAILY_LLM_LIMIT`` 设成 0，``llm_stream`` 会在调用
API 之前就短路返回空串（check_quota 熔断），因此可以确定性地跑完整条生成链路。

用法：``python eval_chat_order.py``
"""
import io
import os
import sys
import atexit
from tempfile import TemporaryDirectory

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
os.chdir(_HERE)
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["DAILY_LLM_LIMIT"] = "0"          # ← 熔断 stub：不真的调 LLM
os.environ["INDUSTRY_STAT_OFF"] = "1"
_fixture_dir = TemporaryDirectory(prefix="chat-order-test-")
atexit.register(_fixture_dir.cleanup)
os.environ["ANALYSIS_HISTORY_CACHE"] = os.path.join(_fixture_dir.name, "history.json")

from streamlit.testing.v1 import AppTest   # noqa: E402

APP = os.path.join(_HERE, "app.py")
SRC = io.open(APP, encoding="utf-8").read()

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  OK  {name}")
    else:
        print(f"  FAIL {name}  {extra}")
        FAILS.append(name)


def ss(at, k, default=None):
    """AppTest 的 session_state 没有 .get() -> 安全读取。"""
    try:
        return at.session_state[k]
    except Exception:
        return default


def _seq_of(node):
    """对某个容器做深度优先遍历（按 delta_path 的整数索引），返回类名顺序。"""
    seq = []

    def walk(n):
        # 容器节点（chat_message 本身就是 Block）也要记名字，否则找不到它
        seq.append(type(n).__name__)
        ch = getattr(n, "children", None)
        if isinstance(ch, dict):
            for k in sorted(ch):
                walk(ch[k])

    walk(node)
    return seq


def order_of(at):
    """页面上的元素先后顺序（类名列表）。

    ⚠️ 必须遍历**整棵树**（at._tree），不能只遍历 at.main：

    ``st.chat_input`` 的定位由「父容器」决定（streamlit/elements/widgets/chat.py）——
    直接挂在**页面主体最外层**时是 ``position="bottom"``（钉在页面底部），
    套进任何容器（tab / column / container）里都退化成 ``position="inline"``。

    bottom 形态下 chat_input **不是 at.main 的子节点**，Streamlit 会把它放进
    根节点下另一个独立的 Block（实测 index=3，index 0=main、1=sidebar）。
    只 walk main 会直接 ``ValueError: 'ChatInput' is not in list`` ——
    看起来像"输入框没了"，其实只是换了容器。

    遍历顺序按根节点子索引升序：main → sidebar → 底部块，
    正好等价于「主区内容在上、钉底输入框在下」，可以直接拿来比较先后。
    """
    return _seq_of(at._tree)


def order_of_main(at):
    """只取主区（at.main）的顺序，用来判断输入框是内联还是钉底。"""
    return _seq_of(at.main)


def boot():
    at = AppTest.from_file(APP, default_timeout=240)
    # Explicit fixture secrets: local secrets take precedence over environment
    # variables, so the environment-only quota stub could call a real provider.
    at.secrets = {"DAILY_LLM_LIMIT": "0", "APP_USER": "chat-fixture",
                  "APP_PASSWORD": "fixture-only-password", "DEMO_KEY": "fixture-only-token"}
    at.run()
    at.session_state["authenticated"] = True
    at.run()
    return at


print("=== 追问区：渲染顺序 ===")
at = boot()
# 先注入取数结果跑一轮，让 data_sig 指纹稳定，否则随后注入的 analysis 会被清掉
at.session_state["single_online_code"] = "600519"
at.session_state["single_online_name"] = "贵州茅台"
at.session_state["single_online"] = "贵州茅台（600519）在线取数摘要文本"
at.run()
at.session_state["analysis"] = {
    "company": "贵州茅台", "summary": "s",
    "scores": {"盈利能力": 9.0, "成长性": 8.0}, "total_score": 8.5,
    "metrics": [], "chart_data": [], "risks": [], "highlights": [],
}
at.run()
check("页面无异常（空对话）", not [str(e.value)[:200] for e in at.exception],
      str([str(e.value)[:200] for e in at.exception]))
check("追问区标题已渲染", any("继续追问" in str(m.value) for m in at.markdown))
check("输入框已渲染（空对话）", len(at.chat_input) >= 1)
_seq = order_of(at)
check("空对话时也满足：聊天消息 → 输入框",
      ("ChatMessage" not in _seq) or (_seq.index("ChatInput") > _seq.index("ChatMessage")),
      str(_seq[-6:]))

print("=== 有历史对话：问 → 答 → 输入框 ===")
at.session_state["messages"] = [
    {"role": "user", "content": "第一问：营收趋势？"},
    {"role": "assistant", "content": "第一答：稳步增长。"},
]
at.run()
check("历史对话无异常", not [str(e.value)[:200] for e in at.exception])
_seq = order_of(at)
_first_msg = _seq.index("ChatMessage")
_last_msg = len(_seq) - 1 - _seq[::-1].index("ChatMessage")
_in_idx = _seq.index("ChatInput")
check("输入框在所有聊天消息**之后**（问在上、答在中、输入框在最下）",
      _in_idx > _last_msg, f"msg[{_first_msg}..{_last_msg}] input@{_in_idx}")
check("两条历史消息都在输入框之前", _first_msg < _in_idx)
_blob = "\n".join(str(m.value) for m in at.markdown)
check("历史消息内容确实渲染出来了",
      ("第一问：营收趋势？" in _blob) and ("第一答：稳步增长。" in _blob))

print("=== 提交一轮追问：新问答入队并渲染在输入框上方 ===")
_n_before = len(ss(at, "messages"))
# ⚠️ AppTest 无法模拟 st.chat_input 的「提交」：ChatInput.set_value() 只相当于
#    把文字**打进输入框**（streamlit 内部只把值塞进 proto.value，不触发提交）。
#    所以这里按提交处理器做的事**等价复现**：用户问题入队 + 置 _answer_pending。
#    （入队逻辑本身用源码断言守住，见文末。）
_ASK = "第二问：毛利率如何？"
_msgs0 = list(ss(at, "messages") or [])
_msgs0.append({"role": "user", "content": _ASK})
at.session_state["messages"] = _msgs0
at.session_state["_answer_pending"] = _ASK
at.run()
_msgs = ss(at, "messages") or []
check("提交后无异常", not [str(e.value)[:200] for e in at.exception],
      str([str(e.value)[:200] for e in at.exception]))
check("消息数 +2（用户提问 + 助手回答）", len(_msgs) == _n_before + 2,
      f"{_n_before} -> {len(_msgs)}")
check("用户提问已入队且内容一致",
      len(_msgs) >= 2 and _msgs[-2].get("role") == "user"
      and "第二问" in str(_msgs[-2].get("content")), str(_msgs[-2:]))
check("助手回答已入队", len(_msgs) >= 1 and _msgs[-1].get("role") == "assistant")
check("_answer_pending 已被消费（不会重复回答）", not ss(at, "_answer_pending", ""))
check("关键：生成回答的那一轮，输入框**仍然渲染**（修掉 `a or b` 短路导致输入框消失）",
      len(at.chat_input) >= 1)
_seq = order_of(at)
_last_msg = len(_seq) - 1 - _seq[::-1].index("ChatMessage")
# 输入框有两种合法形态，都满足「问在上、答在中、输入框在最下面」：
#   · 在 at.main 里 → 内联在追问区（外面套了 tab/column/container）
#   · 不在 at.main 里 → Streamlit 把它 position="bottom" 钉在页面底部
# 改版后单公司视图直接在页面主体根层调用，于是变成后者（钉底）。
# 这里只报告形态、不做硬断言 —— 硬断言会把它钉死成某一种，反而变脆。
_where = ("内联在追问区（position=inline）" if "ChatInput" in order_of_main(at)
          else "钉在页面底部（position=bottom）")
check(f"本轮新增的问答也落在输入框上方（输入框形态：{_where}）",
      _seq.index("ChatInput") > _last_msg, str(_seq[-8:]))

print("=== 源码层面的顺序约束（防止以后被改回去） ===")
_i_in = SRC.index("st.chat_input(")
_i_ans = SRC.index('llm_stream(msgs, box, action="followup")')
check("源码中 chat_input 出现在回答生成之后（= 屏幕上更靠下）", _i_in > _i_ans,
      f"chat_input@{_i_in} answer@{_i_ans}")
_code_lines = [ln for ln in SRC.splitlines() if not ln.strip().startswith("#")]
check("chat_input 未被 `or` 短路包裹（必须无条件实例化）",
      not any("or st.chat_input(" in ln for ln in _code_lines),
      str([ln for ln in _code_lines if "st.chat_input(" in ln]))
check("推荐问题已收进可折叠区，避免把输入框推远",
      'st.expander("🎯 智能推荐提问' in SRC)
_PUSH = 'st.session_state["messages"].append({"role": "user", "content": _new_q})'
_PEND = 'st.session_state["_answer_pending"] = _new_q'
check("提交处理器顺序：先入队用户提问 → 再置待答队列",
      SRC.index(_PUSH) < SRC.index(_PEND))
check("推荐问题按钮也同样先入队（否则推荐提问会在对话流里消失、并错删上一条回答）",
      'st.session_state["messages"].append({"role": "user", "content": _sq})' in SRC)
check("清空按钮仍在（对话可重置）", 'key="clear_chat"' in SRC)

print("=== 多栏网格与钉底的共存前提（v6 新增） ===")
# 背景：v6 把单公司 1~5 步排成 st.columns(3) 多栏网格，卡片用 `col=` 挂进某一列。
# Streamlit 判定 chat_input 位置的唯一条件是（见 elements/widgets/chat.py）：
#     root_container == MAIN and not _ancestor_block_types
# 所以"根层先建网格 → 卡片进列 → 之后再在根层调 chat_input"是安全的。
# 但这条前提**不能靠记忆**：一旦有人把追问区也包进 `with col:` / `with st.container()`，
# 它就会静默退化成内联（钉底消失，而且不报错）。这里用一段最小合成 app
# 现场把 root / ancestors 读出来，把结论变成"测出来的"。
import json                      # noqa: E402
import tempfile                  # noqa: E402
_R = os.path.join(tempfile.gettempdir(), "ai_race_chat_pin_probe.json")
try:
    os.remove(_R)
except OSError:
    pass
_PROBE_SRC = """
import json
import streamlit as st

# 与 app.py 的单公司布局保持一致：**两行**网格，卡片全都住进列里
g = st.columns([1, 1, 1.15], gap="medium")
for _i in range(3):
    with g[_i].container(key="kcard-c" + str(_i)):
        st.write("card " + str(_i))
g2 = st.columns([2, 1], gap="medium")
for _j in range(2):
    with g2[_j].container(key="kcard-d" + str(_j)):
        st.write("card " + str(_j))

_dg = st.chat_input.__self__._active_dg
json.dump({"root": int(_dg._root_container),
           "ancestors": list(_dg._ancestor_block_types)},
          open(r"__RESULT__", "w", encoding="utf-8"))
st.chat_input("继续追问")
""".replace("__RESULT__", _R)
_pf = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
_pf.write(_PROBE_SRC)
_pf.close()
_pat = AppTest.from_file(_pf.name, default_timeout=60)
_pat.run()
_probe = {}
if os.path.exists(_R):
    with io.open(_R, encoding="utf-8") as _fh:
        _probe = json.load(_fh)
check("多栏网格建好后，根层仍是「MAIN + 无祖先 block」—— chat_input 仍会钉底",
      _probe.get("root") == 0 and _probe.get("ancestors") == [],
      f"probe={_probe} exceptions={[str(e.value)[:120] for e in _pat.exception]}")
# v10（用户第 2 条）：单公司 1~5 步改成**一行一个板块**，不再"从左到右再从左到右"。
# 实现上从 st.columns 换成纵向 st.container —— 列会把卡片横向切开、窄屏被压成一条；
# 容器则整块从上往下排，卡片宽度始终铺满。
check("单公司 1~5 步改为纵向逐行容器（一行一个板块，不再横向分栏）",
      '_single_grid = [' in SRC and 'st.container(key="sg-r1")' in SRC
      and '_single_grid_r2 = [st.container(key="sg-r4"), st.container(key="sg-r5")]' in SRC
      and 'st.columns([1, 1, 1.15], gap="medium")' not in SRC)
# 追问区必须留在页面根层：不进卡片、也不进列
_CHAT_SRC = SRC.split("# ---- 追问对话（多轮，上下文实时更新） ----", 1)[-1]
_CHAT_HEAD = _CHAT_SRC.split("_typed = st.chat_input", 1)[0]
check("追问区没有被包进卡片容器（父容器一变就丢钉底）",
      "with card" not in _CHAT_HEAD)
check("追问区没有被塞进多栏网格的列里（列同样是容器）",
      "col=_single_grid" not in _CHAT_HEAD and "col=_run_col" not in _CHAT_HEAD)

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL_CHAT_ORDER_TESTS_PASSED")

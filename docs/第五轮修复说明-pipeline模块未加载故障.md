# 第五轮修复说明：`pipeline` 模块未加载 → `'NoneType' object has no attribute 'analyze_core'`

> 现象来自线上截图：点击「🚀 开始结构化分析」后报
> `结构化分析失败：'NoneType' object has no attribute 'analyze_core'。可重试，或检查数据是否完整。`
> 本轮把这句"天书"从根上治掉：**既让它不再出现，也让它再出现时一眼可查。**

---

## 一、这句话到底是什么意思

`analyze_core` 是统一流程模块 `pipeline` 里的函数。这句报错的字面含义是：

> 我要调用 `pipeline.analyze_core()`，但 `pipeline` 这个变量是 `None`。

`pipeline` 为什么是 `None`？看旧代码（问题就出在这三行）：

```python
try:
    import pipeline as _pipeline      # ← 加载失败
except ImportError:
    _pipeline = None                  # ← 于是它变成 None，且**真实原因被丢掉**
```

`pipeline` 没能在部署环境里加载成功 → 被赋值成 `None` → 页面**照常渲染、按钮照常可点**
（因为 app.py 顶层不再抛异常了）→ 直到点下按钮那一刻才炸出这句报错。

**旧写法真正的毛病不是"崩了"，而是"崩得没有信息"**：异常类型、异常原文、期望路径、
到底哪个模块有问题——全都没留下。所以只能靠猜。

---

## 二、为什么它会加载失败

`pipeline.py` 的依赖只有三样：`data_source` / `scoring` / `analysis_validate`，
而这三样 app.py 在更早的位置**已经导入成功**（否则页面根本起不来）。也就是说
`pipeline` 的依赖不可能缺。剩下只有两种可能：

| 可能性 | 典型成因 | 判定方法（新版页面已内置） |
|---|---|---|
| **A. 部署环境里没有这个文件** | 云端工作目录尚未同步到含 `pipeline.py` 的提交；容器是旧的/重建未完成 | 自检表里「该文件是否存在」会显示 **否** |
| **B. 文件在，但 `import` 找不到** | `sys.path` / 工作目录不是 app.py 所在目录（云端部署常见坑） | 自检表显示存在=是，但状态是「按绝对路径加载成功」 |

⚠️ 补充一个容易被忽略的点：`ModuleNotFoundError` 是 `ImportError` 的**子类**，
所以"文件不存在"和"模块内部导入报错"在旧的 `except ImportError` 里**长得一模一样**，
根本分不出来。这也是旧写法必须改掉的原因。

---

## 三、本轮改了什么

### 1. 三级降级加载，每一级都留下可读证据（`_load_pipeline()`）

```
① 常规 import                 → 正常情况
   ↓ 失败则记录：异常类型 + 异常原文 + 完整 traceback
② 按 app.py 所在目录用绝对路径加载同名文件
   → 专治 sys.path / 工作目录异常（文件明明在仓库里却 import 不到）
   ↓ 仍失败
③ 挂上内置的**等价兜底**实现，按钮依然可用
```

任何一级成功/失败都写进 `_PIPELINE_SOURCE` / `_PIPELINE_ERROR` / `_PIPELINE_TRACEBACK`，
不再有"静默变成 None"这种状态。

### 2. 报错自带病因（原来只有一句 `f"{e}"`）

```python
st.error(f"结构化分析失败：{type(e).__name__}: {e}"
         " —— 已保留完整技术细节，展开下方「排查信息」即可定位。")
with st.expander("🔧 排查信息（模块加载状态 + 完整调用堆栈）", expanded=True):
    if isinstance(e, AttributeError) and "NoneType" in str(e):
        st.warning("典型的「统一流程模块未加载」症状……")
    st.markdown(pipeline_diag_markdown())      # 部署自检表
    st.code(traceback.format_exc())            # 完整堆栈
```

### 3. 页面级降级提示（不静默，不用等点按钮才知道）

`_PIPELINE_SOURCE == "fallback"` 时，在「开始结构化分析」按钮**上方**直接给出：

> ⚠️ 统一流程模块 pipeline 未能加载（当前使用内置等价兜底，点击查看原因）

展开即见自检表：

| 项目 | 值 |
|---|---|
| 统一流程模块状态 | ⚠️ 未能加载，已启用内置等价兜底 |
| 期望文件路径 | `/mount/src/ai_race_agent/pipeline.py` |
| 该文件是否存在 | **否** —— 部署里缺少这个文件 |
| app.py 所在目录 | `/mount/src/ai_race_agent` |
| 真实异常 | `ModuleNotFoundError: No module named 'pipeline'` |

**这一张表就是"下次不用再猜"的全部意义。**

### 4. 兜底不是"第二套算法"

这是本轮最关键的取舍。`_PipelineFallback` 里：

- 评分 → `scoring.compute_scores`（同一个函数）
- 校验 → `analysis_validate.validate_analysis`（同一个函数）
- 证据核对 → `analysis_validate.verify_evidence`（同一个函数）
- 证据卡 → `analysis_validate.evidence_map`（同一个函数）
- 引用统计 → `analysis_validate.citation_accuracy`（同一个函数）

**与 `pipeline` 用的是完全相同的那批实现**，兜底只是把编排顺序照抄了一遍。
所以它不会造成 P1-06 想避免的"评测与页面各算一套"——它连算法都不是第二套。

并且这条等价性有测试兜着：同一固定样本下，兜底与真实 `pipeline` 的
**整份 analysis 字典必须完全相等**（含四维分数、评分依据、来源标注、证据卡逐条、
引用正确率）。改了一边忘了另一边，测试立刻红。

---

## 四、你需要做的三步

### 第 1 步：推送

```bash
cd /c/Users/admin/ai_race_agent
git add -A
git commit -m "fix: pipeline 加载失败不再静默吞异常（三级降级+部署自检表+等价兜底）"
git push origin main
```

### 第 2 步：让云端**重建**，而不是只重新运行

这一步是本轮故障最可能的关键。已确认 `pipeline.py` 确实在
`github.com/workyy-SVG/ai_race_agent` 的 `main` 分支上（可直接打开该文件核对），
那么线上却"加载不到"，指向的就是**运行中的容器与仓库状态不一致**。

请在 Streamlit Cloud：**Manage app → 右下角 ⋮ → Reboot**。
若仍不放心，再 push 一个空提交强制全量重建：

```bash
git commit --allow-empty -m "chore: 触发云端全量重建"
git push origin main
```

### 第 3 步：打开应用确认

正常外观 = 按钮区**没有任何黄色提示**。若出现降级提示，点开那张自检表，
把「统一流程模块状态 / 期望文件路径 / 该文件是否存在 / 真实异常」四行发我即可，
不用再猜。

---

## 五、验证结果（全部离线可复算，不需要 Key）

```
python eval_pipeline_load.py    →  34 项断言  →  ALL_PIPELINE_LOAD_TESTS_PASSED
python eval_ui_regression.py    →  54 项断言  →  ALL_P1_TESTS_PASSED
python eval_chat_order.py       →  21 项断言  →  ALL_CHAT_ORDER_TESTS_PASSED
python eval_regression.py       →  4 场景 27 项 → 数值/期间/单位/引用 四项准确率均 100%
```

`eval_pipeline_load.py` 的四段覆盖：

1. **源码级防回归**：断言 `except ImportError: _pipeline = None` 这种写法不复存在、
   报错文案必须带异常类型、降级必须有页面提示 —— 防止以后被改回去。
2. **页面级**：认证后启动零异常、正常部署**不误报**降级、按钮结构完好。
3. **三级加载逐级实测**：挡住常规 `import` → 应降级到「按路径加载」；
   连文件都不存在 → 应报告「文件不存在」而不是假装成功。
4. **兜底等价性**：整份 analysis 与真实 `pipeline` **完全相等**。

---

## 六、本轮顺带修掉的另一个真问题

写测试时发现：**在同一个进程里先 `import app` 再跑 `AppTest`，页面会报
`StreamlitAPIException: st.button() can't be used in an st.form()`**。

这不是应用的 bug —— 直接 `import app.py` 等于在 Streamlit 运行时之外把脚本执行一遍，
会污染全局的表单/脚本上下文。已在纯净进程单独验证：**页面启动 0 异常、9 个按钮齐全**。
测试脚本因此调整了顺序（页面级检查跑在 `import app` 之前），这条坑也写进了技能库。

另：`eval_regression.py` 在只读目录下写报告会崩（目录建得出来、文件写不进去）。
已改为「目录不可写就自动退到临时目录」，并说明"控制台里的准确率才是结论，报告只是留存"。

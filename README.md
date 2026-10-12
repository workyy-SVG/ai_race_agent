# 金融智能投研 Agent

第二届全国人工智能应用创新大赛 参赛作品（通用赛道 · 金融方向）

一个基于大模型的**智能投研分析助手**：输入股票代码即可自动联网取数，
一键完成结构化财务分析、四维评分、趋势图表与研报生成。

## 核心功能

| 功能 | 说明 |
|---|---|
| 🌐 在线自动取数 | 输入股票代码/名称，自动从巨潮资讯网 + 东方财富获取行情、多期财务指标、年报 PDF |
| 📄 年报 PDF 云端解析 | 一键下载并解析上市公司年报 PDF |
| 📊 结构化分析 | 四维评分卡片（盈利/成长/偿债/现金流）+ 综合评分 + 指标明细 |
| 📈 可视化图表 | 关键指标柱状图 + 多年营收/净利润趋势图 |
| 📝 研报生成 | 一键生成完整投研报告，可下载为 Markdown |
| ⚖️ 双公司对比 | 两家公司横向对比，自动生成对比表与综合结论 |
| 📚 行业知识库 | 内置 6 大行业分析框架（通用/消费/金融/新能源/医药/互联网），自动注入 |
| 💬 多轮追问 | 基于数据与已有结论连续对话，上下文实时更新 |

## 数据来源

- **财务数据与行情**：东方财富公开接口（`datacenter-web.eastmoney.com` / `push2.eastmoney.com`）
- **公告与年报 PDF**：巨潮资讯网（官方指定信息披露平台 `cninfo.com.cn`）
- **大模型**：DeepSeek（推理与生成）

## 本地运行

1. 安装依赖：`pip install -r requirements.txt`
2. 在项目根目录创建 `.streamlit/secrets.toml`，内容参考 `secrets.toml.example`
3. 启动：`streamlit run app.py`

## 部署到 Streamlit Cloud

1. 把整个文件夹推送到 GitHub 公开仓库
2. 登录 https://streamlit.io/cloud ，用 GitHub 账号授权
3. New app → 选仓库 → main 分支 → app.py
4. 在 Advanced settings → Secrets 里粘贴：

```toml
DEEPSEEK_API_KEY = "sk-你的新key"
APP_USER = "judge"
APP_PASSWORD = "ai2026"
```

5. Deploy，等待几分钟得到 `https://xxx.streamlit.app` 公开链接
6. 提交给比赛时，把这个链接 + 账号 `judge` / 密码 `ai2026` 一并附上

## 安全说明

- 代码中**不含任何密钥**，全部从环境变量 / Streamlit Secrets 读取
- `.streamlit/secrets.toml` 已被 `.gitignore` 排除，不会推送到 GitHub
- 建议为应用设置访问账号密码，避免 API 额度被滥用

## 免责声明

本工具输出仅供研究参考，不构成任何投资建议。

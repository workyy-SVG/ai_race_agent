# 金融智能投研 Agent

第二届全国人工智能应用创新大赛 参赛作品（通用赛道 · 金融方向）

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

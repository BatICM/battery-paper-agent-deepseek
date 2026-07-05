# Battery Management Literature Daily - DeepSeek API Version

一个用于“每天自动检索最新电池管理相关高水平论文，并生成中文 HTML 日报”的 GitHub Actions 智能体版本。本版本使用 **DeepSeek API** 进行中文凝练总结，不再使用 OpenAI API。

## 功能

- 定时检索 arXiv / OpenAlex / Crossref / Semantic Scholar；
- 重点关注 BMS、SOC、SOH、RUL、故障诊断、安全预警、pack inconsistency、储能/车队现场数据、数字孪生、physics-informed learning、云端BMS；
- 基于关键词、期刊白名单和工程相关性做初筛；
- 调用 DeepSeek API 进行中文标题翻译、摘要凝练、A/B/C 分级和课题组启发整理；
- 输出 HTML 日报；
- 自动发送 HTML 邮件；
- 自动发布到 GitHub Pages，并保留历史归档。

## 仓库结构

```text
battery-paper-agent-deepseek/
├── .github/workflows/daily.yml     # GitHub Actions 每日定时任务
├── config.yaml                     # 检索关键词、期刊白名单、DeepSeek模型配置
├── daily_paper_agent.py             # 主程序
├── prompts/summarize_prompt.txt     # DeepSeek总结提示词
├── templates/report_template.html   # HTML日报模板
├── templates/index_template.html    # 归档首页模板
├── outputs/                         # 自动生成日报和归档
├── requirements.txt
└── .env.example
```

## 快速部署

### 1. 创建 GitHub 仓库

建议仓库名：`battery-paper-agent-deepseek`。

将本项目全部文件上传到仓库。

### 2. 开启 GitHub Pages

进入仓库：

```text
Settings → Pages → Build and deployment → Source → GitHub Actions
```

### 3. 配置 GitHub Secrets

进入：

```text
Settings → Secrets and variables → Actions → New repository secret
```

至少添加：

| Secret | 是否必填 | 说明 |
|---|---:|---|
| `DEEPSEEK_API_KEY` | 必填 | DeepSeek API Key |
| `DEEPSEEK_MODEL` | 可选 | 推荐 `deepseek-v4-flash`；更强推理可用 `deepseek-v4-pro` |
| `DEEPSEEK_BASE_URL` | 可选 | 默认 `https://api.deepseek.com` |
| `DEEPSEEK_MAX_TOKENS` | 可选 | 默认 `6000`，防止 JSON 被截断 |
| `CONTACT_EMAIL` | 推荐 | 用于 OpenAlex polite pool 和 User-Agent |
| `SMTP_HOST` | 邮件必填 | 邮箱 SMTP 地址，例如 `smtp.gmail.com` |
| `SMTP_PORT` | 邮件必填 | 通常为 `465` |
| `SMTP_USER` | 邮件必填 | 发件邮箱账号 |
| `SMTP_PASS` | 邮件必填 | SMTP 授权码，不是邮箱登录密码 |
| `EMAIL_FROM` | 邮件必填 | 发件人邮箱 |
| `EMAIL_TO` | 邮件必填 | 收件人邮箱，多个用英文逗号分隔 |
| `S2_API_KEY` | 可选 | Semantic Scholar API Key |

如果暂时不想发邮件，可以在 `config.yaml` 中设置：

```yaml
email:
  enabled: false
```

如果不想调用 DeepSeek，只使用规则版兜底总结，可以设置：

```yaml
deepseek:
  enabled: false
```

## 推荐 Secrets 示例

```text
DEEPSEEK_API_KEY = sk-xxxxxxxx
DEEPSEEK_MODEL = deepseek-v4-flash
DEEPSEEK_BASE_URL = https://api.deepseek.com
DEEPSEEK_MAX_TOKENS = 6000
CONTACT_EMAIL = your_email@example.com
SMTP_HOST = smtp.gmail.com
SMTP_PORT = 465
SMTP_USER = your_email@gmail.com
SMTP_PASS = your_gmail_app_password
EMAIL_FROM = your_email@gmail.com
EMAIL_TO = receiver1@example.com,receiver2@example.com
```

## 手动测试一次

进入：

```text
Actions → Daily BMS Paper Agent DeepSeek → Run workflow
```

运行成功后会：

1. 在 `outputs/` 下生成 `YYYY-MM-DD.html` 和 `index.html`；
2. 自动提交归档文件；
3. 自动部署到 GitHub Pages；
4. 向 `EMAIL_TO` 推送 HTML 邮件。

## 定时运行

默认每天北京时间/新加坡时间 08:30 运行。对应 workflow 中：

```yaml
- cron: "30 0 * * *"
```

GitHub Actions 的 cron 使用 UTC 时间，00:30 UTC 即 UTC+8 的 08:30。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
python daily_paper_agent.py
```

Windows PowerShell 可先手动设置环境变量：

```powershell
$env:DEEPSEEK_API_KEY="sk-..."
$env:DEEPSEEK_MODEL="deepseek-v4-flash"
$env:DEEPSEEK_BASE_URL="https://api.deepseek.com"
$env:SMTP_HOST="smtp.gmail.com"
$env:SMTP_PORT="465"
$env:SMTP_USER="your_email@gmail.com"
$env:SMTP_PASS="your_gmail_app_password"
$env:EMAIL_FROM="your_email@gmail.com"
$env:EMAIL_TO="receiver@example.com"
python daily_paper_agent.py
```

## 如何调整关注方向

编辑 `config.yaml`：

- `queries.include`：增加关注关键词；
- `queries.exclude`：增加排除关键词；
- `journal_whitelist`：增加重点期刊；
- `high_value_terms`：增加高价值任务词；
- `lookback_days`：默认近 2 天；
- `fallback_lookback_days`：如果近 2 天论文太少，自动放宽到近 7 天；
- `deepseek.model`：调整 DeepSeek 模型。

## 常见问题

### 1. 没有 `DEEPSEEK_API_KEY` 会怎样？

程序不会失败，会自动退回到规则版总结，但中文标题翻译、创新性判断和课题组启发会比较模板化。

### 2. 为什么使用 `response_format={"type":"json_object"}`？

日报生成需要结构化字段，程序要求模型输出 JSON，然后再渲染到 HTML 模板中。

### 3. GitHub Pages 部署失败怎么办？

确认：

```text
Settings → Pages → Source → GitHub Actions
Settings → Actions → General → Workflow permissions → Read and write permissions
```

本项目 workflow 已拆分为 build / deploy 两个 job，deploy job 已配置 `pages: write` 和 `id-token: write` 权限。

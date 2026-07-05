# battery-paper-agent-deepseek 修改说明

本修改包面向仓库：`https://github.com/BatICM/battery-paper-agent-deepseek`

## 修改目标

1. 增加 Google Scholar 补充检索。
2. 按用户指定顺序强化高水平期刊推荐：Nature、Nature Energy、Nature Communications、Joule、EES、AEM、ESM、Applied Energy、JPS、JES、IEEE TPEL/TTE/TIE/TITS、eTransportation、Cell Reports Physical Science、RSER。
3. 调整 A/B/C/DROP 分级：
   - A级：强相关 + 高水平期刊/高潜力预印本，必须读。
   - B级：相关性强，但期刊或创新性一般，建议浏览。
   - C级：边缘相关，进入备选列表。
   - DROP：材料制备、纯化学机理、与 BMS 无关。
4. 加强剔除纯材料制备、纯化学机理、与 BMS 无关论文的规则。

## 替换文件

将本文件夹内以下文件覆盖到仓库对应位置：

```text
daily_paper_agent.py
config.yaml
prompts/summarize_prompt.txt
.env.example
.github/workflows/daily.yml
```

## 必须新增的 GitHub Secret

若启用 Google Scholar 补充检索，需要在 GitHub 仓库中新增：

```text
SERPAPI_KEY
```

路径：

```text
Settings → Secrets and variables → Actions → New repository secret
```

如果暂时没有 SerpAPI key，程序会自动跳过 Google Scholar，不影响 arXiv/OpenAlex/Crossref/Semantic Scholar 检索。

## 重要说明

Google Scholar 没有官方公开 API，不建议在 GitHub Actions 中直接爬取网页，容易触发 CAPTCHA 或请求限制。本版本通过 SerpAPI 的 `google_scholar` engine 做补充召回。Google Scholar 通常只能稳定按年份过滤，不能精确到 24–48 小时，因此它只作为补充来源，不作为唯一最新性依据。

## 本地测试

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
python daily_paper_agent.py
```

若只想测试规则逻辑，可先把 `config.yaml` 中：

```yaml
deepseek:
  enabled: false
```

若不想用 Google Scholar，可把：

```yaml
sources:
  google_scholar:
    enabled: false
```

# hotrank_pipeline

TopHub 新闻页热点抓取与公众号草稿生成项目，使用本地 PostgreSQL 持久化、聚类、正文补抓，并支持通过 Web 页面配置最终生成模型。

## 当前实现范围

- 抓取 `https://tophub.today/c/news?p=1`
- 支持扩展内容源：DailyHot API、RSSHub / 普通 RSS
- 解析每个榜单卡片与榜单条目
- 保存原始 HTML 到本地
- 入库到本地 PostgreSQL
- 对白名单榜单做热点聚类
- 对热点条目做原文补抓 / 摘要抽取
- 调用 `mimo-v2.5` 生成公众号初稿
- 自动生成并插入多张配图到 Markdown 初稿
  - 优先生图模型生成真实摄影风格配图，失败后再回退正文来源图片
  - 回退取图会过滤新闻源图片、logo、水印、二维码、关注引导横幅、小尺寸横幅图
  - 支持限制“每篇最多插图数 / 单来源最多取图数”
- 默认过滤新华社、央视新闻、人民日报、中国新闻网等新闻通稿 / 官方通报 / 快讯类条目，优先保留适合公众号二创解读的选题
- 支持把已生成初稿一键上传到微信公众号草稿箱
  - 草稿 JSON 使用 UTF-8 直传，避免中文在公众号后台显示成 `\uXXXX`
  - Markdown 会转换成公众号友好的内联 HTML 样式，并自动上传封面与正文插图
- 按“月份 / 天”两级目录归档到 `T:\微信公众号文档`
- 提供 FastAPI Web 页面配置模型、API、白名单并触发流程
- 内置公众号 Markdown 编辑器，支持打开已生成稿件后直接实时渲染预览

## 目录

- `run.py`：命令行入口
- `src/hotrank_pipeline/main.py`：CLI
- `src/hotrank_pipeline/tophub.py`：抓取与解析逻辑
- `src/hotrank_pipeline/multi_source.py`：DailyHot / RSSHub / RSS 扩展内容源适配
- `src/hotrank_pipeline/db.py`：数据库初始化与入库
- `src/hotrank_pipeline/clustering.py`：热点聚类逻辑
- `src/hotrank_pipeline/fetchers.py`：正文补抓与摘要抽取
- `src/hotrank_pipeline/content_filters.py`：新闻通稿与不可用图片过滤规则
- `src/hotrank_pipeline/llm.py`：OpenAI 兼容 LLM 调用
- `src/hotrank_pipeline/wechat_publisher.py`：微信公众号草稿箱推送
- `src/hotrank_pipeline/services.py`：业务流程编排
- `src/hotrank_pipeline/webapp.py`：Web 页面
- `src/hotrank_pipeline/config.py`：本地 PostgreSQL 配置
- `schema.sql`：建表 SQL
- `data/raw/`：保存原始 HTML
- `local_settings.example.json`：本地可配置项示例
- `local_settings.json`：本地实际配置（不提交 Git）

## 默认数据库连接

数据库默认按当前本地环境读取：

- host: `127.0.0.1`
- port: `5432`
- dbname: `hotrank_pipeline`
- user: `postgres`
- password: `dfq666.`

也可以通过环境变量覆盖：

- `POSTGRES_HOST`
- `POSTGRES_PORT`
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`

## 初始化数据库

```powershell
cd C:\ai_work\hotrank_pipeline
python .\run.py init-db
```

## 执行一次抓取

```powershell
cd C:\ai_work\hotrank_pipeline
python .\run.py scrape-news
```

抓取会按 Web 配置页的“内容源配置”执行：

- `TopHub`：默认保留，用于兼容现有流程。
- `DailyHot API`：填写自建 API 地址后，按 route 抓取，例如 `weibo`、`zhihu`、`bilibili`、`36kr`。
- `RSS / RSSHub`：每行配置一个源，格式为 `名称|URL`；也可以只填 URL。

扩展源抓到的数据会写入同一套 PostgreSQL 表，后续仍复用聚类、正文补抓、成稿、评分、公众号草稿箱推送流程。

## 执行热点聚类

```powershell
python .\run.py cluster-topics
```

## 执行正文补抓

```powershell
python .\run.py enrich-articles
```

## 生成公众号初稿

```powershell
python .\run.py generate-drafts --limit 1
```

## 推送到微信公众号草稿箱

先在 Web 配置页填好 `微信公众号网关地址` 和 `微信公众号网关 Token`，也可以直接写入 `local_settings.json` 的 `wechat_gateway`：

```json
{
  "wechat_gateway": {
    "base_url": "http://106.12.11.147:18080",
    "token": "your-gateway-token",
    "max_images": 4
  }
}
```

然后可以：

- 在“公众号编辑器”打开某篇已生成初稿，点击“推送到微信公众号草稿箱”。
- 或命令行批量推送高分稿件：

```powershell
python .\run.py publish-wechat-drafts --limit 10
```

推送时会读取归档 Markdown，转成公众号内联 HTML，并把本地图片上传成微信图片 URL；正文图片异常时会统一重编码为 JPEG，减少微信接口 `invalid image format` 问题。

## 一键跑完整流程

```powershell
python .\run.py run-pipeline --draft-limit 1
```

## 查看统计

```powershell
cd C:\ai_work\hotrank_pipeline
python .\run.py stats
```

## 启动 Web 页面

```powershell
cd C:\ai_work\hotrank_pipeline
python .\run.py run-web --host 127.0.0.1 --port 8899
```

浏览器打开：

- [http://127.0.0.1:8899](http://127.0.0.1:8899)

## 本地模型配置

首次可复制：

```powershell
Copy-Item .\local_settings.example.json .\local_settings.json
```

然后把真实 API Key 只写进 `local_settings.json`，不要提交到 Git。

## Navicat 推荐查看表

- `crawl_runs`
- `boards`
- `board_snapshots`
- `board_snapshot_items`
- `v_latest_board_items`
- `cluster_runs`
- `topic_clusters`
- `topic_cluster_items`
- `article_sources`
- `article_source_images`
- `article_drafts`

## 常用 SQL

### 查看最新榜单条目

```sql
select *
from v_latest_board_items
order by board_name, rank_num;
```

### 查看某个榜单最近一次快照

```sql
select b.board_name, s.fetched_at, s.updated_text, i.rank_num, i.title, i.hot_value_raw, i.source_url
from board_snapshots s
join boards b on b.id = s.board_id
join board_snapshot_items i on i.snapshot_id = s.id
where b.board_name = '微博'
order by s.fetched_at desc, i.rank_num asc;
```

## 下一步建议

当前版本已经覆盖：

1. 榜单白名单过滤
2. 标题归一化 + 聚类
3. 对 `source_url` 做正文补抓
4. LLM 草稿生成
5. 归档到 `T:\微信公众号文档\YYYY-MM\YYYY-MM-DD\`

后续可以继续补：

1. 更强的原文抓取适配器
2. 发布前人工审核页
3. 已发文记录与复盘统计

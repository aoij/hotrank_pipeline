from __future__ import annotations

import argparse
import json

from .config import get_settings
from .db import fetch_stats, init_db
from .services import (
    run_article_enrichment,
    run_cluster,
    run_full_pipeline,
    run_generate_drafts,
    run_review_drafts,
    run_scrape,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TopHub 新闻热点抓取 + PostgreSQL 入库")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="初始化 PostgreSQL 表结构")
    subparsers.add_parser("scrape-news", help="抓取 TopHub 新闻页并入库")
    cluster_parser = subparsers.add_parser("cluster-topics", help="对新闻白名单榜单做热点聚类")
    cluster_parser.add_argument("--pretty", action="store_true", help="输出缩进 JSON")
    enrich_parser = subparsers.add_parser("enrich-articles", help="补抓热点来源正文/摘要")
    enrich_parser.add_argument("--limit", type=int, default=20, help="最多处理多少条")
    draft_parser = subparsers.add_parser("generate-drafts", help="生成公众号初稿")
    draft_parser.add_argument("--limit", type=int, default=1, help="最多生成多少篇")
    review_parser = subparsers.add_parser("review-drafts", help="让模型审核已生成初稿并写入文章评分")
    review_parser.add_argument("--limit", type=int, default=10, help="最多评分多少篇未评分初稿")
    pipeline_parser = subparsers.add_parser("run-pipeline", help="执行 scrape -> cluster -> enrich -> generate")
    pipeline_parser.add_argument("--draft-limit", type=int, default=1, help="完整流程最后生成多少篇")
    web_parser = subparsers.add_parser("run-web", help="启动 Web 页面")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", default=8899, type=int)
    subparsers.add_parser("stats", help="查看当前库内统计")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    settings = get_settings()

    if args.command == "init-db":
        init_db(settings)
        print("Database initialized.")
        return 0

    if args.command == "scrape-news":
        print(json.dumps(run_scrape(settings), ensure_ascii=False, indent=2))
        return 0

    if args.command == "cluster-topics":
        payload = run_cluster(settings)
        print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0

    if args.command == "enrich-articles":
        print(json.dumps(run_article_enrichment(settings, limit=args.limit), ensure_ascii=False, indent=2))
        return 0

    if args.command == "generate-drafts":
        print(json.dumps(run_generate_drafts(settings, limit=args.limit), ensure_ascii=False, indent=2))
        return 0

    if args.command == "review-drafts":
        print(json.dumps(run_review_drafts(settings, limit=args.limit), ensure_ascii=False, indent=2))
        return 0

    if args.command == "run-pipeline":
        print(json.dumps(run_full_pipeline(settings, draft_limit=args.draft_limit), ensure_ascii=False, indent=2))
        return 0

    if args.command == "run-web":
        import uvicorn

        uvicorn.run("hotrank_pipeline.webapp:app", host=args.host, port=args.port, app_dir="src")
        return 0

    if args.command == "stats":
        print(json.dumps(fetch_stats(settings), ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1

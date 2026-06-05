from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import Settings
from .models import ArticleFetchResult, BoardCard, ScrapeResult, TopicCluster


def _normalize_topic_text(value: str) -> str:
    clean = unicodedata.normalize("NFKC", (value or "").strip()).lower()
    return re.sub(r"[\W_]+", "", clean, flags=re.UNICODE)


def get_connection(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(settings.dsn)


def init_db(settings: Settings) -> None:
    schema_path = Path(__file__).resolve().parents[2] / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def _insert_crawl_run(cur: psycopg.Cursor, result: ScrapeResult) -> int:
    cur.execute(
        """
        insert into crawl_runs (source_name, source_url, status_code, raw_html_path, html_sha256, note)
        values (%s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            result.source_name,
            result.page_url,
            result.status_code,
            result.raw_html_path,
            result.html_sha256,
            f"boards={len(result.boards)}",
        ),
    )
    return cur.fetchone()[0]


def _upsert_board(cur: psycopg.Cursor, board: BoardCard, result: ScrapeResult) -> int:
    cur.execute(
        """
        insert into boards (
            source_name, page_category, tophub_node_id, board_name, board_type, board_url
        )
        values (%s, %s, %s, %s, %s, %s)
        on conflict (board_url) do update
        set
            tophub_node_id = excluded.tophub_node_id,
            board_name = excluded.board_name,
            board_type = excluded.board_type,
            updated_at = now()
        returning id
        """,
        (
            result.source_name,
            result.page_category,
            board.tophub_node_id,
            board.board_name,
            board.board_type,
            board.board_url,
        ),
    )
    return cur.fetchone()[0]


def persist_scrape_result(settings: Settings, result: ScrapeResult) -> dict[str, int]:
    board_count = 0
    item_count = 0

    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            run_id = _insert_crawl_run(cur, result)

            for board in result.boards:
                board_id = _upsert_board(cur, board, result)
                cur.execute(
                    """
                    insert into board_snapshots (run_id, board_id, updated_text, item_count)
                    values (%s, %s, %s, %s)
                    returning id
                    """,
                    (run_id, board_id, board.updated_text, len(board.items)),
                )
                snapshot_id = cur.fetchone()[0]
                board_count += 1

                for item in board.items:
                    cur.execute(
                        """
                        insert into board_snapshot_items (
                            snapshot_id, rank_num, title, normalized_title, hot_value_raw,
                            source_url, source_item_id, raw_text
                        )
                        values (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            snapshot_id,
                            item.rank_num,
                            item.title,
                            item.normalized_title,
                            item.hot_value_raw,
                            item.source_url,
                            item.source_item_id,
                            item.raw_text,
                        ),
                    )
                    item_count += 1

        conn.commit()

    return {
        "board_count": board_count,
        "item_count": item_count,
        "run_count": 1,
    }


def fetch_stats(settings: Settings) -> dict[str, int]:
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from crawl_runs")
            runs = cur.fetchone()[0]
            cur.execute("select count(*) from boards")
            boards = cur.fetchone()[0]
            cur.execute("select count(*) from board_snapshots")
            snapshots = cur.fetchone()[0]
            cur.execute("select count(*) from board_snapshot_items")
            items = cur.fetchone()[0]
            cur.execute("select count(*) from cluster_runs")
            cluster_runs = cur.fetchone()[0]
            cur.execute("select count(*) from topic_clusters")
            clusters = cur.fetchone()[0]
            cur.execute("select count(*) from article_sources")
            article_sources = cur.fetchone()[0]
            cur.execute("select count(*) from article_drafts")
            drafts = cur.fetchone()[0]

    return {
        "runs": runs,
        "boards": boards,
        "snapshots": snapshots,
        "items": items,
        "cluster_runs": cluster_runs,
        "clusters": clusters,
        "article_sources": article_sources,
        "drafts": drafts,
    }


def fetch_latest_whitelisted_items(settings: Settings, whitelist_boards: list[str]) -> list[dict]:
    with psycopg.connect(settings.dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            where_clause = "where board_name = any(%s)" if whitelist_boards else ""
            params = (whitelist_boards,) if whitelist_boards else ()
            cur.execute(
                f"""
                select
                    item_id,
                    board_name,
                    board_type,
                    rank_num,
                    title,
                    normalized_title,
                    hot_value_raw,
                    source_url,
                    source_item_id,
                    fetched_at,
                    updated_text
                from v_latest_board_items
                {where_clause}
                order by board_name, rank_num
                """,
                params,
            )
            return list(cur.fetchall())


def persist_cluster_run(settings: Settings, whitelist_boards: list[str], clusters: list[TopicCluster]) -> int:
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into cluster_runs (whitelist_boards, cluster_count, note)
                values (%s, %s, %s)
                returning id
                """,
                (whitelist_boards, len(clusters), f"clusters={len(clusters)}"),
            )
            cluster_run_id = cur.fetchone()[0]

            for cluster in clusters:
                cur.execute(
                    """
                    insert into topic_clusters (
                        cluster_run_id, cluster_key, canonical_title, cluster_summary,
                        signal_score, item_count
                    )
                    values (%s, %s, %s, %s, %s, %s)
                    returning id
                    """,
                    (
                        cluster_run_id,
                        cluster.cluster_key,
                        cluster.canonical_title,
                        cluster.cluster_summary,
                        cluster.signal_score,
                        len(cluster.members),
                    ),
                )
                cluster_id = cur.fetchone()[0]

                for member in cluster.members:
                    cur.execute(
                        """
                        insert into topic_cluster_items (
                            cluster_id, board_snapshot_item_id, board_name, rank_num, title,
                            hot_value_raw, source_url, match_score, is_primary
                        )
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            cluster_id,
                            member.item_id,
                            member.board_name,
                            member.rank_num,
                            member.title,
                            member.hot_value_raw,
                            member.source_url,
                            member.match_score,
                            member.item_id == cluster.members[0].item_id,
                        ),
                    )

        conn.commit()
        return cluster_run_id


def count_recent_clusters(settings: Settings) -> int:
    with psycopg.connect(settings.dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from topic_clusters")
            row = cur.fetchone() or {}
            return int(row.get("count", 0))


def fetch_recent_clusters(settings: Settings, limit: int = 12, offset: int = 0) -> list[dict]:
    with psycopg.connect(settings.dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                    tc.id as cluster_id,
                    tc.canonical_title,
                    tc.cluster_summary,
                    tc.signal_score,
                    tc.item_count,
                    tc.created_at,
                    cr.id as cluster_run_id,
                    to_char(tc.created_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD') as created_date,
                    to_char(tc.created_at at time zone 'Asia/Shanghai', 'HH24:MI:SS') as created_time
                from topic_clusters tc
                join cluster_runs cr on cr.id = tc.cluster_run_id
                order by tc.created_at desc, tc.id desc
                limit %s offset %s
                """,
                (limit, offset),
            )
            return list(cur.fetchall())


def fetch_unfetched_cluster_items(settings: Settings, limit: int = 20) -> list[dict]:
    with psycopg.connect(settings.dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                with latest_cluster_run as (
                    select id
                    from cluster_runs
                    order by created_at desc, id desc
                    limit 1
                ),
                ranked_candidates as (
                    select
                        tci.board_snapshot_item_id as item_id,
                        tci.board_name,
                        tci.title,
                        tci.source_url,
                        tci.rank_num,
                        row_number() over (
                            partition by tci.board_snapshot_item_id
                            order by tc.signal_score desc, tci.rank_num asc
                        ) as rn
                    from topic_cluster_items tci
                    join topic_clusters tc on tc.id = tci.cluster_id
                    join latest_cluster_run lcr on lcr.id = tc.cluster_run_id
                    left join article_sources a on a.board_snapshot_item_id = tci.board_snapshot_item_id
                    where a.id is null
                )
                select
                    item_id,
                    board_name,
                    title,
                    source_url
                from ranked_candidates
                where rn = 1
                order by
                    case board_name
                        when '澎湃' then 1
                        when '今日头条' then 2
                        when '微博' then 3
                        when '微信' then 4
                        when '百度' then 5
                        else 9
                    end,
                    rank_num
                limit %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def persist_article_fetch_result(settings: Settings, result: ArticleFetchResult) -> int:
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into article_sources (
                    board_snapshot_item_id, board_name, source_url, source_host, final_url,
                    fetch_status, http_status, content_type, title, summary, content_text,
                    lead_image_url,
                    content_hash, fetched_at, note, updated_at
                )
                values (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, now(), %s, now()
                )
                on conflict (board_snapshot_item_id) do update
                set
                    source_host = excluded.source_host,
                    final_url = excluded.final_url,
                    fetch_status = excluded.fetch_status,
                    http_status = excluded.http_status,
                    content_type = excluded.content_type,
                    title = excluded.title,
                    summary = excluded.summary,
                    content_text = excluded.content_text,
                    lead_image_url = excluded.lead_image_url,
                    content_hash = excluded.content_hash,
                    fetched_at = now(),
                    note = excluded.note,
                    updated_at = now()
                returning id
                """,
                (
                    result.board_snapshot_item_id,
                    result.board_name,
                    result.source_url,
                    result.source_host,
                    result.final_url,
                    result.fetch_status,
                    result.http_status,
                    result.content_type,
                    result.title,
                    result.summary,
                    result.content_text,
                    result.lead_image_url,
                    result.content_hash,
                    result.note,
                ),
            )
            fetch_id = cur.fetchone()[0]

            cur.execute("delete from article_source_images where source_id = %s", (fetch_id,))
            for idx, image_url in enumerate(result.image_urls, start=1):
                cur.execute(
                    """
                    insert into article_source_images (source_id, image_url, sort_order)
                    values (%s, %s, %s)
                    on conflict (source_id, image_url) do nothing
                    """,
                    (fetch_id, image_url, idx),
                )
        conn.commit()
        return fetch_id


def fetch_recent_drafts(settings: Settings, limit: int = 20) -> list[dict]:
    with psycopg.connect(settings.dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                    d.id,
                    d.cluster_id,
                    d.model_name,
                    d.title,
                    d.archive_path,
                    d.review_score,
                    d.review_summary,
                    d.review_model,
                    d.reviewed_at,
                    d.wechat_uploaded_at,
                    d.wechat_media_id,
                    d.toutiao_uploaded_at,
                    d.toutiao_article_id,
                    d.created_at,
                    tc.canonical_title,
                    to_char(d.reviewed_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as reviewed_at_text,
                    to_char(d.wechat_uploaded_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as wechat_uploaded_at_text,
                    to_char(d.toutiao_uploaded_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as toutiao_uploaded_at_text,
                    to_char(d.created_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as created_at_text
                from article_drafts d
                join topic_clusters tc on tc.id = d.cluster_id
                order by d.review_score desc nulls last, d.created_at desc, d.id desc
                limit %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def fetch_draft_by_id(settings: Settings, draft_id: int) -> dict | None:
    with psycopg.connect(settings.dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                    d.id,
                    d.cluster_id,
                    d.model_name,
                    d.model_base_url,
                    d.title,
                    d.content_md,
                    d.archive_path,
                    d.prompt_excerpt,
                    d.review_score,
                    d.review_summary,
                    d.review_model,
                    d.reviewed_at,
                    d.wechat_uploaded_at,
                    d.wechat_media_id,
                    d.toutiao_uploaded_at,
                    d.toutiao_article_id,
                    d.created_at,
                    tc.canonical_title,
                    to_char(d.reviewed_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as reviewed_at_text,
                    to_char(d.wechat_uploaded_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as wechat_uploaded_at_text,
                    to_char(d.toutiao_uploaded_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as toutiao_uploaded_at_text,
                    to_char(d.created_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as created_at_text
                from article_drafts d
                join topic_clusters tc on tc.id = d.cluster_id
                where d.id = %s
                limit 1
                """,
                (draft_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def find_existing_draft_for_topic(
    settings: Settings,
    *,
    cluster_id: int | None = None,
    canonical_title: str = "",
    draft_title: str = "",
    lookback_limit: int = 200,
) -> dict | None:
    clean_title = (canonical_title or "").strip()
    clean_draft_title = (draft_title or "").strip()
    with psycopg.connect(settings.dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            if cluster_id and clean_title:
                cur.execute(
                    """
                    select
                        d.id,
                        d.cluster_id,
                        d.title,
                        d.archive_path,
                        d.review_score,
                        d.review_summary,
                        d.wechat_uploaded_at,
                        d.wechat_media_id,
                        d.toutiao_uploaded_at,
                        d.toutiao_article_id,
                        d.created_at,
                        tc.canonical_title,
                        to_char(d.created_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as created_at_text
                    from article_drafts d
                    join topic_clusters tc on tc.id = d.cluster_id
                    where d.cluster_id = %s or tc.canonical_title = %s
                    order by
                        case when d.cluster_id = %s then 0 else 1 end,
                        d.created_at desc,
                        d.id desc
                    limit 1
                    """,
                    (cluster_id, clean_title, cluster_id),
                )
            elif cluster_id:
                cur.execute(
                    """
                    select
                        d.id,
                        d.cluster_id,
                        d.title,
                        d.archive_path,
                        d.review_score,
                        d.review_summary,
                        d.wechat_uploaded_at,
                        d.wechat_media_id,
                        d.toutiao_uploaded_at,
                        d.toutiao_article_id,
                        d.created_at,
                        tc.canonical_title,
                        to_char(d.created_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as created_at_text
                    from article_drafts d
                    join topic_clusters tc on tc.id = d.cluster_id
                    where d.cluster_id = %s
                    order by d.created_at desc, d.id desc
                    limit 1
                    """,
                    (cluster_id,),
                )
            elif clean_title:
                cur.execute(
                    """
                    select
                        d.id,
                        d.cluster_id,
                        d.title,
                        d.archive_path,
                        d.review_score,
                        d.review_summary,
                        d.wechat_uploaded_at,
                        d.wechat_media_id,
                        d.toutiao_uploaded_at,
                        d.toutiao_article_id,
                        d.created_at,
                        tc.canonical_title,
                        to_char(d.created_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as created_at_text
                    from article_drafts d
                    join topic_clusters tc on tc.id = d.cluster_id
                    where tc.canonical_title = %s
                    order by d.created_at desc, d.id desc
                    limit 1
                    """,
                    (clean_title,),
                )
            else:
                row = None

            if cluster_id or clean_title:
                row = cur.fetchone()
                if row:
                    return dict(row)

            normalized_targets = {
                value
                for value in (
                    _normalize_topic_text(clean_title),
                    _normalize_topic_text(clean_draft_title),
                )
                if value
            }
            if not normalized_targets:
                return None

            cur.execute(
                """
                select
                    d.id,
                    d.cluster_id,
                    d.title,
                    d.archive_path,
                    d.review_score,
                    d.review_summary,
                    d.wechat_uploaded_at,
                    d.wechat_media_id,
                    d.toutiao_uploaded_at,
                    d.toutiao_article_id,
                    d.created_at,
                    tc.canonical_title,
                    to_char(d.created_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as created_at_text
                from article_drafts d
                join topic_clusters tc on tc.id = d.cluster_id
                order by d.created_at desc, d.id desc
                limit %s
                """,
                (max(20, int(lookback_limit or 200)),),
            )
            for candidate in cur.fetchall():
                row_dict = dict(candidate)
                normalized_existing = {
                    value
                    for value in (
                        _normalize_topic_text(row_dict.get("canonical_title") or ""),
                        _normalize_topic_text(row_dict.get("title") or ""),
                    )
                    if value
                }
                if normalized_existing & normalized_targets:
                    return row_dict
            return None


def delete_drafts_by_ids(settings: Settings, draft_ids: list[int]) -> list[dict]:
    ids = sorted({int(draft_id) for draft_id in draft_ids if int(draft_id) > 0})
    if not ids:
        return []
    with get_connection(settings) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select id, title, archive_path
                from article_drafts
                where id = any(%s)
                order by id
                """,
                (ids,),
            )
            rows = [dict(row) for row in cur.fetchall()]
            if rows:
                cur.execute("delete from article_drafts where id = any(%s)", (ids,))
        conn.commit()
    return rows


def delete_old_drafts(settings: Settings, retention_hours: int = 48) -> list[dict]:
    retention_hours = max(1, int(retention_hours))
    with get_connection(settings) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                with old_drafts as (
                    select id, title, archive_path
                    from article_drafts
                    where created_at < now() - (%s::text || ' hours')::interval
                ),
                deleted as (
                    delete from article_drafts
                    where id in (select id from old_drafts)
                    returning id, title, archive_path
                )
                select id, title, archive_path
                from deleted
                order by id
                """,
                (retention_hours,),
            )
            rows = [dict(row) for row in cur.fetchall()]
        conn.commit()
    return rows


def fetch_unreviewed_drafts(settings: Settings, limit: int = 10) -> list[dict]:
    with psycopg.connect(settings.dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                    d.id,
                    d.cluster_id,
                    d.model_name,
                    d.title,
                    d.content_md,
                    d.archive_path,
                    d.created_at,
                    tc.canonical_title,
                    to_char(d.created_at at time zone 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS') as created_at_text
                from article_drafts d
                join topic_clusters tc on tc.id = d.cluster_id
                where d.review_score is null
                order by d.created_at desc, d.id desc
                limit %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def update_draft_review(
    settings: Settings,
    draft_id: int,
    review_score: float,
    review_summary: str,
    review_model: str,
) -> None:
    score = max(0.0, min(10.0, round(float(review_score), 1)))
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update article_drafts
                set
                    review_score = %s,
                    review_summary = %s,
                    review_model = %s,
                    reviewed_at = now()
                where id = %s
                """,
                (score, review_summary, review_model, draft_id),
            )
        conn.commit()


def mark_draft_wechat_uploaded(settings: Settings, draft_id: int, media_id: str | None = None) -> None:
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update article_drafts
                set
                    wechat_uploaded_at = now(),
                    wechat_media_id = coalesce(%s, wechat_media_id)
                where id = %s
                """,
                (media_id, draft_id),
            )
        conn.commit()


def mark_draft_toutiao_uploaded(settings: Settings, draft_id: int, article_id: str | int | None = None) -> None:
    article_id_text = str(article_id).strip() if article_id is not None else None
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update article_drafts
                set
                    toutiao_uploaded_at = now(),
                    toutiao_article_id = coalesce(%s, toutiao_article_id)
                where id = %s
                """,
                (article_id_text or None, draft_id),
            )
        conn.commit()


def fetch_cluster_sources_for_generation(settings: Settings, limit: int = 1) -> list[dict]:
    with psycopg.connect(settings.dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                with latest_cluster_run as (
                    select id
                    from cluster_runs
                    order by created_at desc, id desc
                    limit 1
                ),
                candidate_clusters as (
                    select
                        tc.id as cluster_id,
                        tc.canonical_title,
                        tc.cluster_summary,
                        tc.signal_score,
                        tc.item_count,
                        sum(case when a.fetch_status = 'fetched' then 1 else 0 end) as fetched_source_count,
                        sum(case when img.image_count > 0 then 1 else 0 end) as image_source_count,
                        sum(
                            case
                                when a.fetch_status = 'fetched'
                                    and a.board_name in ('澎湃', '微信', '微博')
                                then 1
                                else 0
                            end
                        ) as preferred_fetched_source_count,
                        sum(
                            case
                                when img.image_count > 0
                                    and a.board_name in ('澎湃', '微信', '微博')
                                then 1
                                else 0
                            end
                        ) as preferred_image_source_count
                    from topic_clusters tc
                    join latest_cluster_run lcr on lcr.id = tc.cluster_run_id
                    join topic_cluster_items tci on tci.cluster_id = tc.id
                    left join article_sources a on a.board_snapshot_item_id = tci.board_snapshot_item_id
                    left join (
                        select source_id, count(*) as image_count
                        from article_source_images
                        group by source_id
                    ) img on img.source_id = a.id
                    left join article_drafts d on d.cluster_id = tc.id
                    where d.id is null
                    group by tc.id
                    having sum(case when a.fetch_status = 'fetched' then 1 else 0 end) > 0
                    order by
                        preferred_image_source_count desc,
                        preferred_fetched_source_count desc,
                        image_source_count desc,
                        fetched_source_count desc,
                        tc.signal_score desc,
                        tc.item_count desc
                    limit %s
                )
                select
                    cc.cluster_id,
                    cc.canonical_title,
                    cc.cluster_summary,
                    cc.signal_score,
                    cc.item_count,
                    cc.fetched_source_count,
                    cc.image_source_count,
                    tci.board_name,
                    tci.rank_num,
                    tci.title as member_title,
                    tci.source_url,
                    a.source_host,
                    a.final_url,
                    a.title,
                    a.summary,
                    a.content_text,
                    a.fetch_status,
                    coalesce(
                        (
                            select json_agg(asi.image_url order by asi.sort_order)
                            from article_source_images asi
                            join article_sources a2 on a2.id = asi.source_id
                            where a2.board_snapshot_item_id = tci.board_snapshot_item_id
                        ),
                        '[]'::json
                    ) as image_urls
                from candidate_clusters cc
                join topic_cluster_items tci on tci.cluster_id = cc.cluster_id
                left join article_sources a on a.board_snapshot_item_id = tci.board_snapshot_item_id
                order by cc.signal_score desc, tci.is_primary desc, tci.rank_num asc
                """,
                (limit,),
            )
            rows = list(cur.fetchall())

    grouped: dict[int, dict] = {}
    for row in rows:
        cluster = grouped.setdefault(
            row["cluster_id"],
            {
                "cluster_id": row["cluster_id"],
                "canonical_title": row["canonical_title"],
                "cluster_summary": row["cluster_summary"],
                "signal_score": float(row["signal_score"]),
                "item_count": row["item_count"],
                "sources": [],
            },
        )
        cluster["sources"].append(row)
    return list(grouped.values())


def persist_draft_record(
    settings: Settings,
    cluster_id: int,
    model_name: str,
    model_base_url: str,
    title: str,
    content_md: str,
    archive_path: str,
    prompt_excerpt: str,
) -> int:
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into article_drafts (
                    cluster_id, model_name, model_base_url, title, content_md, archive_path, prompt_excerpt
                )
                values (%s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    cluster_id,
                    model_name,
                    model_base_url,
                    title,
                    content_md,
                    archive_path,
                    prompt_excerpt,
                ),
            )
            draft_id = cur.fetchone()[0]
        conn.commit()
        return draft_id


def fetch_draft_source_images(settings: Settings, draft_id: int) -> list[str]:
    with psycopg.connect(settings.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select distinct asi.image_url
                from article_drafts d
                join topic_cluster_items tci on tci.cluster_id = d.cluster_id
                join article_sources a on a.board_snapshot_item_id = tci.board_snapshot_item_id
                join article_source_images asi on asi.source_id = a.id
                where d.id = %s
                order by asi.image_url
                """,
                (draft_id,),
            )
            return [row[0] for row in cur.fetchall() if row and row[0]]


def update_draft_content(
    settings: Settings,
    draft_id: int,
    content_md: str,
    archive_path: str | None = None,
) -> None:
    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            if archive_path is not None:
                cur.execute(
                    """
                    update article_drafts
                    set content_md = %s, archive_path = %s
                    where id = %s
                    """,
                    (content_md, archive_path, draft_id),
                )
            else:
                cur.execute(
                    """
                    update article_drafts
                    set content_md = %s
                    where id = %s
                    """,
                    (content_md, draft_id),
                )
        conn.commit()


def cleanup_old_hotspots(settings: Settings, retention_hours: int = 48) -> dict[str, int]:
    retention_hours = max(1, int(retention_hours))
    with get_connection(settings) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                with old_runs as (
                    select id from crawl_runs
                    where fetched_at < now() - (%s::text || ' hours')::interval
                ),
                deleted as (
                    delete from crawl_runs
                    where id in (select id from old_runs)
                    returning id
                )
                select count(*) as deleted_crawl_runs from deleted
                """,
                (retention_hours,),
            )
            deleted_crawl_runs = int((cur.fetchone() or {}).get("deleted_crawl_runs", 0))

            cur.execute(
                """
                with old_cluster_runs as (
                    select cr.id
                    from cluster_runs cr
                    where cr.created_at < now() - (%s::text || ' hours')::interval
                      and not exists (
                          select 1
                          from topic_clusters tc
                          join article_drafts d on d.cluster_id = tc.id
                          where tc.cluster_run_id = cr.id
                      )
                ),
                deleted as (
                    delete from cluster_runs
                    where id in (select id from old_cluster_runs)
                    returning id
                )
                select count(*) as deleted_cluster_runs from deleted
                """,
                (retention_hours,),
            )
            deleted_cluster_runs = int((cur.fetchone() or {}).get("deleted_cluster_runs", 0))
        conn.commit()

    return {
        "retention_hours": retention_hours,
        "deleted_crawl_runs": deleted_crawl_runs,
        "deleted_cluster_runs": deleted_cluster_runs,
    }


def persist_manual_topic_bundle(
    settings: Settings,
    topic: str,
    sources: list[dict],
    cluster_summary: str,
) -> dict[str, int]:
    from .tophub import normalize_title

    clean_topic = (topic or "").strip()
    if not clean_topic:
        raise ValueError("topic is empty")

    with get_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into crawl_runs (source_name, source_url, status_code, raw_html_path, html_sha256, note)
                values (%s, %s, %s, %s, %s, %s)
                returning id
                """,
                ("manual_topic", f"manual://{clean_topic}", 200, "", "", f"topic={clean_topic}; sources={len(sources)}"),
            )
            run_id = cur.fetchone()[0]

            board_url = "manual://topic-search"
            cur.execute(
                """
                insert into boards (source_name, page_category, tophub_node_id, board_name, board_type, board_url)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (board_url) do update
                set updated_at = now(), board_name = excluded.board_name
                returning id
                """,
                ("manual_topic", "manual", "manual:topic", "手动话题", "manual", board_url),
            )
            board_id = cur.fetchone()[0]

            cur.execute(
                """
                insert into board_snapshots (run_id, board_id, updated_text, item_count)
                values (%s, %s, %s, %s)
                returning id
                """,
                (run_id, board_id, "手动话题搜索", len(sources)),
            )
            snapshot_id = cur.fetchone()[0]

            item_ids: list[int] = []
            article_source_rows: list[int] = []
            for idx, source in enumerate(sources, start=1):
                title = (source.get("title") or clean_topic).strip()
                url = (source.get("source_url") or source.get("final_url") or f"manual://{idx}").strip()
                summary = (source.get("summary") or "").strip()
                content_text = (source.get("content_text") or summary or title).strip()
                cur.execute(
                    """
                    insert into board_snapshot_items (
                        snapshot_id, rank_num, title, normalized_title, hot_value_raw, source_url, source_item_id, raw_text
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s)
                    returning id
                    """,
                    (snapshot_id, idx, title, normalize_title(title), "手动搜索", url, f"manual-{run_id}-{idx}", summary),
                )
                item_id = cur.fetchone()[0]
                item_ids.append(item_id)

                cur.execute(
                    """
                    insert into article_sources (
                        board_snapshot_item_id, board_name, source_url, source_host, final_url,
                        fetch_status, http_status, content_type, title, summary, content_text,
                        lead_image_url, content_hash, fetched_at, note, updated_at
                    )
                    values (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, now(), %s, now()
                    )
                    returning id
                    """,
                    (
                        item_id,
                        source.get("board_name") or "手动搜索",
                        url,
                        source.get("source_host") or "",
                        source.get("final_url") or url,
                        "fetched",
                        source.get("http_status"),
                        source.get("content_type") or "text/html",
                        title,
                        summary,
                        content_text,
                        (source.get("image_urls") or [""])[0] if source.get("image_urls") else "",
                        source.get("content_hash") or "",
                        "manual topic bundle",
                    ),
                )
                article_source_id = cur.fetchone()[0]
                article_source_rows.append(article_source_id)
                for image_idx, image_url in enumerate(source.get("image_urls") or [], start=1):
                    cur.execute(
                        """
                        insert into article_source_images (source_id, image_url, sort_order)
                        values (%s, %s, %s)
                        on conflict (source_id, image_url) do nothing
                        """,
                        (article_source_id, image_url, image_idx),
                    )

            cur.execute(
                """
                insert into cluster_runs (whitelist_boards, cluster_count, note)
                values (%s, %s, %s)
                returning id
                """,
                (["手动话题"], 1, f"manual topic={clean_topic}"),
            )
            cluster_run_id = cur.fetchone()[0]

            cur.execute(
                """
                insert into topic_clusters (cluster_run_id, cluster_key, canonical_title, cluster_summary, signal_score, item_count)
                values (%s, %s, %s, %s, %s, %s)
                returning id
                """,
                (cluster_run_id, f"manual:{run_id}", clean_topic, cluster_summary, 9999, len(item_ids)),
            )
            cluster_id = cur.fetchone()[0]

            for idx, (item_id, source) in enumerate(zip(item_ids, sources), start=1):
                cur.execute(
                    """
                    insert into topic_cluster_items (
                        cluster_id, board_snapshot_item_id, board_name, rank_num, title, hot_value_raw, source_url, match_score, is_primary
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        cluster_id,
                        item_id,
                        source.get("board_name") or "手动搜索",
                        idx,
                        source.get("title") or clean_topic,
                        "手动搜索",
                        source.get("source_url") or source.get("final_url") or f"manual://{idx}",
                        1.0,
                        idx == 1,
                    ),
                )
        conn.commit()

    return {
        "run_id": run_id,
        "cluster_run_id": cluster_run_id,
        "cluster_id": cluster_id,
        "source_count": len(sources),
    }

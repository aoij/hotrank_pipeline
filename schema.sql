create extension if not exists pg_trgm;

create table if not exists crawl_runs (
    id bigint generated always as identity primary key,
    source_name text not null,
    source_url text not null,
    status_code integer not null,
    fetched_at timestamptz not null default now(),
    raw_html_path text,
    html_sha256 text,
    note text
);

create table if not exists boards (
    id bigint generated always as identity primary key,
    source_name text not null,
    page_category text not null,
    tophub_node_id text,
    board_name text not null,
    board_type text,
    board_url text not null unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_boards_board_name on boards(board_name);

create table if not exists board_snapshots (
    id bigint generated always as identity primary key,
    run_id bigint not null references crawl_runs(id) on delete cascade,
    board_id bigint not null references boards(id) on delete restrict,
    fetched_at timestamptz not null default now(),
    updated_text text,
    item_count integer not null default 0,
    unique (run_id, board_id)
);

create index if not exists idx_board_snapshots_board_id_fetched_at
    on board_snapshots(board_id, fetched_at desc);

create table if not exists board_snapshot_items (
    id bigint generated always as identity primary key,
    snapshot_id bigint not null references board_snapshots(id) on delete cascade,
    rank_num integer not null,
    title text not null,
    normalized_title text not null,
    hot_value_raw text,
    source_url text not null,
    source_item_id text,
    raw_text text,
    created_at timestamptz not null default now(),
    unique (snapshot_id, rank_num)
);

create index if not exists idx_board_snapshot_items_snapshot_rank
    on board_snapshot_items(snapshot_id, rank_num);

create index if not exists idx_board_snapshot_items_source_url
    on board_snapshot_items(source_url);

create index if not exists idx_board_snapshot_items_title_trgm
    on board_snapshot_items using gin(normalized_title gin_trgm_ops);

drop view if exists v_latest_board_items;

create view v_latest_board_items as
with latest_snapshots as (
    select
        s.*,
        row_number() over (partition by s.board_id order by s.fetched_at desc, s.id desc) as rn
    from board_snapshots s
)
select
    b.id as board_id,
    b.source_name,
    b.page_category,
    b.tophub_node_id,
    b.board_name,
    b.board_type,
    b.board_url,
    ls.id as snapshot_id,
    i.id as item_id,
    ls.fetched_at,
    ls.updated_text,
    i.rank_num,
    i.title,
    i.normalized_title,
    i.hot_value_raw,
    i.source_url,
    i.source_item_id
from latest_snapshots ls
join boards b on b.id = ls.board_id
join board_snapshot_items i on i.snapshot_id = ls.id
where ls.rn = 1;

create table if not exists cluster_runs (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    whitelist_boards text[] not null,
    cluster_count integer not null default 0,
    note text
);

create table if not exists topic_clusters (
    id bigint generated always as identity primary key,
    cluster_run_id bigint not null references cluster_runs(id) on delete cascade,
    cluster_key text not null,
    canonical_title text not null,
    cluster_summary text,
    signal_score numeric(12,4) not null default 0,
    item_count integer not null default 0,
    created_at timestamptz not null default now(),
    unique (cluster_run_id, cluster_key)
);

create index if not exists idx_topic_clusters_run_score
    on topic_clusters(cluster_run_id, signal_score desc);

create table if not exists topic_cluster_items (
    id bigint generated always as identity primary key,
    cluster_id bigint not null references topic_clusters(id) on delete cascade,
    board_snapshot_item_id bigint not null references board_snapshot_items(id) on delete cascade,
    board_name text not null,
    rank_num integer not null,
    title text not null,
    hot_value_raw text,
    source_url text not null,
    match_score numeric(12,4) not null default 0,
    is_primary boolean not null default false,
    unique (cluster_id, board_snapshot_item_id)
);

create index if not exists idx_topic_cluster_items_cluster_id
    on topic_cluster_items(cluster_id);

create table if not exists article_sources (
    id bigint generated always as identity primary key,
    board_snapshot_item_id bigint not null unique references board_snapshot_items(id) on delete cascade,
    board_name text not null,
    source_url text not null,
    source_host text,
    final_url text,
    fetch_status text not null default 'pending',
    http_status integer,
    content_type text,
    title text,
    summary text,
    content_text text,
    content_hash text,
    fetched_at timestamptz,
    note text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_article_sources_status
    on article_sources(fetch_status, fetched_at desc);

alter table article_sources
    add column if not exists lead_image_url text;

create table if not exists article_source_images (
    id bigint generated always as identity primary key,
    source_id bigint not null references article_sources(id) on delete cascade,
    image_url text not null,
    sort_order integer not null default 0,
    created_at timestamptz not null default now(),
    unique (source_id, image_url)
);

create index if not exists idx_article_source_images_source_id
    on article_source_images(source_id, sort_order);

create table if not exists article_drafts (
    id bigint generated always as identity primary key,
    cluster_id bigint not null references topic_clusters(id) on delete cascade,
    model_name text not null,
    model_base_url text not null,
    title text not null,
    content_md text not null,
    archive_path text not null,
    prompt_excerpt text,
    review_score numeric(4,1),
    review_summary text,
    review_model text,
    reviewed_at timestamptz,
    wechat_uploaded_at timestamptz,
    wechat_media_id text,
    toutiao_uploaded_at timestamptz,
    toutiao_article_id text,
    created_at timestamptz not null default now()
);

alter table article_drafts
    add column if not exists review_score numeric(4,1),
    add column if not exists review_summary text,
    add column if not exists review_model text,
    add column if not exists reviewed_at timestamptz,
    add column if not exists wechat_uploaded_at timestamptz,
    add column if not exists wechat_media_id text,
    add column if not exists toutiao_uploaded_at timestamptz,
    add column if not exists toutiao_article_id text;

create index if not exists idx_article_drafts_cluster_id
    on article_drafts(cluster_id, created_at desc);

create index if not exists idx_article_drafts_review_score_created_at
    on article_drafts(review_score desc nulls last, created_at desc, id desc);

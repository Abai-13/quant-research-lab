"""数据库 schema 定义与结构升级。

- schema 1：仅 ``app_metadata``（阶段 1）；
- schema 2：阶段二结构，含来源注册、导入批次与请求日志、原始档案索引、
  规范化记录修订、数据版本清单、质量问题、股票池快照；
- schema 3：收尾结构，新增不可变分块清单、版本对分块的引用、逻辑/修订统计、
  引用保护与归档索引；旧版本成员表保留兼容。
- 升级必须通过显式迁移入口执行：先做一致性备份并核对备份，再在单个事务内完成；
  未知 schema 或损坏库不迁移、不删除、不重建。

时间口径：``observed_at`` / ``ingested_at`` 是本系统观察与落库时刻（UTC 带时区）；
``source_published_at`` 是供应商可证明的发布/修订时刻（未知为 NULL）；
``historical_available_at`` 是可证明该值在历史上何时可用（不可证明时为 NULL）。
业务日期 ``trade_date`` / ``cal_date`` / ``end_date`` 一律为交易所或事件业务日期，
不使用下载日期代替。
"""

from __future__ import annotations

SCHEMA_VERSION = 3
METADATA_TABLE = "app_metadata"

SCHEMA_2_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE schema_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_version INTEGER NOT NULL,
        to_version INTEGER NOT NULL,
        applied_at TEXT NOT NULL,
        backup_path TEXT,
        result TEXT NOT NULL,
        notes TEXT
    )
    """,
    """
    CREATE TABLE source_registry (
        source_code TEXT NOT NULL,
        interface TEXT NOT NULL,
        field_mapping TEXT NOT NULL,
        unit_notes TEXT NOT NULL,
        permission_status TEXT NOT NULL,
        coverage_notes TEXT,
        rate_limit TEXT,
        cost_notes TEXT,
        evidence_date TEXT,
        docs_url TEXT,
        unknown_fields TEXT,
        PRIMARY KEY (source_code, interface)
    )
    """,
    """
    CREATE TABLE import_batch (
        batch_id TEXT PRIMARY KEY,
        source_code TEXT NOT NULL,
        mode TEXT CHECK(mode IN ('history','incremental','custom')) NOT NULL,
        categories TEXT NOT NULL,
        scope TEXT NOT NULL,
        status TEXT CHECK(status IN ('pending','running','completed','failed','partial')) NOT NULL,
        pages_expected INTEGER NOT NULL DEFAULT 0,
        pages_ok INTEGER NOT NULL DEFAULT 0,
        pages_failed INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts_per_page INTEGER NOT NULL,
        checkpoint TEXT,
        error_class TEXT,
        started_at TEXT,
        finished_at TEXT,
        config_snapshot TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE import_request_log (
        request_id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        interface TEXT NOT NULL,
        request_params TEXT NOT NULL,
        page_no INTEGER NOT NULL,
        raw_digest TEXT,
        status TEXT CHECK(status IN (
            'ok','empty_ok','empty_end','failed','transport_error','rate_limited',
            'wait_budget_exhausted','no_permission','contract_error','page_incomplete',
            'unknown_api_error','credentials_required','skipped'
        )) NOT NULL,
        error_class TEXT,
        detail TEXT,
        attempt INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        elapsed_ms INTEGER
    )
    """,
    "CREATE INDEX idx_request_log_batch ON import_request_log(batch_id, page_no)",
    """
    CREATE TABLE raw_archive (
        digest TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        content_bytes INTEGER NOT NULL,
        interface TEXT NOT NULL,
        source_code TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        first_batch_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE trade_calendar (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exchange TEXT NOT NULL,
        cal_date TEXT NOT NULL,
        is_open INTEGER NOT NULL CHECK(is_open IN (0,1)),
        pretrade_date TEXT,
        source_code TEXT NOT NULL,
        revision_no INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        source_published_at TEXT,
        historical_available_at TEXT,
        observed_at TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        first_version_id TEXT,
        batch_id TEXT,
        UNIQUE(exchange, cal_date, revision_no, source_code)
    )
    """,
    "CREATE INDEX idx_calendar_exchange_date ON trade_calendar(exchange, cal_date)",
    """
    CREATE TABLE security_info (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        symbol TEXT,
        name TEXT,
        market TEXT,
        exchange TEXT,
        list_date TEXT,
        delist_date TEXT,
        list_status TEXT,
        source_code TEXT NOT NULL,
        revision_no INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        source_published_at TEXT,
        historical_available_at TEXT,
        observed_at TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        first_version_id TEXT,
        batch_id TEXT,
        UNIQUE(ts_code, revision_no, source_code)
    )
    """,
    "CREATE INDEX idx_security_code ON security_info(ts_code)",
    """
    CREATE TABLE daily_price (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        exchange TEXT,
        trade_date TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        pre_close REAL,
        volume REAL,
        amount REAL,
        source_volume_raw REAL,
        source_amount_raw REAL,
        currency TEXT NOT NULL DEFAULT 'CNY',
        source_code TEXT NOT NULL,
        revision_no INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        source_published_at TEXT,
        historical_available_at TEXT,
        observed_at TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        first_version_id TEXT,
        batch_id TEXT,
        UNIQUE(ts_code, trade_date, revision_no, source_code)
    )
    """,
    "CREATE INDEX idx_daily_code_date ON daily_price(ts_code, trade_date)",
    """
    CREATE TABLE adj_factor (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        adj_factor REAL NOT NULL,
        source_code TEXT NOT NULL,
        revision_no INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        source_published_at TEXT,
        historical_available_at TEXT,
        observed_at TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        first_version_id TEXT,
        batch_id TEXT,
        UNIQUE(ts_code, trade_date, revision_no, source_code)
    )
    """,
    "CREATE INDEX idx_adj_code_date ON adj_factor(ts_code, trade_date)",
    """
    CREATE TABLE suspension_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('S','R')),
        detail TEXT,
        source_code TEXT NOT NULL,
        revision_no INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        source_published_at TEXT,
        historical_available_at TEXT,
        observed_at TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        first_version_id TEXT,
        batch_id TEXT,
        UNIQUE(ts_code, trade_date, event_type, revision_no, source_code)
    )
    """,
    "CREATE INDEX idx_suspend_code_date ON suspension_event(ts_code, trade_date)",
    """
    CREATE TABLE corporate_action (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        end_date TEXT NOT NULL DEFAULT '',
        ann_date TEXT,
        record_date TEXT,
        ex_date TEXT,
        pay_date TEXT,
        div_listdate TEXT,
        div_proc TEXT NOT NULL DEFAULT '',
        is_effective INTEGER NOT NULL DEFAULT 0,
        cash_div REAL,
        cash_div_tax REAL,
        stk_div REAL,
        stk_bo_rate REAL,
        stk_co_rate REAL,
        base_share REAL,
        source_code TEXT NOT NULL,
        revision_no INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        source_published_at TEXT,
        historical_available_at TEXT,
        observed_at TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        first_version_id TEXT,
        batch_id TEXT,
        UNIQUE(ts_code, end_date, div_proc, revision_no, source_code)
    )
    """,
    "CREATE INDEX idx_action_code_date ON corporate_action(ts_code, ex_date)",
    """
    CREATE TABLE data_version (
        version_id TEXT PRIMARY KEY,
        source_code TEXT NOT NULL,
        scope TEXT NOT NULL,
        categories TEXT NOT NULL,
        quality_summary TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        status TEXT CHECK(status IN ('draft','published')) NOT NULL,
        created_at TEXT NOT NULL,
        published_at TEXT
    )
    """,
    """
    CREATE TABLE data_version_member (
        version_id TEXT NOT NULL,
        category TEXT NOT NULL,
        record_id INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        logical_key TEXT NOT NULL,
        PRIMARY KEY (version_id, category, record_id)
    )
    """,
    "CREATE INDEX idx_version_member_key ON data_version_member(version_id, category, logical_key)",
    """
    CREATE TABLE quality_issue (
        issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_code TEXT NOT NULL,
        severity TEXT CHECK(severity IN ('info','warn','high')) NOT NULL,
        scope TEXT NOT NULL,
        evidence TEXT,
        rule_version TEXT NOT NULL,
        data_version_id TEXT,
        batch_id TEXT,
        detected_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_quality_resume ON quality_issue(rule_code, batch_id)",
    """
    CREATE TABLE pool_snapshot (
        snapshot_id TEXT PRIMARY KEY,
        as_of TEXT NOT NULL,
        data_version_id TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        params TEXT NOT NULL,
        limit_flags TEXT NOT NULL,
        member_count INTEGER NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        generated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE pool_member (
        snapshot_id TEXT NOT NULL,
        ts_code TEXT NOT NULL,
        seq INTEGER NOT NULL,
        PRIMARY KEY (snapshot_id, ts_code)
    )
    """,
    """
    CREATE TABLE pool_exclusion (
        snapshot_id TEXT NOT NULL,
        ts_code TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        detail TEXT,
        seq INTEGER NOT NULL,
        PRIMARY KEY (snapshot_id, ts_code, seq)
    )
    """,
    "CREATE INDEX idx_pool_exclusion_snapshot ON pool_exclusion(snapshot_id)",
)


SCHEMA_3_STATEMENTS: tuple[str, ...] = (
    # 摘要算法与成员存储格式分开记录：旧版本保持 manifest_version=1 与旧摘要。
    "ALTER TABLE data_version ADD COLUMN manifest_version INTEGER NOT NULL DEFAULT 2",
    "ALTER TABLE data_version ADD COLUMN member_storage TEXT NOT NULL DEFAULT 'chunked'",
    "ALTER TABLE data_version ADD COLUMN member_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE data_version ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE data_version ADD COLUMN content_digest TEXT",
    "ALTER TABLE data_version ADD COLUMN retention_state TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE data_version ADD COLUMN window_mode TEXT NOT NULL DEFAULT 'full'",
    "ALTER TABLE data_version ADD COLUMN window_start TEXT",
    "ALTER TABLE data_version ADD COLUMN archive_id TEXT",
    """
    CREATE TABLE data_chunk (
        chunk_id TEXT PRIMARY KEY,
        source_code TEXT NOT NULL,
        category TEXT NOT NULL,
        partition_algorithm TEXT NOT NULL,
        partition_key TEXT NOT NULL,
        logical_sha256 TEXT NOT NULL,
        physical_sha256 TEXT NOT NULL,
        row_count INTEGER NOT NULL,
        byte_size INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        first_seen_version_id TEXT,
        UNIQUE(source_code, category, partition_algorithm, partition_key, physical_sha256)
    )
    """,
    "CREATE INDEX idx_chunk_partition ON data_chunk(source_code, category, partition_key)",
    """
    CREATE TABLE data_chunk_member (
        chunk_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        logical_key TEXT NOT NULL,
        record_id INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        ts_code TEXT,
        biz_date TEXT,
        PRIMARY KEY(chunk_id, logical_key)
    )
    """,
    "CREATE INDEX idx_chunk_member_record ON data_chunk_member(record_id)",
    "CREATE INDEX idx_chunk_member_filter ON data_chunk_member(ts_code, biz_date)",
    """
    CREATE TABLE data_version_chunk (
        version_id TEXT NOT NULL,
        category TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        PRIMARY KEY(version_id, category, chunk_id)
    )
    """,
    "CREATE INDEX idx_version_chunk_chunk ON data_version_chunk(chunk_id)",
    """
    CREATE TABLE coverage_stats (
        source_code TEXT NOT NULL,
        category TEXT NOT NULL,
        logical_count INTEGER NOT NULL,
        revision_rows INTEGER NOT NULL,
        start_date TEXT,
        end_date TEXT,
        computed_from_version_id TEXT,
        computed_at TEXT NOT NULL,
        stale INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(source_code, category)
    )
    """,
    """
    CREATE TABLE maintenance_generation (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        generation INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE retention_policy (
        policy_version TEXT PRIMARY KEY,
        config_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE retention_hold (
        hold_id TEXT PRIMARY KEY,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT
    )
    """,
    "CREATE INDEX idx_hold_target ON retention_hold(target_type, target_id)",
    """
    CREATE TABLE archive_manifest (
        archive_id TEXT PRIMARY KEY,
        version_id TEXT,
        source_code TEXT NOT NULL,
        category TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        manifest_path TEXT,
        manifest_sha256 TEXT,
        content_digest TEXT,
        bytes INTEGER NOT NULL,
        member_count INTEGER NOT NULL,
        record_count INTEGER NOT NULL,
        restore_meta TEXT,
        verified_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE cleanup_journal (
        journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        preview_id TEXT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        state TEXT NOT NULL,
        archive_id TEXT,
        detail TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE(preview_id, action, target_type, target_id)
    )
    """,
)


def create_schema_2(conn) -> None:
    """在连接上创建全部 schema 2 结构（调用方负责事务边界）。"""
    for statement in SCHEMA_2_STATEMENTS:
        conn.execute(statement)


def create_schema_3(conn) -> None:
    """在 schema 2 基础上创建 schema 3 结构（调用方负责事务边界）。"""
    for statement in SCHEMA_3_STATEMENTS:
        conn.execute(statement)
    conn.execute(
        "INSERT OR IGNORE INTO maintenance_generation(id, generation, updated_at) VALUES (1, 0, '')"
    )


def create_schema(conn) -> None:
    """新库直接初始化为目标 schema。"""
    create_schema_2(conn)
    create_schema_3(conn)


def table_names(conn) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return sorted(row[0] for row in rows)


def metadata_table_exists(conn) -> bool:
    return METADATA_TABLE in table_names(conn)

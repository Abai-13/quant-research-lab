"""数据库 schema 定义与结构升级。

- schema 1：仅 ``app_metadata``（阶段 1）；
- schema 2：阶段二结构，含来源注册、导入批次与请求日志、原始档案索引、
  规范化记录修订、数据版本清单、质量问题、股票池快照；
- schema 3：收尾结构，新增不可变分块清单、版本对分块的引用、逻辑/修订统计、
  引用保护与归档索引；旧版本成员表保留兼容。
- schema 4：阶段三基础结构（纯增量）：账户/账户事件/账户快照、不可变规则组合、
  分析运行与目标、模拟段/订单/成交、公司行为应收、估值与收益点；阶段二全部
  表与语义保持不变。
- 升级必须通过显式迁移入口执行：先做一致性备份并核对备份，再在单个事务内完成；
  未知 schema 或损坏库不迁移、不删除、不重建。

时间口径：``observed_at`` / ``ingested_at`` 是本系统观察与落库时刻（UTC 带时区）；
``source_published_at`` 是供应商可证明的发布/修订时刻（未知为 NULL）；
``historical_available_at`` 是可证明该值在历史上何时可用（不可证明时为 NULL）。
业务日期 ``trade_date`` / ``cal_date`` / ``end_date`` 一律为交易所或事件业务日期，
不使用下载日期代替。
"""

from __future__ import annotations

SCHEMA_VERSION = 4
SCHEMA_3_VERSION = 3
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


# schema 4：阶段三 S1 基础结构。设计约束（阶段三 §3/§9 与派工决定）：
# - 金额一律 INTEGER 分（cents），股数一律非负整数股；未知金额保持 NULL，不用 0 冒充；
#   换手/补仓后每股成本可能是非整数分：持仓只保留权威的 total_cost_cents（整数分），
#   每股成本由服务层用 Decimal 派生，不得在库里存近似值；
# - 幂等键由调用方提供或由确定性输入摘要生成，绝不含随机 nonce；可选股票池快照缺席时
#   依靠 NOT NULL 的规范化 input_sha256 唯一键保持幂等；
# - 规则/费用组合不可变并显式标注 synthetic_simulation / verified_configuration；
# - 公司行为按 record_date 定权益、ex_date 确认应收、pay_date/list_date 到账；
# - 实际与模拟账户共用同一账户表并 bundle kind 显式区分，事件/引用同事务防混账；
#   模拟账户必须显式引用一个**实际**账户（外键 + 触发器校验 kind），不得自引用、
#   不得指向另一个模拟账户；
# - 引用数据版本/股票池快照的结果保存必须同事务登记 retention_hold 并推进 generation；
# - 全部新表带 FOREIGN KEY 引用其真实父记录（阶段二表 data_version/pool_snapshot
#   也作为父表引用；子表约束只加在阶段三新表上，不改阶段二表结构）。SQLite 外键
#   按连接生效：阶段三写入必须走 ``app.portfolio.db.portfolio_connect``（或显式
#   执行 ``PRAGMA foreign_keys=ON``），否则孤儿引用不报错——这是既定边界，不是缺陷；
# - ``pool_scope_evidence`` 是显式登记的市场范围/风险状态核验记录（两个核验位都必须
#   为真才能登记成功）。登记必须带可独立复核的凭据：``verification_source``、
#   ``verified_by``、原样保存的 ``evidence_payload`` 及其摘要 ``evidence_sha256``
#   （读取时重算核对）。S1 阶段没有真实的核验工作流，登记只能是
#   ``pending_external_review``；只有工作流显式写 ``confirmed_by_workflow`` 并附
#   confirmed_at/confirmation_source 才可能用于解除 signal_input 限制。
#   缺少该状态 或 快照仍带 unresolved 范围旗标时，signal_input 一律受限失败关闭；
#   绝不从代码前缀推断板块/ST 状态。
#
# S2（账本/会计）补充约束：
# - 实际事件只能挂 kind='actual' 的账户（触发器，任何连接生效）；
# - 事件追加式（append-only）：更正以 supersedes_event_id 显式关联，不覆盖不删除；
#   被更正事件不可再被第二次更正（服务层拒绝，保证有效事件流无歧义）。
SCHEMA_4_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE account (
        account_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('actual','simulation')) NOT NULL,
        display_name TEXT NOT NULL,
        base_currency TEXT NOT NULL DEFAULT 'CNY',
        account_revision INTEGER NOT NULL DEFAULT 1,
        simulation_of_account_id TEXT REFERENCES account(account_id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(
            (
                kind = 'actual' AND simulation_of_account_id IS NULL
            )
            OR (
                kind = 'simulation' AND simulation_of_account_id IS NOT NULL
                AND simulation_of_account_id != account_id
            )
        )
    )
    """,
    "CREATE INDEX idx_account_kind ON account(kind)",
    # 外键按连接生效；触发器不依赖 pragma，跨域引用（模拟→模拟）在这里被一律拒绝。
    """
    CREATE TRIGGER trg_account_simulation_of_actual_insert
    AFTER INSERT ON account
    WHEN NEW.simulation_of_account_id IS NOT NULL
    BEGIN
        SELECT CASE
            WHEN (SELECT kind FROM account WHERE account_id = NEW.simulation_of_account_id)
                IS NOT 'actual'
            THEN RAISE(ABORT, 'simulation_of_account_id_must_reference_actual_account')
        END;
    END
    """,
    """
    CREATE TRIGGER trg_account_simulation_of_actual_update
    AFTER UPDATE OF kind, simulation_of_account_id ON account
    WHEN NEW.simulation_of_account_id IS NOT NULL
    BEGIN
        SELECT CASE
            WHEN (SELECT kind FROM account WHERE account_id = NEW.simulation_of_account_id)
                IS NOT 'actual'
            THEN RAISE(ABORT, 'simulation_of_account_id_must_reference_actual_account')
        END;
    END
    """,
    """
    CREATE TRIGGER trg_account_kind_change_requires_no_simulation_children
    AFTER UPDATE OF kind ON account
    WHEN OLD.kind = 'actual' AND NEW.kind = 'simulation'
    BEGIN
        SELECT CASE
            WHEN EXISTS(
                SELECT 1 FROM account WHERE simulation_of_account_id = OLD.account_id
            )
            THEN RAISE(ABORT, 'actual_account_has_simulation_children')
        END;
    END
    """,
    # 父记录防护：一旦被 simulation_segment 引用（作为来源或模拟分身），
    # 账户的领域身份（kind / simulation_of_account_id）不可再改，
    # 否则段落里"来源=实际、分身=该实际的模拟"配对会被事后作废。
    """
    CREATE TRIGGER trg_account_identity_immutable_when_referenced_by_segment
    AFTER UPDATE OF kind, simulation_of_account_id ON account
    WHEN EXISTS(
        SELECT 1 FROM simulation_segment
        WHERE simulation_segment.source_account_id = OLD.account_id
           OR simulation_segment.simulation_account_id = OLD.account_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'account_identity_referenced_by_simulation_segment');
    END
    """,
    """
    CREATE TABLE account_event (
        event_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES account(account_id),
        event_type TEXT NOT NULL CHECK(event_type IN (
            'opening','buy','sell','cash_in','cash_out','corporate_action','correction'
        )) NOT NULL,
        business_date TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual_entry',
        idempotency_key TEXT NOT NULL,
        event_version INTEGER NOT NULL DEFAULT 1,
        supersedes_event_id TEXT REFERENCES account_event(event_id),
        reason TEXT,
        payload TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(account_id, idempotency_key)
    )
    """,
    "CREATE INDEX idx_account_event_account ON account_event(account_id, business_date)",
    "CREATE INDEX idx_account_event_supersedes ON account_event(supersedes_event_id)",
    # 同域一致性（触发器，任何连接都生效）：被更正事件必须属于同一账户。
    """
    CREATE TRIGGER trg_account_event_supersedes_same_account_insert
    AFTER INSERT ON account_event
    WHEN NEW.supersedes_event_id IS NOT NULL
    BEGIN
        SELECT CASE
            WHEN (SELECT account_id FROM account_event
                  WHERE event_id = NEW.supersedes_event_id)
                IS NOT NEW.account_id
            THEN RAISE(ABORT, 'supersedes_event_cross_account')
        END;
    END
    """,
    """
    CREATE TRIGGER trg_account_event_supersedes_same_account_update
    AFTER UPDATE OF account_id, supersedes_event_id ON account_event
    WHEN NEW.supersedes_event_id IS NOT NULL
    BEGIN
        SELECT CASE
            WHEN (SELECT account_id FROM account_event
                  WHERE event_id = NEW.supersedes_event_id)
                IS NOT NEW.account_id
            THEN RAISE(ABORT, 'supersedes_event_cross_account')
        END;
    END
    """,
    # 父记录防护：一旦被更正事件 supersede，被更正事件的归属账户不可再改挂，
    # 否则"更正与被更正同账户"的既有关系会被事后作废。
    """
    CREATE TRIGGER trg_account_event_account_id_immutable_when_superseded
    AFTER UPDATE OF account_id ON account_event
    WHEN EXISTS(
        SELECT 1 FROM account_event c WHERE c.supersedes_event_id = OLD.event_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'account_event_account_id_referenced_by_correction');
    END
    """,
    # S2 账本领域隔离：实际事件只能挂在 kind='actual' 的账户上。触发器不依赖
    # 外键 pragma，任何连接都生效；模拟账户的记账属于模拟段/模拟表，不混入实际账本。
    """
    CREATE TRIGGER trg_account_event_actual_domain_insert
    AFTER INSERT ON account_event
    BEGIN
        SELECT CASE
            WHEN (SELECT kind FROM account WHERE account_id = NEW.account_id)
                IS NOT 'actual'
            THEN RAISE(ABORT, 'account_event_requires_actual_account')
        END;
    END
    """,
    """
    CREATE TRIGGER trg_account_event_actual_domain_update
    AFTER UPDATE OF account_id ON account_event
    BEGIN
        SELECT CASE
            WHEN (SELECT kind FROM account WHERE account_id = NEW.account_id)
                IS NOT 'actual'
            THEN RAISE(ABORT, 'account_event_requires_actual_account')
        END;
    END
    """,
    """
    CREATE TABLE account_snapshot (
        snapshot_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES account(account_id),
        as_of TEXT NOT NULL,
        account_revision INTEGER NOT NULL,
        cash_cents INTEGER NOT NULL CHECK(cash_cents >= 0),
        data_version_id TEXT REFERENCES data_version(version_id),
        source_code TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_account_snapshot_account ON account_snapshot(account_id, as_of)",
    # 父记录防护：一旦被 analysis_run / simulation_segment 引用，快照的所有权
    # 不可改挂到别的账户（引用仍然有效的其他更新，如金额修正，不受影响）。
    """
    CREATE TRIGGER trg_account_snapshot_account_id_immutable_when_referenced
    AFTER UPDATE OF account_id ON account_snapshot
    WHEN EXISTS(
        SELECT 1 FROM analysis_run WHERE analysis_run.account_snapshot_id = OLD.snapshot_id
    )
    OR EXISTS(
        SELECT 1 FROM simulation_segment
        WHERE simulation_segment.base_account_snapshot_id = OLD.snapshot_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'account_snapshot_account_id_referenced');
    END
    """,
    """
    CREATE TABLE account_position (
        snapshot_id TEXT NOT NULL REFERENCES account_snapshot(snapshot_id),
        ts_code TEXT NOT NULL,
        shares INTEGER NOT NULL CHECK(shares >= 0),
        total_cost_cents INTEGER
            CHECK(total_cost_cents IS NULL OR total_cost_cents >= 0),
        sellable_shares INTEGER CHECK(sellable_shares IS NULL OR sellable_shares >= 0),
        acquire_date TEXT,
        locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)),
        price_status TEXT NOT NULL DEFAULT 'user_reported'
            CHECK(price_status IN ('user_reported','estimated','unknown')),
        PRIMARY KEY (snapshot_id, ts_code)
    )
    """,
    """
    CREATE TABLE pool_scope_evidence (
        snapshot_id TEXT PRIMARY KEY REFERENCES pool_snapshot(snapshot_id),
        exchange_board_verified INTEGER NOT NULL CHECK(exchange_board_verified = 1),
        risk_status_verified INTEGER NOT NULL CHECK(risk_status_verified = 1),
        verification_source TEXT NOT NULL,
        verified_by TEXT NOT NULL,
        evidence_payload TEXT NOT NULL,
        evidence_sha256 TEXT NOT NULL,
        verification_status TEXT NOT NULL
            CHECK(verification_status IN ('pending_external_review','confirmed_by_workflow'))
            NOT NULL DEFAULT 'pending_external_review',
        confirmed_at TEXT,
        confirmation_source TEXT,
        notes TEXT,
        registered_at TEXT NOT NULL,
        CHECK(verification_status != 'confirmed_by_workflow'
              OR (confirmed_at IS NOT NULL AND confirmation_source IS NOT NULL))
    )
    """,
    """
    CREATE TABLE rule_bundle (
        bundle_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('parameter','fee','execution','corporate_action')) NOT NULL,
        version TEXT NOT NULL,
        assumption_scope TEXT NOT NULL CHECK(assumption_scope IN ('synthetic_simulation','verified_configuration')),
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        notes TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(kind, version)
    )
    """,
    """
    CREATE TABLE account_constraint (
        constraint_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES account(account_id),
        revision INTEGER NOT NULL,
        rule_bundle_id TEXT NOT NULL REFERENCES rule_bundle(bundle_id),
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        frozen_at TEXT NOT NULL,
        UNIQUE(account_id, revision)
    )
    """,
    "CREATE INDEX idx_account_constraint_account ON account_constraint(account_id, revision)",
    """
    CREATE TABLE analysis_run (
        run_id TEXT PRIMARY KEY,
        mode TEXT NOT NULL CHECK(mode IN ('incremental','independent')) NOT NULL,
        account_id TEXT NOT NULL REFERENCES account(account_id),
        account_snapshot_id TEXT NOT NULL REFERENCES account_snapshot(snapshot_id),
        account_revision INTEGER NOT NULL,
        source_code TEXT NOT NULL,
        data_version_id TEXT NOT NULL REFERENCES data_version(version_id),
        pool_snapshot_id TEXT REFERENCES pool_snapshot(snapshot_id),
        as_of TEXT NOT NULL,
        data_cutoff_date TEXT,
        rule_bundle_id TEXT NOT NULL REFERENCES rule_bundle(bundle_id),
        input_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('complete','restricted')) NOT NULL,
        restriction_code TEXT,
        restriction_detail TEXT,
        generated_at TEXT NOT NULL,
        valid_from TEXT,
        valid_until TEXT,
        invalidation_reason TEXT,
        source_changed INTEGER NOT NULL DEFAULT 0 CHECK(source_changed IN (0,1)),
        UNIQUE(input_sha256),
        CHECK(status != 'restricted' OR (restriction_code IS NOT NULL AND restriction_detail IS NOT NULL))
    )
    """,
    "CREATE INDEX idx_analysis_run_account ON analysis_run(account_id, generated_at)",
    # 同域一致性：分析必须绑定自己账户的快照（触发器，任何连接都生效）。
    """
    CREATE TRIGGER trg_analysis_run_snapshot_owner_matches_insert
    AFTER INSERT ON analysis_run
    BEGIN
        SELECT CASE
            WHEN (SELECT account_id FROM account_snapshot
                  WHERE snapshot_id = NEW.account_snapshot_id)
                IS NOT NEW.account_id
            THEN RAISE(ABORT, 'analysis_run_account_snapshot_owner_mismatch')
        END;
    END
    """,
    """
    CREATE TRIGGER trg_analysis_run_snapshot_owner_matches_update
    AFTER UPDATE OF account_id, account_snapshot_id ON analysis_run
    BEGIN
        SELECT CASE
            WHEN (SELECT account_id FROM account_snapshot
                  WHERE snapshot_id = NEW.account_snapshot_id)
                IS NOT NEW.account_id
            THEN RAISE(ABORT, 'analysis_run_account_snapshot_owner_mismatch')
        END;
    END
    """,
    """
    CREATE TABLE analysis_item (
        run_id TEXT NOT NULL REFERENCES analysis_run(run_id),
        seq INTEGER NOT NULL,
        ts_code TEXT NOT NULL,
        decision TEXT,
        reason TEXT,
        quantity_shares INTEGER,
        weight_bp INTEGER,
        restriction_code TEXT,
        PRIMARY KEY (run_id, seq)
    )
    """,
    "CREATE INDEX idx_analysis_item_run_code ON analysis_item(run_id, ts_code)",
    """
    CREATE TABLE analysis_artifact (
        run_id TEXT NOT NULL REFERENCES analysis_run(run_id),
        artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('input','output')) NOT NULL,
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, artifact_kind)
    )
    """,
    """
    CREATE TABLE portfolio_target (
        target_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES analysis_run(run_id),
        kind TEXT NOT NULL CHECK(kind IN ('ideal','executable_transition')) NOT NULL,
        cash_cents INTEGER NOT NULL CHECK(cash_cents >= 0),
        turnover_budget_bp INTEGER,
        created_at TEXT NOT NULL,
        UNIQUE(run_id, kind)
    )
    """,
    """
    CREATE TABLE target_item (
        target_id TEXT NOT NULL REFERENCES portfolio_target(target_id),
        ts_code TEXT NOT NULL,
        target_shares INTEGER NOT NULL CHECK(target_shares >= 0),
        delta_shares INTEGER NOT NULL,
        price_basis TEXT,
        limit_price_cents INTEGER,
        reason TEXT,
        seq INTEGER NOT NULL,
        PRIMARY KEY (target_id, ts_code)
    )
    """,
    """
    CREATE TABLE simulation_segment (
        segment_id TEXT PRIMARY KEY,
        source_account_id TEXT NOT NULL REFERENCES account(account_id),
        simulation_account_id TEXT NOT NULL REFERENCES account(account_id),
        base_account_snapshot_id TEXT NOT NULL REFERENCES account_snapshot(snapshot_id),
        rule_bundle_id TEXT NOT NULL REFERENCES rule_bundle(bundle_id),
        fee_bundle_id TEXT NOT NULL REFERENCES rule_bundle(bundle_id),
        source_code TEXT NOT NULL,
        data_version_id TEXT NOT NULL REFERENCES data_version(version_id),
        as_of TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','closed')) NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        UNIQUE(input_sha256)
    )
    """,
    "CREATE INDEX idx_simulation_segment_account ON simulation_segment(source_account_id, created_at)",
    """
    CREATE TRIGGER trg_simulation_segment_domains_differ_insert
    AFTER INSERT ON simulation_segment
    BEGIN
        SELECT CASE
            WHEN (SELECT kind FROM account WHERE account_id = NEW.source_account_id)
                IS NOT 'actual'
            THEN RAISE(ABORT, 'simulation_segment_source_must_be_actual_account')
            WHEN (SELECT kind FROM account WHERE account_id = NEW.simulation_account_id)
                IS NOT 'simulation'
            THEN RAISE(ABORT, 'simulation_segment_simulated_must_be_simulation_account')
            WHEN (SELECT account_id FROM account_snapshot
                  WHERE snapshot_id = NEW.base_account_snapshot_id)
                IS NOT NEW.source_account_id
            THEN RAISE(ABORT, 'simulation_segment_base_snapshot_owner_mismatch')
            WHEN (SELECT simulation_of_account_id FROM account
                  WHERE account_id = NEW.simulation_account_id)
                IS NOT NEW.source_account_id
            THEN RAISE(ABORT, 'simulation_segment_simulation_of_source_mismatch')
        END;
    END
    """,
    """
    CREATE TRIGGER trg_simulation_segment_domains_differ_update
    AFTER UPDATE OF source_account_id, simulation_account_id, base_account_snapshot_id
    ON simulation_segment
    BEGIN
        SELECT CASE
            WHEN (SELECT kind FROM account WHERE account_id = NEW.source_account_id)
                IS NOT 'actual'
            THEN RAISE(ABORT, 'simulation_segment_source_must_be_actual_account')
            WHEN (SELECT kind FROM account WHERE account_id = NEW.simulation_account_id)
                IS NOT 'simulation'
            THEN RAISE(ABORT, 'simulation_segment_simulated_must_be_simulation_account')
            WHEN (SELECT account_id FROM account_snapshot
                  WHERE snapshot_id = NEW.base_account_snapshot_id)
                IS NOT NEW.source_account_id
            THEN RAISE(ABORT, 'simulation_segment_base_snapshot_owner_mismatch')
            WHEN (SELECT simulation_of_account_id FROM account
                  WHERE account_id = NEW.simulation_account_id)
                IS NOT NEW.source_account_id
            THEN RAISE(ABORT, 'simulation_segment_simulation_of_source_mismatch')
        END;
    END
    """,
    """
    CREATE TABLE simulation_order (
        order_id TEXT PRIMARY KEY,
        segment_id TEXT NOT NULL REFERENCES simulation_segment(segment_id),
        trade_date TEXT NOT NULL,
        ts_code TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('buy','sell')) NOT NULL,
        planned_shares INTEGER NOT NULL CHECK(planned_shares > 0),
        filled_shares INTEGER NOT NULL DEFAULT 0 CHECK(filled_shares >= 0),
        remaining_shares INTEGER NOT NULL CHECK(remaining_shares >= 0),
        status TEXT NOT NULL CHECK(status IN (
            'pending','unfilled','partially_filled','filled','rejected','expired','cancelled'
        )) NOT NULL,
        reason_code TEXT,
        model_price_cents INTEGER,
        fee_estimate_cents INTEGER,
        slippage_estimate_cents INTEGER,
        last_attempt_date TEXT,
        idempotency_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(segment_id, idempotency_key),
        CHECK(remaining_shares = planned_shares - filled_shares)
    )
    """,
    "CREATE INDEX idx_simulation_order_segment ON simulation_order(segment_id, trade_date)",
    """
    CREATE TABLE simulation_fill (
        fill_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES simulation_order(order_id),
        fill_seq INTEGER NOT NULL,
        trade_date TEXT NOT NULL,
        filled_shares INTEGER NOT NULL CHECK(filled_shares > 0),
        price_cents INTEGER NOT NULL CHECK(price_cents > 0),
        fee_cents INTEGER NOT NULL DEFAULT 0 CHECK(fee_cents >= 0),
        slippage_cents INTEGER NOT NULL DEFAULT 0 CHECK(slippage_cents >= 0),
        fee_breakdown TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(order_id, fill_seq)
    )
    """,
    # S5a 方案 A：模拟账户的期初/成交/持仓只写模拟域，绝不写
    # account_event / account_snapshot / account_position（S2 触发器仍拒绝）。
    """
    CREATE TABLE simulation_event (
        event_id TEXT PRIMARY KEY,
        segment_id TEXT NOT NULL REFERENCES simulation_segment(segment_id),
        event_type TEXT NOT NULL CHECK(event_type IN
            ('opening','buy','sell','corporate_action')) NOT NULL,
        business_date TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        payload TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(segment_id, idempotency_key)
    )
    """,
    "CREATE INDEX idx_simulation_event_segment ON simulation_event(segment_id, business_date)",
    """
    CREATE TABLE simulation_account_state (
        segment_id TEXT PRIMARY KEY REFERENCES simulation_segment(segment_id),
        cash_cents INTEGER NOT NULL CHECK(cash_cents >= 0),
        revision INTEGER NOT NULL CHECK(revision >= 0),
        as_of TEXT NOT NULL,
        last_advanced_date TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE simulation_position_state (
        segment_id TEXT NOT NULL REFERENCES simulation_segment(segment_id),
        ts_code TEXT NOT NULL,
        shares INTEGER NOT NULL CHECK(shares >= 0),
        total_cost_cents INTEGER CHECK(total_cost_cents IS NULL OR total_cost_cents >= 0),
        sellable_shares INTEGER CHECK(sellable_shares IS NULL OR sellable_shares >= 0),
        acquire_date TEXT,
        locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)),
        price_status TEXT NOT NULL DEFAULT 'user_reported'
            CHECK(price_status = 'user_reported'),
        note TEXT,
        PRIMARY KEY(segment_id, ts_code),
        CHECK(sellable_shares IS NULL OR sellable_shares <= shares)
    )
    """,
    # 可卖股数按批次追踪：unlock_date 为 NULL 表示未知/尚不可卖（fail-closed）。
    """
    CREATE TABLE simulation_lot_state (
        lot_id TEXT PRIMARY KEY,
        segment_id TEXT NOT NULL REFERENCES simulation_segment(segment_id),
        ts_code TEXT NOT NULL,
        shares INTEGER NOT NULL CHECK(shares > 0),
        acquire_date TEXT NOT NULL,
        unlock_date TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_simulation_lot_state ON simulation_lot_state(segment_id, ts_code, unlock_date)",
    """
    CREATE TABLE corporate_action_application (
        application_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES account(account_id),
        ts_code TEXT NOT NULL,
        action_kind TEXT NOT NULL CHECK(action_kind IN ('cash_dividend','stock_dividend','other')) NOT NULL,
        record_date TEXT NOT NULL,
        ex_date TEXT,
        pay_date TEXT,
        list_date TEXT,
        entitlement_shares INTEGER NOT NULL CHECK(entitlement_shares >= 0),
        cash_receivable_cents INTEGER CHECK(cash_receivable_cents IS NULL OR cash_receivable_cents >= 0),
        status TEXT NOT NULL CHECK(status IN (
            'pending','receivable','settled','blocked_insufficient_evidence'
        )) NOT NULL DEFAULT 'pending',
        tax_status TEXT NOT NULL DEFAULT 'unknown' CHECK(tax_status IN ('unknown','estimated','confirmed')),
        data_version_id TEXT REFERENCES data_version(version_id),
        idempotency_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        settled_at TEXT,
        UNIQUE(account_id, idempotency_key),
        CHECK(
            status != 'settled'
            OR (action_kind = 'cash_dividend' AND pay_date IS NOT NULL)
            OR (action_kind = 'stock_dividend' AND list_date IS NOT NULL)
            OR (action_kind = 'other' AND (pay_date IS NOT NULL OR list_date IS NOT NULL))
        )
    )
    """,
    "CREATE INDEX idx_corporate_action_account ON corporate_action_application(account_id, record_date)",
    """
    CREATE TABLE valuation_snapshot (
        valuation_id TEXT PRIMARY KEY,
        owner_type TEXT NOT NULL CHECK(owner_type IN ('account','simulation_segment')) NOT NULL,
        owner_id TEXT NOT NULL,
        as_of TEXT NOT NULL,
        source_code TEXT,
        data_version_id TEXT REFERENCES data_version(version_id),
        cash_cents INTEGER,
        holdings_value_cents INTEGER,
        receivable_cents INTEGER NOT NULL DEFAULT 0 CHECK(receivable_cents >= 0),
        payable_cents INTEGER NOT NULL DEFAULT 0 CHECK(payable_cents >= 0),
        nav_cents INTEGER,
        completeness TEXT NOT NULL CHECK(completeness IN ('complete','partial_unknown')) NOT NULL,
        restriction_code TEXT,
        created_at TEXT NOT NULL,
        CHECK(nav_cents IS NULL OR completeness = 'complete'),
        CHECK(
            completeness != 'complete'
            OR (cash_cents IS NOT NULL AND holdings_value_cents IS NOT NULL AND nav_cents IS NOT NULL)
        )
    )
    """,
    "CREATE INDEX idx_valuation_owner ON valuation_snapshot(owner_type, owner_id, as_of)",
    # 多态 owner（account / simulation_segment）无法用外键表达：用触发器按 owner_type 校验目标存在。
    """
    CREATE TRIGGER trg_valuation_snapshot_owner_exists_insert
    AFTER INSERT ON valuation_snapshot
    BEGIN
        SELECT CASE
            WHEN NEW.owner_type = 'account'
                AND (SELECT COUNT(*) FROM account WHERE account_id = NEW.owner_id) = 0
            THEN RAISE(ABORT, 'valuation_owner_missing')
            WHEN NEW.owner_type = 'simulation_segment'
                AND (SELECT COUNT(*) FROM simulation_segment WHERE segment_id = NEW.owner_id) = 0
            THEN RAISE(ABORT, 'valuation_owner_missing')
        END;
    END
    """,
    """
    CREATE TRIGGER trg_valuation_snapshot_owner_exists_update
    AFTER UPDATE OF owner_type, owner_id ON valuation_snapshot
    BEGIN
        SELECT CASE
            WHEN NEW.owner_type = 'account'
                AND (SELECT COUNT(*) FROM account WHERE account_id = NEW.owner_id) = 0
            THEN RAISE(ABORT, 'valuation_owner_missing')
            WHEN NEW.owner_type = 'simulation_segment'
                AND (SELECT COUNT(*) FROM simulation_segment WHERE segment_id = NEW.owner_id) = 0
            THEN RAISE(ABORT, 'valuation_owner_missing')
        END;
    END
    """,
    # 父记录防护：一旦被 performance_point 引用，估值快照的
    # owner_type / owner_id / as_of 不可再改，否则业绩点的
    # "同 owner 同 as_of" 三元组匹配关系会被事后作废。
    """
    CREATE TRIGGER trg_valuation_snapshot_identity_immutable_when_performances
    AFTER UPDATE OF owner_type, owner_id, as_of ON valuation_snapshot
    WHEN EXISTS(
        SELECT 1 FROM performance_point WHERE performance_point.valuation_id = OLD.valuation_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'valuation_snapshot_identity_referenced_by_performance');
    END
    """,
    """
    CREATE TABLE performance_point (
        point_id TEXT PRIMARY KEY,
        owner_type TEXT NOT NULL CHECK(owner_type IN ('account','simulation_segment')) NOT NULL,
        owner_id TEXT NOT NULL,
        as_of TEXT NOT NULL,
        valuation_id TEXT NOT NULL REFERENCES valuation_snapshot(valuation_id),
        method_version TEXT NOT NULL,
        external_cash_flow_cents INTEGER NOT NULL DEFAULT 0,
        period_return_bp INTEGER,
        nav_cents INTEGER,
        created_at TEXT NOT NULL,
        UNIQUE(owner_type, owner_id, as_of, method_version)
    )
    """,
    "CREATE INDEX idx_performance_owner ON performance_point(owner_type, owner_id, as_of)",
    # 同域一致性：业绩点必须引用同一 owner、同一 as_of 的估值快照。
    """
    CREATE TRIGGER trg_performance_point_valuation_matches_insert
    AFTER INSERT ON performance_point
    BEGIN
        SELECT CASE
            WHEN (SELECT owner_type FROM valuation_snapshot
                  WHERE valuation_id = NEW.valuation_id)
                IS NOT NEW.owner_type
            THEN RAISE(ABORT, 'performance_valuation_owner_mismatch')
            WHEN (SELECT owner_id FROM valuation_snapshot
                  WHERE valuation_id = NEW.valuation_id)
                IS NOT NEW.owner_id
            THEN RAISE(ABORT, 'performance_valuation_owner_mismatch')
            WHEN (SELECT as_of FROM valuation_snapshot
                  WHERE valuation_id = NEW.valuation_id)
                IS NOT NEW.as_of
            THEN RAISE(ABORT, 'performance_valuation_as_of_mismatch')
        END;
    END
    """,
    """
    CREATE TRIGGER trg_performance_point_valuation_matches_update
    AFTER UPDATE OF owner_type, owner_id, as_of, valuation_id ON performance_point
    BEGIN
        SELECT CASE
            WHEN (SELECT owner_type FROM valuation_snapshot
                  WHERE valuation_id = NEW.valuation_id)
                IS NOT NEW.owner_type
            THEN RAISE(ABORT, 'performance_valuation_owner_mismatch')
            WHEN (SELECT owner_id FROM valuation_snapshot
                  WHERE valuation_id = NEW.valuation_id)
                IS NOT NEW.owner_id
            THEN RAISE(ABORT, 'performance_valuation_owner_mismatch')
            WHEN (SELECT as_of FROM valuation_snapshot
                  WHERE valuation_id = NEW.valuation_id)
                IS NOT NEW.as_of
            THEN RAISE(ABORT, 'performance_valuation_as_of_mismatch')
        END;
    END
    """,
    """
    CREATE TRIGGER trg_performance_point_owner_exists_insert
    AFTER INSERT ON performance_point
    BEGIN
        SELECT CASE
            WHEN NEW.owner_type = 'account'
                AND (SELECT COUNT(*) FROM account WHERE account_id = NEW.owner_id) = 0
            THEN RAISE(ABORT, 'performance_owner_missing')
            WHEN NEW.owner_type = 'simulation_segment'
                AND (SELECT COUNT(*) FROM simulation_segment WHERE segment_id = NEW.owner_id) = 0
            THEN RAISE(ABORT, 'performance_owner_missing')
        END;
    END
    """,
    """
    CREATE TRIGGER trg_performance_point_owner_exists_update
    AFTER UPDATE OF owner_type, owner_id ON performance_point
    BEGIN
        SELECT CASE
            WHEN NEW.owner_type = 'account'
                AND (SELECT COUNT(*) FROM account WHERE account_id = NEW.owner_id) = 0
            THEN RAISE(ABORT, 'performance_owner_missing')
            WHEN NEW.owner_type = 'simulation_segment'
                AND (SELECT COUNT(*) FROM simulation_segment WHERE segment_id = NEW.owner_id) = 0
            THEN RAISE(ABORT, 'performance_owner_missing')
        END;
    END
    """,
)

PHASE3_TABLE_NAMES: tuple[str, ...] = (
    "account",
    "account_event",
    "account_snapshot",
    "account_position",
    "pool_scope_evidence",
    "rule_bundle",
    "account_constraint",
    "analysis_run",
    "analysis_item",
    "analysis_artifact",
    "portfolio_target",
    "target_item",
    "simulation_segment",
    "simulation_order",
    "simulation_fill",
    "simulation_event",
    "simulation_account_state",
    "simulation_position_state",
    "simulation_lot_state",
    "corporate_action_application",
    "valuation_snapshot",
    "performance_point",
)


def create_schema_4(conn) -> None:
    """在 schema 3 基础上创建阶段三基础结构（调用方负责事务边界）。"""
    for statement in SCHEMA_4_STATEMENTS:
        conn.execute(statement)


def create_schema(conn) -> None:
    """新库直接初始化为目标 schema。"""
    create_schema_2(conn)
    create_schema_3(conn)
    create_schema_4(conn)


def table_names(conn) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return sorted(row[0] for row in rows)


def metadata_table_exists(conn) -> bool:
    return METADATA_TABLE in table_names(conn)

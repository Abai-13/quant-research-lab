"""阶段三 S1：数据上下文绑定、规则组合、引用保护与金额/股数边界。

全部使用隔离临时库（``temp_settings``），先完成 bundle/fixture 版本构建；
不访问真实数据源、不运行应用。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from app.data.errors import DataError
from app.data.migration import initialize_database
from app.data.retention import apply_cleanup, preview_cleanup
from app.data.store import write_records
from app.data.timeutil import utc_now_iso
from app.portfolio.money import (
    MONEY_UNIT_VERSION,
    cents_to_decimal_string,
    derive_per_share_cost,
    parse_money_to_cents,
    parse_shares,
)
from app.portfolio.db import portfolio_connect
from app.portfolio.references import (
    ReferenceTarget,
    analysis_input_sha256,
    register_reference_holds,
    release_reference_holds,
)
from app.portfolio.rule_bundles import (
    DEFAULT_BUNDLE_VERSIONS,
    DEFAULT_FEE_BUNDLE,
    create_rule_bundle,
    ensure_default_bundles,
    load_rule_bundle,
    require_assumption_scope,
)
from app.portfolio.service import (
    RESTRICTION_POOL_VERSION_MISMATCH,
    RESTRICTION_UNIVERSE_UNVERIFIED,
    UNRESOLVED_SCOPE_FLAGS,
    DataSelectionContext,
    bind_data_context,
    load_pool_scope_evidence,
    register_pool_scope_evidence,
)


@pytest.fixture
def portfolio_conn(data_settings):
    """阶段三连接边界：外键约束已启用的连接（schema 4 库）。"""
    initialize_database(data_settings)
    conn = portfolio_connect(data_settings.db_path)
    try:
        yield conn, data_settings
    finally:
        conn.close()


def _default_bundle_ids(conn: sqlite3.Connection) -> str:
    return ensure_default_bundles(conn)["parameter"]


def _published_version(conn: sqlite3.Connection, *, codes=("000001.SZ",), as_of="20250304") -> str:
    """用阶段二既有写入/发布路径构造真实可读的数据版本。

    覆盖截至 ``as_of``（含）的 120+ 个交易日（工作日近似），并写入复权因子和
    日历，满足阶段二信号输入的最小证据要求。
    """
    import datetime as dt

    start = dt.date(2024, 9, 9)
    end = dt.date(int(as_of[:4]), int(as_of[4:6]), int(as_of[6:8]))
    days: list[str] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y%m%d"))
        cursor += dt.timedelta(days=1)
    assert len(days) > 120, "夹具必须提供足够的历史长度"

    records = []
    for code in codes:
        for day in days:
            records.append(
                {
                    "ts_code": code,
                    "trade_date": day,
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.9,
                    "close": 10.1,
                    "pre_close": 10.0,
                    "volume": 1000.0,
                    "amount": 10000.0,
                    "currency": "CNY",
                    "source_volume_raw": 1000.0,
                    "source_amount_raw": 10000.0,
                }
            )
    write_records(conn, "daily", records, source_code="tushare-fixture")
    factors = [
        {"ts_code": code, "trade_date": day, "adj_factor": 1.0}
        for code in codes
        for day in days
    ]
    write_records(conn, "adj_factor", factors, source_code="tushare-fixture")
    calendar = [
        {"ts_code": "", "exchange": "SSE", "cal_date": day, "is_open": 1} for day in days
    ]
    calendar.append({"ts_code": "", "exchange": "SSE", "cal_date": "20250308", "is_open": 0})
    write_records(conn, "calendar", calendar, source_code="tushare-fixture")
    from app.data.chunks import publish_chunked_version

    return str(
        publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={},
            categories=["daily", "adj_factor", "calendar"],
            rule_version="test-rules",
            quality_summary={},
            window_mode="full",
        )["version_id"]
    )


@pytest.fixture
def migrated_conn(data_settings):
    initialize_database(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn, data_settings
    finally:
        conn.close()


def test_parse_money_rejects_float_and_decimal_string_parsing() -> None:
    assert parse_money_to_cents(100) == 100
    assert parse_money_to_cents("10.05") == 1005
    assert parse_money_to_cents(Decimal("12.345")) == 1235  # ROUND_HALF_UP
    assert parse_money_to_cents("-0.05") == -5
    with pytest.raises(DataError):
        parse_money_to_cents(10.0)


def test_money_round_trip_and_unknown() -> None:
    assert cents_to_decimal_string(1013) == "10.13"
    assert cents_to_decimal_string(-5) == "-0.05"
    assert cents_to_decimal_string(None) is None
    with pytest.raises(DataError):
        cents_to_decimal_string("10")


def test_parse_shares_rejects_bad_units() -> None:
    assert parse_shares(0) == 0
    assert parse_shares("100") == 100
    with pytest.raises(DataError):
        parse_shares(100.5)
    with pytest.raises(DataError):
        parse_shares(-3)
    with pytest.raises(DataError):
        parse_shares("3.14")


def test_default_bundles_are_synthetic_simulation_and_deterministic(migrated_conn) -> None:
    conn, settings = migrated_conn

    first = ensure_default_bundles(conn)
    second = ensure_default_bundles(conn)
    assert first == second

    fee = load_rule_bundle(conn, first["fee"])
    assert fee is not None and fee["assumption_scope"] == "synthetic_simulation"
    assert fee["content"]["bundle_kind"] == "fee"
    require_assumption_scope(fee, "synthetic_simulation")

    parameter = load_rule_bundle(conn, first["parameter"])
    assert parameter is not None
    reusable = parameter["content"]["execution_assumptions"]
    assert reusable["same_day_sale_proceeds_reusable"] is False
    assert reusable["verification_status"] == "unverified"


def test_fee_bundle_rates_and_notes_are_mathematically_consistent() -> None:
    """费率注记必须与 rate_bp 数学一致：1bp = 0.01% = 万分之1。"""
    components = {c["code"]: c for c in DEFAULT_FEE_BUNDLE["components"]}
    commission = components["commission"]
    assert commission["rate_bp"] == 5
    assert "万分之5" in commission["note"]
    assert "0.05%" in commission["note"]
    assert "万分之2.5" not in commission["note"]
    slippage = components["slippage"]
    assert slippage["rate_bp"] == 10
    assert "万分之10" in slippage["note"]


def test_load_rule_bundle_verifies_content_sha256(migrated_conn) -> None:
    conn, settings = migrated_conn
    bundle_id = _default_bundle_ids(conn)
    # 篡改存储内容 → 读取必须报告损坏，而不是返回未经验证的内容。
    with conn:
        conn.execute(
            "UPDATE rule_bundle SET content='{\"tampered\": true}' WHERE bundle_id=?",
            (bundle_id,),
        )
    with pytest.raises(DataError) as excinfo:
        load_rule_bundle(conn, bundle_id)
    assert excinfo.value.reason == "bundle_content_corrupt"


def test_create_rule_bundle_same_content_different_scope_conflicts(migrated_conn) -> None:
    conn, settings = migrated_conn
    content = {"model": "demo"}
    created = create_rule_bundle(
        conn,
        kind="parameter",
        version="scope-test-1",
        assumption_scope="synthetic_simulation",
        content=content,
    )
    reused = create_rule_bundle(
        conn,
        kind="parameter",
        version="scope-test-1",
        assumption_scope="synthetic_simulation",
        content=content,
    )
    assert reused["reused"] is True and reused["bundle_id"] == created["bundle_id"]
    with pytest.raises(DataError) as excinfo:
        create_rule_bundle(
            conn,
            kind="parameter",
            version="scope-test-1",
            assumption_scope="verified_configuration",
            content=content,
        )
    assert excinfo.value.reason == "bundle_scope_conflict"


def test_fee_bundle_cannot_be_marked_verified(migrated_conn) -> None:
    conn, settings = migrated_conn
    with pytest.raises(DataError) as excinfo:
        create_rule_bundle(
            conn,
            kind="fee",
            version="p3-fee-verified-test",
            assumption_scope="verified_configuration",
            content={"rate_bp": 25},
        )
    assert excinfo.value.reason == "bundle_scope_conflict"


def test_rule_bundle_version_conflict_refuses_overwrite(migrated_conn) -> None:
    conn, settings = migrated_conn
    create_rule_bundle(
        conn,
        kind="parameter",
        version="param-test-1",
        assumption_scope="synthetic_simulation",
        content={"a": 1},
    )
    with pytest.raises(DataError) as excinfo:
        create_rule_bundle(
            conn,
            kind="parameter",
            version="param-test-1",
            assumption_scope="synthetic_simulation",
            content={"a": 2},
        )
    assert excinfo.value.reason == "bundle_version_conflict"


def test_bind_context_uses_phase2_versions_and_explicit_restriction(migrated_conn) -> None:
    conn, settings = migrated_conn
    version_id = _published_version(conn)
    bundle_id = _default_bundle_ids(conn)

    bound = bind_data_context(
        conn,
        DataSelectionContext(
            source_code="tushare-fixture",
            as_of="20250304",
            purpose="valuation_reference",
            data_version_id=version_id,
            account_revision=1,
            rule_bundle_id=bundle_id,
        ),
    )
    assert bound.view is not None
    assert bound.context.data_version_id == version_id
    assert bound.eligibility is not None and bound.eligibility.eligible is True
    assert bound.restricted is False
    assert bound.latest_data_date == "20250304"
    assert bound.rule_bundle is not None and bound.rule_bundle["bundle_id"] == bundle_id


def test_bind_requires_complete_explicit_context(migrated_conn) -> None:
    conn, settings = migrated_conn
    version_id = _published_version(conn)

    # 阶段三契约：source + version + as_of + purpose + account revision + rule bundle
    # 显式齐全；缺失项在构造上下文时就被拒绝，不得退化为"取当前最新数据"。
    with pytest.raises(ValueError):
        DataSelectionContext(
            source_code="tushare-fixture",
            as_of="20250304",
            purpose="valuation_reference",
            data_version_id=None,  # type: ignore[arg-type]
            account_revision=1,
            rule_bundle_id="rb_x",
        )
    with pytest.raises(ValueError):
        DataSelectionContext(
            source_code="tushare-fixture",
            as_of="20250304",
            purpose="valuation_reference",
            data_version_id=version_id,
            account_revision=0,
            rule_bundle_id="rb_x",
        )
    with pytest.raises(ValueError):
        DataSelectionContext(
            source_code="tushare-fixture",
            as_of="20250304",
            purpose="valuation_reference",
            data_version_id=version_id,
            account_revision=1,
            rule_bundle_id="",
        )
    with pytest.raises(DataError) as excinfo:
        bind_data_context(
            conn,
            DataSelectionContext(
                source_code="tushare-fixture",
                as_of="20250304",
                purpose="valuation_reference",
                data_version_id=version_id,
                account_revision=1,
                rule_bundle_id="rb_missing_no_such_bundle",
            ),
        )
    assert excinfo.value.reason == "rule_bundle_not_found"


def test_bind_requires_universe_evidence_for_signal_input(migrated_conn) -> None:
    conn, settings = migrated_conn
    version_id = _published_version(conn)
    bundle_id = _default_bundle_ids(conn)

    bound = bind_data_context(
        conn,
        DataSelectionContext(
            source_code="tushare-fixture",
            as_of="20250304",
            purpose="signal_input",
            data_version_id=version_id,
            account_revision=1,
            rule_bundle_id=bundle_id,
        ),
    )
    assert bound.restricted is True
    assert bound.restriction_code == RESTRICTION_UNIVERSE_UNVERIFIED
    assert bound.eligibility is not None and bound.eligibility.eligible is True


def test_bind_rejects_source_version_mismatch_and_unknown_version(migrated_conn) -> None:
    conn, settings = migrated_conn
    version_id = _published_version(conn)
    bundle_id = _default_bundle_ids(conn)

    def _context(source_code: str, version: str) -> DataSelectionContext:
        return DataSelectionContext(
            source_code=source_code,
            as_of="20250304",
            purpose="display",
            data_version_id=version,
            account_revision=1,
            rule_bundle_id=bundle_id,
        )

    with pytest.raises(DataError) as excinfo:
        bind_data_context(conn, _context("tushare", version_id))
    assert excinfo.value.reason == "version_source_mismatch"

    with pytest.raises(DataError) as excinfo:
        bind_data_context(conn, _context("tushare-fixture", "dv_missing_not_here"))
    assert excinfo.value.reason == "version_not_found"


def test_bind_rejects_pool_snapshot_bound_to_other_version_or_as_of(migrated_conn) -> None:
    conn, settings = migrated_conn
    version_a = _published_version(conn, codes=("000001.SZ",))
    version_b = _published_version(conn, codes=("600000.SH", "000001.SZ"))
    bundle_id = _default_bundle_ids(conn)
    from app.data.universe import generate_pool_snapshot

    pool = generate_pool_snapshot(
        conn,
        as_of="20250304",
        data_version_id=version_b,
        source_code="tushare-fixture",
    )

    # 版本不一致 → 显式拒绝（不再作为受限绑定放行）。
    with pytest.raises(DataError) as excinfo:
        bind_data_context(
            conn,
            DataSelectionContext(
                source_code="tushare-fixture",
                as_of="20250304",
                purpose="signal_input",
                data_version_id=version_a,
                account_revision=1,
                rule_bundle_id=bundle_id,
                pool_snapshot_id=pool["snapshot_id"],
            ),
        )
    assert excinfo.value.reason == RESTRICTION_POOL_VERSION_MISMATCH

    # 时点不一致 → 显式拒绝。
    with pytest.raises(DataError) as excinfo:
        bind_data_context(
            conn,
            DataSelectionContext(
                source_code="tushare-fixture",
                as_of="20250305",
                purpose="signal_input",
                data_version_id=version_b,
                account_revision=1,
                rule_bundle_id=bundle_id,
                pool_snapshot_id=pool["snapshot_id"],
            ),
        )
    assert excinfo.value.reason == "pool_snapshot_as_of_mismatch"

    with pytest.raises(DataError) as excinfo:
        bind_data_context(
            conn,
            DataSelectionContext(
                source_code="tushare-fixture",
                as_of="20250304",
                purpose="display",
                data_version_id=version_a,
                account_revision=1,
                rule_bundle_id=bundle_id,
                pool_snapshot_id="pool_missing_id",
            ),
        )
    assert excinfo.value.reason == "pool_snapshot_not_found"


def test_signal_input_fails_closed_on_phase2_snapshot_with_unresolved_flags(
    migrated_conn,
) -> None:
    """阶段二快照总带幸存者/历史全集未决旗标 → 不得用于候选选择（fail closed）。"""
    conn, settings = migrated_conn
    version_id = _published_version(conn)
    bundle_id = _default_bundle_ids(conn)
    from app.data.universe import generate_pool_snapshot

    pool = generate_pool_snapshot(
        conn, as_of="20250304", data_version_id=version_id, source_code="tushare-fixture"
    )
    assert UNRESOLVED_SCOPE_FLAGS & set(pool["limit_flags"]), "阶段二快照应携带未决旗标"

    bound = bind_data_context(
        conn,
        DataSelectionContext(
            source_code="tushare-fixture",
            as_of="20250304",
            purpose="signal_input",
            data_version_id=version_id,
            account_revision=1,
            rule_bundle_id=bundle_id,
            pool_snapshot_id=pool["snapshot_id"],
        ),
    )
    assert bound.restricted is True
    assert bound.restriction_code == RESTRICTION_UNIVERSE_UNVERIFIED
    assert set(bound.risk_status["unresolved_scope_flags"]) == UNRESOLVED_SCOPE_FLAGS
    assert "survivorship_bias_unresolved" in bound.risk_status["limit_flags"]
    assert bound.restriction_detail

    # 同一上下文里的估值参考（不用于选股）不受该证据门槛约束。
    valuation_bound = bind_data_context(
        conn,
        DataSelectionContext(
            source_code="tushare-fixture",
            as_of="20250304",
            purpose="valuation_reference",
            data_version_id=version_id,
            account_revision=1,
            rule_bundle_id=bundle_id,
            pool_snapshot_id=pool["snapshot_id"],
        ),
    )
    assert valuation_bound.restricted is False
    # 证据缺失：只有显式登记的核验记录 + 无未决旗标的快照才能解除限制。
    assert bound.risk_status["scope_evidence_registered"] is False


_EVIDENCE_PAYLOAD = (
    '{"basis":"示例证据材料：占位文本，仅验证登记/复核边界","exchange":"SSE","as_of":"20250304"}'
)


def _register_evidence(conn: sqlite3.Connection, snapshot_id: str) -> dict:
    return register_pool_scope_evidence(
        conn,
        snapshot_id=snapshot_id,
        verification_source="示例：占位来源说明（S1 无真实工作流，仅登记凭据）",
        verified_by="示例：测试登记方",
        evidence_payload=_EVIDENCE_PAYLOAD,
    )


def _insert_clean_snapshot(conn: sqlite3.Connection, version_id: str, snapshot_id: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO pool_snapshot(snapshot_id, as_of, data_version_id, rule_version,"
            " params, limit_flags, member_count, manifest_sha256, generated_at)"
            " VALUES (?, '20250304', ?, 'pool-2026-10-22.1', '{}', '[]', 1,"
            " 'fixture-clean-manifest', '2025-06-02T00:00:00+00:00')",
            (snapshot_id, version_id),
        )
        conn.execute(
            "INSERT INTO pool_member(snapshot_id, ts_code, seq) VALUES (?, '000001.SZ', 0)",
            (snapshot_id,),
        )


def _signal_context(version_id: str, bundle_id: str, snapshot_id: str) -> DataSelectionContext:
    return DataSelectionContext(
        source_code="tushare-fixture",
        as_of="20250304",
        purpose="signal_input",
        data_version_id=version_id,
        account_revision=1,
        rule_bundle_id=bundle_id,
        pool_snapshot_id=snapshot_id,
    )


def test_pool_scope_evidence_requires_verifiable_provenance(migrated_conn) -> None:
    conn, settings = migrated_conn
    version_id = _published_version(conn)
    bundle_id = _default_bundle_ids(conn)
    from app.data.universe import generate_pool_snapshot

    pool = generate_pool_snapshot(
        conn, as_of="20250304", data_version_id=version_id, source_code="tushare-fixture"
    )
    with pytest.raises(DataError) as excinfo:
        register_pool_scope_evidence(
            conn, snapshot_id="pool_no_such", verification_source="s", verified_by="b",
            evidence_payload="{}",
        )
    assert excinfo.value.reason == "pool_snapshot_not_found"

    # 任意非空 notes 不再足以登记：来源说明、责任方、原样证据材料缺一不可。
    with pytest.raises(DataError) as excinfo:
        register_pool_scope_evidence(
            conn, snapshot_id=pool["snapshot_id"], verification_source="",
            verified_by="b", evidence_payload=_EVIDENCE_PAYLOAD,
        )
    assert excinfo.value.reason == "pool_scope_evidence_provenance_required"
    with pytest.raises(DataError) as excinfo:
        register_pool_scope_evidence(
            conn, snapshot_id=pool["snapshot_id"], verification_source="s",
            verified_by="", evidence_payload=_EVIDENCE_PAYLOAD,
        )
    assert excinfo.value.reason == "pool_scope_evidence_provenance_required"
    with pytest.raises(DataError) as excinfo:
        register_pool_scope_evidence(
            conn, snapshot_id=pool["snapshot_id"], verification_source="s",
            verified_by="b", evidence_payload="   ",
        )
    assert excinfo.value.reason == "pool_scope_evidence_provenance_required"

    # 显式传入的摘要与 payload 不一致 → 拒绝（可独立核对）。
    with pytest.raises(DataError) as excinfo:
        register_pool_scope_evidence(
            conn, snapshot_id=pool["snapshot_id"], verification_source="s",
            verified_by="b", evidence_payload=_EVIDENCE_PAYLOAD,
            evidence_sha256="0" * 64,
        )
    assert excinfo.value.reason == "pool_scope_evidence_digest_mismatch"

    registered = _register_evidence(conn, pool["snapshot_id"])
    assert registered["verification_status"] == "pending_external_review"
    evidence = load_pool_scope_evidence(conn, pool["snapshot_id"])
    assert evidence is not None
    assert evidence["verification_status"] == "pending_external_review"
    assert evidence["evidence_sha256"] == registered["evidence_sha256"]

    # 证据材料被篡改 → 读取必须显式失败而不是静默采信。
    with conn:
        conn.execute(
            "UPDATE pool_scope_evidence SET evidence_payload='{\"tampered\": 1}'"
            " WHERE snapshot_id=?",
            (pool["snapshot_id"],),
        )
    with pytest.raises(DataError) as excinfo:
        load_pool_scope_evidence(conn, pool["snapshot_id"])
    assert excinfo.value.reason == "evidence_corrupt"


def test_signal_input_stays_restricted_without_confirmed_workflow(migrated_conn) -> None:
    """S1 边界：登记凭据（pending_external_review）不解除任何选股限制。"""
    conn, settings = migrated_conn
    version_id = _published_version(conn)
    bundle_id = _default_bundle_ids(conn)

    # 1) 阶段二快照带未决旗标 + 完整登记：仍受限。
    from app.data.universe import generate_pool_snapshot

    pool = generate_pool_snapshot(
        conn, as_of="20250304", data_version_id=version_id, source_code="tushare-fixture"
    )
    _register_evidence(conn, pool["snapshot_id"])
    bound = bind_data_context(conn, _signal_context(version_id, bundle_id, pool["snapshot_id"]))
    assert bound.restricted is True
    assert bound.restriction_code == RESTRICTION_UNIVERSE_UNVERIFIED
    assert bound.risk_status["scope_evidence_status"] == "pending_external_review"

    # 2) 干净快照 + 已登记（pending）：仍受限——不存在可解锁的工作流调用。
    clean_snapshot_id = "pool_cleanverify000001"
    _insert_clean_snapshot(conn, version_id, clean_snapshot_id)
    _register_evidence(conn, clean_snapshot_id)
    still = bind_data_context(conn, _signal_context(version_id, bundle_id, clean_snapshot_id))
    assert still.restricted is True
    assert still.restriction_code == RESTRICTION_UNIVERSE_UNVERIFIED

    # 3) 只有真正的核验工作流（S1 无此调用方；测试直接以该未来边界写入）把证据
    #    置为 confirmed_by_workflow 并登记确认来源，才解除限制。
    # 3) Only a genuine verification workflow (which S1 has no caller for; the test
    #    writes directly at that future boundary) may mark the evidence
    #    `confirmed_by_workflow` with an explicit confirmation source, and only that
    #    lifts the restriction.
    with conn:
        conn.execute(
            "UPDATE pool_scope_evidence SET verification_status='confirmed_by_workflow',"
            " confirmed_at='2026-10-23T00:00:00+00:00',"
            " confirmation_source='verification-workflow:v1' WHERE snapshot_id=?",
            (clean_snapshot_id,),
        )
    unblocked = bind_data_context(
        conn, _signal_context(version_id, bundle_id, clean_snapshot_id)
    )
    assert unblocked.restricted is False

    # 4) schema CHECK：confirmed 状态必须同时有确认来源与时间。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE pool_scope_evidence SET confirmation_source=NULL WHERE snapshot_id=?",
                (clean_snapshot_id,),
            )


def test_analysis_input_digest_deterministic_and_null_pool() -> None:
    base = {
        "source_code": "tushare-fixture",
        "as_of": "20250304",
        "data_version_id": "dv_abc",
        "pool_snapshot_id": None,
        "mode": "incremental",
    }
    assert analysis_input_sha256(base) == analysis_input_sha256(dict(base))
    shuffled = {"mode": "incremental", "pool_snapshot_id": None, "as_of": "20250304",
                "source_code": "tushare-fixture", "data_version_id": "dv_abc"}
    assert analysis_input_sha256(base) == analysis_input_sha256(shuffled)
    assert analysis_input_sha256({**base, "pool_snapshot_id": "pool_x"}) != analysis_input_sha256(base)


def test_reference_holds_are_atomic_with_result_and_invalidate_previews(migrated_conn) -> None:
    conn, settings = migrated_conn
    version_id = _published_version(conn)
    import datetime as _dt

    preview = preview_cleanup(
        settings,
        source_code="tushare-fixture",
        as_of="20250304",
        now=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
    )

    # 结果保存 + 引用保护在同一事务：一起成功。
    # 插入前先建立一致的账户/快照对（分析只允许绑定本账户快照）。
    analysis_id = "an_" + analysis_input_sha256({"id": "atomic-run"})[:16]
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_x', 'actual', 'x', '2025-06-01T00:00:00+00:00',"
            " '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account_snapshot(snapshot_id, account_id, as_of, account_revision,"
            " cash_cents, created_at) VALUES ('as_y', 'acc_x', '20250304', 1, 100,"
            " '2025-06-04T00:00:00+00:00')"
        )
    with conn:
        conn.execute(
            "INSERT INTO analysis_run(run_id, mode, account_id, account_snapshot_id,"
            " account_revision, source_code, data_version_id, as_of, rule_bundle_id,"
            " input_sha256, status, restriction_code, restriction_detail, generated_at)"
            " VALUES (?, 'incremental', 'acc_x', 'as_y', 1, 'tushare-fixture', ?, '20250304',"
            " ?, ?, 'restricted', 'universe_unverified', '受限演示', ?)",
            (
                analysis_id,
                version_id,
                "rb_test",
                analysis_input_sha256({"id": "atomic-run"}),
                utc_now_iso(),
            ),
        )
        holds = register_reference_holds(
            conn,
            [ReferenceTarget("version", version_id, "analysis_run")],
            reason_prefix=f"analysis_run:{analysis_id}",
        )
    assert holds and holds[0]["created"] is True

    # 引用保护推进了 generation：升级前生成的清理预览立即失效。
    with pytest.raises(DataError) as excinfo:
        apply_cleanup(settings, preview_id=preview["preview_id"])
    assert excinfo.value.reason == "preview_stale"

    # 回滚时结果与保护一起消失（一致事务边界）。
    with pytest.raises(RuntimeError):
        with conn:
            conn.execute(
                "INSERT INTO analysis_run(run_id, mode, account_id, account_snapshot_id,"
                " account_revision, source_code, data_version_id, as_of, rule_bundle_id,"
                " input_sha256, status, restriction_code, restriction_detail, generated_at)"
                " VALUES ('an_rollback', 'incremental', 'acc_x', 'as_y', 1,"
                " 'tushare-fixture', ?, '20250304', 'rb_test', 'sha_rollback', 'restricted',"
                " 'universe_unverified', '说明', ?)",
                (version_id, utc_now_iso()),
            )
            register_reference_holds(
                conn,
                [ReferenceTarget("version", version_id, "analysis_run")],
                reason_prefix="analysis_run:an_rollback",
            )
            raise RuntimeError("force rollback")

    row = conn.execute(
        "SELECT COUNT(*) FROM analysis_run WHERE run_id='an_rollback'"
    ).fetchone()
    assert row[0] == 0, "回滚必须连结果与引用保护一起撤销"
    row = conn.execute(
        "SELECT COUNT(*) FROM retention_hold WHERE reason LIKE 'analysis_run:an_rollback%'"
    ).fetchone()
    assert row[0] == 0

    release_reference_holds(conn, [holds[0]["hold_id"]])
    assert conn.execute(
        "SELECT COUNT(*) FROM retention_hold WHERE hold_id=?", (holds[0]["hold_id"],)
    ).fetchone()[0] == 0


def test_reference_hold_rejects_missing_target_and_unknown_type(migrated_conn) -> None:
    conn, settings = migrated_conn
    with pytest.raises(DataError) as excinfo:
        register_reference_holds(
            conn,
            [ReferenceTarget("version", "dv_missing", "test")],
            reason_prefix="test",
        )
    assert excinfo.value.reason == "hold_target_not_found"
    with pytest.raises(DataError) as excinfo:
        register_reference_holds(
            conn,
            [ReferenceTarget("raw_archive", "junk", "test")],
            reason_prefix="test",
        )
    assert excinfo.value.reason == "unsupported_hold_type"


def test_idempotency_keys_and_null_pool_snapshot_uniqueness(migrated_conn) -> None:
    conn, settings = migrated_conn
    version_id = _published_version(conn)
    digest = analysis_input_sha256(
        {"source_code": "tushare-fixture", "as_of": "20250304", "data_version_id": version_id,
         "pool_snapshot_id": None}
    )
    with conn:
        # 同域触发器要求 analysis 绑定本账户快照：先建立一致对。
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_x', 'actual', 'x', '2025-06-01T00:00:00+00:00',"
            " '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account_snapshot(snapshot_id, account_id, as_of, account_revision,"
            " cash_cents, created_at) VALUES ('as_y', 'acc_x', '20250304', 1, 100,"
            " '2025-06-04T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO analysis_run(run_id, mode, account_id, account_snapshot_id,"
            " account_revision, source_code, data_version_id, as_of, rule_bundle_id,"
            " input_sha256, status, restriction_code, restriction_detail, generated_at)"
            " VALUES ('an_idem', 'independent', 'acc_x', 'as_y', 1, 'tushare-fixture',"
            " ?, '20250304', 'rb_x', ?, 'restricted', 'universe_unverified', '说明', ?)",
            (version_id, digest, utc_now_iso()),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO analysis_run(run_id, mode, account_id, account_snapshot_id,"
                " account_revision, source_code, data_version_id, as_of, rule_bundle_id,"
                " input_sha256, status, restriction_code, restriction_detail, generated_at)"
                " VALUES ('an_idem2', 'independent', 'acc_x', 'as_y', 1, 'tushare-fixture',"
                " ?, '20250304', 'rb_x', ?, 'restricted', 'universe_unverified', '说明', ?)",
                (version_id, digest, utc_now_iso()),
            )


def test_account_event_idempotency_and_uniqueness(migrated_conn) -> None:
    conn, settings = migrated_conn
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_1', 'actual', '测试账户', '2025-06-01T00:00:00+00:00',"
            " '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account_event(event_id, account_id, event_type, business_date,"
            " recorded_at, idempotency_key, payload, content_sha256, created_at)"
            " VALUES ('evt_1', 'acc_1', 'opening', '20250601',"
            " '2025-06-01T00:00:00+00:00', 'caller-key-1', '{}', 'sha1',"
            " '2025-06-01T00:00:00+00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO account_event(event_id, account_id, event_type, business_date,"
                " recorded_at, idempotency_key, payload, content_sha256, created_at)"
                " VALUES ('evt_2', 'acc_1', 'opening', '20250601',"
                " '2025-06-01T00:00:00+00:00', 'caller-key-1', '{}', 'sha1',"
                " '2025-06-01T00:00:00+00:00')"
            )


def test_simulation_account_domain_checks(migrated_conn) -> None:
    conn, settings = migrated_conn
    # 模拟账户必须声明来源实际账户，且不得自引用（防混账基础约束）。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO account(account_id, kind, display_name, simulation_of_account_id,"
                " created_at, updated_at) VALUES ('acc_sim', 'actual', 'sim', 'acc_sim',"
                " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
            )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
                " VALUES ('acc_sim2', 'simulation', 'sim2',"
                " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
            )
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_src', 'actual', '实际账户',"
            " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, simulation_of_account_id,"
            " created_at, updated_at) VALUES ('acc_sim3', 'simulation', '模拟', 'acc_src',"
            " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )


def test_corporate_action_settlement_requires_payment_dates(migrated_conn) -> None:
    conn, settings = migrated_conn
    good = (
        "INSERT INTO corporate_action_application(application_id, account_id, ts_code,"
        " action_kind, record_date, ex_date, pay_date, entitlement_shares, status,"
        " idempotency_key, created_at)"
        " VALUES ('ca_cash', 'acc_1', '000001.SZ', 'cash_dividend', '20250605',"
        " '20250610', '20250613', 100, 'settled', 'ca-cash-key', '2025-06-20T00:00:00+00:00')"
    )
    with conn:
        conn.execute(good)
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            # 现金分红没有 pay_date 不允许置为 settled（按 record_date 定权益、
            # ex_date 定应收、pay_date 到账的边界在 schema 层可核对）。
            conn.execute(
                "INSERT INTO corporate_action_application(application_id, account_id, ts_code,"
                " action_kind, record_date, ex_date, entitlement_shares, status,"
                " idempotency_key, created_at)"
                " VALUES ('ca_bad', 'acc_1', '000001.SZ', 'cash_dividend', '20250605',"
                " '20250610', 100, 'settled', 'ca-bad-key', '2025-06-20T00:00:00+00:00')"
            )


def test_valuation_nav_requires_complete_components(migrated_conn) -> None:
    conn, settings = migrated_conn
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_1', 'actual', '测试账户',"
            " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
    # completeness=complete 却缺组件 → 拒绝；partial_unknown 允许 nav 为 NULL（未知不当零）。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO valuation_snapshot(valuation_id, owner_type, owner_id, as_of,"
                " cash_cents, completeness, created_at)"
                " VALUES ('val_x', 'account', 'acc_1', '20250610', 1000, 'complete',"
                " '2025-06-10T00:00:00+00:00')"
            )
    with conn:
        conn.execute(
            "INSERT INTO valuation_snapshot(valuation_id, owner_type, owner_id, as_of,"
            " cash_cents, completeness, created_at)"
            " VALUES ('val_x', 'account', 'acc_1', '20250610', 1000, 'partial_unknown',"
            " '2025-06-10T00:00:00+00:00')"
        )
    # 多态 owner 触发器（不依赖外键 pragma）：owner 记录不存在 → 一律拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO valuation_snapshot(valuation_id, owner_type, owner_id, as_of,"
                " cash_cents, completeness, created_at)"
                " VALUES ('val_ghost', 'account', 'acc_missing', '20250610', 1000,"
                " 'partial_unknown', '2025-06-10T00:00:00+00:00')"
            )


def test_account_positions_store_authoritative_total_cost(migrated_conn) -> None:
    conn, settings = migrated_conn
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_1', 'actual', '测试账户',"
            " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account_snapshot(snapshot_id, account_id, as_of, account_revision,"
            " cash_cents, created_at)"
            " VALUES ('as_1', 'acc_1', '20250610', 1, 100000, '2025-06-10T00:00:00+00:00')"
        )
        # 库里只存权威 total_cost_cents；每股成本由 Decimal 派生（可能非整数分）。
        conn.execute(
            "INSERT INTO account_position(snapshot_id, ts_code, shares, total_cost_cents)"
            " VALUES ('as_1', '000001.SZ', 3, 100000)"
        )
    per_share = derive_per_share_cost(100000, 3)
    assert per_share == Decimal(100000) / Decimal(3)
    assert derive_per_share_cost(None, 3) is None
    assert derive_per_share_cost(100000, 0) is None
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO account_position(snapshot_id, ts_code, shares)"
                " VALUES ('as_1', '600000.SH', -1)"
            )


def test_derive_per_share_cost_boundary() -> None:
    assert derive_per_share_cost(1000, 100) == Decimal("10")
    assert derive_per_share_cost(1000, 3) == Decimal(1000) / Decimal(3)
    assert derive_per_share_cost(None, 5) is None
    assert derive_per_share_cost(1000, 0) is None
    with pytest.raises(DataError):
        derive_per_share_cost("1000", 5)  # type: ignore[arg-type]
    with pytest.raises(DataError):
        derive_per_share_cost(1000, "5")  # type: ignore[arg-type]


def test_simulation_order_supports_unfilled_status(migrated_conn) -> None:
    conn, settings = migrated_conn
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_src', 'actual', '实际', '2025-06-01T00:00:00+00:00',"
            " '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, simulation_of_account_id,"
            " created_at, updated_at) VALUES ('acc_sim', 'simulation', '模拟', 'acc_src',"
            " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account_snapshot(snapshot_id, account_id, as_of, account_revision,"
            " cash_cents, created_at) VALUES ('as_seg', 'acc_src', '20250605', 1, 1000000,"
            " '2025-06-05T00:00:00+00:00')"
        )
        bundles = ensure_default_bundles(conn)
        conn.execute(
            "INSERT INTO simulation_segment(segment_id, source_account_id,"
            " simulation_account_id, base_account_snapshot_id, rule_bundle_id,"
            " fee_bundle_id, source_code, data_version_id, as_of, input_sha256,"
            " created_at) VALUES ('seg_1', 'acc_src', 'acc_sim', 'as_seg', ?, ?,"
            " 'tushare-fixture', 'dv_x', '20250605', 'sha_seg',"
            " '2025-06-05T00:00:00+00:00')",
            (bundles["parameter"], bundles["fee"]),
        )
        conn.execute(
            "INSERT INTO simulation_order(order_id, segment_id, trade_date, ts_code,"
            " direction, planned_shares, filled_shares, remaining_shares, status,"
            " idempotency_key, created_at) VALUES ('ord_unf', 'seg_1', '20250605',"
            " '000001.SZ', 'buy', 100, 0, 100, 'unfilled', 'key-unfilled',"
            " '2025-06-05T00:00:00+00:00')"
        )
    # 未知状态仍被拒绝；remaining = planned - filled 的域约束继续有效。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO simulation_order(order_id, segment_id, trade_date, ts_code,"
                " direction, planned_shares, filled_shares, remaining_shares, status,"
                " idempotency_key, created_at) VALUES ('ord_bad', 'seg_1', '20250605',"
                " '000001.SZ', 'buy', 100, 0, 100, 'mystery', 'key-bad',"
                " '2025-06-05T00:00:00+00:00')"
            )


# ---------- 外键 / 阶段三连接边界（PRAGMA foreign_keys=ON） ----------


def _actual_simulation_fixture(conn: sqlite3.Connection, bundle_ids: dict[str, str]) -> None:
    conn.execute(
        "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
        " VALUES ('acc_src', 'actual', '实际', '2025-06-01T00:00:00+00:00',"
        " '2025-06-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO account(account_id, kind, display_name, simulation_of_account_id,"
        " created_at, updated_at) VALUES ('acc_sim', 'simulation', '模拟', 'acc_src',"
        " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO account_snapshot(snapshot_id, account_id, as_of, account_revision,"
        " cash_cents, created_at) VALUES ('as_seg', 'acc_src', '20250304', 1, 100000,"
        " '2025-06-04T00:00:00+00:00')"
    )


def test_foreign_keys_enforced_at_phase3_boundary(portfolio_conn) -> None:
    """经阶段三连接边界写入时，孤儿引用必须被拒绝（SQLite 外键按连接生效）。"""
    conn, settings = portfolio_conn
    version_id = _published_version(conn)

    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO analysis_run(run_id, mode, account_id, account_snapshot_id,"
                " account_revision, source_code, data_version_id, as_of, rule_bundle_id,"
                " input_sha256, status, generated_at)"
                " VALUES ('an_orphan', 'incremental', 'acc_ghost', 'as_ghost', 1,"
                " 'tushare-fixture', ?, '20250304', 'rb_ghost', 'sha_orphan', 'complete', ?)",
                (version_id, utc_now_iso()),
            )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO account_event(event_id, account_id, event_type, business_date,"
                " recorded_at, idempotency_key, payload, content_sha256, created_at)"
                " VALUES ('evt_ghost', 'acc_ghost', 'opening', '20250601',"
                " '2025-06-01T00:00:00+00:00', 'k', '{}', 'sha',"
                " '2025-06-01T00:00:00+00:00')"
            )
    # 有效父记录链 → 写入成功（证明拒绝只来自完备性，而非路径本身）。
    bundle_id = _default_bundle_ids(conn)
    from app.data.universe import generate_pool_snapshot

    pool = generate_pool_snapshot(
        conn, as_of="20250304", data_version_id=version_id, source_code="tushare-fixture"
    )
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_ok', 'actual', '正式账户',"
            " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account_snapshot(snapshot_id, account_id, as_of, account_revision,"
            " cash_cents, data_version_id, source_code, created_at)"
            " VALUES ('as_ok', 'acc_ok', '20250304', 1, 500000, ?, 'tushare-fixture',"
            " '2025-06-04T00:00:00+00:00')",
            (version_id,),
        )
        conn.execute(
            "INSERT INTO analysis_run(run_id, mode, account_id, account_snapshot_id,"
            " account_revision, source_code, data_version_id, pool_snapshot_id, as_of,"
            " rule_bundle_id, input_sha256, status, restriction_code, restriction_detail,"
            " generated_at) VALUES ('an_ok', 'incremental', 'acc_ok', 'as_ok', 1,"
            " 'tushare-fixture', ?, ?, '20250304', ?, 'sha_okcontainer', 'restricted',"
            " 'universe_unverified', '演示受限', ?)",
            (version_id, pool["snapshot_id"], bundle_id, utc_now_iso()),
        )
        conn.execute(
            "INSERT INTO analysis_item(run_id, seq, ts_code, decision, reason)"
            " VALUES ('an_ok', 1, '000001.SZ', NULL, '受限：未核验证据不得选股')"
        )
        # 孤儿 data_version 引用被拒绝（被归档/不存在的版本也不得挂到快照上）。
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO account_snapshot(snapshot_id, account_id, as_of,"
                " account_revision, cash_cents, data_version_id, created_at)"
                " VALUES ('as_badversion', 'acc_ok', '20250305', 2, 500000,"
                " 'dv_ghost', '2025-06-05T00:00:00+00:00')"
            )


def test_boundary_transaction_rollback_respects_foreign_keys(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    version_id = _published_version(conn)
    bundle_id = _default_bundle_ids(conn)
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_ok2', 'actual', '正式账户',"
            " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account_snapshot(snapshot_id, account_id, as_of, account_revision,"
            " cash_cents, created_at) VALUES ('as_ok2', 'acc_ok2', '20250304', 1, 100,"
            " '2025-06-04T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO analysis_run(run_id, mode, account_id, account_snapshot_id,"
            " account_revision, source_code, data_version_id, as_of, rule_bundle_id,"
            " input_sha256, status, restriction_code, restriction_detail, generated_at)"
            " VALUES ('an_ok2', 'incremental', 'acc_ok2', 'as_ok2', 1, 'tushare-fixture',"
            " ?, '20250304', ?, 'sha_roll', 'restricted', 'universe_unverified', 'demo', ?)",
            (version_id, bundle_id, utc_now_iso()),
        )
    # 外键即时校验：同一事务里先删父记录再写子记录 → 拒绝且不落库。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("DELETE FROM account_snapshot WHERE snapshot_id='as_ok2'")
            conn.execute(
                "INSERT INTO portfolio_target(target_id, run_id, kind, cash_cents,"
                " created_at) VALUES ('tgt_roll', 'an_ok2', 'ideal', 0,"
                " '2025-06-04T00:00:00+00:00')"
            )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO portfolio_target(target_id, run_id, kind, cash_cents,"
                " created_at) VALUES ('tgt_orphan', 'an_missing_run', 'ideal', 0,"
                " '2025-06-04T00:00:00+00:00')"
            )


def test_account_simulation_linkage_database_level(portfolio_conn) -> None:
    """模拟账户必须指向存在的实际账户；孤儿、自引用、模拟→模拟都被拒绝。"""
    conn, settings = portfolio_conn
    with pytest.raises(sqlite3.IntegrityError):  # 孤儿来源账户（外键）
        with conn:
            conn.execute(
                "INSERT INTO account(account_id, kind, display_name,"
                " simulation_of_account_id, created_at, updated_at)"
                " VALUES ('acc_sim_ghost', 'simulation', 'x', 'acc_no_such',"
                " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
            )
    with pytest.raises(sqlite3.IntegrityError):  # 自引用
        with conn:
            conn.execute(
                "INSERT INTO account(account_id, kind, display_name,"
                " simulation_of_account_id, created_at, updated_at)"
                " VALUES ('acc_self', 'simulation', 'x', 'acc_self',"
                " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
            )
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, simulation_of_account_id,"
            " created_at, updated_at) VALUES ('acc_actual', 'actual', '实际账户', NULL,"
            " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name,"
            " simulation_of_account_id, created_at, updated_at)"
            " VALUES ('acc_sim_a', 'simulation', 'sim A', 'acc_actual',"
            " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
    # 模拟 → 模拟引用被跨域触发器拒绝（不依赖外键 pragma；外键允许、触发器拦截）。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO account(account_id, kind, display_name,"
                " simulation_of_account_id, created_at, updated_at)"
                " VALUES ('acc_sim_b', 'simulation', 'sim B', 'acc_sim_a',"
                " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
            )
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name,"
            " simulation_of_account_id, created_at, updated_at)"
            " VALUES ('acc_sim_c', 'simulation', 'sim C', 'acc_actual',"
            " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
    # 已有模拟分身的实际账户不得改为 simulation（触发器拒绝）。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE account SET kind='simulation' WHERE account_id='acc_actual'"
            )


def test_simulation_segment_cross_domain_writes_rejected(portfolio_conn) -> None:
    """模拟段来源必须是实际账户、目标必须是模拟账户（触发器，任何连接都生效）。"""
    conn, settings = portfolio_conn
    version_id = _published_version(conn)
    bundle_id = _default_bundle_ids(conn)
    with conn:
        _actual_simulation_fixture(conn, bundle_id)

    def _segment_sql(source: str, simulated: str) -> str:
        return (
            "INSERT INTO simulation_segment(segment_id, source_account_id,"
            " simulation_account_id, base_account_snapshot_id, rule_bundle_id,"
            " fee_bundle_id, source_code, data_version_id, as_of, input_sha256,"
            " created_at) VALUES (?, ?, ?, 'as_seg', ?, ?, 'tushare-fixture',"
            f" '{version_id}', '20250304', ?, '2025-06-04T00:00:00+00:00')"
        )

    with pytest.raises(sqlite3.IntegrityError):  # 实际→实际（跨域错误）
        with conn:
            conn.execute(
                _segment_sql("acc_sim", "acc_sim"),
                ("seg_rev", "acc_sim", "acc_sim", bundle_id, bundle_id, "sha_rev"),
            )
    with pytest.raises(sqlite3.IntegrityError):  # 目标不是模拟账户（跨域错误）
        with conn:
            conn.execute(
                _segment_sql("acc_src", "acc_src"),
                ("seg_crossdomain", "acc_src", "acc_src", bundle_id, bundle_id, "sha_crossdomain"),
            )
    # 正确组合 → 成功，且既有订单/删除行为继续受外键约束。
    with conn:
        conn.execute(
            _segment_sql("acc_src", "acc_sim"),
            ("seg_ok", "acc_src", "acc_sim", bundle_id, bundle_id, "sha_ok"),
        )
        conn.execute(
            "INSERT INTO simulation_order(order_id, segment_id, trade_date, ts_code,"
            " direction, planned_shares, filled_shares, remaining_shares, status,"
            " idempotency_key, created_at) VALUES ('ord_ok', 'seg_ok', '20250304',"
            " '000001.SZ', 'buy', 100, 0, 100, 'unfilled', 'key_ok',"
            " '2025-06-04T00:00:00+00:00')"
        )
        # 模拟段已有订单时删除段（外键 RESTRICT）必须失败。
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM simulation_segment WHERE segment_id='seg_ok'")
        # 孤儿订单引用（不存在的父段）拒绝。
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO simulation_order(order_id, segment_id, trade_date, ts_code,"
                " direction, planned_shares, filled_shares, remaining_shares, status,"
                " idempotency_key, created_at) VALUES ('ord_ghost', 'seg_ghost',"
                " '20250304', '000001.SZ', 'buy', 100, 0, 100, 'unfilled', 'k_g',"
                " '2025-06-04T00:00:00+00:00')"
            )


def test_default_bundle_versions_registered(migrated_conn) -> None:
    conn, settings = migrated_conn
    bundles = ensure_default_bundles(conn)
    parameter = load_rule_bundle(conn, bundles["parameter"])
    assert parameter is not None
    assert parameter["version"] == DEFAULT_BUNDLE_VERSIONS["parameter"]
    assert MONEY_UNIT_VERSION == "money-unit-1.0"


# ---------- 同域一致性触发器（外键之外的同 owner/同账户/同时点关系） ----------


def _two_accounts_with_snapshots(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
        " VALUES ('acc_a', 'actual', 'A', '2025-06-01T00:00:00+00:00',"
        " '2025-06-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
        " VALUES ('acc_b', 'actual', 'B', '2025-06-01T00:00:00+00:00',"
        " '2025-06-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO account_snapshot(snapshot_id, account_id, as_of, account_revision,"
        " cash_cents, created_at) VALUES ('as_a', 'acc_a', '20250304', 1, 100,"
        " '2025-06-04T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO account_snapshot(snapshot_id, account_id, as_of, account_revision,"
        " cash_cents, created_at) VALUES ('as_b', 'acc_b', '20250304', 1, 200,"
        " '2025-06-04T00:00:00+00:00')"
    )


def _analysis_run_sql() -> str:
    return (
        "INSERT INTO analysis_run(run_id, mode, account_id, account_snapshot_id,"
        " account_revision, source_code, data_version_id, as_of, rule_bundle_id,"
        " input_sha256, status, generated_at)"
        " VALUES (?, 'incremental', ?, ?, 1, 'tushare-fixture', 'dv_x', '20250304',"
        " 'rb_x', ?, 'complete', '2025-06-05T00:00:00+00:00')"
    )


def test_analysis_run_snapshot_owner_same_domain(migrated_conn) -> None:
    """分析必须绑定自己账户的快照；跨账户绑定被触发器拒绝（与外键无关的通路）。"""
    conn, settings = migrated_conn
    bundle_id = _default_bundle_ids(conn)
    with conn:
        _two_accounts_with_snapshots(conn)
        conn.execute(
            _analysis_run_sql(), ("an_a", "acc_a", "as_a", "sha_a"),
        )
    # 跨账户组合：acc_b + as_a（快照属于 acc_a）→ 拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(_analysis_run_sql(), ("an_bad", "acc_b", "as_a", "sha_b"))
    # 更新路径同样被拦：把 an_a 改成别人的快照 → 拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE analysis_run SET account_snapshot_id='as_b' WHERE run_id='an_a'"
            )
    # 同账户组合（acc_b + as_b）→ 通过。
    with conn:
        conn.execute(_analysis_run_sql(), ("an_c", "acc_b", "as_b", "sha_c"))


def test_simulation_segment_same_account_invariants(migrated_conn) -> None:
    """模拟段的基础快照必须属于来源账户；模拟账户必须就是来源账户的模拟分身。"""
    conn, settings = migrated_conn
    bundle_id = _default_bundle_ids(conn)
    with conn:
        _actual_simulation_fixture(conn, bundle_id)
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_other', 'actual', '其他实际', '2025-06-01T00:00:00+00:00',"
            " '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account_snapshot(snapshot_id, account_id, as_of, account_revision,"
            " cash_cents, created_at) VALUES ('as_other', 'acc_other', '20250304', 1, 300,"
            " '2025-06-04T00:00:00+00:00')"
        )

    def _segment(segment_id: str, source: str, simulated: str, base: str, sha: str):
        conn.execute(
            "INSERT INTO simulation_segment(segment_id, source_account_id,"
            " simulation_account_id, base_account_snapshot_id, rule_bundle_id,"
            " fee_bundle_id, source_code, data_version_id, as_of, input_sha256,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, 'tushare-fixture', 'dv_x',"
            " '20250304', ?, '2025-06-04T00:00:00+00:00')",
            (segment_id, source, simulated, base, bundle_id, bundle_id, sha),
        )

    # 基础快照属于其他实际账户 → 拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            _segment("seg_baseother", "acc_src", "acc_sim", "as_other", "sha_a")
    # 模拟账户是别人（acc_sim2 指向 acc_other）的分身 → 拒绝。
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, simulation_of_account_id,"
            " created_at, updated_at) VALUES ('acc_sim2', 'simulation', '其他模拟',"
            " 'acc_other', '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            _segment("seg_mixedsim", "acc_src", "acc_sim2", "as_seg", "sha_b")
    # 更新路径：先把合法段的模拟账户换成别人的模拟分身 → 拒绝。
    with conn:
        _segment("seg_valid", "acc_src", "acc_sim", "as_seg", "sha_c")
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE simulation_segment SET simulation_account_id='acc_sim2'"
                " WHERE segment_id='seg_valid'"
            )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE simulation_segment SET base_account_snapshot_id='as_other'"
                " WHERE segment_id='seg_valid'"
            )


def test_account_event_supersedes_same_account(migrated_conn) -> None:
    """被更正事件必须属于同一账户；跨账户 supersede 被触发器拒绝。"""
    conn, settings = migrated_conn
    with conn:
        _two_accounts_with_snapshots(conn)
        conn.execute(
            "INSERT INTO account_event(event_id, account_id, event_type, business_date,"
            " recorded_at, idempotency_key, payload, content_sha256, created_at)"
            " VALUES ('evt_a', 'acc_a', 'opening', '20250601',"
            " '2025-06-01T00:00:00+00:00', 'ka', '{}', 'sha',"
            " '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account_event(event_id, account_id, event_type, business_date,"
            " recorded_at, idempotency_key, payload, content_sha256, created_at)"
            " VALUES ('evt_b', 'acc_b', 'opening', '20250601',"
            " '2025-06-01T00:00:00+00:00', 'kb', '{}', 'sha',"
            " '2025-06-01T00:00:00+00:00')"
        )
    # 插入路径：acc_b 的事件更正 acc_a 的事件 → 拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO account_event(event_id, account_id, event_type, business_date,"
                " recorded_at, idempotency_key, payload, content_sha256, created_at,"
                " supersedes_event_id) VALUES ('evt_bad', 'acc_b', 'correction',"
                " '20250602', '2025-06-02T00:00:00+00:00', 'kbad', '{}', 'sha',"
                " '2025-06-02T00:00:00+00:00', 'evt_a')"
            )
    # 同账户更正 → 通过；随后更新成跨账户 → 拒绝。
    with conn:
        conn.execute(
            "INSERT INTO account_event(event_id, account_id, event_type, business_date,"
            " recorded_at, idempotency_key, payload, content_sha256, created_at,"
            " supersedes_event_id) VALUES ('evt_ok', 'acc_a', 'correction',"
            " '20250602', '2025-06-02T00:00:00+00:00', 'kok', '{}', 'sha',"
            " '2025-06-02T00:00:00+00:00', 'evt_a')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE account_event SET supersedes_event_id='evt_b' WHERE event_id='evt_ok'"
            )
    # 更新把事件挪去别的账户也不得带着跨账户更正关系。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE account_event SET account_id='acc_b' WHERE event_id='evt_ok'"
            )


def test_performance_point_valuation_same_owner_and_as_of(migrated_conn) -> None:
    """业绩点必须引用同一 owner、同一 as_of 的估值快照。"""
    conn, settings = migrated_conn
    bundle_id = _default_bundle_ids(conn)
    with conn:
        _actual_simulation_fixture(conn, bundle_id)
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_other', 'actual', '其他实际', '2025-06-01T00:00:00+00:00',"
            " '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO simulation_segment(segment_id, source_account_id,"
            " simulation_account_id, base_account_snapshot_id, rule_bundle_id,"
            " fee_bundle_id, source_code, data_version_id, as_of, input_sha256,"
            " created_at) VALUES ('seg_perf', 'acc_src', 'acc_sim', 'as_seg', ?, ?,"
            " 'tushare-fixture', 'dv_x', '20250304', 'sha_perfseg',"
            " '2025-06-04T00:00:00+00:00')",
            (bundle_id, bundle_id),
        )
        conn.execute(
            "INSERT INTO valuation_snapshot(valuation_id, owner_type, owner_id, as_of,"
            " completeness, created_at) VALUES ('val_src', 'account', 'acc_src',"
            " '20250610', 'partial_unknown', '2025-06-10T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO valuation_snapshot(valuation_id, owner_type, owner_id, as_of,"
            " completeness, created_at) VALUES ('val_src_later', 'account', 'acc_src',"
            " '20250611', 'partial_unknown', '2025-06-11T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO valuation_snapshot(valuation_id, owner_type, owner_id, as_of,"
            " completeness, created_at) VALUES ('val_other', 'account', 'acc_other',"
            " '20250610', 'partial_unknown', '2025-06-10T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO valuation_snapshot(valuation_id, owner_type, owner_id, as_of,"
            " completeness, created_at) VALUES ('val_seg', 'simulation_segment',"
            " 'seg_perf', '20250610', 'partial_unknown', '2025-06-10T00:00:00+00:00')"
        )

    def _perf_sql() -> str:
        return (
            "INSERT INTO performance_point(point_id, owner_type, owner_id, as_of,"
            " valuation_id, method_version, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'perf-method-1.0',"
            " '2025-06-11T00:00:00+00:00')"
        )

    # 合法：账户 owner + 对应 as_of。
    with conn:
        conn.execute(_perf_sql(), ("pp_src", "account", "acc_src", "20250610", "val_src"))
        conn.execute(
            _perf_sql(), ("pp_seg", "simulation_segment", "seg_perf", "20250610", "val_seg")
        )
    # owner 不匹配（估值属于别人）→ 拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(_perf_sql(), ("pp_owner", "account", "acc_other", "20250610", "val_src"))
    # as_of 不匹配 → 拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(_perf_sql(), ("pp_asof", "account", "acc_src", "20250610", "val_src_later"))
    # 更新路径：把合法业绩点换到别人的估值 → 拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE performance_point SET valuation_id='val_other' WHERE point_id='pp_src'"
            )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE performance_point SET as_of='20250611' WHERE point_id='pp_src'"
            )


# ---------- 父记录防护：被引用后的父表 UPDATE 不得作废既有关系 ----------


def test_snapshot_account_id_immutable_once_referenced(migrated_conn) -> None:
    """被 analysis_run / simulation_segment 引用后，快照不得改挂到别的账户；
    未被引用的快照与金额类修正更新不受影响。"""
    conn, settings = migrated_conn
    bundle_id = _default_bundle_ids(conn)
    with conn:
        _two_accounts_with_snapshots(conn)
        _actual_simulation_fixture(conn, bundle_id)
        conn.execute(
            _analysis_run_sql(), ("an_a", "acc_a", "as_a", "sha_a"),
        )
        conn.execute(
            "INSERT INTO simulation_segment(segment_id, source_account_id,"
            " simulation_account_id, base_account_snapshot_id, rule_bundle_id,"
            " fee_bundle_id, source_code, data_version_id, as_of, input_sha256,"
            " created_at) VALUES ('seg_a', 'acc_src', 'acc_sim', 'as_seg', ?, ?,"
            " 'tushare-fixture', 'dv_x', '20250304', 'sha_seg_a',"
            " '2025-06-04T00:00:00+00:00')",
            (bundle_id, bundle_id),
        )
    # as_a 已被分析引用 → 改挂到 acc_b 拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE account_snapshot SET account_id='acc_b'"
                " WHERE snapshot_id='as_a'"
            )
    # as_seg 已被模拟段引用 → 改挂拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE account_snapshot SET account_id='acc_other'"
                " WHERE snapshot_id='as_seg'"
            )
    # 未被引用的 as_b 自由改挂 → 允许（守护按引用生效，不是一刀切禁改）。
    with conn:
        conn.execute(
            "UPDATE account_snapshot SET account_id='acc_a' WHERE snapshot_id='as_b'"
        )
    # 仍被引用的快照只改金额（不涉及所有权）→ 允许。
    with conn:
        conn.execute(
            "UPDATE account_snapshot SET cash_cents=999 WHERE snapshot_id='as_a'"
        )


def test_account_identity_immutable_once_segment_referenced(migrated_conn) -> None:
    """被模拟段引用（来源或模拟分身）后，账户的 kind / simulation_of_account_id
    不得再改；删除引用后同一更新恢复合法。"""
    conn, settings = migrated_conn
    bundle_id = _default_bundle_ids(conn)
    with conn:
        _actual_simulation_fixture(conn, bundle_id)
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, simulation_of_account_id,"
            " created_at, updated_at) VALUES ('acc_sim2', 'simulation', '其他模拟',"
            " 'acc_src', '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO simulation_segment(segment_id, source_account_id,"
            " simulation_account_id, base_account_snapshot_id, rule_bundle_id,"
            " fee_bundle_id, source_code, data_version_id, as_of, input_sha256,"
            " created_at) VALUES ('seg_id', 'acc_src', 'acc_sim', 'as_seg', ?, ?,"
            " 'tushare-fixture', 'dv_x', '20250304', 'sha_seg_id',"
            " '2025-06-04T00:00:00+00:00')",
            (bundle_id, bundle_id),
        )
    # acc_sim 已作为段内分身被引用 → 改挂别人的来源（须先有 acc_other）拒绝。
    with conn:
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name, created_at, updated_at)"
            " VALUES ('acc_other', 'actual', '其他实际', '2025-06-01T00:00:00+00:00',"
            " '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account(account_id, kind, display_name,"
            " simulation_of_account_id, created_at, updated_at)"
            " VALUES ('acc_a1', 'actual', 'A1', NULL,"
            " '2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE account SET simulation_of_account_id='acc_a1'"
                " WHERE account_id='acc_sim'"
            )
    # 段内来源账户 acc_src 被引用后身份同理不可变（同值身份写也被守护拒绝，
    # 证明守护按"是否被引用"生效，而非写入值本身）。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("UPDATE account SET kind='actual' WHERE account_id='acc_src'")
    # 段内模拟分身 acc_sim 被引用后 kind 不可再改（写入值合法，守护拒绝）。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE account SET kind='simulation' WHERE account_id='acc_sim'"
            )
    # 未被段引用的模拟账户自由改挂 → 允许。
    with conn:
        conn.execute(
            "UPDATE account SET simulation_of_account_id='acc_other'"
            " WHERE account_id='acc_sim2'"
        )
    # 删除段的引用后，同一更新恢复合法（守护基于引用而非绝对禁改）。
    with conn:
        conn.execute("DELETE FROM simulation_segment WHERE segment_id='seg_id'")
    with conn:
        conn.execute(
            "UPDATE account SET simulation_of_account_id='acc_other'"
            " WHERE account_id='acc_sim'"
        )


def test_superseded_event_account_id_immutable(migrated_conn) -> None:
    """被更正事件 supersede 后，被更正事件的归属账户不得改挂；
    删除更正事件后恢复可改，业务日期等非归属更新不受影响。"""
    conn, settings = migrated_conn
    with conn:
        _two_accounts_with_snapshots(conn)
        conn.execute(
            "INSERT INTO account_event(event_id, account_id, event_type, business_date,"
            " recorded_at, idempotency_key, payload, content_sha256, created_at)"
            " VALUES ('evt_a', 'acc_a', 'opening', '20250601',"
            " '2025-06-01T00:00:00+00:00', 'ka', '{}', 'sha',"
            " '2025-06-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO account_event(event_id, account_id, event_type, business_date,"
            " recorded_at, idempotency_key, payload, content_sha256, created_at,"
            " supersedes_event_id) VALUES ('evt_ok', 'acc_a', 'correction',"
            " '20250602', '2025-06-02T00:00:00+00:00', 'kok', '{}', 'sha',"
            " '2025-06-02T00:00:00+00:00', 'evt_a')"
        )
    # evt_a 被 evt_ok 更正引用 → 改挂到 acc_b 拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE account_event SET account_id='acc_b' WHERE event_id='evt_a'"
            )
    # 非归属字段（业务日期）更新不受守护影响。
    with conn:
        conn.execute(
            "UPDATE account_event SET business_date='20250603' WHERE event_id='evt_a'"
        )
    # 更正事件被删除后，同一归属更新恢复合法。
    with conn:
        conn.execute("DELETE FROM account_event WHERE event_id='evt_ok'")
    with conn:
        conn.execute(
            "UPDATE account_event SET account_id='acc_b' WHERE event_id='evt_a'"
        )


def test_valuation_identity_immutable_once_performance_referenced(migrated_conn) -> None:
    """被业绩点引用后，估值快照的 owner_type / owner_id / as_of 不得再改；
    未被引用的估值与删除引用后的更新不受影响。"""
    conn, settings = migrated_conn
    bundle_id = _default_bundle_ids(conn)
    with conn:
        _actual_simulation_fixture(conn, bundle_id)
        conn.execute(
            "INSERT INTO simulation_segment(segment_id, source_account_id,"
            " simulation_account_id, base_account_snapshot_id, rule_bundle_id,"
            " fee_bundle_id, source_code, data_version_id, as_of, input_sha256,"
            " created_at) VALUES ('seg_perf', 'acc_src', 'acc_sim', 'as_seg', ?, ?,"
            " 'tushare-fixture', 'dv_x', '20250304', 'sha_perfseg',"
            " '2025-06-04T00:00:00+00:00')",
            (bundle_id, bundle_id),
        )
        conn.execute(
            "INSERT INTO valuation_snapshot(valuation_id, owner_type, owner_id, as_of,"
            " completeness, created_at) VALUES ('val_src', 'account', 'acc_src',"
            " '20250610', 'partial_unknown', '2025-06-10T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO valuation_snapshot(valuation_id, owner_type, owner_id, as_of,"
            " completeness, created_at) VALUES ('val_unref', 'account', 'acc_src',"
            " '20250611', 'partial_unknown', '2025-06-11T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO performance_point(point_id, owner_type, owner_id, as_of,"
            " valuation_id, method_version, created_at)"
            " VALUES ('pp_src', 'account', 'acc_src', '20250610', 'val_src',"
            " 'perf-method-1.0', '2025-06-11T00:00:00+00:00')"
        )
    # 改挂 owner（换成模拟段）→ 拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE valuation_snapshot SET owner_type='simulation_segment',"
                " owner_id='seg_perf' WHERE valuation_id='val_src'"
            )
    # 改 as_of（试图让已有业绩点对不上）→ 拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE valuation_snapshot SET as_of='20250612' WHERE valuation_id='val_src'"
            )
    # 未被引用的估值改 as_of → 允许。
    with conn:
        conn.execute(
            "UPDATE valuation_snapshot SET as_of='20250612'"
            " WHERE valuation_id='val_unref'"
        )
    # 删除业绩点引用后，同一身份更新恢复合法。
    with conn:
        conn.execute("DELETE FROM performance_point WHERE point_id='pp_src'")
    with conn:
        conn.execute(
            "UPDATE valuation_snapshot SET as_of='20250612' WHERE valuation_id='val_src'"
        )


def test_parent_update_guards_fire_on_portfolio_boundary(portfolio_conn) -> None:
    """阶段三连接边界（外键开启）下父记录防护同样生效。"""
    conn, settings = portfolio_conn
    bundle_id = _default_bundle_ids(conn)
    version_id = _published_version(conn)
    with conn:
        _two_accounts_with_snapshots(conn)
        conn.execute(
            "INSERT INTO analysis_run(run_id, mode, account_id, account_snapshot_id,"
            " account_revision, source_code, data_version_id, as_of, rule_bundle_id,"
            " input_sha256, status, generated_at)"
            " VALUES ('an_bd', 'incremental', 'acc_a', 'as_a', 1, 'tushare-fixture',"
            " ?, '20250304', ?, 'sha_bd', 'complete', ?)",
            (version_id, bundle_id, utc_now_iso()),
        )
    # 外键合法（acc_b 存在）但触发器拒绝：as_a 已被分析引用。
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "UPDATE account_snapshot SET account_id='acc_b'"
                " WHERE snapshot_id='as_a'"
            )
    # 数据未被破坏：拒绝后引用关系依旧成立。
    with conn:
        owner = conn.execute(
            "SELECT account_id FROM account_snapshot WHERE snapshot_id='as_a'"
        ).fetchone()[0]
        assert owner == "acc_a"

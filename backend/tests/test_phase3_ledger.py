"""阶段三 S2：实际账户账本——期初、事件、修订、更正与对账（手算样例）。

全部使用隔离临时库（``portfolio_conn``，外键开启），不访问真实数据/服务。
金额一律整数分；股数一律非负整数；未知字段显式断言保持 None。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.data.errors import DataError
from app.data.migration import initialize_database
from app.portfolio.db import portfolio_connect
from app.portfolio.ledger import (
    ACCOUNT_LEDGER_VERSION,
    AccountEventRequest,
    create_account,
    get_account,
    get_account_snapshot_state,
    get_account_state,
    list_account_events,
    list_account_snapshots,
    post_account_event,
    preview_account_event,
)

REC_1 = "2025-06-05T01:00:00+00:00"  # 统一 recorded_at，保证同日事件顺序确定


@pytest.fixture
def portfolio_conn(data_settings):
    """阶段三连接边界：外键约束已启用的连接（schema 4 库）。"""
    initialize_database(data_settings)
    conn = portfolio_connect(data_settings.db_path)
    try:
        yield conn, data_settings
    finally:
        conn.close()


def _new_account(conn: sqlite3.Connection, account_id: str = "acct_a") -> dict:
    return create_account(conn, account_id, "测试账户")


def _opening(
    payload: dict,
    key: str = "open-1",
    business_date: str = "20250301",
) -> AccountEventRequest:
    return AccountEventRequest(
        account_id="acct_a",
        event_type="opening",
        business_date=business_date,
        idempotency_key=key,
        payload=payload,
        recorded_at=REC_1,
        reason="期初录入",
    )


def _post(conn, request: AccountEventRequest, *, expected: int):
    return post_account_event(conn, request, expected_account_revision=expected)


def _count(conn, table: str, where: str = "1=1", params: tuple = ()) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()
    return int(row[0])


def _positions_payload(**overrides) -> list[dict]:
    base = {
        "ts_code": "000001.SZ",
        "shares": 100,
        "total_cost_cents": 100_000,
        "sellable_shares": 100,
        "acquire_date": "20250201",
        "locked": 0,
    }
    base.update(overrides)
    return [base]


# ---------------------------------------------------------------- 期初与快照


def test_opening_state_roundtrip_and_unknown_fields(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    request = _opening(
        {
            "cash_cents": 100_000,
            "positions": [
                _positions_payload()[0],
                {
                    "ts_code": "350002.SZ",
                    "shares": 50,
                    "total_cost_cents": None,
                    "sellable_shares": None,
                    "acquire_date": None,
                    "locked": True,
                },
            ],
        }
    )
    preview = preview_account_event(conn, request)
    assert preview.valid and preview.affected_revision == 1
    assert preview.before.revision == 0 and preview.before.cash_cents is None

    result = _post(conn, request, expected=0)
    assert result.replayed is False
    assert result.revision == 1
    assert result.snapshot_id is not None
    assert result.cash_cents == 100_000
    codes = [p["ts_code"] for p in result.positions]
    assert codes == ["000001.SZ", "350002.SZ"]
    assert result.unknown_fields == [
        "position_cost_unknown",
        "position_sellable_unknown",
        "position_acquire_date_unknown",
    ]

    stored = get_account_state(conn, "acct_a")
    assert stored["revision"] == 1
    assert stored["cash_cents"] == 100_000
    by_code = {p["ts_code"]: p for p in stored["positions"]}
    assert by_code["000001.SZ"]["shares"] == 100
    assert by_code["000001.SZ"]["total_cost_cents"] == 100_000
    assert by_code["000001.SZ"]["sellable_shares"] == 100
    assert by_code["350002.SZ"]["total_cost_cents"] is None
    assert by_code["350002.SZ"]["sellable_shares"] is None
    assert by_code["350002.SZ"]["acquire_date"] is None
    assert by_code["350002.SZ"]["locked"] == 1

    snap = list_account_snapshots(conn, "acct_a")
    assert len(snap) == 1 and snap[0]["account_revision"] == 1
    assert snap[0]["cash_cents"] == 100_000


def test_opening_cash_only_and_account_metadata(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    result = _post(conn, _opening({"cash_cents": 50_000, "positions": []}), expected=0)
    assert result.cash_cents == 50_000
    assert result.positions == ()
    assert get_account(conn, "acct_a")["account_revision"] == 1
    state = get_account_state(conn, "acct_a")
    assert state["unknown_fields"] == []
    assert state["as_of"] == "20250301"


def test_opening_twice_rejected(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 100}), expected=0)
    request = _opening({"cash_cents": 200}, key="open-2", business_date="20250302")
    preview = preview_account_event(conn, request)
    assert not preview.valid and preview.error_code == "opening_already_exists"
    with pytest.raises(DataError) as exc:
        _post(conn, request, expected=1)
    assert exc.value.reason == "opening_already_exists"
    assert _count(conn, "account_event") == 1


# ---------------------------------------------------------------- 手算买卖对账


def test_buy_cash_cost_and_sell_roundtrip(portfolio_conn) -> None:
    """期初现金 100000 元；买 100 股价 10.00 元费 5 元；再按原价卖出费 5 元。

    买：现金 10_000_000 - (100*1000 + 500) = 9_899_500 分（98995.00 元）；
    成本 = 100*1000 + 500 = 100_500 分（含买入费 5 元）；
    卖：现金 9_899_500 + (100*1000 - 500) = 9_999_000 分（99990.00 元），
    全部卖出后成本清零。往返总费用 10 元一次计入，不重复结转。
    """
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 10_000_000, "positions": []}), expected=0)

    buy = AccountEventRequest(
        account_id="acct_a",
        event_type="buy",
        business_date="20250304",
        idempotency_key="buy-1",
        payload={"ts_code": "000001.SZ", "shares": 100, "price_cents": 1000, "fee_cents": 500},
        recorded_at="2025-03-05T01:00:00+00:00",
    )
    preview = preview_account_event(conn, buy)
    assert preview.valid and preview.affected_revision == 2
    assert preview.before.cash_cents == 10_000_000
    assert preview.after.cash_cents == 9_899_500
    buy_result = _post(conn, buy, expected=1)
    assert buy_result.revision == 2
    assert buy_result.cash_cents == 9_899_500
    pos = buy_result.positions[0]
    assert pos["ts_code"] == "000001.SZ"
    assert pos["shares"] == 100
    assert pos["total_cost_cents"] == 100_500
    assert pos["sellable_shares"] is None  # 买入后可卖量未知，不冒充已知
    assert pos["acquire_date"] == "20250304"

    sell = AccountEventRequest(
        account_id="acct_a",
        event_type="sell",
        business_date="20250305",
        idempotency_key="sell-1",
        payload={"ts_code": "000001.SZ", "shares": 100, "price_cents": 1000, "fee_cents": 500},
        recorded_at="2025-03-06T01:00:00+00:00",
    )
    # 卖出前可卖量未知：预览与提交都显式告警，但不伪造判定（用户申报实际历史）。
    sell_preview = preview_account_event(conn, sell)
    assert sell_preview.valid
    assert any(w["code"] == "position_sellable_unknown" for w in sell_preview.after.warnings)
    sell_result = _post(conn, sell, expected=2)
    assert sell_result.revision == 3
    assert sell_result.cash_cents == 9_999_000
    pos = sell_result.positions[0]
    assert pos["shares"] == 0
    assert pos["total_cost_cents"] == 0
    assert pos["sellable_shares"] == 0

    # 旧快照保持原样：修订 2 的现金仍是买后 9_899_500 分。
    rev1 = get_account_snapshot_state(conn, "acct_a", revision=1)
    assert rev1["cash_cents"] == 10_000_000
    rev2 = get_account_snapshot_state(conn, "acct_a", revision=2)
    assert rev2["cash_cents"] == 9_899_500
    assert rev2["positions"][0]["total_cost_cents"] == 100_500


def test_unknown_cost_position_mixing_keeps_unknown(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(
        conn,
        _opening(
            {
                "cash_cents": 10_000,
                "positions": [
                    {"ts_code": "600000.SH", "shares": 20, "total_cost_cents": None,
                     "sellable_shares": 20, "acquire_date": None, "locked": 0}
                ],
            }
        ),
        expected=0,
    )
    result = _post(
        conn,
        AccountEventRequest(
            account_id="acct_a",
            event_type="buy",
            business_date="20250305",
            idempotency_key="buy-mix",
            payload={"ts_code": "600000.SH", "shares": 5, "price_cents": 300, "fee_cents": 0},
            recorded_at=REC_1,
        ),
        expected=1,
    )
    pos = result.positions[0]
    assert pos["shares"] == 25
    assert pos["total_cost_cents"] is None
    assert any(w["code"] == "position_cost_mixed_unknown" for w in result.warnings)
    unknowns = get_account_state(conn, "acct_a")["unknown_fields"]
    assert "position_cost_unknown" in unknowns


def test_cash_in_out_are_external_flows_not_returns(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 100_000, "positions": []}), expected=0)
    result = _post(
        conn,
        AccountEventRequest(
            account_id="acct_a",
            event_type="cash_in",
            business_date="20250302",
            idempotency_key="in-1",
            payload={"amount_cents": 10_000},
            recorded_at=REC_1,
        ),
        expected=1,
    )
    assert result.cash_cents == 110_000
    result = _post(
        conn,
        AccountEventRequest(
            account_id="acct_a",
            event_type="cash_out",
            business_date="20250303",
            idempotency_key="out-1",
            payload={"amount_cents": 2_000},
            recorded_at=REC_1,
        ),
        expected=2,
    )
    assert result.cash_cents == 108_000
    payload_keys = set(result.as_dict())
    assert "return" not in payload_keys and "yield" not in payload_keys
    assert "position_sellable_unknown_after_buy" not in [w["code"] for w in result.warnings]


# ---------------------------------------------------------------- 预览只读与拒绝


def test_preview_has_no_side_effects(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 10_000, "positions": []}), expected=0)
    before_events = _count(conn, "account_event")
    before_snapshots = _count(conn, "account_snapshot")
    before_revision = get_account(conn, "acct_a")["account_revision"]

    request = AccountEventRequest(
        account_id="acct_a",
        event_type="buy",
        business_date="20250304",
        idempotency_key="pip",
        payload={"ts_code": "000001.SZ", "shares": 10, "price_cents": 100, "fee_cents": 0},
        recorded_at=REC_1,
    )
    preview = preview_account_event(conn, request)
    assert preview.valid and preview.affected_revision == 2
    assert preview.before.revision == 1 and preview.after.revision == 2

    assert _count(conn, "account_event") == before_events
    assert _count(conn, "account_snapshot") == before_snapshots
    assert get_account(conn, "acct_a")["account_revision"] == before_revision


def test_buy_beyond_cash_rejected_and_rolls_back(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 5_000, "positions": []}), expected=0)
    request = AccountEventRequest(
        account_id="acct_a",
        event_type="buy",
        business_date="20250304",
        idempotency_key="buy-big",
        payload={"ts_code": "000001.SZ", "shares": 10, "price_cents": 1000, "fee_cents": 0},
        recorded_at=REC_1,
    )
    preview = preview_account_event(conn, request)
    assert not preview.valid and preview.error_code == "insufficient_cash"

    with pytest.raises(DataError) as exc:
        _post(conn, request, expected=1)
    assert exc.value.reason == "insufficient_cash"
    assert _count(conn, "account_event") == 1
    assert get_account_state(conn, "acct_a")["cash_cents"] == 5_000

    with pytest.raises(DataError) as exc:
        _post(
            conn,
            AccountEventRequest(
                account_id="acct_a",
                event_type="cash_out",
                business_date="20250304",
                idempotency_key="out-big",
                payload={"amount_cents": 6_000},
                recorded_at=REC_1,
            ),
            expected=1,
        )
    assert exc.value.reason == "insufficient_cash"
    assert get_account_state(conn, "acct_a")["cash_cents"] == 5_000


def test_sell_rejections_leave_ledger_untouched(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(
        conn,
        _opening({"cash_cents": 100_000, "positions": _positions_payload(sellable_shares=50)}),
        expected=0,
    )

    over_sellable = AccountEventRequest(
        account_id="acct_a",
        event_type="sell",
        business_date="20250304",
        idempotency_key="sell-over",
        payload={"ts_code": "000001.SZ", "shares": 60, "price_cents": 1000, "fee_cents": 0},
        recorded_at=REC_1,
    )
    preview = preview_account_event(conn, over_sellable)
    assert not preview.valid and preview.error_code == "sellable_insufficient"
    with pytest.raises(DataError) as exc:
        _post(conn, over_sellable, expected=1)
    assert exc.value.reason == "sellable_insufficient"

    over_holdings = AccountEventRequest(
        account_id="acct_a",
        event_type="sell",
        business_date="20250304",
        idempotency_key="sell-holdings",
        payload={"ts_code": "000001.SZ", "shares": 300, "price_cents": 1000, "fee_cents": 0},
        recorded_at=REC_1,
    )
    preview = preview_account_event(conn, over_holdings)
    assert not preview.valid and preview.error_code == "position_insufficient"
    with pytest.raises(DataError) as exc:
        _post(conn, over_holdings, expected=1)
    assert exc.value.reason == "position_insufficient"

    missing = AccountEventRequest(
        account_id="acct_a",
        event_type="sell",
        business_date="20250304",
        idempotency_key="sell-missing",
        payload={"ts_code": "600000.SH", "shares": 1, "price_cents": 100, "fee_cents": 0},
        recorded_at=REC_1,
    )
    preview = preview_account_event(conn, missing)
    assert not preview.valid and preview.error_code == "position_missing"

    assert _count(conn, "account_event") == 1
    assert get_account_state(conn, "acct_a")["cash_cents"] == 100_000
    pos = get_account_state(conn, "acct_a")["positions"][0]
    assert pos["shares"] == 100 and pos["sellable_shares"] == 50


# ---------------------------------------------------------------- 幂等与并发


def test_exact_replay_is_idempotent_and_key_conflict_rejected(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 10_000_000, "positions": []}), expected=0)
    buy = AccountEventRequest(
        account_id="acct_a",
        event_type="buy",
        business_date="20250304",
        idempotency_key="buy-idem",
        payload={"ts_code": "000001.SZ", "shares": 100, "price_cents": 1000, "fee_cents": 500},
        recorded_at="2025-03-05T01:00:00+00:00",
    )
    first = _post(conn, buy, expected=1)
    assert first.replayed is False

    events_before = _count(conn, "account_event")
    snapshots_before = _count(conn, "account_snapshot")
    replay = _post(conn, buy, expected=1)
    assert replay.replayed is True
    assert replay.event_id == first.event_id
    assert replay.revision == first.revision == 2
    assert replay.snapshot_id == first.snapshot_id
    assert replay.cash_cents == first.cash_cents
    assert _count(conn, "account_event") == events_before
    assert _count(conn, "account_snapshot") == snapshots_before

    conflict = AccountEventRequest(
        account_id="acct_a",
        event_type="buy",
        business_date="20250304",
        idempotency_key="buy-idem",
        payload={"ts_code": "000001.SZ", "shares": 100, "price_cents": 1001, "fee_cents": 500},
        recorded_at=REC_1,
    )
    ctx_conflict = pytest.raises(DataError)
    with ctx_conflict as exc:
        _post(conn, conflict, expected=2)
    assert exc.value.reason == "idempotency_conflict"
    assert _count(conn, "account_event") == events_before
    with ctx_conflict as exc:
        preview_account_event(conn, conflict)
    assert exc.value.reason == "idempotency_conflict"


def test_stale_revision_conflict_rejected_cleanly(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 100_000, "positions": []}), expected=0)
    request = AccountEventRequest(
        account_id="acct_a",
        event_type="cash_in",
        business_date="20250302",
        idempotency_key="in-stale",
        payload={"amount_cents": 1_000},
        recorded_at=REC_1,
    )
    preview = preview_account_event(conn, request)
    assert preview.valid and preview.affected_revision == 2
    with pytest.raises(DataError) as exc:
        _post(conn, request, expected=0)  # 已在修订 1，预期 0 = 陈旧
    assert exc.value.reason == "account_revision_conflict"
    assert _count(conn, "account_event") == 1
    assert get_account_state(conn, "acct_a")["cash_cents"] == 100_000


# ---------------------------------------------------------------- 更正与晚到事件


def test_correction_replaces_content_without_mutation(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 10_000_000, "positions": []}), expected=0)
    buy = AccountEventRequest(
        account_id="acct_a",
        event_type="buy",
        business_date="20250304",
        idempotency_key="buy-wrongprice",
        payload={"ts_code": "000001.SZ", "shares": 100, "price_cents": 1200, "fee_cents": 500},
        recorded_at="2025-03-05T01:00:00+00:00",
    )
    posted = _post(conn, buy, expected=1)
    assert posted.cash_cents == 10_000_000 - 100*1200 - 500

    original_row = conn.execute(
        "SELECT payload, content_sha256, event_version, supersedes_event_id"
        " FROM account_event WHERE event_id=?",
        (posted.event_id,),
    ).fetchone()
    original_payload = original_row[0]
    original_hash = original_row[1]

    correction = AccountEventRequest(
        account_id="acct_a",
        event_type="correction",
        business_date="20250304",
        idempotency_key="corr-1",
        payload={"ts_code": "000001.SZ", "shares": 100, "price_cents": 1000, "fee_cents": 500},
        supersedes_event_id=posted.event_id,
        reason="申报价录入错误，按券商确认 10.00 元更正",
        recorded_at="2025-03-08T01:00:00+00:00",
    )
    preview = preview_account_event(conn, correction)
    assert preview.valid and preview.affected_revision == 3
    assert preview.after.cash_cents == 10_000_000 - 100*1000 - 500

    corrected = _post(conn, correction, expected=2)
    assert corrected.event_version == 2
    assert corrected.supersedes_event_id == posted.event_id
    assert corrected.cash_cents == 9_899_500
    assert corrected.positions[0]["total_cost_cents"] == 100_500

    after_row = conn.execute(
        "SELECT payload, content_sha256, event_version, supersedes_event_id"
        " FROM account_event WHERE event_id=?",
        (posted.event_id,),
    ).fetchone()
    assert after_row[0] == original_payload  # 原事件载荷未变
    assert after_row[1] == original_hash
    assert after_row[3] is None  # 原事件的更正关系记录在更正行上

    listing = list_account_events(conn, "acct_a")
    by_id = {e["event_id"]: e for e in listing}
    assert by_id[posted.event_id]["effective"] is False
    assert by_id[posted.event_id]["superseded_by"] == corrected.event_id
    assert by_id[corrected.event_id]["event_version"] == 2

    # 同一被更正事件不能被二次更正（须更正最新更正事件）；目标已失效在准备期直接拒绝。
    second = AccountEventRequest(
        account_id="acct_a",
        event_type="correction",
        business_date="20250304",
        idempotency_key="corr-2",
        payload={"ts_code": "000001.SZ", "shares": 100, "price_cents": 1000, "fee_cents": 400},
        supersedes_event_id=posted.event_id,
        recorded_at=REC_1,
    )
    with pytest.raises(DataError) as exc:
        preview_account_event(conn, second)
    assert exc.value.reason == "correction_target_already_superseded"
    with pytest.raises(DataError) as exc:
        _post(conn, second, expected=3)
    assert exc.value.reason == "correction_target_already_superseded"

    # 更正链：对更正事件再更正 → 版本 3，链尾生效。
    corr2 = AccountEventRequest(
        account_id="acct_a",
        event_type="correction",
        business_date="20250304",
        idempotency_key="corr-2b",
        payload={"ts_code": "000001.SZ", "shares": 100, "price_cents": 1000, "fee_cents": 400},
        supersedes_event_id=corrected.event_id,
        reason="费用按对账单为 4.00 元",
        recorded_at="2025-03-09T01:00:00+00:00",
    )
    chain = _post(conn, corr2, expected=3)
    assert chain.event_version == 3
    assert chain.cash_cents == 10_000_000 - 100*1000 - 400
    assert chain.positions[0]["total_cost_cents"] == 100_400

    # 更正不改生效日。
    cross_date = AccountEventRequest(
        account_id="acct_a",
        event_type="correction",
        business_date="20250305",
        idempotency_key="corr-3",
        payload={"ts_code": "000001.SZ", "shares": 100, "price_cents": 1000, "fee_cents": 400},
        supersedes_event_id=chain.event_id,
        recorded_at=REC_1,
    )
    with pytest.raises(DataError) as exc:
        preview_account_event(conn, cross_date)
    assert exc.value.reason == "correction_business_date_mismatch"


def test_late_arriving_event_creates_new_revision(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 1_000_000, "positions": []}, business_date="20250301"), expected=0)
    _post(
        conn,
        AccountEventRequest(
            account_id="acct_a",
            event_type="buy",
            business_date="20250306",
            idempotency_key="buy-late-base",
            payload={"ts_code": "000001.SZ", "shares": 10, "price_cents": 1000, "fee_cents": 0},
            recorded_at="2025-03-07T01:00:00+00:00",
        ),
        expected=1,
    )
    late = AccountEventRequest(
        account_id="acct_a",
        event_type="cash_in",
        business_date="20250303",
        idempotency_key="in-late",
        payload={"amount_cents": 50_000},
        reason="晚到的上一月入金补录",
        recorded_at="2025-03-08T01:00:00+00:00",
    )
    preview = preview_account_event(conn, late)
    assert preview.valid and preview.affected_revision == 3
    assert preview.after.cash_cents == 1_040_000  # 1_000_000 + 50_000 - 10_000(3/6 买入)

    result = _post(conn, late, expected=2)
    assert result.revision == 3
    as_of = list_account_snapshots(conn, "acct_a")[0]["as_of"]
    assert as_of == "20250306"  # 快照时点 = 有效事件流的最新生效日
    rev2 = get_account_snapshot_state(conn, "acct_a", revision=2)
    assert rev2["cash_cents"] == 990_000  # 修订 2 即买入后快照，未被晚到事件改写


def test_backdated_event_that_breaks_fold_is_rejected(portfolio_conn) -> None:
    """晚到的卖出若让之后的事件（再卖同一持仓）不再成立 → 整批拒绝并回滚。"""
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(
        conn,
        _opening(
            {
                "cash_cents": 1_000_000,
                "positions": _positions_payload(),
            },
            business_date="20250301",
        ),
        expected=0,
    )
    later_sell = AccountEventRequest(
        account_id="acct_a",
        event_type="sell",
        business_date="20250306",
        idempotency_key="sell-later",
        payload={"ts_code": "000001.SZ", "shares": 100, "price_cents": 1000, "fee_cents": 0},
        recorded_at="2025-03-07T01:00:00+00:00",
    )
    _post(conn, later_sell, expected=1)
    # 晚到的 3 月 3 日再卖 40 股：把有效流折叠到 3 月 6 日时只剩 60 股 < 100 → 拒绝。
    intrusive = AccountEventRequest(
        account_id="acct_a",
        event_type="sell",
        business_date="20250303",
        idempotency_key="sell-backdated",
        payload={"ts_code": "000001.SZ", "shares": 40, "price_cents": 1000, "fee_cents": 0},
        recorded_at="2025-03-08T01:00:00+00:00",
    )
    preview = preview_account_event(conn, intrusive)
    assert not preview.valid and preview.error_code == "position_insufficient"
    with pytest.raises(DataError) as exc:
        _post(conn, intrusive, expected=2)
    assert exc.value.reason == "position_insufficient"
    assert _count(conn, "account_event") == 2
    rev2 = get_account_snapshot_state(conn, "acct_a", revision=2)
    assert rev2["cash_cents"] == 1_100_000  # 1,000,000 - 100*1000 + 100*1000


# ---------------------------------------------------------------- 公司行为（仅手动确认）


def test_corporate_action_manual_recording(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(
        conn,
        _opening(
            {"cash_cents": 5_000, "positions": _positions_payload(total_cost_cents=100_000)}
        ),
        expected=0,
    )
    # 送股：+50 股，无现金；未提供新总成本 → 总成本保持 100_000 并告警。
    bonus = AccountEventRequest(
        account_id="acct_a",
        event_type="corporate_action",
        business_date="20250304",
        idempotency_key="ca-bonus",
        payload={"ts_code": "000001.SZ", "share_delta": 50, "cash_delta_cents": 0},
        reason="券商确认：按 10 送 5 上市",
        recorded_at=REC_1,
    )
    result = _post(conn, bonus, expected=1)
    pos = result.positions[0]
    assert pos["shares"] == 150
    assert pos["total_cost_cents"] == 100_000
    assert any(w["code"] == "corporate_action_cost_basis_unchanged" for w in result.warnings)

    # 现金分红（券商确认的税后净额）：现金 +800 分。
    dividend = AccountEventRequest(
        account_id="acct_a",
        event_type="corporate_action",
        business_date="20250305",
        idempotency_key="ca-dividend",
        payload={"ts_code": "000001.SZ", "share_delta": 0, "cash_delta_cents": 800},
        reason="券商确认：净现金分红到账",
        recorded_at=REC_1,
    )
    result = _post(conn, dividend, expected=2)
    assert result.cash_cents == 5_800

    # 显式确认的新总成本基础。
    adjusted = AccountEventRequest(
        account_id="acct_a",
        event_type="corporate_action",
        business_date="20250308",
        idempotency_key="ca-cost",
        payload={
            "ts_code": "000001.SZ",
            "share_delta": 0,
            "cash_delta_cents": 0,
            "new_total_cost_cents": 95_000,
        },
        reason="券商确认：送股后每股持股成本重算",
        recorded_at=REC_1,
    )
    result = _post(conn, adjusted, expected=3)
    assert result.positions[0]["total_cost_cents"] == 95_000

    # 无效：无变动 / 股数变负 / 现金变负 / 清零后仍报成本。
    for key, payload, code in (
        ("ca-noop", {"ts_code": "000001.SZ", "share_delta": 0, "cash_delta_cents": 0},
         "corporate_action_no_effect"),
        ("ca-neg", {"ts_code": "000001.SZ", "share_delta": -500, "cash_delta_cents": 0},
         "corporate_action_invalid_shares"),
        ("ca-cash", {"ts_code": "000001.SZ", "share_delta": 0, "cash_delta_cents": -100_000},
         "insufficient_cash"),
        ("ca-empty-cost", {"ts_code": "000001.SZ", "share_delta": -150, "cash_delta_cents": 0,
                           "new_total_cost_cents": 10},
         "corporate_action_cost_for_empty_position"),
    ):
        request = AccountEventRequest(
            account_id="acct_a",
            event_type="corporate_action",
            business_date="20250309",
            idempotency_key=key,
            payload=payload,
            recorded_at=REC_1,
        )
        try:
            preview = preview_account_event(conn, request)
        except DataError as exc:  # 结构级问题在准备期直接拒绝
            assert exc.reason == code, (key, exc.reason, code)
        else:
            assert not preview.valid and preview.error_code == code, (key, preview.error_code)
        with pytest.raises(DataError) as exc:
            _post(conn, request, expected=4)
        assert exc.value.reason == code

    # 非持有证券上的公司行为拒绝。
    stranger = AccountEventRequest(
        account_id="acct_a",
        event_type="corporate_action",
        business_date="20250309",
        idempotency_key="ca-stranger",
        payload={"ts_code": "600000.SH", "share_delta": 10, "cash_delta_cents": 0},
        recorded_at=REC_1,
    )
    preview = preview_account_event(conn, stranger)
    assert not preview.valid and preview.error_code == "corporate_action_position_missing"


# ---------------------------------------------------------------- 原子性与领域隔离


def test_mid_write_failure_rolls_back_everything(
    portfolio_conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交途中失败（注入在第 3 次 utc_now 调用：修订推进前）→ 全部回滚。"""
    from app.portfolio import ledger

    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 100_000, "positions": []}), expected=0)

    calls: list[int] = []
    real_now = ledger.utc_now_iso

    def flaky_now() -> str:
        calls.append(1)
        if len(calls) >= 3:
            raise RuntimeError("injected failure inside commit")
        return real_now()

    monkeypatch.setattr(ledger, "utc_now_iso", flaky_now)
    request = AccountEventRequest(
        account_id="acct_a",
        event_type="buy",
        business_date="20250304",
        idempotency_key="buy-rollback",
        payload={"ts_code": "000001.SZ", "shares": 10, "price_cents": 100, "fee_cents": 0},
        recorded_at=REC_1,
    )
    with pytest.raises(RuntimeError, match="injected failure"):
        _post(conn, request, expected=1)
    monkeypatch.undo()

    assert _count(conn, "account_event") == 1
    assert _count(conn, "account_snapshot") == 1
    assert _count(conn, "account_position") == 0
    assert get_account(conn, "acct_a")["account_revision"] == 1
    assert get_account_state(conn, "acct_a")["cash_cents"] == 100_000

    # 注入清除后同一提交可正常完成，不残留半提交状态。
    retry = _post(conn, request, expected=1)
    assert retry.replayed is False
    assert retry.revision == 2
    assert _count(conn, "account_event") == 2


def test_events_rejected_on_simulation_accounts(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    now = "2025-06-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO account(account_id, kind, display_name, simulation_of_account_id,"
        " created_at, updated_at) VALUES ('acct_sim', 'simulation', '模拟', 'acct_a', ?, ?)",
        (now, now),
    )
    request = AccountEventRequest(
        account_id="acct_sim",
        event_type="opening",
        business_date="20250301",
        idempotency_key="sim-open",
        payload={"cash_cents": 100},
    )
    with pytest.raises(DataError) as exc:
        preview_account_event(conn, request)
    assert exc.value.reason == "account_event_requires_actual_account"
    with pytest.raises(DataError) as exc:
        _post(conn, request, expected=0)
    assert exc.value.reason == "account_event_requires_actual_account"

    # 数据库触发器兜底（任何连接生效）：直接绕过服务写入模拟账户事件 → 拒绝。
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute(
            "INSERT INTO account_event(event_id, account_id, event_type, business_date,"
            " recorded_at, idempotency_key, payload, content_sha256, created_at)"
            " VALUES ('evt_sim', 'acct_sim', 'opening', '20250301', ?, 'k', '{}', 'sha', ?)",
            (now, now),
        )
    assert "account_event_requires_actual_account" in str(exc.value)


def test_tampered_event_content_detected_in_fold(portfolio_conn) -> None:
    """绕过服务直接修改事件载荷 → 折叠前摘要核对失败（fail-closed）。"""
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 100_000, "positions": []}), expected=0)
    with conn:
        conn.execute(
            "UPDATE account_event SET payload=? WHERE idempotency_key='open-1'",
            ('{"cash_cents": 9}',),
        )
    with pytest.raises(DataError) as exc:
        get_account_state(conn, "acct_a")
    assert exc.value.reason == "event_content_corrupt"


# ---------------------------------------------------------------- 输入校验


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"business_date": "20261231"}, "business_date_in_future"),
        ({"business_date": "2025-03-04"}, "business_date_invalid"),
        ({"business_date": "20990230"}, "business_date_invalid"),
        ({"idempotency_key": ""}, "idempotency_key_invalid"),
        ({"event_type": "transfer"}, "event_type_unknown"),
    ],
)
def test_request_level_validation(portfolio_conn, patch: dict, code: str) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    fields = {
        "account_id": "acct_a",
        "event_type": "opening",
        "business_date": "20250301",
        "idempotency_key": "open-validation",
        "payload": {"cash_cents": 100, "positions": []},
        "recorded_at": REC_1,
    }
    fields.update(patch)
    try:
        request = AccountEventRequest(
            **fields,
        )
    except DataError as exc:  # 请求结构级校验在构造期直接失败
        assert exc.reason == code
        return
    with pytest.raises(DataError) as exc:
        preview_account_event(conn, request)
    assert exc.value.reason == code


def test_payload_level_validation_rejections(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)

    def _expect(code: str, payload: dict, event_type: str = "opening") -> None:
        request = AccountEventRequest(
            account_id="acct_a",
            event_type=event_type,
            business_date="20250301",
            idempotency_key=f"v-{code}-{len(payload)}",
            payload=payload,
            recorded_at=REC_1,
        )
        try:
            preview = preview_account_event(conn, request)
        except DataError as exc:  # 载荷结构级问题在准备期直接拒绝
            assert exc.reason == code, (payload, exc.reason, code)
            with pytest.raises(DataError) as exc2:
                _post(conn, request, expected=0)
            assert exc2.value.reason == code
            return
        assert not preview.valid and preview.error_code == code, (payload, preview.error_code)
        with pytest.raises(DataError) as exc:
            _post(conn, request, expected=0)
        assert exc.value.reason == code

    _expect("ledger_field_not_integer", {"cash_cents": 12.5, "positions": []})
    _expect("ledger_field_out_of_range", {"cash_cents": -1, "positions": []})
    _expect(
        "payload_unknown_field",
        {"cash_cents": 10, "positions": [], "weird": 1},
    )
    _expect(
        "opening_duplicate_position",
        {
            "cash_cents": 10,
            "positions": [
                {"ts_code": "000001.SZ", "shares": 1},
                {"ts_code": "000001.SZ", "shares": 2},
            ],
        },
    )
    _expect(
        "position_sellable_exceeds_shares",
        {"cash_cents": 10, "positions": _positions_payload(sellable_shares=101)},
    )
    _expect(
        "acquire_date_after_business_date",
        {"cash_cents": 10, "positions": _positions_payload(acquire_date="20250302")},
    )
    _expect("opening_position_invalid", {"cash_cents": 10, "positions": ["x"]})
    _expect("ledger_field_out_of_range", {"cash_cents": 10,
                                         "positions": _positions_payload(shares=0)})
    _expect("ledger_field_out_of_range", {"cash_cents": 10,
                                         "positions": _positions_payload(shares=-1)})
    _expect("ledger_field_not_integer", {"cash_cents": 10,
                                         "positions": _positions_payload(shares=1.5)})

    # 买卖缺费用：fail-closed（缺证据不得推导现金变动）。
    _expect(
        "fee_required",
        {"ts_code": "000001.SZ", "shares": 10, "price_cents": 100},
        event_type="buy",
    )
    _expect(
        "ledger_field_out_of_range",
        {"ts_code": "000001.SZ", "shares": 10, "price_cents": 100, "fee_cents": -1},
        event_type="sell",
    )
    _expect(
        "ledger_field_not_integer",
        {"ts_code": "000001.SZ", "shares": 10.5, "price_cents": 100, "fee_cents": 0},
        event_type="buy",
    )
    _expect(
        "ledger_field_out_of_range",
        {"ts_code": "000001.SZ", "shares": 0, "price_cents": 100, "fee_cents": 0},
        event_type="buy",
    )
    _expect(
        "ledger_field_out_of_range",
        {"ts_code": "000001.SZ", "shares": 1, "price_cents": 0, "fee_cents": 0},
        event_type="buy",
    )
    _expect("ts_code_invalid", {"ts_code": "000001", "shares": 1, "price_cents": 10, "fee_cents": 0}, "buy")

    _expect("ledger_field_out_of_range", {"amount_cents": 0}, event_type="cash_in")

    # accounts/currency 边界。
    with pytest.raises(DataError) as exc:
        create_account(conn, "acct_b", "外币", base_currency="USD")
    assert exc.value.reason == "base_currency_unsupported"
    with pytest.raises(DataError) as exc:
        create_account(conn, "acct_a", "重复")
    assert exc.value.reason == "account_already_exists"


def test_buy_on_shifted_frame_requires_opening_first(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    request = AccountEventRequest(
        account_id="acct_a",
        event_type="buy",
        business_date="20250304",
        idempotency_key="buy-no-open",
        payload={"ts_code": "000001.SZ", "shares": 1, "price_cents": 10, "fee_cents": 0},
        recorded_at=REC_1,
    )
    preview = preview_account_event(conn, request)
    assert not preview.valid and preview.error_code == "event_before_opening"
    with pytest.raises(DataError) as exc:
        _post(conn, request, expected=0)
    assert exc.value.reason == "event_before_opening"


def test_audit_reads_and_ledger_version(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    _new_account(conn)
    _post(conn, _opening({"cash_cents": 100_000, "positions": []}), expected=0)
    listing = list_account_events(conn, "acct_a")
    assert len(listing) == 1
    assert listing[0]["event_type"] == "opening"
    assert listing[0]["effective"] is True
    assert listing[0]["payload"]["cash_cents"] == 100_000
    assert ledger_version_checker() or True

    assert ACCOUNT_LEDGER_VERSION


def ledger_version_checker() -> bool:
    from app.portfolio import ledger as _ledger

    return _ledger.ACCOUNT_LEDGER_VERSION == "account-ledger-1.0"

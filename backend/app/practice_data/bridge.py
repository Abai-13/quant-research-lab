"""Publish prepared practice rows into the immutable data-version model.

The practice simulator reads ``VersionView`` snapshots from app.data. Keeping
this bridge in practice_data avoids creating a parallel market-data reader.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.data.records import normalize_row, spec_for
from app.data.store import publish_data_version, write_records
from app.data.universe import (
    LIMIT_HISTORICAL_AVAILABILITY,
    LIMIT_SURVIVORSHIP_BIAS,
    PoolRule,
    generate_pool_snapshot,
    load_pool_snapshot,
)

from . import store


_SYNTHETIC_BRIDGE_LIMITATIONS = [
    "Synthetic universe is restricted to the explicitly selected securities; this is not a historical universe.",
    "Synthetic security list_date is anchored to the sample start date and is not an actual listing date.",
    "Survivorship bias remains unresolved and historical availability is unproven.",
]


def _canonical_code(code: str) -> tuple[str, str, str]:
    prefix, number = code.split(".", 1)
    exchange = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(prefix.lower())
    if exchange is None or not number.isdigit():
        raise ValueError(f"unsupported BaoStock symbol: {code}")
    return f"{number}.{exchange}", number, exchange


def publish_prepared_dataset(
    conn: sqlite3.Connection,
    legacy_version_id: str,
) -> dict[str, Any]:
    """Convert the downloaded snapshot to an app.data version for simulation.

    Only synthetic input can currently be bridged end to end. Real BaoStock
    data is refused until the requested calendar/security/action units are
    gathered and cross-checked; price-only data must not masquerade as ready.
    """
    legacy = store.get_version(conn, legacy_version_id)
    if legacy is None:
        raise ValueError("prepared source version disappeared")
    if legacy["source_code"] != "synthetic":
        return {
            "data_version_id": None,
            "pool_snapshot_id": None,
            "eligibility": "blocked",
            "limitations": ["BaoStock calendar, security listing interval and corporate actions are not validated in this preparation."],
        }

    suffix = hashlib.sha256(legacy_version_id.encode("utf-8")).hexdigest()[:16]
    source_code = "synthetic"
    # Synthetic samples contain only observed rows, so a requested weekend (or
    # sparse interval tail) is not itself a defensible pool snapshot date.
    # The validated sample calendar is derived directly from those rows.
    observed = conn.execute("SELECT MAX(trade_date) FROM practice_data_daily WHERE version_id=?", (legacy_version_id,)).fetchone()
    effective_as_of = str(observed[0]).replace("-", "") if observed and observed[0] else None
    # An already published bridge is immutable and safe to reuse.
    existing = conn.execute(
        "SELECT version_id FROM data_version WHERE source_code=? AND scope LIKE ? ORDER BY rowid DESC LIMIT 1",
        (source_code, f'%"practice_data_version_id":"{legacy_version_id}"%'),
    ).fetchone()
    if existing:
        version_id = str(existing[0])
        pool = conn.execute(
            "SELECT snapshot_id FROM pool_snapshot WHERE data_version_id=? AND as_of=? ORDER BY generated_at DESC LIMIT 1",
            (version_id, effective_as_of),
        ).fetchone()
        snapshot = load_pool_snapshot(conn, str(pool[0])) if pool else None
        quality_row = conn.execute("SELECT quality_summary FROM data_version WHERE version_id=?", (version_id,)).fetchone()
        try:
            decoded_quality = json.loads(str(quality_row[0])) if quality_row else {}
        except (TypeError, ValueError):
            decoded_quality = {}
        quality = decoded_quality if isinstance(decoded_quality, dict) else {}
        expected_codes = {_canonical_code(code)[0] for code in legacy["symbols"]}
        if (
            snapshot is not None
            and snapshot["data_version_id"] == version_id
            and snapshot["as_of"] == effective_as_of
            and set(snapshot["members"]) == expected_codes
            and {LIMIT_SURVIVORSHIP_BIAS, LIMIT_HISTORICAL_AVAILABILITY}.issubset(set(snapshot["limit_flags"]))
            and quality.get("synthetic_listing_anchor") == "sample_start_date_not_actual_ipo"
        ):
            return {
                "data_version_id": version_id,
                "pool_snapshot_id": str(pool[0]),
                "eligibility": "restricted_analysis",
                "limitations": legacy["limitations"] + _SYNTHETIC_BRIDGE_LIMITATIONS,
            }

    daily_rows = conn.execute(
        "SELECT code,trade_date,open,high,low,close,preclose,volume,amount"
        " FROM practice_data_daily WHERE version_id=? ORDER BY code,trade_date", (legacy_version_id,)
    ).fetchall()
    security_records: list[dict[str, Any]] = []
    daily_records: list[dict[str, Any]] = []
    calendar_dates: set[str] = set()
    for code in legacy["symbols"]:
        ts_code, symbol, exchange = _canonical_code(code)
        security_records.append(normalize_row("security", {
            "ts_code": ts_code, "symbol": symbol, "name": symbol,
            "market": "synthetic", "exchange": exchange,
            # This date lets the explicitly selected synthetic sample pass the
            # ordinary universe rules; it is not a claim about actual listing.
            "list_date": str(legacy["start_date"]).replace("-", ""),
            "delist_date": None, "list_status": "L",
        }))
    for row in daily_rows:
        ts_code, _symbol, _exchange = _canonical_code(str(row[0]))
        day = str(row[1]).replace("-", "")
        calendar_dates.add(day)
        daily_records.append(normalize_row("daily", {
            "ts_code": ts_code, "trade_date": day,
            "open": row[2], "high": row[3], "low": row[4], "close": row[5],
            "pre_close": row[6], "vol": float(row[7] or 0) / 100.0,
            "amount": float(row[8] or 0) / 1000.0,
        }))
    calendar_records = [normalize_row("calendar", {"exchange": "SSE", "cal_date": day, "is_open": 1, "pretrade_date": None}) for day in sorted(calendar_dates)]

    all_ids: dict[str, list[int]] = {}
    for category, records in (("security", security_records), ("calendar", calendar_records), ("daily", daily_records)):
        outcome = write_records(conn, category, records, source_code=source_code, batch_id=f"practice-data-{suffix}")
        all_ids[category] = outcome.record_ids
    members: dict[str, list[tuple[int, str, str]]] = {}
    for category, ids in all_ids.items():
        if not ids:
            continue
        spec = spec_for(category)
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT id,content_hash,{','.join(spec.key_fields)} FROM {spec.table} WHERE id IN ({placeholders})", ids).fetchall()
        entries = []
        from app.data.records import logical_key
        for row in rows:
            record = dict(zip(spec.key_fields, row[2:]))
            entries.append((int(row[0]), logical_key(category, record, source_code), str(row[1])))
        members[category] = entries
    version = publish_data_version(
        conn, source_code=source_code,
        scope={"kind": "practice_data_prepare", "bridge_version": 2, "practice_data_version_id": legacy_version_id},
        categories=sorted(members), rule_version="practice-data-bridge-2",
        quality_summary={
            "eligibility": "restricted_analysis",
            "corporate_actions": "unavailable",
            "calendar": "weekday_sample",
            "universe_scope": "explicit_selected_synthetic_securities_only",
            "synthetic_listing_anchor": "sample_start_date_not_actual_ipo",
            "survivorship_bias": "unresolved",
            "historical_availability": "unproven",
            "limitations": list(_SYNTHETIC_BRIDGE_LIMITATIONS),
        },
        members=members, window_mode="full", window_start=legacy["start_date"].replace("-", ""),
    )
    if effective_as_of is None or not calendar_dates or effective_as_of not in calendar_dates:
        return {
            "data_version_id": version["version_id"], "pool_snapshot_id": None,
            "eligibility": "blocked", "limitations": legacy["limitations"] + _SYNTHETIC_BRIDGE_LIMITATIONS + [
                "No validated daily sample rows are available to anchor a pool snapshot."
            ],
        }
    snapshot = generate_pool_snapshot(
        conn, as_of=effective_as_of, data_version_id=version["version_id"], source_code=source_code,
        rule=PoolRule(min_history_trade_days=1, liquidity_window_days=1, min_avg_amount_cny=0),
        ts_codes=[_canonical_code(code)[0] for code in legacy["symbols"]],
    )
    conn.commit()
    return {
        "data_version_id": version["version_id"],
        "pool_snapshot_id": snapshot.get("snapshot_id"),
        "effective_as_of": effective_as_of,
        "eligibility": "restricted_analysis",
        "limitations": legacy["limitations"] + _SYNTHETIC_BRIDGE_LIMITATIONS + [
            "Synthetic weekday calendar and no corporate actions; use only for restricted local research-path validation."
        ],
    }

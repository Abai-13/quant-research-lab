"""Conservative cross-version exposure checks keyed by normalized security/date."""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Iterable

from .service import now

PURPOSES = {"exploration", "training", "evaluation_reserved", "evaluation_revealed", "unknown_history"}


def normalize_symbol(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("证券代码无效")
    text = value.strip().upper()
    tushare = re.fullmatch(r"(\d{6})\.(SH|SZ)", text)
    baostock = re.fullmatch(r"(SH|SZ)\.(\d{6})", text)
    if tushare:
        code, exchange = tushare.groups()
    elif baostock:
        exchange, code = baostock.groups()
    else:
        raise ValueError("证券代码必须为 Tushare 6位.SH/.SZ 或 BaoStock sh./sz. 格式")
    valid_prefix = code[0] in ("569" if exchange == "SH" else "0123")
    if not valid_prefix:
        raise ValueError("证券代码与交易所后缀不匹配或属于未知交易所")
    return f"{code}.{exchange}"


def register_exposure(conn, *, owner_type: str, owner_id: str, source_instance_id: str,
                      source_code: str, data_version_id: str, symbols: Iterable[str],
                      dates: Iterable[str], purpose: str, status: str = "known") -> int:
    if purpose not in PURPOSES:
        raise ValueError("用途无效")
    rows = [("exp_" + hashlib.sha256(f"{owner_type}|{owner_id}|{source_instance_id}|{source_code}|{data_version_id}|{s}|{d}|{purpose}".encode()).hexdigest()[:32],
             owner_type, owner_id, source_instance_id, source_code, data_version_id, normalize_symbol(s), d,
             purpose, status, now()) for s in set(symbols) for d in set(dates)]
    if not rows:
        return 0
    before = conn.total_changes
    with conn:
        conn.executemany("INSERT OR IGNORE INTO exposure_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        return conn.total_changes - before


def capture_practice_exposure(settings, practice_conn, session) -> int:
    """Record facts returned by practice views; fail closed if audit cannot persist."""
    from .store import research_connect

    snapshot = (session.get("settings") or {}).get("pool_snapshot_id")
    symbols = [str(row[0]) for row in practice_conn.execute(
        "SELECT ts_code FROM pool_member WHERE snapshot_id=? ORDER BY ts_code", (snapshot,)
    )] if snapshot else []
    dates = [str(row[0]) for row in practice_conn.execute(
        "SELECT DISTINCT cal_date FROM trade_calendar WHERE source_code=? AND is_open=1 AND cal_date BETWEEN ? AND ? ORDER BY cal_date",
        (str(session["source_code"]), str(session["start_date"]), str(session["revealed_date"])),
    )]
    research = research_connect(settings)
    try:
        if not symbols or not dates:
            # Unknown past visibility can never be promoted to first-use eligibility.
            timestamp = now()
            with research:
                for day in dates or [str(session.get("revealed_date", "unknown"))]:
                    research.execute("INSERT OR IGNORE INTO exposure_history VALUES(?,?,?,?,?,?,?)",
                        ("hist_" + hashlib.sha256(f"{session['practice_id']}|{day}".encode()).hexdigest()[:32],
                         "*", day, "unknown_history", "unknown", "legacy practice visibility is unprovable", timestamp))
            return 0
        prior = {str(row[0]) for row in research.execute(
            "SELECT DISTINCT trade_date FROM exposure_ledger WHERE owner_type='practice' AND owner_id=? AND data_version_id=? AND purpose='exploration'",
            (str(session["practice_id"]), str(session["data_version_id"])),
        )}
        dates = [day for day in dates if day not in prior]
        if not dates:
            return 0
        source = hashlib.sha256(str(settings.runtime_dir.resolve()).encode("utf-8")).hexdigest()
        return register_exposure(research, owner_type="practice", owner_id=str(session["practice_id"]),
            source_instance_id=source, source_code=str(session["source_code"]),
            data_version_id=str(session["data_version_id"]), symbols=symbols, dates=dates,
            purpose="exploration")
    finally:
        research.close()


def capture_market_query(settings, session, symbol: str, dates: Iterable[str]) -> int:
    from .store import research_connect

    research = research_connect(settings)
    try:
        return register_exposure(research, owner_type="practice", owner_id=str(session["practice_id"]),
            source_instance_id=hashlib.sha256(str(settings.runtime_dir.resolve()).encode("utf-8")).hexdigest(),
            source_code=str(session["source_code"]), data_version_id=str(session["data_version_id"]),
            symbols=[symbol], dates=dates, purpose="exploration")
    finally:
        research.close()


def assess_exposure(conn, *, symbols: Iterable[str], dates: Iterable[str]) -> dict:
    normalized = {normalize_symbol(s) for s in symbols}
    day_set = set(dates)
    if not normalized or not day_set:
        return {"eligibility": "blocked_unknown_history", "overlap_count": 0, "unknown_count": 0,
                "limitations": ["没有可核查的规范化证券和日期范围。"]}
    # A TEMP key table avoids SQLite's host-parameter ceiling for the supported
    # maximum of 20 securities × 252 trading days (5,040 composite keys).
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS research_exposure_query (symbol TEXT NOT NULL, alias_symbol TEXT NOT NULL, trade_date TEXT NOT NULL, PRIMARY KEY(symbol,trade_date)) WITHOUT ROWID")
    keys = [(symbol, f"{symbol.rsplit('.', 1)[1]}.{symbol.split('.', 1)[0]}", day) for symbol in sorted(normalized) for day in sorted(day_set)]
    with conn:
        conn.execute("DELETE FROM research_exposure_query")
        conn.executemany("INSERT INTO research_exposure_query(symbol,alias_symbol,trade_date) VALUES(?,?,?)", keys)
    known = conn.execute("SELECT DISTINCT q.symbol,q.trade_date FROM exposure_ledger e JOIN research_exposure_query q ON q.trade_date=e.trade_date AND upper(e.symbol) IN (q.symbol,q.alias_symbol) WHERE e.purpose IN ('exploration','training','evaluation_revealed','unknown_history')").fetchall()
    unknown = conn.execute("SELECT q.symbol,q.trade_date FROM research_exposure_query q WHERE EXISTS(SELECT 1 FROM exposure_history h WHERE h.trade_date=q.trade_date AND h.status IN ('unknown','seen') AND upper(h.symbol) IN (q.symbol,q.alias_symbol,'*'))").fetchall()
    count = len(known)
    unknown_count = len(unknown)
    return {"eligibility": "eligible_unseen_in_application" if count == 0 and unknown_count == 0 else "blocked_seen_or_unknown",
            "overlap_count": count, "unknown_count": unknown_count,
            "scope": "本应用台账范围；不表示人或模型从未见过这些历史数据。"}

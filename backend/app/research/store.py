"""SQLite persistence for append-only research and exposure records."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.config import Settings

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS research_record (record_id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL, current_revision_id TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS research_revision (revision_id TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES research_record(record_id), parent_revision_id TEXT, body_json TEXT NOT NULL, body_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_research_revision_record ON research_revision(record_id, created_at)",
    "CREATE TABLE IF NOT EXISTS research_plan (plan_id TEXT PRIMARY KEY, plan_revision INTEGER NOT NULL, status TEXT NOT NULL, plan_json TEXT NOT NULL, plan_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS research_experiment (experiment_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, status TEXT NOT NULL, status_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS research_operation (operation_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL, generation INTEGER NOT NULL, progress_json TEXT NOT NULL, checkpoint_json TEXT NOT NULL, usage_json TEXT NOT NULL, limits_json TEXT NOT NULL, resumable INTEGER NOT NULL, stop_reason TEXT, idempotency_key TEXT NOT NULL UNIQUE, request_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS research_operation_attempt (operation_id TEXT NOT NULL, attempt INTEGER NOT NULL, generation INTEGER NOT NULL, status TEXT NOT NULL, checkpoint_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(operation_id,attempt))",
    "CREATE TABLE IF NOT EXISTS research_operation_resume_key (operation_id TEXT NOT NULL, idempotency_key TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, generation INTEGER NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS research_reference_journal (generation INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS exposure_ledger (exposure_id TEXT PRIMARY KEY, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, source_instance_id TEXT NOT NULL, source_code TEXT NOT NULL, data_version_id TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(owner_type,owner_id,source_instance_id,source_code,data_version_id,symbol,trade_date,purpose))",
    "CREATE INDEX IF NOT EXISTS idx_exposure_symbol_date ON exposure_ledger(symbol, trade_date, purpose)",
    "CREATE TABLE IF NOT EXISTS exposure_history (history_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, trade_date TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(symbol,trade_date,purpose))",
    "CREATE TABLE IF NOT EXISTS research_meta (meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS research_idempotency (operation TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(operation,idempotency_key))",
    "CREATE INDEX IF NOT EXISTS idx_research_record_kind_time ON research_record(kind, created_at)",
    "CREATE TRIGGER IF NOT EXISTS research_revision_reference AFTER INSERT ON research_revision BEGIN INSERT INTO research_reference_journal(entity_type,entity_id,created_at) VALUES('research_record',NEW.record_id,NEW.created_at); END",
    "CREATE TRIGGER IF NOT EXISTS research_plan_reference AFTER INSERT ON research_plan BEGIN INSERT INTO research_reference_journal(entity_type,entity_id,created_at) VALUES('research_plan',NEW.plan_id,NEW.created_at); END",
    "CREATE TRIGGER IF NOT EXISTS research_exposure_reference AFTER INSERT ON exposure_ledger BEGIN INSERT INTO research_reference_journal(entity_type,entity_id,created_at) VALUES('exposure',NEW.exposure_id,NEW.created_at); END",
    "CREATE TRIGGER IF NOT EXISTS research_experiment_reference AFTER INSERT ON research_experiment BEGIN INSERT INTO research_reference_journal(entity_type,entity_id,created_at) VALUES('research_experiment',NEW.experiment_id,NEW.created_at); END",
    "CREATE TRIGGER IF NOT EXISTS research_operation_reference AFTER INSERT ON research_operation BEGIN INSERT INTO research_reference_journal(entity_type,entity_id,created_at) VALUES('research_experiment',NEW.operation_id,NEW.created_at); END",
)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def research_path(settings: Settings) -> Path:
    return settings.runtime_dir.resolve() / "research" / "research.sqlite3"


def research_connect(settings: Settings) -> sqlite3.Connection:
    path = research_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    for statement in SCHEMA:
        conn.execute(statement)
    # Additive generation fence for the legacy experiment row. Existing attempt
    # data stays in place; no migration rewrites prior result payloads.
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(research_experiment)")}
    if "generation" not in columns:
        conn.execute("ALTER TABLE research_experiment ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
    if conn.execute("SELECT 1 FROM research_reference_journal LIMIT 1").fetchone() is None:
        # Establish a monotonic baseline for rows predating the journal triggers.
        for table, key in (("research_revision", "record_id"), ("research_plan", "plan_id"),
                           ("exposure_ledger", "exposure_id"), ("research_experiment", "experiment_id"),
                           ("research_operation", "operation_id")):
            conn.execute(f"INSERT INTO research_reference_journal(entity_type,entity_id,created_at) SELECT ?,{key},created_at FROM {table}", (table,))
        conn.commit()
    conn.commit()
    seed_legacy_exposures(conn, settings)
    return conn


def seed_legacy_exposures(conn: sqlite3.Connection, settings: Settings) -> None:
    """Import pre-ledger visible history as unknown, never as first-use eligible."""
    from hashlib import sha256
    from .exposure import normalize_symbol

    marker = conn.execute("SELECT 1 FROM research_meta WHERE meta_key='legacy_exposure_imported'").fetchone()
    practice_path = settings.runtime_dir.resolve() / "practice" / "app.db"
    if marker or not practice_path.is_file():
        return
    source = sqlite3.connect(practice_path.as_uri() + "?mode=ro", uri=True, timeout=3.0)
    source.row_factory = sqlite3.Row
    try:
        present = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='practice_session'").fetchone()
        if not present:
            return
        sessions = source.execute("SELECT practice_id,start_date,revealed_date,source_code,settings_json FROM practice_session").fetchall()
        timestamp = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        entries = []
        for session in sessions:
            settings_json = json.loads(str(session["settings_json"]))
            snapshot_id = settings_json.get("pool_snapshot_id")
            symbols = [str(row[0]) for row in source.execute("SELECT ts_code FROM pool_member WHERE snapshot_id=?", (snapshot_id,))] if snapshot_id else []
            dates = [str(row[0]) for row in source.execute("SELECT DISTINCT cal_date FROM trade_calendar WHERE source_code=? AND is_open=1 AND cal_date BETWEEN ? AND ?", (str(session["source_code"]), str(session["start_date"]), str(session["revealed_date"])))]
            if not dates:
                dates = [str(session["revealed_date"])]
            if not symbols:
                symbols = ["*"]
            for symbol in symbols:
                try:
                    normalized = normalize_symbol(symbol)
                except ValueError:
                    normalized = "*"
                for day in dates:
                    key = sha256(f"legacy|{normalized}|{day}".encode("utf-8")).hexdigest()
                    entries.append(("hist_" + key[:32], normalized, day, "unknown_history", "unknown", "legacy simulation exposure was not auditable", timestamp))
        with conn:
            conn.executemany("INSERT OR IGNORE INTO exposure_history VALUES(?,?,?,?,?,?,?)", entries)
            conn.execute("INSERT INTO research_meta(meta_key,meta_value) VALUES('legacy_exposure_imported',?)", (timestamp,))
    finally:
        source.close()


def maintenance_reference_snapshot(settings: Settings) -> dict[str, Any]:
    """Read actual cache/version references; any unreadable state fails closed."""
    path = research_path(settings)
    if not path.is_file():
        raise sqlite3.OperationalError("research_reference_store_missing")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        generation = conn.execute("SELECT COALESCE(MAX(generation),0) FROM research_reference_journal").fetchone()[0]
        refs: dict[tuple[str, str, str], dict[str, Any]] = {}
        cache_ids: set[str] = set()
        entity_ids: dict[str, set[str]] = {"research_record": set(), "research_plan": set(),
                                           "research_experiment": set(), "exposure": set()}
        def add(owner_type: str, owner_id: str, source_code: Any, version_id: Any,
                symbols: Any = None, interval: Any = None) -> None:
            if not isinstance(source_code, str) or not source_code or not isinstance(version_id, str) or not version_id:
                raise ValueError("research_cache_reference_incomplete")
            cache_ids.add(version_id)
            if owner_type in entity_ids:
                entity_ids[owner_type].add(owner_id)
            key = (owner_type, owner_id, version_id)
            item = refs.setdefault(key, {"owner_type": owner_type, "owner_id": owner_id,
                                         "source_code": source_code, "data_version_id": version_id,
                                         "symbols": set(), "intervals": set()})
            if isinstance(symbols, list): item["symbols"].update(x for x in symbols if isinstance(x, str))
            if isinstance(interval, dict):
                start, end = interval.get("start_date"), interval.get("end_date")
                if isinstance(start, str) and isinstance(end, str): item["intervals"].add((start, end))
        for row in conn.execute("SELECT record_id,body_json FROM research_revision"):
            body = json.loads(row["body_json"])
            refs = body.get("source_refs", [])
            if not isinstance(refs, list):
                raise ValueError("research_source_refs_invalid")
            for ref in refs:
                if not isinstance(ref, dict) or not ref.get("owner_type") or not ref.get("owner_id"):
                    raise ValueError("research_source_ref_invalid")
                add("research_record", row["record_id"], ref.get("source_code"), ref.get("data_version_id"),
                    ref.get("symbols"), ref.get("interval"))
        for row in conn.execute("SELECT plan_id,plan_json FROM research_plan"):
            plan = json.loads(row["plan_json"])
            source = plan.get("source_context")
            if not isinstance(source, dict):
                raise ValueError("research_plan_source_context_invalid")
            add("research_plan", row["plan_id"], source.get("source_code"), source.get("data_version_id"),
                plan.get("symbols"), {"start_date": (plan.get("training_dates") or [None])[0],
                                      "end_date": (plan.get("evaluation_dates") or [None])[-1]})
        for row in conn.execute("SELECT exposure_id,owner_type,owner_id,source_code,data_version_id,symbol,trade_date FROM exposure_ledger"):
            add("exposure", str(row["exposure_id"]), row["source_code"], row["data_version_id"],
                [row["symbol"]] if row["symbol"] != "*" else [], {"start_date": row["trade_date"], "end_date": row["trade_date"]})
        for row in conn.execute("SELECT e.experiment_id,p.plan_json FROM research_experiment e JOIN research_plan p ON p.plan_id=e.plan_id"):
            plan = json.loads(row["plan_json"])
            source = plan.get("source_context")
            if not isinstance(source, dict):
                raise ValueError("research_operation_plan_invalid")
            add("research_experiment", row["experiment_id"], source.get("source_code"), source.get("data_version_id"),
                plan.get("symbols"), {"start_date": (plan.get("training_dates") or [None])[0],
                                      "end_date": (plan.get("evaluation_dates") or [None])[-1]})
        result = []
        for item in refs.values():
            item["symbols"] = sorted(item["symbols"])
            item["intervals"] = [{"start_date": a, "end_date": b} for a, b in sorted(item["intervals"])]
            result.append(item)
        references = {"cache": sorted(cache_ids), **{kind: sorted(values) for kind, values in entity_ids.items()}}
        return {"generation": int(generation), "references": references,
                "reference_details": sorted(result, key=lambda x: (x["owner_type"], x["owner_id"], x["data_version_id"]))}
    finally:
        conn.close()

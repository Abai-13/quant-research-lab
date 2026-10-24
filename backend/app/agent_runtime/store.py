"""Independent SQLite task store. Raw model prompts and responses are never persisted."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_JSON = 256_000
MAX_EVENTS_PER_TASK = 500


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class AgentStore:
    """Task records and bounded event history in runtime/agent/tasks.sqlite3."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def initialize(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL, practice_id TEXT, scope TEXT NOT NULL,
                    status TEXT NOT NULL, goal TEXT NOT NULL, config_version TEXT NOT NULL,
                    context_json TEXT NOT NULL, context_fingerprint TEXT NOT NULL,
                    consent_digest TEXT NOT NULL, limits_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    parent_task_id TEXT, stop_reason TEXT, usage_json TEXT NOT NULL,
                    result_json TEXT, stale INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS task_scope_idx ON tasks(practice_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    task_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
                    created_at TEXT NOT NULL, tool_name TEXT, summary TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL, reason TEXT,
                    PRIMARY KEY(task_id, seq), FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
            """)
            conn.execute("PRAGMA foreign_keys=ON")
            rows = conn.execute("SELECT task_id FROM tasks WHERE status IN ('queued','running')").fetchall()
            for (task_id,) in rows:
                now = utc_now()
                conn.execute("UPDATE tasks SET status='interrupted',stop_reason='process_restarted',updated_at=? WHERE task_id=?", (now, task_id))
                self._append_event(conn, task_id, "task_interrupted", "Process restarted before completion.", reason="process_restarted")
            return len(rows)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _append_event(conn: sqlite3.Connection, task_id: str, kind: str, summary: str,
                      tool_name: str | None = None, evidence_ids: list[str] | None = None,
                      reason: str | None = None) -> dict[str, Any]:
        row = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE task_id=?", (task_id,)).fetchone()
        seq = int(row[0]) + 1
        conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)", (task_id, seq, kind, utc_now(), tool_name,
                     summary[:1000], canonical((evidence_ids or [])[:100]), reason))
        conn.execute("DELETE FROM events WHERE task_id=? AND seq <= ?", (task_id, max(0, seq - MAX_EVENTS_PER_TASK)))
        return {"seq": seq, "kind": kind, "created_at": utc_now(), "tool_name": tool_name,
                "summary": summary[:1000], "evidence_ids": (evidence_ids or [])[:100], "reason": reason}

    def create(self, *, key: str, payload: dict[str, Any], practice_id: str | None, scope: str,
               goal: str, config_version: str, context: dict[str, Any], consent_digest: str,
               limits: dict[str, Any], parent_task_id: str | None = None) -> tuple[dict[str, Any], bool]:
        encoded = canonical(payload)
        if len(encoded.encode("utf-8")) > MAX_JSON:
            raise ValueError("request_too_large")
        payload_hash = digest(payload)
        key_hash = digest(key)
        now = utc_now()
        task_id = str(uuid.uuid4())
        context_public = {k: context.get(k) for k in ("scope", "practice_id", "revision", "revealed_date", "data_version_id", "source_code", "context_fingerprint", "limitations")}
        if len(canonical(context_public).encode("utf-8")) > 32_000:
            raise ValueError("context_too_large")
        context_fingerprint = str(context.get("context_fingerprint") or digest(context_public))
        with self._lock, self._connect() as conn:
            existing = conn.execute("SELECT * FROM tasks WHERE idempotency_key=?", (key_hash,)).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflict("idempotency_conflict")
                return self._view(existing), False
            conn.execute("""INSERT INTO tasks(task_id,idempotency_key,payload_hash,practice_id,scope,status,goal,
                config_version,context_json,context_fingerprint,consent_digest,limits_json,created_at,updated_at,
                parent_task_id,usage_json) VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?)""",
                (task_id,key_hash,payload_hash,practice_id,scope,goal,config_version,canonical(context_public),context_fingerprint,
                 consent_digest,canonical(limits),now,now,parent_task_id,canonical({"input_tokens":None,"output_tokens":None,"total_tokens":None,
                 "model_calls":0,"tool_calls":0,"elapsed_ms":0,"estimated_cost":None})))
            self._append_event(conn,task_id,"task_started","Task queued.")
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?",(task_id,)).fetchone()
            return self._view(row), True

    def existing(self, key: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve idempotency before rechecking mutable context or provider settings."""
        with self._connect() as conn:
            row=conn.execute("SELECT * FROM tasks WHERE idempotency_key=?",(digest(key),)).fetchone()
            if row is None:return None
            if row["payload_hash"] != digest(payload):raise IdempotencyConflict("idempotency_conflict")
            return self._view(row)

    @staticmethod
    def _view(row: sqlite3.Row) -> dict[str, Any]:
        context = json.loads(row["context_json"])
        return {"task_id":row["task_id"],"practice_id":row["practice_id"],"scope":row["scope"],"status":row["status"],
          "goal":row["goal"],"config_version":row["config_version"],"context":context,"created_at":row["created_at"],
          "updated_at":row["updated_at"],"parent_task_id":row["parent_task_id"],"stop_reason":row["stop_reason"],
          "usage":json.loads(row["usage_json"]),"result":json.loads(row["result_json"]) if row["result_json"] else None,
          "stale":bool(row["stale"]),"consent_digest":row["consent_digest"],"limits":json.loads(row["limits_json"]),"attempt":row["attempt"]}

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row=conn.execute("SELECT * FROM tasks WHERE task_id=?",(task_id,)).fetchone()
            return self._view(row) if row else None

    def list(self, practice_id: str | None, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        limit=max(1,min(limit,100)); offset=max(0,min(offset,1_000_000))
        with self._connect() as conn:
            where=" WHERE practice_id=?" if practice_id is not None else ""
            args=(practice_id,) if practice_id is not None else ()
            total=int(conn.execute("SELECT COUNT(*) FROM tasks"+where,args).fetchone()[0])
            rows=conn.execute("SELECT * FROM tasks"+where+" ORDER BY created_at DESC LIMIT ? OFFSET ?",(*args,limit,offset)).fetchall()
            return [self._view(r) for r in rows],total

    def events(self, task_id: str, after_seq: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        limit=max(1,min(limit,200))
        with self._connect() as conn:
            rows=conn.execute("SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",(task_id,max(0,after_seq),limit)).fetchall()
            out=[{"seq":r["seq"],"kind":r["kind"],"created_at":r["created_at"],"tool_name":r["tool_name"],"summary":r["summary"],"evidence_ids":json.loads(r["evidence_ids_json"]),"reason":r["reason"]} for r in rows]
            return out, (out[-1]["seq"] if out else max(0,after_seq))

    def append_event(self, task_id: str, kind: str, summary: str, tool_name: str | None = None,
                     evidence_ids: list[str] | None = None, reason: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            self._append_event(conn,task_id,kind,summary,tool_name,evidence_ids,reason)

    def transition(self, task_id: str, expected: tuple[str,...], status: str, *, reason: str | None = None,
                   result: dict[str,Any] | None = None, usage: dict[str,Any] | None = None, stale: bool | None = None) -> bool:
        if not expected:return False
        with self._lock,self._connect() as conn:
            fields=["status=?","stop_reason=?","updated_at=?"]; vals=[status,reason,utc_now()]
            if result is not None: fields.append("result_json=?"); vals.append(canonical(result))
            if usage is not None: fields.append("usage_json=?"); vals.append(canonical(usage))
            if stale is not None: fields.append("stale=?"); vals.append(int(stale))
            placeholders=",".join("?" for _ in expected)
            vals.extend([task_id,*expected])
            cur=conn.execute("UPDATE tasks SET "+",".join(fields)+f" WHERE task_id=? AND status IN ({placeholders})",vals)
            return cur.rowcount==1

    def update_usage(self, task_id: str, usage: dict[str,Any]) -> None:
        with self._lock,self._connect() as conn: conn.execute("UPDATE tasks SET usage_json=?,updated_at=? WHERE task_id=? AND status='running'",(canonical(usage),utc_now(),task_id))

    def start(self, task_id: str) -> bool:
        with self._lock,self._connect() as conn:
            cur=conn.execute("UPDATE tasks SET status='running',updated_at=? WHERE task_id=? AND status='queued'",(utc_now(),task_id))
            return cur.rowcount==1

    def cancel(self, task_id: str) -> dict[str,Any] | None:
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT * FROM tasks WHERE task_id=?",(task_id,)).fetchone()
            if row is None: return None
            if row["status"] in ("queued","running"):
                cur=conn.execute("UPDATE tasks SET status='cancelled',stop_reason='user_cancelled',updated_at=? WHERE task_id=? AND status IN ('queued','running')",(utc_now(),task_id))
                if cur.rowcount==1:self._append_event(conn,task_id,"task_cancelled","Task cancelled by user.",reason="user_cancelled")
            return self._view(conn.execute("SELECT * FROM tasks WHERE task_id=?",(task_id,)).fetchone())

    def delete(self, task_id: str) -> bool:
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT status FROM tasks WHERE task_id=?",(task_id,)).fetchone()
            if row is None:return False
            if row["status"] in ("queued","running"):raise ActiveTaskError("task_active")
            conn.execute("DELETE FROM tasks WHERE task_id=?",(task_id,)); return True

    def mark_stale(self, task_id: str) -> None:
        with self._lock,self._connect() as conn: conn.execute("UPDATE tasks SET stale=1,updated_at=? WHERE task_id=?",(utc_now(),task_id))


class IdempotencyConflict(Exception): pass
class ActiveTaskError(Exception): pass

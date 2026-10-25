"""Independent SQLite task store. Raw model prompts and responses are never persisted."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone, timedelta
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
                    attempt INTEGER NOT NULL DEFAULT 1,
                    generation INTEGER NOT NULL DEFAULT 1, checkpoint_json TEXT NOT NULL DEFAULT '{"sequence":0,"steps":[],"validation":"not_checked"}',
                    resumable INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_until TEXT,
                    resume_key_hash TEXT, resume_payload_hash TEXT, credential_generation TEXT
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
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
            for name, ddl in (("generation", "INTEGER NOT NULL DEFAULT 1"),
                              ("checkpoint_json", "TEXT NOT NULL DEFAULT '{\"sequence\":0,\"steps\":[],\"validation\":\"not_checked\"}'"),
                              ("resumable", "INTEGER NOT NULL DEFAULT 0"),
                              ("lease_owner", "TEXT"), ("lease_until", "TEXT"),
                              ("resume_key_hash", "TEXT"), ("resume_payload_hash", "TEXT"),
                              ("credential_generation", "TEXT")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
            rows = conn.execute("SELECT task_id,checkpoint_json FROM tasks WHERE status IN ('queued','running')").fetchall()
            for task_id, _checkpoint_json in rows:
                now = utc_now()
                checkpoint = json.loads(conn.execute("SELECT checkpoint_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0])
                usage = json.loads(conn.execute("SELECT usage_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0])
                steps = checkpoint.get("steps", [])
                # A pending external model call may have been accepted remotely;
                # after a crash it must never be sent again automatically.
                changed = False
                for step in steps:
                    if step.get("state") == "pending" and step.get("kind") == "external_model":
                        step.update({"state": "unknown", "reason": "external_result_unknown"})
                        changed = True
                if changed:
                    checkpoint["sequence"] = int(checkpoint.get("sequence", 0)) + 1
                can_resume = not checkpoint.get("steps") and not usage.get("model_calls", 0) and not usage.get("tool_calls", 0)
                conn.execute("UPDATE tasks SET status='interrupted',stop_reason='process_restarted',updated_at=?,generation=generation+1,checkpoint_json=?,resumable=?,lease_owner=NULL,lease_until=NULL WHERE task_id=?", (now, canonical(checkpoint), int(can_resume), task_id))
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
               limits: dict[str, Any], parent_task_id: str | None = None,
               credential_generation: str | int | None = None) -> tuple[dict[str, Any], bool]:
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
                parent_task_id,usage_json,credential_generation) VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id,key_hash,payload_hash,practice_id,scope,goal,config_version,canonical(context_public),context_fingerprint,
                 consent_digest,canonical(limits),now,now,parent_task_id,canonical({"input_tokens":None,"output_tokens":None,"total_tokens":None,
                 "model_calls":0,"tool_calls":0,"elapsed_ms":0,"estimated_cost":None}),
                 str(credential_generation) if credential_generation is not None else None))
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
        result = {"task_id":row["task_id"],"operation_id":row["task_id"],"kind":"agent_task",
          "practice_id":row["practice_id"],"scope":row["scope"],"status":row["status"],
          "goal":row["goal"],"config_version":row["config_version"],"context":context,"created_at":row["created_at"],
          "updated_at":row["updated_at"],"parent_task_id":row["parent_task_id"],"stop_reason":row["stop_reason"],
          "usage":json.loads(row["usage_json"]),"result":json.loads(row["result_json"]) if row["result_json"] else None,
          "stale":bool(row["stale"]),"consent_digest":row["consent_digest"],"limits":json.loads(row["limits_json"]),"attempt":row["attempt"],
          "generation":row["generation"],"checkpoint":json.loads(row["checkpoint_json"]),"resumable":bool(row["resumable"]),
          "credential_generation":row["credential_generation"]}
        return result

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
                   result: dict[str,Any] | None = None, usage: dict[str,Any] | None = None, stale: bool | None = None,
                   generation: int | None = None) -> bool:
        if not expected:return False
        with self._lock,self._connect() as conn:
            fields=["status=?","stop_reason=?","updated_at=?"]; vals=[status,reason,utc_now()]
            if result is not None: fields.append("result_json=?"); vals.append(canonical(result))
            if usage is not None: fields.append("usage_json=?"); vals.append(canonical(usage))
            if stale is not None: fields.append("stale=?"); vals.append(int(stale))
            placeholders=",".join("?" for _ in expected)
            vals.extend([task_id,*expected])
            where=f" WHERE task_id=? AND status IN ({placeholders})"
            if generation is not None:where+=" AND generation=?"; vals.append(generation)
            cur=conn.execute("UPDATE tasks SET "+",".join(fields)+where,vals)
            return cur.rowcount==1

    def update_usage(self, task_id: str, usage: dict[str,Any], generation: int | None = None) -> None:
        with self._lock,self._connect() as conn:
            sql="SELECT usage_json FROM tasks WHERE task_id=? AND status='running'"
            args: tuple[Any,...]=(task_id,)
            if generation is not None:sql+=" AND generation=?"; args+=(generation,)
            row=conn.execute(sql,args).fetchone()
            if row is None:return
            prior=json.loads(row[0])
            prior_calls=int(prior.get("model_calls") or 0)
            # Counts and elapsed time are cumulative across attempts. Unknown
            # measurements remain unknown rather than being reset to zero.
            for key in ("input_tokens","output_tokens","total_tokens","estimated_cost"):
                if (prior_calls and prior.get(key) is None) or usage.get(key) is None:
                    usage[key]=None
                else:
                    usage[key]=max(prior.get(key) or 0,usage.get(key) or 0)
            for key in ("model_calls","tool_calls","elapsed_ms"):
                usage[key]=max(int(prior.get(key,0)),int(usage.get(key,0)))
            sql="UPDATE tasks SET usage_json=?,updated_at=? WHERE task_id=? AND status='running'"
            args=(canonical(usage),utc_now(),task_id)
            if generation is not None:sql+=" AND generation=?"; args+=(generation,)
            conn.execute(sql,args)

    def persist_elapsed(self, task_id: str, generation: int, aggregate_elapsed_ms: int,
                        attempt_elapsed_ms: int) -> None:
        """Persist final elapsed time when cancellation already fenced the run."""
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT generation,status,usage_json FROM tasks WHERE task_id=?",(task_id,)).fetchone()
            # cancel() advances the task generation before cancelling its
            # coroutine. Accept only that exact fenced cancellation, so a late
            # prior attempt cannot write into a resumed generation.
            if row is None or row["status"]!="cancelled" or int(row["generation"])!=int(generation)+1:return
            usage=json.loads(row["usage_json"])
            usage["elapsed_ms"]=max(int(usage.get("elapsed_ms") or 0),int(aggregate_elapsed_ms))
            usage["attempt_elapsed_ms"]=int(attempt_elapsed_ms)
            conn.execute("UPDATE tasks SET usage_json=?,updated_at=? WHERE task_id=? AND generation=? AND status='cancelled'",
                         (canonical(usage),utc_now(),task_id,int(generation)+1))

    def checkpoint_step(self, task_id: str, generation: int, step_id: str, state: str,
                        *, kind: str = "local", result_digest: str | None = None) -> bool:
        """Record a stable logical step under the task generation fence."""
        if state not in {"pending", "committed", "unknown"} or not step_id or len(step_id)>160:
            return False
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT checkpoint_json FROM tasks WHERE task_id=? AND generation=? AND status IN ('queued','running')",(task_id,generation)).fetchone()
            if row is None:return False
            checkpoint=json.loads(row[0]); steps=checkpoint.setdefault("steps",[])
            prior=next((item for item in steps if item.get("step_id")==step_id),None)
            if prior and prior.get("state")=="committed":return state=="committed"
            if prior is None:
                prior={"step_id":step_id,"attempt":1}; steps.append(prior)
            prior.update({"state":state,"kind":kind})
            if state=="committed":
                prior["result_digest"]=result_digest or digest({"step_id":step_id,"state":"committed"})
                prior.pop("reason",None)
            elif state=="unknown":prior["reason"]="external_result_unknown"
            checkpoint["sequence"]=int(checkpoint.get("sequence",0))+1
            checkpoint["validation"]="pending"
            cur=conn.execute("UPDATE tasks SET checkpoint_json=?,updated_at=? WHERE task_id=? AND generation=? AND status IN ('queued','running')",
                             (canonical(checkpoint),utc_now(),task_id,generation))
            return cur.rowcount==1

    def set_checkpoint_validation(self, task_id: str, generation: int, state: str) -> bool:
        if state not in {"valid", "invalid", "pending"}:return False
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT checkpoint_json FROM tasks WHERE task_id=? AND generation=? AND status IN ('queued','running')",(task_id,generation)).fetchone()
            if row is None:return False
            checkpoint=json.loads(row[0]); checkpoint["validation"]=state
            checkpoint["sequence"]=int(checkpoint.get("sequence",0))+1
            cur=conn.execute("UPDATE tasks SET checkpoint_json=?,updated_at=? WHERE task_id=? AND generation=? AND status IN ('queued','running')",
                             (canonical(checkpoint),utc_now(),task_id,generation))
            return cur.rowcount==1

    def commit_completion(self, task_id: str, generation: int, result: dict[str,Any],
                          usage: dict[str,Any]) -> bool:
        """Atomically publish final task result and its valid checkpoint."""
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT checkpoint_json,usage_json FROM tasks WHERE task_id=? AND generation=? AND status='running'",(task_id,generation)).fetchone()
            if row is None:return False
            checkpoint=json.loads(row[0]); checkpoint["validation"]="valid"
            checkpoint["sequence"]=int(checkpoint.get("sequence",0))+1
            prior=json.loads(row[1]); prior_calls=int(prior.get("model_calls") or 0)
            merged=dict(usage)
            for key in ("input_tokens","output_tokens","total_tokens","estimated_cost"):
                if (prior_calls and prior.get(key) is None) or merged.get(key) is None:
                    merged[key]=None
                else:
                    merged[key]=max(prior.get(key) or 0,merged.get(key) or 0)
            for key in ("model_calls","tool_calls","elapsed_ms"):
                merged[key]=max(int(prior.get(key,0) or 0),int(merged.get(key,0) or 0))
            cur=conn.execute("UPDATE tasks SET status='completed',stop_reason=NULL,result_json=?,usage_json=?,checkpoint_json=?,resumable=0,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE task_id=? AND generation=? AND status='running'",
                             (canonical(result),canonical(merged),canonical(checkpoint),utc_now(),task_id,generation))
            if cur.rowcount!=1:return False
            evidence_ids=[str(e.get("evidence_id")) for e in result.get("evidence",[]) if isinstance(e,dict) and e.get("evidence_id")][:100]
            self._append_event(conn,task_id,"task_completed","Task completed with evidence and draft references.",evidence_ids=evidence_ids)
            return True

    def start(self, task_id: str) -> bool:
        with self._lock,self._connect() as conn:
            owner=uuid.uuid4().hex
            until=(datetime.now(timezone.utc)+timedelta(seconds=150)).isoformat(timespec="milliseconds")
            cur=conn.execute("UPDATE tasks SET status='running',lease_owner=?,lease_until=?,updated_at=? WHERE task_id=? AND status='queued'",(owner,until,utc_now(),task_id))
            return cur.rowcount==1

    def cancel(self, task_id: str, expected_generation: int | None = None) -> dict[str,Any] | None:
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT * FROM tasks WHERE task_id=?",(task_id,)).fetchone()
            if row is None: return None
            if expected_generation is not None and int(row["generation"])!=int(expected_generation):return None
            if row["status"] in ("queued","running","interrupted"):
                cur=conn.execute("UPDATE tasks SET status='cancelled',stop_reason='user_cancelled',generation=generation+1,resumable=0,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE task_id=? AND status IN ('queued','running','interrupted')",(utc_now(),task_id))
                if cur.rowcount==1:self._append_event(conn,task_id,"task_cancelled","Task cancelled by user.",reason="user_cancelled")
            return self._view(conn.execute("SELECT * FROM tasks WHERE task_id=?",(task_id,)).fetchone())

    def claim_resume(self, task_id: str, expected_generation: int, *, idempotency_key: str,
                     payload: dict[str,Any], owner: str | None = None,
                     lease_seconds: int = 120) -> tuple[dict[str,Any],bool] | None:
        """Atomically acquire the single-process resume lease and generation fence."""
        owner=owner or uuid.uuid4().hex
        now=datetime.now(timezone.utc); until=(now+timedelta(seconds=max(1,min(lease_seconds,300)))).isoformat(timespec="milliseconds")
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT * FROM tasks WHERE task_id=?",(task_id,)).fetchone()
            if row is None:return None
            key_hash=digest(idempotency_key); payload_hash=digest(payload)
            if row["resume_key_hash"]:
                if row["resume_key_hash"]!=key_hash or row["resume_payload_hash"]!=payload_hash:
                    raise IdempotencyConflict("idempotency_conflict")
                return self._view(row),False
            if int(row["generation"])!=int(expected_generation) or row["status"]!="interrupted":return None
            checkpoint=json.loads(row["checkpoint_json"])
            usage=json.loads(row["usage_json"])
            # Without a persisted model response and tool execution context we
            # can resume only work interrupted before any external/local step.
            if usage.get("model_calls",0) or usage.get("tool_calls",0) or checkpoint.get("steps"):
                return None
            cur=conn.execute("UPDATE tasks SET status='queued',attempt=attempt+1,generation=generation+1,lease_owner=?,lease_until=?,resumable=0,stop_reason=NULL,resume_key_hash=?,resume_payload_hash=?,updated_at=? WHERE task_id=? AND generation=? AND status='interrupted'",
                             (owner,until,key_hash,payload_hash,utc_now(),task_id,expected_generation))
            if cur.rowcount!=1:return None
            self._append_event(conn,task_id,"task_resume_claimed","Resume lease acquired; cumulative budgets retained.")
            return self._view(conn.execute("SELECT * FROM tasks WHERE task_id=?",(task_id,)).fetchone()),True

    def resume_existing(self, task_id: str, *, idempotency_key: str, payload: dict[str,Any]) -> dict[str,Any] | None:
        with self._connect() as conn:
            row=conn.execute("SELECT * FROM tasks WHERE task_id=?",(task_id,)).fetchone()
            if row is None or not row["resume_key_hash"]:return None
            if row["resume_key_hash"]!=digest(idempotency_key) or row["resume_payload_hash"]!=digest(payload):
                raise IdempotencyConflict("idempotency_conflict")
            return self._view(row)

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

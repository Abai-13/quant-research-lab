"""练习会话的持久化工具与领域校验。"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.data.chunks import VersionView

from .storage import decode_json, encode_json


class PracticeError(RuntimeError):
    def __init__(self, reason: str, detail: str, status_code: int = 400):
        self.reason = reason
        self.status_code = status_code
        super().__init__(detail)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value: Any) -> str:
    return hashlib.sha256(encode_json(value).encode("utf-8")).hexdigest()


def parse_date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise PracticeError("invalid_date", f"{field} 必须使用 YYYYMMDD")
    text = value.strip()
    if len(text) != 8 or not text.isdigit():
        raise PracticeError("invalid_date", f"{field} 必须使用 YYYYMMDD")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise PracticeError("invalid_date", f"{field} 不是有效日期") from exc
    return text


def session_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key in ("segments_json", "settings_json", "user_state_json"):
        value[key.removesuffix("_json")] = decode_json(value.pop(key))
    return value


def require_session(conn: sqlite3.Connection, practice_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM practice_session WHERE practice_id=?", (practice_id,)
    ).fetchone()
    if row is None:
        raise PracticeError("session_not_found", f"练习 {practice_id} 不存在", 404)
    return session_dict(row)


def next_trade_date(conn: sqlite3.Connection, session: dict[str, Any]) -> str | None:
    view = VersionView(conn, session["data_version_id"], expected_source_code=session["source_code"])
    return next(
        (day for day in view.calendar_days() if day > session["revealed_date"] and day <= session["end_date"]),
        None,
    )


def check_revision(session: dict[str, Any], expected_revision: Any) -> int:
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise PracticeError("expected_revision_required", "写操作必须提供 expected_revision")
    if expected_revision != session["revision"]:
        raise PracticeError(
            "revision_conflict",
            f"练习 revision 已从 {expected_revision} 更新为 {session['revision']}，请刷新后重试",
            409,
        )
    if session["status"] != "active":
        raise PracticeError("session_finished", "练习已结束，不能继续写入", 409)
    return expected_revision


def idempotent_response(
    conn: sqlite3.Connection,
    *,
    operation: str,
    key: Any,
    request: Any,
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(key, str) or not (1 <= len(key.strip()) <= 120):
        raise PracticeError("idempotency_key_required", "写操作必须提供 1..120 字符 idempotency_key")
    request_hash = digest(request)
    row = conn.execute(
        "SELECT request_sha256,response_json FROM practice_idempotency"
        " WHERE operation=? AND idempotency_key=?",
        (operation, key),
    ).fetchone()
    if row is None:
        return None, request_hash
    if str(row[0]) != request_hash:
        raise PracticeError("idempotency_conflict", "该 idempotency_key 已被不同请求内容使用", 409)
    return decode_json(str(row[1])), request_hash


def save_idempotency(
    conn: sqlite3.Connection,
    *,
    operation: str,
    key: str,
    request_hash: str,
    practice_id: str,
    response: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO practice_idempotency(operation,idempotency_key,request_sha256,practice_id,response_json,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (operation, key, request_hash, practice_id, encode_json(response), utc_now()),
    )


def compare_and_update(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    *,
    revealed_date: str | None = None,
    status: str | None = None,
    user_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """原子递增 revision；调用方须已开始事务。"""
    now = utc_now()
    next_revision = int(session["revision"]) + 1
    fields = ["revision=?", "updated_at=?"]
    values: list[Any] = [next_revision, now]
    if revealed_date is not None:
        fields.append("revealed_date=?")
        values.append(revealed_date)
    if status is not None:
        fields.append("status=?")
        values.append(status)
    if user_state is not None:
        fields.append("user_state_json=?")
        values.append(encode_json(user_state))
    values.extend((session["practice_id"], session["revision"]))
    cursor = conn.execute(
        f"UPDATE practice_session SET {', '.join(fields)} WHERE practice_id=? AND revision=? AND status='active'",
        values,
    )
    if cursor.rowcount != 1:
        raise PracticeError("revision_conflict", "练习已由另一请求更新，请刷新后重试", 409)
    session["revision"] = next_revision
    session["updated_at"] = now
    if revealed_date is not None:
        session["revealed_date"] = revealed_date
    if status is not None:
        session["status"] = status
    if user_state is not None:
        session["user_state"] = user_state
    return session

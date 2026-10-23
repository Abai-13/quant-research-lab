"""R3：有限保留政策接入手动清理，含边界、引用保护、备份数量与中断恢复。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.data.backup import MANIFEST_NAME, create_backup
from app.data.errors import DataError
from app.data.maintenance import (
    get_preview,
    open_maintenance,
    record_recovery_point,
    start_run,
)
from app.data.retention import apply_cleanup, preview_cleanup
from test_cleanup import _seed


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _add_batch(conn: sqlite3.Connection, batch_id: str, status: str) -> None:
    conn.execute(
        "INSERT INTO import_batch(batch_id, source_code, mode, categories, scope, status,"
        " max_attempts_per_page, config_snapshot, created_at)"
        " VALUES (?, 'tushare-fixture', 'history', 'daily', '{}', ?, 2, '{}', ?)",
        (batch_id, status, _iso(datetime.now(timezone.utc))),
    )


def _add_request_log(
    conn: sqlite3.Connection, batch_id: str, started_at: datetime, digest: str | None = None
) -> int:
    cursor = conn.execute(
        "INSERT INTO import_request_log(batch_id, interface, request_params, page_no,"
        " raw_digest, status, attempt, started_at)"
        " VALUES (?, 'daily', '{}', 1, ?, 'ok', 1, ?)",
        (batch_id, digest, _iso(started_at)),
    )
    return int(cursor.lastrowid)


def _add_raw(
    settings: Settings,
    conn: sqlite3.Connection,
    label: str,
    relative: str,
    fetched_at: datetime,
    payload: bytes | None = None,
    batch_id: str = "batch-done",
) -> tuple[Path, str]:
    payload = payload if payload is not None else label.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    target = Path(settings.raw_archive_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    conn.execute(
        "INSERT INTO raw_archive(digest, path, content_bytes, interface, source_code,"
        " fetched_at, first_batch_id) VALUES (?, ?, ?, 'daily', 'tushare-fixture', ?, ?)",
        (digest, relative, len(payload), _iso(fetched_at), batch_id),
    )
    return target, digest


def _add_quality(
    conn: sqlite3.Connection,
    rule_code: str,
    severity: str,
    detected_at: datetime,
    *,
    data_version_id: str | None = None,
    batch_id: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO quality_issue(rule_code, severity, scope, evidence, rule_version,"
        " data_version_id, batch_id, detected_at) VALUES (?, ?, '{}', 'ev', 'qr-test', ?, ?, ?)",
        (rule_code, severity, data_version_id, batch_id, _iso(detected_at)),
    )
    return int(cursor.lastrowid)


def _verified_packages(settings: Settings) -> list[Path]:
    root = Path(settings.backup_dir)
    if not root.is_dir():
        return []
    packages: list[Path] = []
    for child in root.iterdir():
        manifest_path = child / MANIFEST_NAME
        if not child.is_dir() or not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("verified_at"):
            packages.append(child)
    return packages


def test_raw_archive_referenced_by_recent_log_is_protected_without_log_candidates(
    data_settings: Settings,
) -> None:
    _seed(data_settings)
    now = datetime.now(timezone.utc)
    with sqlite3.connect(data_settings.db_path) as conn:
        _add_batch(conn, "batch-done", "completed")
        _add_batch(conn, "batch-open", "running")
        _path, old_digest = _add_raw(
            data_settings, conn, "r-old", "tushare/daily/aa/r-old.json", now - timedelta(days=400)
        )
        _add_request_log(conn, "batch-done", now - timedelta(days=1), digest=old_digest)

    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101", now=now
    )
    raw_summary = preview["summary"]["raw_archive"]
    assert raw_summary["eligible"] == 0
    assert not [
        action for action in preview["plan"]["actions"] if action["action"] == "delete_raw_archive"
    ]


def test_raw_archive_retention_boundaries_and_holds(data_settings: Settings) -> None:
    _seed(data_settings)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    with sqlite3.connect(data_settings.db_path) as conn:
        _add_batch(conn, "batch-done", "completed")
        _before_path, before_digest = _add_raw(
            data_settings,
            conn,
            "r-before",
            "tushare/daily/aa/r-before.json",
            cutoff - timedelta(seconds=1),
        )
        _after_path, after_digest = _add_raw(
            data_settings,
            conn,
            "r-after",
            "tushare/daily/bb/r-after.json",
            cutoff + timedelta(seconds=1),
        )
        held, held_digest = _add_raw(
            data_settings,
            conn,
            "r-held",
            "tushare/daily/cc/r-held.json",
            cutoff - timedelta(days=1),
        )
        from app.data.retention import add_hold

        add_hold(conn, target_type="raw_archive", target_id=held_digest, reason="audit")

    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101", now=now
    )
    raw_action = next(
        action for action in preview["plan"]["actions"] if action["action"] == "delete_raw_archive"
    )
    assert {item["digest"] for item in raw_action["items"]} == {before_digest}

    apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert not (Path(data_settings.raw_archive_dir) / "tushare/daily/aa/r-before.json").is_file()
    assert (Path(data_settings.raw_archive_dir) / "tushare/daily/bb/r-after.json").is_file()
    assert held.is_file()
    with sqlite3.connect(data_settings.db_path) as conn:
        remaining = {row[0] for row in conn.execute("SELECT digest FROM raw_archive")}
    assert before_digest not in remaining
    assert {after_digest, held_digest} <= remaining


def test_unsafe_raw_path_is_reported_and_never_deleted(data_settings: Settings) -> None:
    _seed(data_settings)
    now = datetime.now(timezone.utc)
    outside = Path(data_settings.project_root) / "outside.json"
    outside.write_bytes(b"do-not-delete")
    with sqlite3.connect(data_settings.db_path) as conn:
        _add_batch(conn, "batch-done", "completed")
        conn.execute(
            "INSERT INTO raw_archive(digest, path, content_bytes, interface, source_code,"
            " fetched_at, first_batch_id) VALUES ('r-unsafe', '../outside.json', 13,"
            " 'daily', 'tushare-fixture', ?, 'batch-done')",
            (_iso(now - timedelta(days=400)),),
        )
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101", now=now
    )
    assert preview["summary"]["raw_archive"]["unsafe_skipped"] == 1
    assert not [
        action for action in preview["plan"]["actions"] if action["action"] == "delete_raw_archive"
    ]
    # 越界索引让一致性备份明确拒绝；清理不会为了继续而删除或忽略该路径。
    with pytest.raises(DataError):
        apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert outside.is_file()
    with sqlite3.connect(data_settings.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM raw_archive WHERE digest='r-unsafe'"
        ).fetchone()[0] == 1


def test_request_log_and_quality_issue_retention_with_protection(
    data_settings: Settings,
) -> None:
    v1, v2 = _seed(data_settings)
    now = datetime.now(timezone.utc)
    with sqlite3.connect(data_settings.db_path) as conn:
        _add_batch(conn, "batch-done", "completed")
        _add_batch(conn, "batch-open", "running")
        old_log = _add_request_log(conn, "batch-done", now - timedelta(days=91))
        recent_log = _add_request_log(conn, "batch-done", now - timedelta(days=89))
        open_log = _add_request_log(conn, "batch-open", now - timedelta(days=500))
        old_orphan = _add_quality(conn, "Q001", "warn", now - timedelta(days=401))
        old_active = _add_quality(
            conn, "Q002", "high", now - timedelta(days=401), data_version_id=v2
        )
        young = _add_quality(conn, "Q003", "warn", now - timedelta(days=399))

    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101", now=now
    )
    log_action = next(
        action for action in preview["plan"]["actions"] if action["action"] == "delete_request_log"
    )
    assert log_action["request_ids"] == [old_log]
    quality_action = next(
        action
        for action in preview["plan"]["actions"]
        if action["action"] == "delete_quality_issue"
    )
    assert quality_action["issue_ids"] == [old_orphan]

    applied = apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert applied["deleted"]["request_logs"] == 1
    assert applied["deleted"]["quality_issues"] == 1
    with sqlite3.connect(data_settings.db_path) as conn:
        logs = {row[0] for row in conn.execute("SELECT request_id FROM import_request_log")}
        issues = {row[0] for row in conn.execute("SELECT issue_id FROM quality_issue")}
    assert old_log not in logs
    assert {recent_log, open_log} <= logs
    assert old_orphan not in issues
    assert {old_active, young} <= issues

    side = open_maintenance(data_settings)
    try:
        summaries = side.execute(
            "SELECT issue_count, state FROM quality_issue_summary WHERE preview_id=?",
            (preview["preview_id"],),
        ).fetchall()
    finally:
        side.close()
    assert summaries and int(summaries[0][0]) == 1 and summaries[0][1] == "applied"

    # 重复预览不再产生同类动作（幂等）。
    again = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101", now=now
    )
    kinds = {action["action"] for action in again["plan"]["actions"]}
    assert "delete_request_log" not in kinds
    assert "delete_quality_issue" not in kinds


def test_backup_retention_leaves_three_points_including_new_recovery_point(
    data_settings: Settings,
) -> None:
    _seed(data_settings)
    for _ in range(4):
        create_backup(data_settings, verify=True)
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    backup_summary = preview["summary"]["backup"]
    assert backup_summary["verified_total"] == 4
    assert backup_summary["new_recovery_point_expected"] is True
    assert len(backup_summary["candidates"]) == 2

    applied = apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert applied["backup_retention"]["deleted"]
    assert len(_verified_packages(data_settings)) == 3
    active = applied["recovery_point"]
    assert Path(active).is_dir()

    # 连续第二轮：再建两份后仍然有界为 3（含新的当前恢复点）。
    for _ in range(2):
        create_backup(data_settings, verify=True)
    assert len(_verified_packages(data_settings)) == 5
    second = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    applied2 = apply_cleanup(data_settings, preview_id=second["preview_id"])
    assert applied2["status"] == "applied"
    assert len(_verified_packages(data_settings)) == 3


def test_protected_old_backup_over_limit_is_reported(data_settings: Settings) -> None:
    _seed(data_settings)
    for _ in range(4):
        create_backup(data_settings, verify=True)
    packages = _verified_packages(data_settings)
    oldest = min(packages, key=lambda item: item.stat().st_mtime_ns)
    side = open_maintenance(data_settings)
    try:
        start_run(
            side,
            kind="migrate",
            from_version=2,
            to_version=3,
            backup_path=str(oldest),
        )
    finally:
        side.close()

    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    summary = preview["summary"]["backup"]
    assert summary["over_limit_protected"] == 1
    assert summary["protected"]
    applied = apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert applied["backup_retention"]["over_limit_protected"] == 1
    assert oldest.is_dir()
    assert len(_verified_packages(data_settings)) == 4


def test_interrupted_post_commit_apply_is_reconciled_from_business_journal(
    data_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1, _v2 = _seed(data_settings)
    now = datetime.now(timezone.utc)
    with sqlite3.connect(data_settings.db_path) as conn:
        _add_batch(conn, "batch-done", "completed")
        _add_raw(
            data_settings,
            conn,
            "r-interrupt",
            "tushare/daily/dd/r-interrupt.json",
            now - timedelta(days=400),
        )
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101", now=now
    )

    def _crash(*args, **kwargs):
        raise RuntimeError("simulated crash after business commit")

    monkeypatch.setattr("app.data.retention._post_commit_cleanup", _crash)
    with pytest.raises(RuntimeError):
        apply_cleanup(data_settings, preview_id=preview["preview_id"])
    monkeypatch.undo()

    side = open_maintenance(data_settings)
    try:
        pending = get_preview(side, preview["preview_id"])
    finally:
        side.close()
    assert pending["state"] == "open"
    with sqlite3.connect(data_settings.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM data_version WHERE version_id=? AND retention_state='archived'",
            (v1,),
        ).fetchone()[0] == 1
        assert not conn.execute(
            "SELECT 1 FROM raw_archive WHERE digest='r-interrupt'"
        ).fetchone()

    retried = apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert retried["status"] == "reconciled_after_interruption"
    side = open_maintenance(data_settings)
    try:
        state = get_preview(side, preview["preview_id"])["state"]
        recovery = side.execute(
            "SELECT state, binding FROM cleanup_recovery_point WHERE preview_id=?",
            (preview["preview_id"],),
        ).fetchall()
        pending_files = side.execute(
            "SELECT state FROM cleanup_pending_file WHERE path LIKE '%r-interrupt%'"
        ).fetchall()
    finally:
        side.close()
    assert state == "applied"
    # 只使用提交前精确登记的恢复点与绑定，不按 generation/时间猜测。
    assert recovery and recovery[0][0] == "active"
    assert recovery[0][1] == "instance+path+content"
    assert retried["recovery_point"]
    assert Path(retried["recovery_point"]).is_dir()
    assert all(row[0] in {"deleted", "aborted"} for row in pending_files)
    assert not (
        Path(data_settings.raw_archive_dir) / "tushare/daily/dd/r-interrupt.json"
    ).is_file()
    with sqlite3.connect(data_settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM archive_manifest").fetchone()[0] == 1

    # 再次 apply 同一预览：already_applied，不重复归档。
    again = apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert again["status"] == "already_applied"


def test_reconcile_without_precise_recovery_registration_never_guesses_or_releases(
    data_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(data_settings)
    old_backup = create_backup(data_settings, verify=True)
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    side = open_maintenance(data_settings)
    try:
        record_recovery_point(
            side,
            preview_id="cp_previous",
            path=str(old_backup.path),
            manifest_sha256=None,
            instance_id=None,
            binding="previous",
            content_fingerprint=None,
            state="active",
        )
    finally:
        side.close()

    def _crash(*args, **kwargs):
        raise RuntimeError("simulated crash after business commit")

    monkeypatch.setattr("app.data.retention._post_commit_cleanup", _crash)
    with pytest.raises(RuntimeError):
        apply_cleanup(data_settings, preview_id=preview["preview_id"])
    monkeypatch.undo()

    # 模拟精确登记丢失（侧库损坏/旧版本残留）：不得按时间或 generation 猜包。
    side = open_maintenance(data_settings)
    try:
        side.execute(
            "DELETE FROM cleanup_recovery_point WHERE preview_id=?",
            (preview["preview_id"],),
        )
        side.commit()
    finally:
        side.close()

    with pytest.raises(DataError) as excinfo:
        apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert excinfo.value.reason == "recovery_point_revalidation_failed"

    side = open_maintenance(data_settings)
    try:
        states = dict(
            side.execute(
                "SELECT preview_id, state FROM cleanup_recovery_point"
            ).fetchall()
        )
        preview_state = get_preview(side, preview["preview_id"])["state"]
        action_states = {
            row[0]
            for row in side.execute(
                "SELECT state FROM cleanup_action WHERE preview_id=?",
                (preview["preview_id"],),
            ).fetchall()
        }
    finally:
        side.close()
    # 旧保护没有被释放；也没有登记任何猜测出来的新恢复点，预览保持未完成。
    assert states.get("cp_previous") == "active"
    assert preview["preview_id"] not in states
    assert preview_state == "open"
    assert "applied" not in action_states

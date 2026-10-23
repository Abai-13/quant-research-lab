"""R4：中断恢复前必须完整复验精确登记的恢复点，失败时零副作用。"""

from __future__ import annotations

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
)
from app.data.retention import apply_cleanup, preview_cleanup
from test_cleanup import _seed
from test_retention_policy import _add_batch, _add_raw


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _side_rows(settings: Settings, preview_id: str) -> dict:
    side = open_maintenance(settings)
    try:
        registration = side.execute(
            "SELECT path, manifest_sha256, instance_id, binding, content_fingerprint, state"
            " FROM cleanup_recovery_point WHERE preview_id=?",
            (preview_id,),
        ).fetchone()
        pending = side.execute(
            "SELECT file_id, path, state FROM cleanup_pending_file"
        ).fetchall()
        actions = side.execute(
            "SELECT seq, action, state FROM cleanup_action WHERE preview_id=?",
            (preview_id,),
        ).fetchall()
        preview_state = get_preview(side, preview_id)["state"]
        recovery_states = dict(
            side.execute("SELECT preview_id, state FROM cleanup_recovery_point").fetchall()
        )
    finally:
        side.close()
    return {
        "registration": dict(registration) if registration else None,
        "pending": [dict(row) for row in pending],
        "actions": [dict(row) for row in actions],
        "preview_state": preview_state,
        "recovery_states": recovery_states,
    }


def _prepare_interrupted(
    data_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_backups: int = 0,
) -> dict:
    """业务 commit 后、侧库收尾前中断；返回目标文件与登记信息。"""
    v1, _v2 = _seed(data_settings)
    now = datetime.now(timezone.utc)
    target = None
    digest = None
    with sqlite3.connect(data_settings.db_path) as conn:
        _add_batch(conn, "batch-done", "completed")
        target, digest = _add_raw(
            data_settings,
            conn,
            "r-r4",
            "tushare/daily/ee/r-r4.json",
            now - timedelta(days=400),
            payload=b"r4-original-payload",
        )
    backups = [create_backup(data_settings, verify=True)]
    for _ in range(extra_backups):
        backups.append(create_backup(data_settings, verify=True))
    old_backup = backups[0]
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

    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101", now=now
    )

    def _crash(*args, **kwargs):
        raise RuntimeError("simulated crash after business commit")

    monkeypatch.setattr("app.data.retention._post_commit_cleanup", _crash)
    with pytest.raises(RuntimeError):
        apply_cleanup(data_settings, preview_id=preview["preview_id"])
    monkeypatch.undo()

    rows = _side_rows(data_settings, preview["preview_id"])
    assert rows["registration"] is not None
    assert rows["registration"]["state"] == "recorded"
    with sqlite3.connect(data_settings.db_path) as conn:
        assert not conn.execute(
            "SELECT 1 FROM raw_archive WHERE digest=?", (digest,)
        ).fetchone()
    assert target.is_file()
    assert target.read_bytes() == b"r4-original-payload"
    return {
        "preview": preview,
        "target": target,
        "digest": digest,
        "v1": v1,
        "old_backup": old_backup,
        "backups": backups,
    }


def _package_external_entry(package: Path, target: Path) -> dict:
    manifest = json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry.get("kind") == "side_db":
            continue
        if Path(str(entry["path"])).name == target.name:
            return entry
    raise AssertionError("package does not contain the target raw file")


def _assert_no_side_effects(
    data_settings: Settings, prepared: dict, *, require_registration: bool = True
) -> None:
    preview_id = prepared["preview"]["preview_id"]
    rows = _side_rows(data_settings, preview_id)
    assert rows["preview_state"] == "open"
    if require_registration:
        assert rows["registration"]["state"] == "recorded"
    else:
        assert rows["registration"] is None
    assert rows["recovery_states"].get("cp_previous") == "active"
    assert any(
        item["path"].endswith("r-r4.json") and item["state"] == "pending"
        for item in rows["pending"]
    )
    assert all(action["state"] != "applied" for action in rows["actions"])
    assert prepared["target"].is_file()
    assert prepared["target"].read_bytes() == b"r4-original-payload"


@pytest.mark.parametrize("damage", ["app_db", "external_file"])
def test_corrupted_registered_package_blocks_reconcile_until_restored(
    data_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    prepared = _prepare_interrupted(data_settings, monkeypatch)
    preview_id = prepared["preview"]["preview_id"]
    rows = _side_rows(data_settings, preview_id)
    package = Path(rows["registration"]["path"])
    manifest_before = (package / MANIFEST_NAME).read_bytes()
    backups_before = {
        item.name for item in Path(data_settings.backup_dir).iterdir() if item.is_dir()
    }

    if damage == "app_db":
        db_path = package / "app.db"
        original = db_path.read_bytes()
        db_path.write_bytes(original + b"corrupted-tail")
        restore = lambda: db_path.write_bytes(original)  # noqa: E731
    else:
        entry = _package_external_entry(package, prepared["target"])
        external_path = package / entry["package_path"]
        original = external_path.read_bytes()
        # 拆开可能的 hardlink：先删除包内目录项，再写损坏副本；在线源不受影响。
        external_path.unlink()
        external_path.write_bytes(b"corrupted-external")
        assert prepared["target"].read_bytes() == b"r4-original-payload"
        restore = lambda: external_path.write_bytes(original)  # noqa: E731

    assert (package / MANIFEST_NAME).read_bytes() == manifest_before
    with pytest.raises(DataError) as excinfo:
        apply_cleanup(data_settings, preview_id=preview_id)
    assert excinfo.value.reason == "recovery_point_revalidation_failed"
    _assert_no_side_effects(data_settings, prepared)
    assert {
        item.name for item in Path(data_settings.backup_dir).iterdir() if item.is_dir()
    } == backups_before

    # 按原 manifest 恢复包内内容后，同一预览可安全重试并完成收尾。
    restore()
    retried = apply_cleanup(data_settings, preview_id=preview_id)
    assert retried["status"] == "reconciled_after_interruption"
    assert not prepared["target"].is_file()
    rows = _side_rows(data_settings, preview_id)
    assert rows["preview_state"] == "applied"
    assert rows["registration"]["state"] == "active"
    assert rows["recovery_states"].get("cp_previous") == "released"
    assert all(
        item["state"] in {"deleted", "aborted"} for item in rows["pending"]
    )
    with sqlite3.connect(data_settings.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM archive_manifest WHERE version_id=?", (prepared["v1"],)
        ).fetchone()[0] == 1
    assert apply_cleanup(data_settings, preview_id=preview_id)["status"] == "already_applied"


@pytest.mark.parametrize("damage", ["missing_registration", "fingerprint", "manifest"])
def test_unprovable_registration_is_rejected_without_side_effects(
    data_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    prepared = _prepare_interrupted(data_settings, monkeypatch)
    preview_id = prepared["preview"]["preview_id"]
    side = open_maintenance(data_settings)
    try:
        if damage == "missing_registration":
            side.execute(
                "DELETE FROM cleanup_recovery_point WHERE preview_id=?", (preview_id,)
            )
        elif damage == "fingerprint":
            side.execute(
                "UPDATE cleanup_recovery_point SET content_fingerprint='not-the-proof'"
                " WHERE preview_id=?",
                (preview_id,),
            )
        else:
            side.execute(
                "UPDATE cleanup_recovery_point SET manifest_sha256='not-the-manifest-hash'"
                " WHERE preview_id=?",
                (preview_id,),
            )
        side.commit()
    finally:
        side.close()

    with pytest.raises(DataError) as excinfo:
        apply_cleanup(data_settings, preview_id=preview_id)
    assert excinfo.value.reason == "recovery_point_revalidation_failed"
    _assert_no_side_effects(
        data_settings,
        prepared,
        require_registration=damage != "missing_registration",
    )

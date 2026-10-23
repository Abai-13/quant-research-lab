"""跨清理任务隔离：新任务不得接管旧待办或释放旧恢复保护。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.data.backup import MANIFEST_NAME, create_backup
from app.data.errors import DataError
from app.data.maintenance import (
    open_maintenance,
    record_pending_file,
    record_recovery_point,
    release_previous_recovery_points,
)
from app.data.retention import (
    _prune_backups,
    _protected_backup_paths,
    apply_cleanup,
    cleanup_status,
    preview_cleanup,
)
from test_cleanup import _seed
from test_recovery_revalidation import _prepare_interrupted, _side_rows
from test_retention_policy import _add_batch, _add_raw


def _backup_names(settings: Settings) -> set[str]:
    root = Path(settings.backup_dir)
    if not root.is_dir():
        return set()
    return {item.name for item in root.iterdir() if item.is_dir()}


def _preview_count(settings: Settings) -> int:
    side = open_maintenance(settings)
    try:
        return int(side.execute("SELECT COUNT(*) FROM cleanup_preview").fetchone()[0])
    finally:
        side.close()


def test_new_preview_is_blocked_while_previous_cleanup_is_unfinished(
    data_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_interrupted(data_settings, monkeypatch)
    before = _preview_count(data_settings)

    with pytest.raises(DataError) as excinfo:
        preview_cleanup(
            data_settings, source_code="tushare-fixture", as_of="20300101"
        )
    assert excinfo.value.reason == "pending_cleanup_unresolved"
    assert prepared["preview"]["preview_id"] in str(excinfo.value)
    assert _preview_count(data_settings) == before

    # 原任务仍可按自身登记恢复点收尾；新任务不能绕过。
    retried = apply_cleanup(
        data_settings, preview_id=prepared["preview"]["preview_id"]
    )
    assert retried["status"] == "reconciled_after_interruption"
    preview_b = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    assert preview_b["preview_id"] != prepared["preview"]["preview_id"]
    assert all(
        action["action"] != "retry_pending_raw_files"
        for action in preview_b["plan"]["actions"]
    )
    applied_b = apply_cleanup(data_settings, preview_id=preview_b["preview_id"])
    assert applied_b["status"] == "applied"
    assert (
        apply_cleanup(data_settings, preview_id=preview_b["preview_id"])["status"]
        == "already_applied"
    )


def test_bad_original_package_still_blocks_new_preview_until_repaired(
    data_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_interrupted(data_settings, monkeypatch, extra_backups=2)
    preview_id = prepared["preview"]["preview_id"]
    rows = _side_rows(data_settings, preview_id)
    package = Path(rows["registration"]["path"])
    db_path = package / "app.db"
    original = db_path.read_bytes()
    db_path.write_bytes(original + b"damaged")

    with pytest.raises(DataError) as blocked:
        preview_cleanup(
            data_settings, source_code="tushare-fixture", as_of="20300101"
        )
    assert blocked.value.reason == "pending_cleanup_unresolved"

    with pytest.raises(DataError) as invalid:
        apply_cleanup(data_settings, preview_id=preview_id)
    assert invalid.value.reason == "recovery_point_revalidation_failed"
    assert prepared["target"].is_file()

    db_path.write_bytes(original)
    retried = apply_cleanup(data_settings, preview_id=preview_id)
    assert retried["status"] == "reconciled_after_interruption"
    assert not prepared["target"].is_file()
    assert (
        apply_cleanup(data_settings, preview_id=preview_id)["status"]
        == "already_applied"
    )


def test_reconcile_preserves_foreign_pending_files_and_recorded_protection(
    data_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_interrupted(data_settings, monkeypatch, extra_backups=3)
    preview_id = prepared["preview"]["preview_id"]
    candidates = prepared["preview"]["summary"]["backup"]["candidates"]
    assert candidates
    victim = Path(candidates[0]["path"])
    other_candidate = Path(candidates[1]["path"]) if len(candidates) > 1 else None

    foreign_relative = "tushare/daily/ff/foreign-pending.json"
    foreign_target = Path(data_settings.raw_archive_dir) / foreign_relative
    foreign_target.parent.mkdir(parents=True, exist_ok=True)
    payload = b"foreign-pending"
    foreign_target.write_bytes(payload)

    side = open_maintenance(data_settings)
    try:
        record_pending_file(
            side,
            file_id="pf_foreign",
            preview_id="cp_other",
            path=foreign_relative,
            expected_bytes=len(payload),
        )
        record_recovery_point(
            side,
            preview_id="cp_other",
            path=str(victim),
            manifest_sha256=None,
            instance_id=None,
            binding="foreign-recorded",
            content_fingerprint=None,
            state="recorded",
        )
    finally:
        side.close()

    retried = apply_cleanup(data_settings, preview_id=preview_id)
    assert retried["status"] == "reconciled_after_interruption"
    assert not prepared["target"].is_file()
    assert foreign_target.is_file()
    assert foreign_target.read_bytes() == payload

    side = open_maintenance(data_settings)
    try:
        foreign_state = side.execute(
            "SELECT state FROM cleanup_pending_file WHERE file_id='pf_foreign'"
        ).fetchone()[0]
        foreign_recovery = side.execute(
            "SELECT state FROM cleanup_recovery_point WHERE preview_id='cp_other'"
        ).fetchone()[0]
    finally:
        side.close()
    assert foreign_state == "pending"
    assert foreign_recovery == "recorded"
    assert victim.is_dir()
    if other_candidate is not None:
        assert not other_candidate.is_dir()
    assert any("保留其他未完成清理" in note for note in retried["notes"])

    # 状态接口可让操作者看到未完成的旧清理，而不是伪报全部完成。
    status = cleanup_status(data_settings)
    unfinished = status["unfinished_cleanups"]
    assert unfinished["unfinished"] is True
    assert "cp_other" in unfinished["preview_ids"]


def _expire_preview(settings: Settings, preview_id: str) -> None:
    side = open_maintenance(settings)
    try:
        side.execute(
            "UPDATE cleanup_preview SET expires_at='2000-01-01T00:00:00+00:00'"
            " WHERE preview_id=?",
            (preview_id,),
        )
        side.commit()
    finally:
        side.close()


def _apply_with_failed_file_delete(
    data_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> dict:
    """一次正常 apply，仅注入外部文件删除失败：业务 commit 完成、预览 applied。"""
    v1, _v2 = _seed(data_settings)
    now = datetime.now(timezone.utc)
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
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101", now=now
    )

    def _fail(settings, stored_paths):
        return {
            "deleted": [],
            "bytes": 0,
            "failed": [f"{path}: injected" for path in stored_paths],
            "missing": [],
            "unsafe": [],
        }

    monkeypatch.setattr("app.data.retention._delete_managed_files", _fail)
    applied = apply_cleanup(data_settings, preview_id=preview["preview_id"])
    monkeypatch.undo()
    assert applied["status"] == "applied"
    rows = _side_rows(data_settings, preview["preview_id"])
    assert rows["preview_state"] == "applied"
    assert any(
        item["path"].endswith("r-r4.json") and item["state"] == "failed"
        for item in rows["pending"]
    )
    assert target.is_file()
    return {
        "preview": preview,
        "target": target,
        "digest": digest,
        "v1": v1,
    }


def test_applied_preview_retries_its_own_failed_files_after_ttl(
    data_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _apply_with_failed_file_delete(data_settings, monkeypatch)
    preview_id = prepared["preview"]["preview_id"]
    _expire_preview(data_settings, preview_id)

    retried = apply_cleanup(data_settings, preview_id=preview_id)
    assert retried["status"] == "finalized_pending_files"
    assert retried["deleted_files"] == 1
    assert not prepared["target"].is_file()
    rows = _side_rows(data_settings, preview_id)
    assert all(item["state"] == "deleted" for item in rows["pending"])
    assert apply_cleanup(data_settings, preview_id=preview_id)["status"] == "already_applied"


def test_applied_preview_failed_files_still_require_valid_recovery_point(
    data_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _apply_with_failed_file_delete(data_settings, monkeypatch)
    preview_id = prepared["preview"]["preview_id"]
    rows = _side_rows(data_settings, preview_id)
    package = Path(rows["registration"]["path"])
    db_path = package / "app.db"
    original = db_path.read_bytes()
    db_path.write_bytes(original + b"damaged")

    with pytest.raises(DataError) as excinfo:
        apply_cleanup(data_settings, preview_id=preview_id)
    assert excinfo.value.reason == "recovery_point_revalidation_failed"
    assert prepared["target"].is_file()
    assert any(
        item["state"] == "failed" for item in _side_rows(data_settings, preview_id)["pending"]
    )

    db_path.write_bytes(original)
    assert (
        apply_cleanup(data_settings, preview_id=preview_id)["status"]
        == "finalized_pending_files"
    )
    assert not prepared["target"].is_file()


def test_committed_preview_can_reconcile_after_ttl_expiry(
    data_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_interrupted(data_settings, monkeypatch)
    _expire_preview(data_settings, prepared["preview"]["preview_id"])
    retried = apply_cleanup(
        data_settings, preview_id=prepared["preview"]["preview_id"]
    )
    assert retried["status"] == "reconciled_after_interruption"
    assert not prepared["target"].is_file()


def test_unexecuted_expired_preview_is_still_rejected(data_settings: Settings) -> None:
    _seed(data_settings)
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    _expire_preview(data_settings, preview["preview_id"])
    backups_before = _backup_names(data_settings)
    with pytest.raises(DataError) as excinfo:
        apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert excinfo.value.reason == "preview_expired"
    assert _backup_names(data_settings) == backups_before

    # 只读/未执行的过期预览不构成未完成清理，后续新预览可正常创建。
    fresh = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    assert fresh["preview_id"] != preview["preview_id"]


def test_new_apply_rejects_foreign_unfinished_before_creating_recovery_point(
    data_settings: Settings,
) -> None:
    _seed(data_settings)
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    side = open_maintenance(data_settings)
    try:
        record_pending_file(
            side,
            file_id="pf_foreign",
            preview_id="cp_foreign",
            path="tushare/daily/zz/foreign.json",
            expected_bytes=1,
        )
    finally:
        side.close()
    backups_before = _backup_names(data_settings)

    with pytest.raises(DataError) as excinfo:
        apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert excinfo.value.reason == "pending_cleanup_unresolved"
    side = open_maintenance(data_settings)
    try:
        registration = side.execute(
            "SELECT 1 FROM cleanup_recovery_point WHERE preview_id=?",
            (preview["preview_id"],),
        ).fetchone()
    finally:
        side.close()
    assert registration is None
    assert _backup_names(data_settings) == backups_before


def test_old_plan_foreign_pending_item_is_rejected_before_registration(
    data_settings: Settings,
) -> None:
    _seed(data_settings)
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    side = open_maintenance(data_settings)
    try:
        row = side.execute(
            "SELECT plan_json FROM cleanup_preview WHERE preview_id=?",
            (preview["preview_id"],),
        ).fetchone()
        plan = json.loads(row[0])
        plan["actions"].append(
            {
                "action": "retry_pending_raw_files",
                "items": [
                    {
                        "file_id": "pf_foreign",
                        "preview_id": "cp_foreign",
                        "path": "tushare/daily/zz/foreign.json",
                        "bytes": 1,
                        "state": "pending",
                    }
                ],
                "truncated": False,
                "unsafe": [],
                "aborted": [],
            }
        )
        side.execute(
            "UPDATE cleanup_preview SET plan_json=? WHERE preview_id=?",
            (json.dumps(plan, ensure_ascii=False, sort_keys=True), preview["preview_id"]),
        )
        side.commit()
    finally:
        side.close()
    backups_before = _backup_names(data_settings)

    with pytest.raises(DataError) as excinfo:
        apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert excinfo.value.reason == "pending_cleanup_unresolved"
    side = open_maintenance(data_settings)
    try:
        registration = side.execute(
            "SELECT 1 FROM cleanup_recovery_point WHERE preview_id=?",
            (preview["preview_id"],),
        ).fetchone()
    finally:
        side.close()
    assert registration is None
    assert _backup_names(data_settings) == backups_before


def test_active_recovery_point_with_pending_is_not_released_or_pruned(
    data_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _apply_with_failed_file_delete(data_settings, monkeypatch)
    preview_id = prepared["preview"]["preview_id"]
    rows = _side_rows(data_settings, preview_id)
    package = Path(rows["registration"]["path"])
    assert rows["registration"]["state"] == "active"

    side = open_maintenance(data_settings)
    try:
        release_previous_recovery_points(side, keep_preview_id="cp_other")
        after_release = dict(
            side.execute("SELECT preview_id, state FROM cleanup_recovery_point").fetchall()
        )
        protected = _protected_backup_paths(side)
    finally:
        side.close()
    assert after_release.get(preview_id) == "active"
    key = os.path.normcase(str(package.resolve()))
    assert protected.get(key, "").startswith("unfinished_cleanup_recovery_point:")

    # 即使该包落在数量保留之外，也不得被备份清理删除。
    for _ in range(4):
        create_backup(data_settings, verify=True)
    manifest_sha = hashlib.sha256((package / MANIFEST_NAME).read_bytes()).hexdigest()
    conn = sqlite3.connect(data_settings.db_path)
    prune_side = open_maintenance(data_settings)
    try:
        result = _prune_backups(
            data_settings,
            prune_side,
            conn,
            {
                "retain_count": 3,
                "candidates": [
                    {
                        "name": package.name,
                        "path": str(package),
                        "manifest_sha256": manifest_sha,
                        "created_at": "",
                        "bytes": 0,
                    }
                ],
            },
            current_recovery=None,
        )
    finally:
        prune_side.close()
        conn.close()
    assert package.is_dir()
    assert any(
        item["name"] == package.name and item["reason"].startswith("protected:")
        for item in result["skipped"]
    )


import sqlite3
from pathlib import Path

from app.agent_runtime.maintenance import MaintenanceError, MaintenanceService


def test_cleanup_preview_invalidates_when_reference_generation_changes(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = runtime / "cache.bin"
    target.write_bytes(b"regenerable")
    state = {"generation": 1, "references": {"cache": []}}
    service = MaintenanceService(tmp_path / "maintenance.sqlite3", runtime)
    service.initialize()
    service.register_reference_source("research", lambda: state)
    candidates = [{"kind": "cache", "target_id": "cache-v1", "path": target, "regenerable": True}]

    preview = service.preview(instance_id="instance-a", policy_version="ret-1", candidates=candidates)
    assert preview["items"][0]["status"] == "eligible"
    state["generation"] = 2
    state["references"]["cache"].append("cache-v1")
    try:
        service.execute(preview["preview_id"], instance_id="instance-a", policy_version="ret-1",
                        candidates=candidates, automatic=True)
    except MaintenanceError as exc:
        assert exc.reason == "preview_stale"
    else:
        raise AssertionError("stale preview was applied")
    assert target.exists()


def test_unknown_reference_source_blocks_automatic_cleanup(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = runtime / "cache.bin"
    target.write_bytes(b"cache")
    service = MaintenanceService(tmp_path / "maintenance.sqlite3", runtime)
    service.initialize()
    preview = service.preview(instance_id="instance-a", policy_version="ret-1", candidates=[
        {"kind": "cache", "target_id": "cache-v1", "path": target, "regenerable": True}])
    assert preview["unknown_references"] is True
    assert preview["items"][0]["status"] == "reference_state_unknown"
    try:
        service.execute(preview["preview_id"], instance_id="instance-a", policy_version="ret-1",
                        candidates=[{"kind": "cache", "target_id": "cache-v1", "path": target, "regenerable": True}],
                        automatic=True)
    except MaintenanceError as exc:
        assert exc.reason in {"preview_stale", "reference_state_unknown"}
    else:
        raise AssertionError("unknown references did not block deletion")
    assert target.exists()


def test_nonregenerable_candidates_are_never_auto_deleted(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = runtime / "research.sqlite3"
    target.write_bytes(b"research")
    service = MaintenanceService(tmp_path / "maintenance.sqlite3", runtime)
    service.initialize()
    service.register_reference_source("empty", lambda: {"generation": 0, "references": {"research": []}})
    candidates = [{"kind": "research", "target_id": "record-1", "path": target, "regenerable": False}]
    preview = service.preview(instance_id="instance-a", policy_version="ret-1", candidates=candidates)
    assert preview["items"][0]["status"] == "manual_review_required"
    result = service.execute(preview["preview_id"], instance_id="instance-a", policy_version="ret-1",
                            candidates=candidates, automatic=True)
    assert result["deleted"] == []
    assert target.exists()


def test_mid_batch_delete_failure_invalidates_preview_and_blocks_replay(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    first, second = runtime / "first.bin", runtime / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    service = MaintenanceService(tmp_path / "maintenance.sqlite3", runtime)
    service.initialize()
    service.register_reference_source("empty", lambda: {"generation": 1, "references": {"cache": []}})
    candidates = [
        {"kind": "cache", "target_id": name, "path": path, "regenerable": True}
        for name, path in (("first", first), ("second", second))
    ]
    preview = service.preview(instance_id="instance-a", policy_version="ret-1", candidates=candidates)
    original_unlink = Path.unlink
    calls = 0

    def injected_unlink(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected mid-batch failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", injected_unlink)
    try:
        service.execute(preview["preview_id"], instance_id="instance-a", policy_version="ret-1",
                        candidates=candidates, automatic=True)
    except MaintenanceError as exc:
        assert exc.reason == "path_changed"
    else:
        raise AssertionError("injected deletion failure was not surfaced")
    with sqlite3.connect(service.db_path) as conn:
        state = conn.execute("SELECT state FROM previews WHERE preview_id=?", (preview["preview_id"],)).fetchone()[0]
    assert state == "failed"
    assert not first.exists() and second.exists()
    try:
        service.execute(preview["preview_id"], instance_id="instance-a", policy_version="ret-1",
                        candidates=candidates, automatic=True)
    except MaintenanceError as exc:
        assert exc.reason == "preview_not_found"
    else:
        raise AssertionError("failed preview was replayed")

"""Fail-closed preview/apply coordinator for cache maintenance.

Domain adapters register read-only reference snapshots. An unavailable adapter
is an unknown reference and blocks cleanup. Only explicitly regenerable files
may be removed by automatic execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from .operation import canonical_json
from .store import utc_now


class MaintenanceError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


ReferenceReader = Callable[[], dict[str, Any]]


class MaintenanceService:
    def __init__(self, db_path: Path, runtime_root: Path):
        self.db_path = Path(db_path)
        self.runtime_root = Path(runtime_root).resolve()
        self._readers: dict[str, ReferenceReader] = {}

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS previews (preview_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, policy_version TEXT NOT NULL, input_digest TEXT NOT NULL, reference_generation TEXT NOT NULL, plan_json TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL)")

    def register_reference_source(self, name: str, reader: ReferenceReader) -> None:
        if not name or len(name) > 80:
            raise ValueError("reference_source_invalid")
        self._readers[name] = reader

    def _reference_snapshot(self) -> tuple[dict[str, set[str]], dict[str, Any], bool]:
        refs: dict[str, set[str]] = {}
        generations: dict[str, Any] = {}
        unknown = not self._readers
        for name, reader in sorted(self._readers.items()):
            try:
                snapshot = reader()
                generation = snapshot["generation"]
                values = snapshot["references"]
                if not isinstance(generation, (str, int)) or not isinstance(values, dict):
                    raise ValueError("reference_snapshot_invalid")
                generations[name] = generation
                for kind, ids in values.items():
                    if not isinstance(kind, str) or not isinstance(ids, (list, tuple, set)):
                        raise ValueError("reference_snapshot_invalid")
                    refs.setdefault(kind, set()).update(str(value) for value in ids)
            except Exception:
                generations[name] = "unknown"
                unknown = True
        return refs, generations, unknown

    def preview(self, *, instance_id: str, policy_version: str,
                candidates: Iterable[dict[str, Any]]) -> dict[str, Any]:
        if not instance_id or not policy_version:
            raise MaintenanceError("preview_binding_invalid")
        rows: list[dict[str, Any]] = []
        normalized: list[dict[str, Any]] = []
        for candidate in candidates:
            kind, target_id = candidate.get("kind"), candidate.get("target_id")
            path_value = candidate.get("path")
            regenerable = candidate.get("regenerable") is True
            status = "eligible"
            rel = None
            size = None
            input_sha = None
            if not isinstance(kind, str) or not isinstance(target_id, str) or not path_value:
                status = "unknown_candidate"
            else:
                path = Path(path_value)
                try:
                    resolved = path.resolve(strict=True)
                    rel = resolved.relative_to(self.runtime_root).as_posix()
                    stat = resolved.stat()
                    if path.is_symlink() or (getattr(stat, "st_file_attributes", 0) & 0x400):
                        status = "reparse_refused"
                    elif not resolved.is_file():
                        status = "not_file"
                    else:
                        size = stat.st_size
                        input_sha = self._sha256(resolved)
                except (OSError, ValueError):
                    status = "path_unreadable_or_outside_root"
            normalized.append({"kind": kind, "target_id": target_id, "path": rel,
                               "size": size, "sha256": input_sha, "regenerable": regenerable})
            rows.append({"kind": kind, "target_id": target_id, "path": rel, "bytes": size,
                         "regenerable": regenerable, "status": status})
        refs, generations, unknown = self._reference_snapshot()
        for row in rows:
            if row["status"] != "eligible":
                continue
            if unknown:
                row["status"] = "reference_state_unknown"
            elif row["target_id"] in refs.get(row["kind"], set()):
                row["status"] = "referenced"
            elif not row["regenerable"]:
                row["status"] = "manual_review_required"
        input_digest = self._digest(normalized)
        reference_generation = self._digest(generations)
        plan = {"instance_id": instance_id, "policy_version": policy_version,
                "input_digest": input_digest, "reference_generation": reference_generation,
                "items": rows, "automatic_candidates": sum(1 for row in rows if row["status"] == "eligible" and row["regenerable"]),
                "unknown_references": unknown, "default_mode": "preview_only"}
        preview_id = "maint_" + uuid.uuid4().hex
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO previews VALUES(?,?,?,?,?,?,?,?)",
                         (preview_id, instance_id, policy_version, input_digest, reference_generation,
                          canonical_json(plan), utc_now(), "preview"))
        return {"preview_id": preview_id, **plan}

    def preview_runtime_cache(self, *, instance_id: str, policy_version: str) -> dict[str, Any]:
        """Preview only the dedicated regenerable cache subtree, never caller paths."""
        root = self.runtime_root / "cache" / "regenerable"
        candidates: list[dict[str, Any]] = []
        if root.exists():
            for current, dirs, files in os.walk(root, followlinks=False):
                base = Path(current)
                dirs[:] = [name for name in dirs if not (base / name).is_symlink()
                           and not (getattr((base / name).stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)]
                for name in files:
                    path = base / name
                    try:
                        if path.is_symlink() or (getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400):
                            continue
                        rel = path.resolve(strict=True).relative_to(self.runtime_root).as_posix()
                    except (OSError, ValueError):
                        continue
                    candidates.append({"kind": "cache", "target_id": rel, "path": path, "regenerable": True})
        return self.preview(instance_id=instance_id, policy_version=policy_version, candidates=candidates)

    def execute_saved(self, preview_id: str, *, instance_id: str, policy_version: str,
                      automatic: bool = False) -> dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT plan_json FROM previews WHERE preview_id=? AND state='preview'", (preview_id,)).fetchone()
        if row is None:
            raise MaintenanceError("preview_not_found")
        plan = json.loads(row[0])
        candidates=[]
        for item in plan.get("items", []):
            rel = str(item.get("path") or "")
            if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
                raise MaintenanceError("preview_invalid")
            candidates.append({"kind": item.get("kind"), "target_id": item.get("target_id"),
                               "path": self.runtime_root / Path(rel), "regenerable": item.get("regenerable") is True})
        return self.execute(preview_id, instance_id=instance_id, policy_version=policy_version,
                            candidates=candidates, automatic=automatic)

    def list_previews(self, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT preview_id,instance_id,policy_version,created_at,state,plan_json FROM previews ORDER BY created_at DESC LIMIT ?", (max(1,min(int(limit),100)),)).fetchall()
        output=[]
        for preview_id, instance_id, policy, created_at, state, encoded in rows:
            plan=json.loads(encoded)
            output.append({"preview_id":preview_id,"instance_id":instance_id,"policy_version":policy,
                           "created_at":created_at,"state":state,"input_digest":plan.get("input_digest"),
                           "reference_generation":plan.get("reference_generation"),
                           "automatic_candidates":plan.get("automatic_candidates",0),
                           "unknown_references":plan.get("unknown_references",True)})
        return output

    def execute(self, preview_id: str, *, instance_id: str, policy_version: str,
                candidates: Iterable[dict[str, Any]], automatic: bool = False) -> dict[str, Any]:
        candidates=list(candidates)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT instance_id,policy_version,input_digest,reference_generation,plan_json,state FROM previews WHERE preview_id=?", (preview_id,)).fetchone()
        if row is None or row[5] != "preview":
            raise MaintenanceError("preview_not_found")
        fresh = self.preview(instance_id=instance_id, policy_version=policy_version, candidates=candidates)
        # The refresh creates a superseding preview; retire it after comparison.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE previews SET state='superseded' WHERE preview_id=?", (fresh["preview_id"],))
        if (row[0] != instance_id or row[1] != policy_version or row[2] != fresh["input_digest"]
                or row[3] != fresh["reference_generation"]):
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE previews SET state='stale' WHERE preview_id=?", (preview_id,))
            raise MaintenanceError("preview_stale")
        deleted: list[str] = []
        if automatic:
            if fresh["unknown_references"]:
                raise MaintenanceError("reference_state_unknown")
            root = self.runtime_root
            plan_items = fresh["items"]
            normalized = candidates
            sha_by_rel={item["path"]:item["sha256"] for item in self._normalized_candidates(normalized)}
            try:
                for candidate, item in zip(normalized, plan_items):
                    if item["status"] != "eligible" or not candidate.get("regenerable"):
                        continue
                    _refs, generations, unknown = self._reference_snapshot()
                    if unknown or self._digest(generations) != fresh["reference_generation"]:
                        raise MaintenanceError("preview_stale")
                    path = Path(candidate["path"])
                    try:
                        resolved = path.resolve(strict=True)
                        resolved.relative_to(root)
                        stat = resolved.stat()
                        if path.is_symlink() or (getattr(stat, "st_file_attributes", 0) & 0x400) or not resolved.is_file():
                            raise MaintenanceError("path_changed")
                        if self._sha256(resolved) != sha_by_rel.get(resolved.relative_to(root).as_posix()):
                            raise MaintenanceError("input_changed")
                        resolved.unlink()
                        deleted.append(str(item["target_id"]))
                    except (OSError, ValueError, StopIteration):
                        raise MaintenanceError("path_changed") from None
            except Exception:
                # Filesystem deletion cannot be rolled back. Persist a terminal
                # state even after a partial batch so a replay cannot reuse a
                # preview whose original plan may no longer describe reality.
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("UPDATE previews SET state='failed' WHERE preview_id=? AND state='preview'", (preview_id,))
                raise
        if automatic:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE previews SET state='applied' WHERE preview_id=?", (preview_id,))
        return {"preview_id": preview_id, "deleted": deleted, "mode": "automatic" if automatic else "preview_only"}

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

    def _normalized_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result=[]
        for candidate in candidates:
            try:
                path=Path(candidate["path"]).resolve(strict=True)
                rel=path.relative_to(self.runtime_root).as_posix()
                result.append({"path":rel,"sha256":self._sha256(path)})
            except (KeyError,OSError,ValueError):
                continue
        return result

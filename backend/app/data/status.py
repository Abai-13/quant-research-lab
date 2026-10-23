"""数据状态摘要（供状态页与接口使用）。

摘要只报告真实检查结果：来源配置、覆盖区间、最近成功导入、质量问题数量、
数据版本与股票池快照。不展示假价格、假收益或“行情接通即策略可交易”的结论。

状态取值：

- ``not_connected``：尚未导入任何数据（阶段 1 的默认状态，保持兼容）；
- ``import_failed``：最近一次导入失败且没有可用版本；
- ``pending_check``：已有数据但还没有发布数据版本；
- ``partial_coverage``：最新版本是缩小范围发布；
- ``limited``：数据可用但存在限制（高严重度质量问题或历史可用时刻未证明）；
- ``available_in_range``：指定范围内可用。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..config import Settings
from .quality import issue_counts_by_severity
from .schema import SCHEMA_VERSION
from .store import coverage_summary, latest_versions, version_count

__all__ = ["data_status", "STATUS_VALUES"]

STATUS_VALUES: tuple[str, ...] = (
    "not_connected",
    "import_failed",
    "pending_check",
    "partial_coverage",
    "limited",
    "available_in_range",
)

LIMITATIONS: tuple[str, ...] = (
    "阶段 2 只建立数据层：没有策略、回测、模拟账户、成交或调度。",
    "行情可用资格由专门接口判断，不等于策略安全或能够盈利。",
    "历史股票池受限于退市证券与历史停牌状态覆盖不足，幸存者偏差尚未消除。",
    "供应商不提供逐条历史发布时刻，严格历史重放资格受限。",
)


def _open_readonly(db_path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    return conn


def data_status(settings: Settings) -> dict[str, Any]:
    """汇总数据准备程度（只读，不修改数据库）。"""
    source = settings.data_source.public_summary()
    base: dict[str, Any] = {
        "status": "not_connected",
        "source": source,
        "coverage": {},
        "quality": {"total": 0, "by_severity": {"info": 0, "warn": 0, "high": 0}},
        "last_import": None,
        "data_versions": [],
        "pool_snapshots": [],
        "limitations": list(LIMITATIONS),
    }
    conn = _open_readonly(settings.db_path)
    if conn is None:
        base["detail"] = "数据库尚未初始化"
        return base
    try:
        metadata = dict(conn.execute("SELECT key, value FROM app_metadata").fetchall())
        schema_version = int(metadata.get("schema_version", "0"))
        base["database"] = {
            "schema_version": schema_version,
            "instance_id": metadata.get("instance_id"),
            "created_at": metadata.get("created_at"),
        }
        if schema_version != SCHEMA_VERSION:
            base["status"] = "migration_required"
            base["detail"] = (
                f"数据库仍是 schema {schema_version}，需要先显式迁移到 schema {SCHEMA_VERSION}"
            )
            return base

        source_code = settings.data_source.source_code
        coverage = coverage_summary(conn, source_code)
        base["coverage"] = coverage
        total_records = sum(
            item["logical_count"] or 0 for item in coverage.values()
        )
        total_revisions = sum(
            item["revision_rows"] or 0 for item in coverage.values()
        )
        base["record_count"] = total_records
        base["record_counts"] = {
            "logical": total_records,
            "revision_rows": total_revisions,
            "coverage_stale": any(item.get("stale") for item in coverage.values()),
        }

        severity = issue_counts_by_severity(conn)
        base["quality"] = {
            "total": sum(severity.values()),
            "by_severity": severity,
        }

        batch_row = conn.execute(
            "SELECT batch_id, mode, status, started_at, finished_at, pages_ok, pages_failed,"
            " error_class FROM import_batch ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if batch_row is not None:
            base["last_import"] = {
                "batch_id": batch_row[0],
                "mode": batch_row[1],
                "status": batch_row[2],
                "started_at": batch_row[3],
                "finished_at": batch_row[4],
                "pages_ok": batch_row[5],
                "pages_failed": _as_int(batch_row[6]),
                "error_class": batch_row[7],
            }

        versions = latest_versions(conn, limit=20)
        base["version_count"] = version_count(conn)
        base["data_versions_limit"] = 20
        base["data_versions"] = [
            {
                "version_id": item["version_id"],
                "published_at": item["published_at"],
                "manifest_sha256": item["manifest_sha256"],
                "manifest_version": item["manifest_version"],
                "member_storage": item["member_storage"],
                "member_count": item["member_count"],
                "chunk_count": item["chunk_count"],
                "retention_state": item["retention_state"],
                "window_mode": item["window_mode"],
                "window_start": item["window_start"],
                "rule_version": item["rule_version"],
                "scope": _parse_json(item["scope"]),
                "quality_summary": _parse_json(item["quality_summary"]),
            }
            for item in versions
        ]
        snapshot_total = conn.execute("SELECT COUNT(*) FROM pool_snapshot").fetchone()[0]
        base["pool_snapshot_count"] = int(snapshot_total or 0)
        base["pool_snapshots_limit"] = 10
        base["pool_snapshots"] = [
            {"snapshot_id": row[0], "as_of": row[1], "member_count": row[2]}
            for row in conn.execute(
                "SELECT snapshot_id, as_of, member_count FROM pool_snapshot"
                " ORDER BY generated_at DESC, snapshot_id DESC LIMIT 10"
            ).fetchall()
        ]

        has_daily_rows = (
            conn.execute(
                "SELECT 1 FROM daily_price WHERE source_code=? LIMIT 1", (source_code,)
            ).fetchone()
            is not None
        )
        if not has_daily_rows and total_records == 0:
            if batch_row is not None and str(batch_row[2]) in {"failed", "partial"}:
                base["status"] = "import_failed"
                base["detail"] = "最近一次导入失败，尚未获得可用数据版本"
                return base
            base["status"] = "not_connected"
            base["detail"] = "尚未导入任何行情数据"
            return base

        if not versions:
            base["status"] = "pending_check"
            base["detail"] = "已有数据但还没有发布数据版本，需完成质量检查"
            return base

        latest = base["data_versions"][-1]
        if latest["scope"].get("partial"):
            base["status"] = "partial_coverage"
            base["detail"] = "最新版本是缩小范围发布，覆盖不完整"
            return base

        if severity.get("high", 0) > 0:
            base["status"] = "limited"
            base["detail"] = "存在高严重度质量问题，受影响范围不可用于新计划输入"
            return base

        base["status"] = "available_in_range"
        base["detail"] = "指定范围内的数据已通过检查"
        return base
    except sqlite3.Error:
        base["status"] = "not_connected"
        base["detail"] = "数据库不可读，无法给出数据状态"
        return base
    finally:
        conn.close()


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}

"""阶段 2 手动操作入口。

所有命令都输出 JSON 到标准输出，便于脚本与人工核对。凭据绝不进入输出。

命令：

- ``source-check``：核对来源配置、凭据是否存在、接口能力矩阵；
- ``migrate``：把 schema 1 显式迁移为 schema 2（先备份再事务化）；
- ``import-history``：批量历史导入；
- ``import-incremental``：按业务范围与成功检查点做增量更新；
- ``quality-check``：对已入库数据执行质量检查并记录问题；
- ``versions``：列出数据版本；
- ``pool-snapshot``：生成历史股票池快照；
- ``eligibility``：按用途判断数据是否够用；
- ``status``：数据准备程度摘要。

用法示例：

    python -m app.data.cli status
    python -m app.data.cli source-check
    python -m app.data.cli migrate
    python -m app.data.cli import-history --start 20240102 --end 20240105 \
        --codes 000001.SZ,600000.SH --categories calendar,security,daily
    python -m app.data.cli import-incremental --categories daily
    python -m app.data.cli quality-check
    python -m app.data.cli versions
    python -m app.data.cli pool-snapshot --as-of 20240105
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any, Sequence

from ..config import Settings, load_settings
from .adapters.registry import build_adapter
from .backup import (
    create_backup,
    directory_bytes,
    file_identity_bytes,
    restore_package,
    verify_backup,
)
from .catalog import DEFAULT_EXCHANGE
from .migration import DatabaseError, initialize_database, migrate_database
from .eligibility import check_eligibility
from .errors import DataError
from .locking import DataWriteLock
from .pipeline import ImportOptions, run_import
from .quality import record_issues, run_dataset_quality_check
from .records import ALL_CATEGORIES
from .retention import (
    add_hold,
    apply_cleanup,
    cleanup_status,
    current_policy,
    ensure_policy,
    list_holds,
    preview_cleanup,
    remove_hold,
    restore_version,
)
from .sources import interface_specs, sync_source_registry
from .status import data_status
from .store import coverage_summary, published_versions, update_coverage_stats
from .timeutil import require_yyyymmdd
from .universe import (
    DEFAULT_POOL_RULE,
    PoolRule,
    generate_pool_snapshot,
    list_pool_snapshots,
    load_pool_snapshot,
)

__all__ = ["main", "build_parser"]


def _emit(payload: Any) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _split_codes(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _split_categories(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    selected = tuple(part.strip() for part in raw.split(",") if part.strip())
    unknown = [item for item in selected if item not in ALL_CATEGORIES]
    if unknown:
        raise SystemExit(f"未知数据类别: {', '.join(unknown)}")
    return selected


def _open_readwrite(settings: Settings) -> sqlite3.Connection:
    if not settings.db_path.exists():
        raise DataError("database_missing", f"数据库文件不存在: {settings.db_path}")
    conn = sqlite3.connect(str(settings.db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _open_readonly(settings: Settings) -> sqlite3.Connection:
    if not settings.db_path.exists():
        raise DataError("database_missing", f"数据库文件不存在: {settings.db_path}")
    conn = sqlite3.connect(settings.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _cmd_source_check(settings: Settings, args: argparse.Namespace) -> int:
    source = settings.data_source
    adapter = build_adapter(settings)
    payload: dict[str, Any] = {
        "source": source.public_summary(),
        "credential_check": "present" if source.token_present else "missing",
        "fixture_dir": str(settings.fixture_dir) if source.is_fixture_source else None,
        "interfaces": [
            {
                "interface": spec.interface,
                "permission_status": spec.permission_status,
                "permission_notes": spec.permission_notes,
                "rate_limit": spec.rate_limit,
                "cost_notes": spec.cost_notes,
                "coverage_notes": spec.coverage_notes,
                "evidence_date": spec.evidence_date,
                "docs_url": spec.docs_url,
                "unknown_fields": list(spec.unknown_fields),
                "unit_notes": [f"{name}: {note}" for name, note in spec.unit_notes],
            }
            for spec in interface_specs(source.source_code)
        ],
    }
    if args.probe:
        outcome = adapter.probe()
        payload["probe"] = {
            "status": outcome.status,
            "reason_code": outcome.error_reason,
            "row_count": len(outcome.rows),
            "detail": str(outcome.error) if outcome.error is not None else None,
        }
    if settings.db_path.exists():
        conn = _open_readwrite(settings)
        try:
            sync_source_registry(conn, source.source_code)
        finally:
            conn.close()
    return _emit(payload)


def _cmd_migrate(settings: Settings, args: argparse.Namespace) -> int:
    result = migrate_database(
        settings,
        convert_legacy=not args.skip_convert,
        reclaim_legacy=args.reclaim_legacy_members,
    )
    return _emit(result)


def _cmd_import(settings: Settings, args: argparse.Namespace, mode: str) -> int:
    # 新库直接建为 schema 2；已有 schema 1 库保持原样，由管线提示先执行迁移。
    initialize_database(settings)
    options = ImportOptions(
        mode=mode,
        categories=_split_categories(args.categories),
        start=args.start,
        end=args.end,
        codes=_split_codes(args.codes),
        trade_dates=tuple(args.trade_date or ()),
        daily_by_code=not args.daily_by_trade_date,
        allow_partial=args.allow_partial,
        exchange=args.exchange or DEFAULT_EXCHANGE,
        resume_batch_id=args.resume_batch,
    )
    result = run_import(settings, options)
    return _emit(result.as_dict())


def _cmd_quality_check(settings: Settings, args: argparse.Namespace) -> int:
    conn = _open_readwrite(settings)
    try:
        report = run_dataset_quality_check(
            conn,
            settings.data_source.source_code,
            as_of=args.as_of,
            data_version_id=args.data_version,
            batch_size=args.batch_size,
        )
        if args.record:
            issues = report["issues"]
            from .quality import QualityIssue

            objects = [
                QualityIssue(
                    rule_code=item["rule_code"],
                    severity=item["severity"],
                    scope=item["scope"],
                    evidence=item["evidence"],
                )
                for item in issues
            ]
            report["recorded"] = record_issues(
                conn, objects, data_version_id=args.data_version
            )
        return _emit(report)
    finally:
        conn.close()


def _cmd_versions(settings: Settings, args: argparse.Namespace) -> int:
    conn = _open_readonly(settings)
    try:
        versions = published_versions(
            conn,
            limit=args.limit if args.limit is not None else None,
            offset=max(0, args.offset),
        )
        payload: dict[str, Any] = {"versions": versions, "count": len(versions)}
        if args.summary:
            for version in versions:
                version.pop("quality_summary", None)
                version.pop("scope", None)
        return _emit(payload)
    finally:
        conn.close()


def _cmd_pool_snapshot(settings: Settings, args: argparse.Namespace) -> int:
    conn = _open_readwrite(settings)
    try:
        version_id = args.data_version
        if version_id is None:
            versions = published_versions(conn)
            if not versions:
                raise DataError("no_data_version", "还没有已发布的数据版本，无法生成股票池快照")
            version_id = versions[-1]["version_id"]
        as_of = require_yyyymmdd(args.as_of) if args.as_of else _default_as_of(conn)
        rule = PoolRule(
            min_history_trade_days=args.min_history_days,
            liquidity_window_days=args.liquidity_window_days,
            min_avg_amount_cny=args.min_avg_amount,
            require_trading_on_as_of=not args.allow_stale_quote,
            max_quote_age_trade_days=args.max_quote_age_days,
        )
        snapshot = generate_pool_snapshot(
            conn,
            as_of=as_of,
            data_version_id=version_id,
            source_code=settings.data_source.source_code,
            rule=rule,
            ts_codes=_split_codes(args.codes) or None,
        )
        return _emit(snapshot)
    finally:
        conn.close()


def _default_as_of(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT MAX(trade_date) FROM daily_price WHERE source_code=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM daily_price d2"
        "                    WHERE d2.source_code=daily_price.source_code"
        "                      AND d2.ts_code=daily_price.ts_code"
        "                      AND d2.trade_date=daily_price.trade_date)",
        (_any_source(conn),),
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    raise DataError("no_data", "库中没有行情记录，无法推断截止时点")


def _any_source(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT source_code FROM daily_price ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else ""


def _cmd_eligibility(settings: Settings, args: argparse.Namespace) -> int:
    conn = _open_readonly(settings)
    try:
        codes = _split_codes(args.codes)
        if not codes and not args.data_version:
            # 缺省范围：最近一次股票池快照的成员；没有快照时退回全部已入库证券。
            snapshots = list_pool_snapshots(conn)
            if snapshots:
                latest = load_pool_snapshot(conn, snapshots[-1]["snapshot_id"])
                codes = tuple(latest["members"]) if latest else ()
        result = check_eligibility(
            conn,
            purpose=args.purpose,
            as_of=require_yyyymmdd(args.as_of),
            source_code=settings.data_source.source_code,
            data_version_id=args.data_version,
            ts_codes=codes or None,
            business_date=require_yyyymmdd(args.business_date) if args.business_date else None,
        )
        return _emit(result.as_dict())
    finally:
        conn.close()


def _cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    return _emit(data_status(settings))


def _cmd_stats(settings: Settings, args: argparse.Namespace) -> int:
    """资源与规模记录：证券数、日期跨度、行数、耗时、存储占用。"""
    conn = _open_readonly(settings)
    try:
        source_code = settings.data_source.source_code
        coverage = coverage_summary(conn, source_code)
        batches = [
            {
                "batch_id": row[0],
                "mode": row[1],
                "status": row[2],
                "pages_ok": row[3],
                "pages_failed": row[4],
                "attempts": row[5],
                "started_at": row[6],
                "finished_at": row[7],
            }
            for row in conn.execute(
                "SELECT batch_id, mode, status, pages_ok, pages_failed, attempts, started_at,"
                " finished_at FROM import_batch ORDER BY created_at"
            ).fetchall()
        ]
        versions = published_versions(conn)
        securities = conn.execute(
            "SELECT COUNT(DISTINCT ts_code) FROM security_info WHERE source_code=?", (source_code,)
        ).fetchone()[0]
    finally:
        conn.close()
    db_bytes = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    raw_bytes = directory_bytes(settings.raw_archive_dir)
    archive_bytes = directory_bytes(__import__("pathlib").Path(settings.runtime_dir) / "archives")
    backup_bytes = directory_bytes(settings.backup_dir)
    side_bytes = (
        (__import__("pathlib").Path(settings.runtime_dir) / "maintenance.db").stat().st_size
        if (__import__("pathlib").Path(settings.runtime_dir) / "maintenance.db").exists()
        else 0
    )
    wal_bytes = 0
    for suffix in ("-wal", "-shm"):
        candidate = __import__("pathlib").Path(str(settings.db_path) + suffix)
        if candidate.exists():
            wal_bytes += candidate.stat().st_size
    physical, limitations = file_identity_bytes(
        [
            settings.runtime_dir,
        ]
    )
    return _emit(
        {
            "source_code": source_code,
            "securities": int(securities),
            "coverage": coverage,
            "batches": batches,
            "versions": [
                {"version_id": item["version_id"], "published_at": item["published_at"]}
                for item in versions
            ],
            "storage": {
                "database_bytes": db_bytes,
                "database_wal_shm_bytes": wal_bytes,
                "raw_archive_bytes": raw_bytes,
                "archive_bytes": archive_bytes,
                "backup_bytes": backup_bytes,
                "maintenance_db_bytes": side_bytes,
                "runtime_logical_bytes": db_bytes + wal_bytes + raw_bytes + archive_bytes + backup_bytes + side_bytes,
                "runtime_physical_bytes": physical,
                "limitations": limitations,
            },
        }
    )


def _cmd_coverage(settings: Settings, args: argparse.Namespace) -> int:
    conn = _open_readwrite(settings) if args.refresh else _open_readonly(settings)
    try:
        if args.refresh:
            with conn:
                update_coverage_stats(conn, settings.data_source.source_code)
        return _emit({"coverage": coverage_summary(conn, settings.data_source.source_code)})
    finally:
        conn.close()


def _cmd_backup(settings: Settings, args: argparse.Namespace) -> int:
    out = args.out
    lock = DataWriteLock(settings.lock_dir / "data-write.lock", wait_seconds=30.0)
    lock.acquire()
    try:
        result = create_backup(settings, out_dir=None if out is None else __import__("pathlib").Path(out))
    finally:
        lock.release()
    return _emit(result.as_dict())


def _cmd_restore(settings: Settings, args: argparse.Namespace) -> int:
    if args.version_id:
        if not args.target_db:
            raise DataError("target_required", "--version-id 恢复需要 --target-db")
        return _emit(
            restore_version(
                settings, version_id=args.version_id, target_db=__import__("pathlib").Path(args.target_db)
            )
        )
    if not args.from_dir or not args.to_dir:
        raise DataError("restore_args", "数据库恢复需要 --from 备份包与 --to 目标目录")
    return _emit(
        restore_package(
            __import__("pathlib").Path(args.from_dir),
            __import__("pathlib").Path(args.to_dir),
        )
    )


def _cmd_cleanup(settings: Settings, args: argparse.Namespace) -> int:
    if args.action == "preview":
        as_of = args.as_of
        if not as_of:
            conn = _open_readonly(settings)
            try:
                as_of = _default_as_of(conn)
            finally:
                conn.close()
        return _emit(
            preview_cleanup(
                settings,
                source_code=args.source or settings.data_source.source_code,
                as_of=as_of,
                policy_version=args.policy,
            )
        )
    if args.action == "apply":
        if not args.preview_id:
            raise DataError("preview_required", "cleanup apply 需要 --preview-id")
        return _emit(
            apply_cleanup(
                settings,
                preview_id=args.preview_id,
                create_recovery_point=not args.no_recovery_point,
                recovery_point=(
                    __import__("pathlib").Path(args.recovery_point)
                    if args.recovery_point
                    else None
                ),
            )
        )
    return _emit(cleanup_status(settings, preview_id=args.preview_id))


def _cmd_hold(settings: Settings, args: argparse.Namespace) -> int:
    if args.action == "list":
        conn = _open_readwrite(settings)
        try:
            return _emit({"holds": list_holds(conn)})
        finally:
            conn.close()
    conn = _open_readwrite(settings)
    try:
        if args.action == "add":
            if not args.target_type or not args.target_id or not args.reason:
                raise DataError("hold_args", "hold add 需要 --target-type/--target-id/--reason")
            hold_id = add_hold(
                conn,
                target_type=args.target_type,
                target_id=args.target_id,
                reason=args.reason,
                expires_at=args.expires_at,
            )
            return _emit({"hold_id": hold_id})
        if not args.hold_id:
            raise DataError("hold_args", "hold remove 需要 --hold-id")
        return _emit({"removed": remove_hold(conn, args.hold_id)})
    finally:
        conn.close()


def _cmd_retention(settings: Settings, args: argparse.Namespace) -> int:
    conn = _open_readwrite(settings)
    try:
        ensure_policy(conn)
        return _emit({"policy": current_policy(conn, args.policy)})
    finally:
        conn.close()


def _cmd_verify_archive(settings: Settings, args: argparse.Namespace) -> int:
    import hashlib

    conn = _open_readonly(settings)
    try:
        row = conn.execute(
            "SELECT archive_id, path, sha256, bytes FROM archive_manifest WHERE archive_id=?",
            (args.archive_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise DataError("archive_not_found", f"归档 {args.archive_id} 不存在")
    target = __import__("pathlib").Path(settings.runtime_dir) / "archives" / row[1]
    if not target.is_file():
        return _emit({"verified": False, "reason": "missing", "path": str(target)})
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return _emit(
        {
            "verified": digest == row[2] and target.stat().st_size == row[3],
            "archive_id": row[0],
            "path": str(target),
            "sha256": digest,
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.data.cli", description="阶段 2 数据操作入口")
    sub = parser.add_subparsers(dest="command", required=True)

    p_source = sub.add_parser("source-check", help="核对来源配置与接口能力")
    p_source.add_argument("--probe", action="store_true", help="发起一次最小请求验证连通性")
    p_source.set_defaults(func=_cmd_source_check)

    p_migrate = sub.add_parser("migrate", help="显式迁移到目标 schema（1/2 → 3）")
    p_migrate.add_argument("--skip-convert", action="store_true", help="暂不把旧成员转换为分块存储")
    p_migrate.add_argument(
        "--reclaim-legacy-members",
        action="store_true",
        help="核对分块副本一致后删除旧全量成员行",
    )
    p_migrate.set_defaults(func=_cmd_migrate)

    p_history = sub.add_parser("import-history", help="批量历史导入")
    _add_import_arguments(p_history)
    p_history.set_defaults(func=lambda settings, args: _cmd_import(settings, args, "history"))

    p_incremental = sub.add_parser("import-incremental", help="增量更新")
    _add_import_arguments(p_incremental)
    p_incremental.set_defaults(func=lambda settings, args: _cmd_import(settings, args, "incremental"))

    p_quality = sub.add_parser("quality-check", help="执行质量检查")
    p_quality.add_argument("--as-of", help="截止时点 YYYYMMDD")
    p_quality.add_argument("--data-version", help="只检查指定数据版本的成员")
    p_quality.add_argument("--batch-size", type=int, default=5000, help="流式批大小（行）")
    p_quality.add_argument("--record", action="store_true", help="把发现的问题写入质量问题表")
    p_quality.set_defaults(func=_cmd_quality_check)

    p_versions = sub.add_parser("versions", help="列出数据版本")
    p_versions.add_argument("--limit", type=int, help="最多返回条数")
    p_versions.add_argument("--offset", type=int, default=0, help="跳过条数")
    p_versions.add_argument("--summary", action="store_true", help="省略 scope/quality 大字段")
    p_versions.set_defaults(func=_cmd_versions)

    p_pool = sub.add_parser("pool-snapshot", help="生成历史股票池快照")
    p_pool.add_argument("--as-of", help="截止时点 YYYYMMDD")
    p_pool.add_argument("--data-version", help="数据版本 ID；缺省为最近发布版本")
    p_pool.add_argument("--codes", help="限定证券样本，逗号分隔")
    p_pool.add_argument(
        "--min-history-days",
        type=int,
        default=DEFAULT_POOL_RULE.min_history_trade_days,
        help="最少历史交易日数",
    )
    p_pool.add_argument(
        "--liquidity-window-days",
        type=int,
        default=DEFAULT_POOL_RULE.liquidity_window_days,
        help="流动性窗口交易日数",
    )
    p_pool.add_argument(
        "--min-avg-amount",
        type=float,
        default=DEFAULT_POOL_RULE.min_avg_amount_cny,
        help="窗口平均成交额下限（元）",
    )
    p_pool.add_argument(
        "--allow-stale-quote",
        action="store_true",
        help="允许截止时点没有当日报价的证券进入候选",
    )
    p_pool.add_argument(
        "--max-quote-age-days",
        type=int,
        default=DEFAULT_POOL_RULE.max_quote_age_trade_days,
        help="允许的行情落后交易日数",
    )
    p_pool.set_defaults(func=_cmd_pool_snapshot)

    p_eligibility = sub.add_parser("eligibility", help="判断数据是否满足指定用途")
    p_eligibility.add_argument(
        "--purpose",
        required=True,
        choices=[
            "display",
            "signal_input",
            "execution_reference",
            "valuation_reference",
            "strict_historical_replay",
        ],
    )
    p_eligibility.add_argument("--as-of", required=True, help="截止时点 YYYYMMDD")
    p_eligibility.add_argument("--business-date", help="业务日期 YYYYMMDD")
    p_eligibility.add_argument("--data-version", help="数据版本 ID")
    p_eligibility.add_argument("--codes", help="限定证券样本，逗号分隔")
    p_eligibility.set_defaults(func=_cmd_eligibility)

    p_status = sub.add_parser("status", help="数据准备程度摘要")
    p_status.set_defaults(func=_cmd_status)

    p_stats = sub.add_parser("stats", help="资源与规模记录（证券数、跨度、行数、存储）")
    p_stats.set_defaults(func=_cmd_stats)

    p_coverage = sub.add_parser("coverage", help="查看或刷新覆盖统计摘要")
    p_coverage.add_argument("--refresh", action="store_true", help="重算并持久化统计")
    p_coverage.set_defaults(func=_cmd_coverage)

    p_backup = sub.add_parser("backup", help="创建含外部文件的验证备份包")
    p_backup.add_argument("--out", help="备份根目录（默认 .runtime/backups）")
    p_backup.set_defaults(func=_cmd_backup)

    p_restore = sub.add_parser("restore", help="从备份包或归档版本恢复")
    p_restore.add_argument("--from", dest="from_dir", help="备份包目录")
    p_restore.add_argument("--to", dest="to_dir", help="恢复目标目录")
    p_restore.add_argument("--version-id", help="从归档恢复指定版本")
    p_restore.add_argument("--target-db", help="归档版本恢复的目标数据库")
    p_restore.set_defaults(func=_cmd_restore)

    p_cleanup = sub.add_parser("cleanup", help="预览/应用/查看有限清理")
    p_cleanup.add_argument("action", choices=["preview", "apply", "status"])
    p_cleanup.add_argument("--as-of", help="窗口截止业务日 YYYYMMDD")
    p_cleanup.add_argument("--source", help="数据来源代码；缺省为当前配置来源")
    p_cleanup.add_argument("--policy", help="保留政策版本")
    p_cleanup.add_argument("--preview-id", help="预览 ID")
    p_cleanup.add_argument(
        "--no-recovery-point",
        action="store_true",
        help="不新建恢复点；必须同时提供 --recovery-point 已验证包",
    )
    p_cleanup.add_argument("--recovery-point", help="已验证的操作前备份包目录")
    p_cleanup.set_defaults(func=_cmd_cleanup)

    p_hold = sub.add_parser("hold", help="引用保护")
    p_hold.add_argument("action", choices=["add", "list", "remove"])
    p_hold.add_argument("--target-type", choices=["version", "snapshot", "record_range", "chunk"])
    p_hold.add_argument("--target-id")
    p_hold.add_argument("--reason")
    p_hold.add_argument("--expires-at")
    p_hold.add_argument("--hold-id")
    p_hold.set_defaults(func=_cmd_hold)

    p_retention = sub.add_parser("retention", help="查看保留政策")
    p_retention.add_argument("action", nargs="?", choices=["show"], default="show")
    p_retention.add_argument("--policy")
    p_retention.set_defaults(func=_cmd_retention)

    p_verify = sub.add_parser("verify-archive", help="校验归档摘要")
    p_verify.add_argument("--archive-id", required=True)
    p_verify.set_defaults(func=_cmd_verify_archive)

    return parser


def _add_import_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", help="起始业务日期 YYYYMMDD")
    parser.add_argument("--end", help="结束业务日期 YYYYMMDD")
    parser.add_argument("--codes", help="证券样本，逗号分隔")
    parser.add_argument("--categories", help="数据类别，逗号分隔；缺省为全部")
    parser.add_argument("--trade-date", action="append", help="指定交易日（可重复）")
    parser.add_argument(
        "--daily-by-trade-date",
        action="store_true",
        help="日线按交易日取全市场（默认按证券取）",
    )
    parser.add_argument("--allow-partial", action="store_true", help="允许缩小范围后发布版本")
    parser.add_argument("--exchange", help="交易所代码，默认 SSE")
    parser.add_argument("--resume-batch", help="从指定批次的检查点继续")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        settings = load_settings()
        return int(args.func(settings, args))
    except DatabaseError as exc:
        return _emit({"error": exc.reason, "message": str(exc)})
    except DataError as exc:
        return _emit({"error": exc.reason, "message": str(exc)})


if __name__ == "__main__":
    sys.exit(main())

"""合成规模与增长基准（离线、临时目录、可重复）。

两种模式：

- ``--mode storage``：直接调用写入/发布/质量/状态层，明确标注为存储层基准；
- ``--mode pipeline``：通过 ``run_import`` 完整导入管线（合成适配器、归一化、质量、
  检查点、窗口）执行历史导入、30 次烟测与 20 次日常增量，并单独报告 250 次
  清单层发布。

安全：默认创建新的唯一临时目录；显式 ``--workdir`` 若已存在，只在其中创建唯一
子目录，绝不递归删除用户目录。所有生成物不进入仓库。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
import uuid
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import load_settings  # noqa: E402
from app.data.adapters.base import DataAdapter, DataRequest, FetchOutcome  # noqa: E402
from app.data.backup import directory_bytes, file_identity_bytes  # noqa: E402
from app.data.chunks import publish_chunked_version  # noqa: E402
from app.data.migration import initialize_database  # noqa: E402
from app.data.pipeline import ImportOptions, run_import  # noqa: E402
from app.data.quality import run_dataset_quality_check  # noqa: E402
from app.data.records import (  # noqa: E402
    ALL_CATEGORIES,
    CATEGORY_ADJ_FACTOR,
    CATEGORY_CALENDAR,
    CATEGORY_DAILY,
    CATEGORY_SECURITY,
)
from app.data.status import data_status  # noqa: E402
from app.data.store import latest_active_version_id, write_records  # noqa: E402
from app.data.timeutil import yyyymmdd  # noqa: E402


def _progress(stage: str, seconds: float, **extra) -> None:
    payload = {"stage": stage, "seconds": round(seconds, 3), **extra}
    print("PROGRESS " + json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def _p95(values: list[float]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _rss_bytes() -> tuple[int | None, str | None]:
    if os.name != "nt":
        return None, "GetProcessMemoryInfo unavailable on this platform"
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        if not ok:
            return None, "GetProcessMemoryInfo returned 0"
        return int(counters.PeakWorkingSetSize), None
    except Exception as exc:  # pragma: no cover - platform dependent
        return None, f"RSS measurement failed: {exc}"


def _trading_days(years: int, end_year: int = 2024) -> list[str]:
    start = date(end_year - years + 1, 1, 2)
    end = date(end_year, 12, 31)
    days: list[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(yyyymmdd(current))
        current += timedelta(days=1)
    return days


def _code(index: int) -> str:
    return f"{600000 + index:06d}.SH"


def _close(code_index: int, day_index: int) -> float:
    return round(10.0 + (code_index * 37 + day_index * 13) % 1000 / 100.0, 2)


def _daily_record(code: str, day: str, close: float) -> dict:
    return {
        "ts_code": code,
        "trade_date": day,
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "pre_close": close,
        "volume": 100000.0,
        "amount": close * 100000.0,
        "currency": "CNY",
        "source_volume_raw": 1000.0,
        "source_amount_raw": close * 1000.0,
    }


def _factor_record(code: str, day: str) -> dict:
    return {"ts_code": code, "trade_date": day, "adj_factor": 1.0}


class SyntheticAdapter(DataAdapter):
    """确定性合成适配器；支持增量日与有限窗口修订。"""

    source_code = "tushare-fixture"

    def __init__(self, days: list[str], codes: int) -> None:
        self.days = days
        self.day_index = {day: index for index, day in enumerate(days)}
        self.codes = codes
        self.extra_days: list[str] = []
        self.revisions: dict[tuple[str, str], float] = {}

    def add_day(self, day: str) -> None:
        if day not in self.day_index:
            self.day_index[day] = len(self.days)
            self.days.append(day)

    def _rows_for_days(self, ts_code: str, start: str, end: str) -> list[dict]:
        code_index = int(ts_code.split(".")[0]) - 600000
        rows = []
        for day in self.days:
            if start and day < start:
                continue
            if end and day > end:
                continue
            close = self.revisions.get((ts_code, day), _close(code_index, self.day_index[day]))
            rows.append(
                {
                    "ts_code": ts_code,
                    "trade_date": day,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "pre_close": close,
                    "vol": 1000.0,
                    "amount": close * 1000.0,
                }
            )
        return rows

    def max_rows(self, interface: str) -> int:
        return 100000

    def fetch_page(self, request: DataRequest) -> FetchOutcome:
        params = request.params
        interface = request.interface
        if request.page_no > 1:
            return FetchOutcome(request=request, status="empty")
        if interface == "trade_cal":
            start = str(params.get("start_date") or "")
            end = str(params.get("end_date") or "")
            rows = [
                {"exchange": params.get("exchange", "SSE"), "cal_date": day, "is_open": 1,
                 "pretrade_date": None}
                for day in self.days
                if (not start or day >= start) and (not end or day <= end)
            ]
        elif interface == "stock_basic":
            status = params.get("list_status")
            rows = []
            if status == "L":
                for index in range(self.codes):
                    code = _code(index)
                    rows.append(
                        {
                            "ts_code": code,
                            "symbol": code.split(".")[0],
                            "name": code,
                            "market": "主板",
                            "exchange": "SSE",
                            "list_date": "20100101",
                            "delist_date": None,
                            "list_status": "L",
                        }
                    )
        elif interface == "daily":
            if params.get("trade_date"):
                ts_codes = [_code(index) for index in range(self.codes)]
                start = end = str(params["trade_date"])
            else:
                ts_codes = [str(params.get("ts_code"))]
                start = str(params.get("start_date") or "")
                end = str(params.get("end_date") or "")
            rows = []
            for code in ts_codes:
                rows.extend(self._rows_for_days(code, start, end))
        elif interface == "adj_factor":
            ts_code = str(params.get("ts_code"))
            start = str(params.get("start_date") or "")
            end = str(params.get("end_date") or "")
            rows = [
                {"ts_code": ts_code, "trade_date": row["trade_date"], "adj_factor": 1.0}
                for row in self._rows_for_days(ts_code, start, end)
            ]
        elif interface == "suspend_d":
            ts_code = str(params.get("ts_code"))
            rows = []
            first = self.days[0]
            if params.get("start_date", "") <= first <= params.get("end_date", "99999999"):
                rows.append(
                    {
                        "ts_code": ts_code,
                        "trade_date": first,
                        "suspend_type": "S",
                        "suspend_timing": None,
                    }
                )
        elif interface == "dividend":
            ts_code = str(params.get("ts_code"))
            rows = [
                {
                    "ts_code": ts_code,
                    "end_date": "20231231",
                    "ann_date": "20240102",
                    "div_proc": "实施",
                    "cash_div": 0.1,
                }
            ]
        else:
            return FetchOutcome(request=request, status="empty")
        return FetchOutcome(request=request, status="ok" if rows else "empty", rows=rows)


def _space(settings) -> dict:
    runtime = Path(settings.runtime_dir)
    dirs = {
        "database": settings.db_path,
        "raw": Path(settings.raw_archive_dir),
        "archives": runtime / "archives",
        "backups": Path(settings.backup_dir),
        "maintenance_db": runtime / "maintenance.db",
    }
    reported = {name: directory_bytes(path) for name, path in dirs.items()}
    wal = sum(
        Path(str(settings.db_path) + suffix).stat().st_size
        for suffix in ("-wal", "-shm")
        if Path(str(settings.db_path) + suffix).exists()
    )
    physical, limitations = file_identity_bytes([runtime])
    return {
        "logical_bytes": reported,
        "wal_shm_bytes": wal,
        "runtime_logical_bytes": sum(reported.values()) + wal,
        "runtime_physical_bytes": physical,
        "limitations": limitations,
    }


def _settings(workdir: Path, years: int):
    return load_settings(
        {
            "APP_DATA_SOURCE": "tushare-fixture",
            "APP_RUNTIME_DIR": str(workdir / "runtime"),
            "APP_IMPORT_MIN_INTERVAL_SECONDS": "0",
            "APP_WORKING_WINDOW_YEARS": str(years),
        },
        project_root=workdir,
    )


def run_storage(codes: int, years: int, workdir: Path, increments: int, manifest_publishes: int) -> dict:
    settings = _settings(workdir, years)
    initialize_database(settings)
    days = _trading_days(years)
    day_index = {day: index for index, day in enumerate(days)}
    code_list = [_code(index) for index in range(codes)]
    result: dict = {
        "mode": "storage-layer (not full import pipeline)",
        "codes": codes,
        "years": years,
        "trading_days": len(days),
        "daily_rows_generated": codes * len(days),
    }
    conn = sqlite3.connect(str(settings.db_path), timeout=60.0)
    conn.row_factory = sqlite3.Row

    t0 = time.perf_counter()
    write_records(conn, CATEGORY_CALENDAR,
                  [{"exchange": "SSE", "cal_date": day, "is_open": 1, "pretrade_date": None} for day in days],
                  source_code="tushare-fixture")
    write_records(conn, CATEGORY_SECURITY,
                  [{"ts_code": code, "symbol": code.split(".")[0], "name": code, "market": "主板",
                    "exchange": "SSE", "list_date": "20100101", "delist_date": None,
                    "list_status": "L"} for code in code_list],
                  source_code="tushare-fixture")
    for code_index, code in enumerate(code_list):
        for start in range(0, len(days), 250):
            batch_days = days[start : start + 250]
            write_records(conn, CATEGORY_DAILY,
                          [_daily_record(code, day, _close(code_index, day_index[day])) for day in batch_days],
                          source_code="tushare-fixture")
    result["load_seconds"] = round(time.perf_counter() - t0, 3)
    _progress("storage_load", result["load_seconds"], rows=result["daily_rows_generated"])

    tracemalloc.start()
    t0 = time.perf_counter()
    version = publish_chunked_version(
        conn, source_code="tushare-fixture", scope={"start": days[0], "end": days[-1]},
        categories=list(ALL_CATEGORIES), rule_version="bench", quality_summary={},
    )
    result["initial_publish_seconds"] = round(time.perf_counter() - t0, 3)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result["initial_publish_py_heap_mib"] = round(peak / 2**20, 2)
    result["initial_members"] = version["member_count"]
    result["initial_chunks"] = version["chunk_count"]
    _progress("storage_publish", result["initial_publish_seconds"], chunks=version["chunk_count"])

    t0 = time.perf_counter()
    quality = run_dataset_quality_check(conn, "tushare-fixture", as_of=days[-1], batch_size=5000)
    result["quality_seconds"] = round(time.perf_counter() - t0, 3)
    result["quality_records"] = quality["record_count"]
    _progress("storage_quality", result["quality_seconds"], records=quality["record_count"])

    timings = []
    for _ in range(30):
        t0 = time.perf_counter()
        data_status(settings)
        timings.append(time.perf_counter() - t0)
    result["status_call_p95_seconds"] = round(_p95(timings), 4)
    result["status_call_mean_seconds"] = round(statistics.mean(timings), 4)
    result["status_call_samples"] = len(timings)

    # 250 次清单层发布：每次产生一条真实修订，不靠改 scope 造假。
    base = latest_active_version_id(conn, "tushare-fixture")
    manifest_times = []
    sizes = []
    for index in range(manifest_publishes):
        code = code_list[index % len(code_list)]
        day = days[index % len(days)]
        t0 = time.perf_counter()
        outcome = write_records(
            conn, CATEGORY_DAILY,
            [_daily_record(code, day, 100.0 + index / 100.0)],
            source_code="tushare-fixture",
        )
        publish_chunked_version(
            conn, source_code="tushare-fixture", scope={"manifest_layer": index},
            categories=list(ALL_CATEGORIES), rule_version="bench", quality_summary={},
            base_version_id=base,
            affected_partitions={category: sorted({p for c, p in outcome.affected_partitions if c == category})
                                 for category in {c for c, _p in outcome.affected_partitions}},
            window_mode="working", window_start=days[0],
        )
        base = latest_active_version_id(conn, "tushare-fixture")
        manifest_times.append(time.perf_counter() - t0)
        sizes.append(settings.db_path.stat().st_size)
    result["manifest_publishes"] = manifest_publishes
    result["manifest_mean_seconds"] = round(statistics.mean(manifest_times), 4) if manifest_times else None
    result["manifest_db_growth_bytes"] = (sizes[-1] - sizes[0]) if len(sizes) > 1 else 0
    _progress("storage_manifest", sum(manifest_times), runs=len(manifest_times))

    result["version_count"] = conn.execute("SELECT COUNT(*) FROM data_version").fetchone()[0]
    result["chunk_count"] = conn.execute("SELECT COUNT(*) FROM data_chunk").fetchone()[0]
    result["version_ref_rows"] = conn.execute("SELECT COUNT(*) FROM data_version_chunk").fetchone()[0]
    result["space"] = _space(settings)
    peak_rss, rss_note = _rss_bytes()
    result["peak_rss_bytes"] = peak_rss
    result["peak_rss_note"] = rss_note
    conn.close()
    return result


def run_pipeline(codes: int, years: int, workdir: Path, smoke: int, increments: int) -> dict:
    settings = _settings(workdir, years)
    initialize_database(settings)
    days = _trading_days(years)
    code_list = [_code(index) for index in range(codes)]
    adapter = SyntheticAdapter(days, codes)
    result: dict = {
        "mode": "full import pipeline (synthetic adapter, offline)",
        "codes": codes,
        "years": years,
        "trading_days": len(days),
    }
    t0 = time.perf_counter()
    history = run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily", "adj_factor", "suspension", "corporate_action"),
            start=days[0],
            end=days[-1],
            codes=tuple(code_list),
        ),
        adapter=adapter,
    )
    result["history_seconds"] = round(time.perf_counter() - t0, 3)
    result["history_status"] = history.status
    result["history_version"] = history.version["version_id"] if history.version else None
    _progress("pipeline_history", result["history_seconds"], status=history.status)

    # 30 次小规模不同版本烟测：每次新增一天，只取前 5 只证券。
    smoke_times = []
    last = date(int(days[-1][:4]), int(days[-1][4:6]), int(days[-1][6:8]))
    for _ in range(smoke):
        last += timedelta(days=1)
        while last.weekday() >= 5:
            last += timedelta(days=1)
        day = yyyymmdd(last)
        adapter.add_day(day)
        t0 = time.perf_counter()
        run_import(
            settings,
            ImportOptions(
                mode="incremental",
                categories=("daily", "adj_factor"),
                start=day,
                end=day,
                codes=tuple(code_list[:5]),
            ),
            adapter=adapter,
        )
        smoke_times.append(time.perf_counter() - t0)
    result["smoke_runs"] = smoke
    result["smoke_mean_seconds"] = round(statistics.mean(smoke_times), 3) if smoke_times else None
    _progress("pipeline_smoke", sum(smoke_times), runs=len(smoke_times))

    # 20 次目标规模日常增量；每 5 次注入有限历史修订。
    increment_times = []
    for index in range(increments):
        last += timedelta(days=1)
        while last.weekday() >= 5:
            last += timedelta(days=1)
        day = yyyymmdd(last)
        adapter.add_day(day)
        if index % 5 == 0 and index > 0:
            revision_day = days[-10 - index]
            for code_index, code in enumerate(code_list[: max(1, codes // 10)]):
                adapter.revisions[(code, revision_day)] = _close(code_index, 0) + 50.0
        t0 = time.perf_counter()
        run_import(
            settings,
            ImportOptions(
                mode="incremental",
                categories=("daily", "adj_factor"),
                start=day,
                end=day,
                codes=tuple(code_list),
            ),
            adapter=adapter,
        )
        increment_times.append(time.perf_counter() - t0)
    result["increment_runs"] = increments
    result["increment_mean_seconds"] = round(statistics.mean(increment_times), 3) if increment_times else None
    result["increment_max_seconds"] = round(max(increment_times), 3) if increment_times else None
    _progress("pipeline_increments", sum(increment_times), runs=len(increment_times))

    # 完整质量检查与状态 p95（TestClient 本地 HTTP 另计）。
    conn = sqlite3.connect(str(settings.db_path), timeout=60.0)
    t0 = time.perf_counter()
    quality = run_dataset_quality_check(
        conn, "tushare-fixture", as_of=yyyymmdd(last), batch_size=5000
    )
    result["quality_seconds"] = round(time.perf_counter() - t0, 3)
    result["quality_records"] = quality["record_count"]
    conn.close()
    _progress("pipeline_quality", result["quality_seconds"], records=quality["record_count"])

    direct = []
    for _ in range(30):
        t0 = time.perf_counter()
        data_status(settings)
        direct.append(time.perf_counter() - t0)
    result["status_direct_p95_seconds"] = round(_p95(direct), 4)
    result["status_direct_mean_seconds"] = round(statistics.mean(direct), 4)
    try:
        from fastapi.testclient import TestClient

        from app.main import create_app

        # 用 with 触发 lifespan，确保 app.state.settings 与真实启动路径一致。
        with TestClient(create_app(settings)) as client:
            http = []
            for _ in range(30):
                t0 = time.perf_counter()
                response = client.get("/api/data/status")
                assert response.status_code == 200, response.text
                http.append(time.perf_counter() - t0)
        result["status_http_p95_seconds"] = round(_p95(http), 4)
        result["status_http_mean_seconds"] = round(statistics.mean(http), 4)
    except Exception as exc:  # pragma: no cover - optional
        result["status_http_p95_seconds"] = None
        result["status_http_note"] = f"HTTP timing failed: {exc}"

    result["space"] = _space(settings)
    peak_rss, rss_note = _rss_bytes()
    result["peak_rss_bytes"] = peak_rss
    result["peak_rss_note"] = rss_note
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["storage", "pipeline"], default="storage")
    parser.add_argument("--codes", type=int, default=100)
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--smoke", type=int, default=30)
    parser.add_argument("--increments", type=int, default=20)
    parser.add_argument("--manifest-publishes", type=int, default=250)
    parser.add_argument("--workdir", default=None)
    args = parser.parse_args()

    if args.workdir:
        base = Path(args.workdir)
        base.mkdir(parents=True, exist_ok=True)
        workdir = base / f"run-{uuid.uuid4().hex[:8]}"
        workdir.mkdir(parents=True, exist_ok=False)
    else:
        # mkdtemp 已创建唯一目录；不删除、不覆盖任何既有目录。
        workdir = Path(tempfile.mkdtemp(prefix="p2bench-"))
    print(f"WORKDIR {workdir}", file=sys.stderr, flush=True)
    try:
        if args.mode == "pipeline":
            result = run_pipeline(args.codes, args.years, workdir, args.smoke, args.increments)
        else:
            result = run_storage(
                args.codes, args.years, workdir, args.increments, args.manifest_publishes
            )
        result["workdir"] = str(workdir)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        # 保留结果目录供审查；只打印位置，不自动删除。
        print(f"WORKDIR_RETAINED {workdir}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

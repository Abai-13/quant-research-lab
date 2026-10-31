"""Reproducible offline signal, registered selection and target allocation example.

Uses synthetic data and a TEMP database; importing has no runtime effects.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def business_days(count: int) -> list[str]:
    days: list[str] = []
    cursor = date(2024, 1, 2)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return days


def formula_cross_check(prices: list[float]):
    """Independent Decimal arithmetic used only to check the production calculator."""
    from decimal import Decimal

    if len(prices) != 120:
        raise ValueError("baseline sample requires exactly 120 valid observations")
    values = [Decimal(str(value)) for value in prices]
    momentum = values[-6] / values[-66] - Decimal("1")
    trend = sum(values, Decimal("0")) / Decimal(120)
    return momentum, trend


def main() -> None:
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))

    import sqlite3

    from app.config import load_settings
    from app.data.chunks import VersionView, publish_chunked_version
    from app.data.migration import initialize_database
    from app.data.store import write_records
    from app.portfolio.allocation import (
        AllocationRequest,
        BaselineEqualWeightCappedAllocation,
    )
    from app.portfolio.selection import (
        SELECTION_POLICY_REGISTRY,
        BaselineMomentumRankSelection,
    )
    from app.portfolio.signals import BaselineMomentumTrendCalculator
    from app.portfolio.service import BoundDataContext, DataSelectionContext
    from app.data.universe import generate_pool_snapshot
    from app.practice.algorithms import calculate_registered_algorithm
    from types import SimpleNamespace

    class ExampleSelection(BaselineMomentumRankSelection):
        """Named extension preserving baseline selection behavior for this sample."""

        code = "example_baseline_selection"
        version = "1.0.0"

    source_code = "synthetic-example"
    days = business_days(120)
    prices_by_code = {
        "SYN-A": [10.0] * 120,
        "SYN-B": [10.0] * 120,
    }
    prices_by_code["SYN-A"][-6] = 11.0
    prices_by_code["SYN-A"][-1] = 12.0
    prices_by_code["SYN-B"][-6] = 10.5

    # The TEMP database is isolated and removed automatically after this run.
    with TemporaryDirectory(prefix="quant-sample-") as temp_dir:
        root = Path(temp_dir)
        settings = load_settings(
            {
                # Valid offline source setting for initialization only; no source adapter is called.
                "APP_DATA_SOURCE": "tushare-fixture",
                "APP_RUNTIME_DIR": str(root / "runtime"),
            },
            project_root=root,
        )
        initialize_database(settings)
        conn = sqlite3.connect(settings.db_path)
        conn.row_factory = sqlite3.Row
        try:
            daily = []
            factors = []
            for code, prices in prices_by_code.items():
                for day, close in zip(days, prices):
                    daily.append(
                        {
                            "ts_code": code,
                            "trade_date": day,
                            "open": close,
                            "high": close,
                            "low": close,
                            "close": close,
                            "pre_close": close,
                            "volume": 10_000_000.0,
                            "amount": close * 10_000_000.0,
                            "currency": "CNY",
                            "source_volume_raw": 10_000_000.0,
                            "source_amount_raw": close * 10_000_000.0,
                        }
                    )
                    factors.append(
                        {"ts_code": code, "trade_date": day, "adj_factor": 1.0}
                    )
            calendar = [
                {"ts_code": "", "exchange": "SSE", "cal_date": day, "is_open": 1}
                for day in days
            ]
            securities = [
                {
                    "ts_code": code,
                    "symbol": code,
                    "name": code,
                    "market": "synthetic",
                    "exchange": "SSE",
                    "list_date": "20000101",
                    "delist_date": None,
                    "list_status": "L",
                }
                for code in sorted(prices_by_code)
            ]
            for category, rows in (
                ("daily", daily),
                ("adj_factor", factors),
                ("calendar", calendar),
                ("security", securities),
            ):
                write_records(conn, category, rows, source_code=source_code)
            version_id = publish_chunked_version(
                conn,
                source_code=source_code,
                scope={},
                categories=["daily", "adj_factor", "calendar", "security"],
                rule_version="synthetic-example-v1",
                quality_summary={},
                window_mode="full",
            )["version_id"]
            view = VersionView(
                conn, version_id, expected_source_code=source_code
            )
            pool = generate_pool_snapshot(
                conn, as_of=days[-1], data_version_id=version_id,
                source_code=source_code, ts_codes=sorted(prices_by_code),
            )
            # Explicit registry extension: reverse the baseline momentum order
            # after applying the same transparent eligibility gates.
            class ExampleSelection(BaselineMomentumRankSelection):
                code = "example_lowest_momentum_first"
                version = "1.0.0"
                called = False

                def select(self, values):
                    type(self).called = True
                    result = super().select(values)
                    from dataclasses import replace
                    return replace(result, selected=tuple(reversed(result.selected)))

            SELECTION_POLICY_REGISTRY.register(ExampleSelection.code, ExampleSelection)
            context = BoundDataContext(
                context=DataSelectionContext(
                    source_code=source_code, as_of=days[-1], purpose="signal_input",
                    data_version_id=version_id, account_revision=1,
                    rule_bundle_id="synthetic-example", pool_snapshot_id=pool["snapshot_id"],
                ),
                view=view,
                eligibility=SimpleNamespace(details=[
                    {"ts_code": code, "reason_code": "ok", "stale": False,
                     "history_trade_days": 120, "notes": []}
                    for code in sorted(prices_by_code)
                ]),
                restricted=False, restriction_code=None, restriction_detail=None,
                rule_bundle={"version": "synthetic-example-v1", "content": {
                    "target_constraints": {"cash_reserve_cents": 100_000,
                                           "per_security_weight_cap_bp": 4_000}
                }},
            )
            configuration = {
                "signal": {"code": "baseline_momentum5_trend120", "version": "s3-signal-1.0", "parameters": {}},
                "selection": {"code": ExampleSelection.code, "version": ExampleSelection.version, "parameters": {}},
                "allocation": {"code": "baseline_equal_weight_capped", "version": "s3-allocation-1.0", "parameters": {}},
            }
            account = {"revision": 1, "cash_cents": 1_000_000, "positions": []}
            algorithm_id = "+".join((configuration["signal"]["code"], configuration["selection"]["code"], configuration["allocation"]["code"]))
            algorithm_version = "+".join(configuration[kind]["version"] for kind in ("signal", "selection", "allocation"))
            service_first = calculate_registered_algorithm(conn, context, account,
                algorithm_id=algorithm_id, algorithm_version=algorithm_version, parameters={})
            service_second = calculate_registered_algorithm(conn, context, account,
                algorithm_id=algorithm_id, algorithm_version=algorithm_version, parameters={})
            assert service_first == service_second
            assert service_first["selection"]["selected"] == ["SYN-A"]
            assert service_first["allocation"]["targets"][0]["target_value_cents"] == 360_000
            assert service_first["components"]["selection"]["name"] == ExampleSelection.code
            assert ExampleSelection.called
            calculator = BaselineMomentumTrendCalculator()
            signals = [
                calculator.calculate(view=view, as_of=days[-1], ts_code=code)
                for code in sorted(prices_by_code)
            ]
            prior_signal = calculator.calculate(view=view, as_of=days[-2], ts_code="SYN-A")
            assert prior_signal.observation_count == 119
            assert not prior_signal.ok
            assert prior_signal.reason_code == "signal_history_insufficient_observations"
        finally:
            conn.close()

    # Check the real calculator output against separate Decimal arithmetic.
    for signal in signals:
        assert signal.ok and signal.observation_count == 120
        assert (signal.momentum, signal.trend) == formula_cross_check(
            prices_by_code[signal.ts_code]
        )

    selection_cls = ExampleSelection
    selected = selection_cls().select(signals)
    allocation = BaselineEqualWeightCappedAllocation().allocate(AllocationRequest(
        selected=selected.selected, cash_cents=1_000_000,
        bundle_content={"target_constraints": {"cash_reserve_cents": 100_000,
            "per_security_weight_cap_bp": 4_000}}, bundle_version="synthetic-example-v1"))

    assert selected.selected == ("SYN-A",)
    assert [item.reason_code for item in selected.excluded] == [
        "price_not_above_trend"
    ]
    assert allocation.targets[0].target_value_cents == 360_000
    assert allocation.reserved_cents == 100_000
    assert allocation.final_cash_cents == 540_000
    assert (
        allocation.reserved_cents
        + sum(item.target_value_cents for item in allocation.targets)
        + allocation.final_cash_cents
        == allocation.cash_cents
    )

    print(
        f"Synthetic input: 120 weekdays {days[0]}..{days[-1]}, "
        f"as_of={days[-1]}; weekday-only synthetic calendar, currency CNY."
    )
    for signal in signals:
        prices = prices_by_code[signal.ts_code]
        print(
            f"{signal.ts_code}: P(t-65)={prices[-66]:.2f}, "
            f"P(t-5)={prices[-6]:.2f}, P(t)={prices[-1]:.2f}, "
            f"trend={signal.trend}, momentum={signal.momentum}"
        )
    print(f"Registered extension selected: {', '.join(selected.selected)}")
    print(f"Practice target service selected: {service_first['selection']['selected'] or 'none (no qualified pool members)'}")
    print(f"Frozen practice components: {service_first['components']}")
    print(f"Frozen parameters: {service_first['frozen_configuration']}")
    print(
        "Allocation (cents): cash=1000000, reserve=100000, "
        "target=360000, final_cash_residual=540000"
    )
    print("Reconciliation: 100000 + 360000 + 540000 = 1000000")


if __name__ == "__main__":
    main()

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.agent_tools import capture_context, execute_tool, tool_definitions
from app.config import load_settings
from app.main import create_app
from app.practice.core import PracticeError
from app.practice.runtime import practice_settings
from app.data.chunks import VersionView
from app.portfolio.rule_bundles import create_rule_bundle, load_rule_bundle
from app.portfolio.simulation import _fee_schedule, _order_fee_estimate


def test_setup_scope_is_draft_only_and_does_not_initialize_practice_database(temp_settings):
    context = capture_context(temp_settings, None)
    result = execute_tool(
        temp_settings,
        context,
        "prepare_setup_draft",
        {"name": "示例", "start_date": "20250102", "end_date": "20250131", "cash_cents": 100000},
    )
    assert context["scope"] == "setup"
    assert result["drafts"][0]["kind"] == "setup"
    assert result["drafts"][0]["practice_id"] is None
    assert not practice_settings(temp_settings).db_path.exists()
    with pytest.raises(PracticeError, match="需要绑定现有模拟"):
        execute_tool(temp_settings, context, "get_simulation_state", {})


def test_tools_reject_unknown_tool_and_extra_arguments(temp_settings):
    context = capture_context(temp_settings, None)
    with pytest.raises(PracticeError, match="未知工具"):
        execute_tool(temp_settings, context, "run_sql", {})
    with pytest.raises(PracticeError, match="缺失或包含"):
        execute_tool(
            temp_settings,
            context,
            "prepare_setup_draft",
            {"name": "x", "start_date": "20250102", "end_date": "20250131", "cash_cents": 1, "sql": "select 1"},
        )
    schemas = tool_definitions()
    assert schemas and all(item["function"]["parameters"]["additionalProperties"] is False for item in schemas)


def test_read_tools_are_read_only_version_bound_and_hide_system_comparison(temp_settings):
    with TestClient(create_app(temp_settings)) as client:
        demo = client.post("/api/practice/demo").json()
        version = next(item for item in demo["versions"] if item["version_id"] == demo["demo"]["version_id"])
        code = demo["symbols"][0]["ts_code"]
        with sqlite3.connect(practice_settings(temp_settings).db_path) as fixture_db:
            simulation_rules = create_rule_bundle(
                fixture_db,
                kind="parameter",
                version="agent-tools-market-rules-1",
                assumption_scope="synthetic_simulation",
                content={"bundle_kind": "parameter", "market_rules": {"verification_status": "verified", "board_lot_shares": 100}},
            )
        response = client.post(
            "/api/practice/sessions",
            json={
                "name": "agent tools fixture",
                "start_date": demo["demo"]["start_date"],
                "end_date": demo["demo"]["end_date"],
                "cash_cents": 10_000_000,
                "positions": [{"ts_code": code, "shares": 200, "sellable_shares": 200, "locked": True}],
                "data_version_id": version["version_id"],
                "source_code": version["source_code"],
                "settings": {
                    "hide_system_hints": True,
                    "simulation_rule_bundle_id": simulation_rules["bundle_id"],
                    "fee_bundle_id": demo["defaults"]["fee_bundle_id"],
                },
                "idempotency_key": "agent-tools-create",
            },
        )
        assert response.status_code == 200, response.text
        session = response.json()["session"]
        practice_id = session["practice_id"]
        context = capture_context(temp_settings, practice_id, session["revision"])
        before = sqlite3.connect(practice_settings(temp_settings).db_path)
        try:
            before_count = before.execute("SELECT COUNT(*) FROM practice_order WHERE practice_id=?", (practice_id,)).fetchone()[0]
        finally:
            before.close()

        state = execute_tool(temp_settings, context, "get_simulation_state", {})
        quality = execute_tool(temp_settings, context, "get_source_quality", {})
        preview = execute_tool(temp_settings, context, "preview_trade", {"ts_code": code, "direction": "buy", "planned_shares": 100})
        invalid_lot = execute_tool(temp_settings, context, "preview_trade", {"ts_code": code, "direction": "buy", "planned_shares": 101})
        unaffordable = execute_tool(temp_settings, context, "preview_trade", {"ts_code": code, "direction": "buy", "planned_shares": 10_000_000})
        locked_sale = execute_tool(temp_settings, context, "preview_trade", {"ts_code": code, "direction": "sell", "planned_shares": 100})
        comparison = execute_tool(temp_settings, context, "compare_review", {})
        analysis = execute_tool(temp_settings, context, "bounded_analysis", {"metric": "net_return_bps", "window_trading_days": 10})

        assert state["as_of"] == session["revealed_date"]
        assert quality["data"]["data_version_id"] == version["version_id"]
        assert preview["data"]["future_execution_price_cents"] is None
        assert preview["data"]["future_execution_status"] == "unknown"
        assert preview["data"]["writes_order"] is False
        assert preview["drafts"][0]["payload"] == {"ts_code": code, "direction": "buy", "planned_shares": 100}
        assert preview["data"]["known_constraints_pass"] is True
        assert preview["data"]["board_lot_shares"] == 100
        assert invalid_lot["data"]["known_checks"]["board_lot_aligned"] is False
        assert invalid_lot["data"]["known_constraints_pass"] is False
        assert unaffordable["data"]["known_checks"]["cash_sufficient_at_known_price"] is False
        assert locked_sale["data"]["known_checks"]["sellable_shares_available"] is False
        assert all(path["key"] == "user" for path in comparison["data"]["paths"])
        assert analysis["data"]["window_trading_days"] <= 10
        assert "candidate_codes" not in analysis["data"]
        assert not any("candidate_codes" in item["function"]["parameters"]["properties"] for item in tool_definitions() if item["function"]["name"] == "bounded_analysis")
        with pytest.raises(PracticeError, match="参数超出允许范围"):
            execute_tool(temp_settings, context, "bounded_analysis", {"metric": "net_return_bps", "window_trading_days": 61})
        with sqlite3.connect(practice_settings(temp_settings).db_path) as audit:
            audit.row_factory = sqlite3.Row
            context_market = VersionView(audit, version["version_id"], expected_source_code=version["source_code"])
            asof_price = context_market.latest_daily(code, on_or_before=session["revealed_date"])
            future_price = context_market.latest_daily(code)
            assert asof_price is not None and future_price is not None
            assert str(future_price["trade_date"]) > session["revealed_date"]
            assert preview["data"]["known_price_cents"] == round(float(asof_price["close"]) * 100)
            fee_bundle = load_rule_bundle(audit, str(session["settings"]["fee_bundle_id"]))
            expected_fee = _order_fee_estimate(_fee_schedule(fee_bundle), "buy", preview["data"]["estimated_notional_cents"])
            assert preview["data"]["fee_estimate"] == expected_fee
            assert state["data"]["positions"][0]["locked"] == 1

        after = sqlite3.connect(practice_settings(temp_settings).db_path)
        try:
            after_count = after.execute("SELECT COUNT(*) FROM practice_order WHERE practice_id=?", (practice_id,)).fetchone()[0]
        finally:
            after.close()
        assert after_count == before_count == 0

        changed = client.post(
            f"/api/practice/sessions/{practice_id}/decisions",
            json={"decision_date": session["revealed_date"], "judgment": "test", "evidence": "test", "exit_condition": "test", "idempotency_key": "agent-tools-decision", "expected_revision": session["revision"]},
        )
        assert changed.status_code == 200, changed.text
        with pytest.raises(PracticeError, match="变化"):
            execute_tool(temp_settings, context, "get_simulation_state", {})


def test_capture_does_not_create_missing_practice_database(temp_settings):
    with pytest.raises(PracticeError) as error:
        capture_context(temp_settings, "missing")
    assert error.value.reason == "practice_database_missing"
    assert not practice_settings(temp_settings).db_path.exists()


def test_tool_limits_reject_large_setup_positions(temp_settings):
    context = capture_context(temp_settings, None)
    positions = [{"ts_code": f"{index:06}.SZ", "shares": 100} for index in range(101)]
    with pytest.raises(PracticeError, match="初始持仓最多100项"):
        execute_tool(temp_settings, context, "prepare_setup_draft", {"name": "limit", "start_date": "20250102", "end_date": "20250131", "cash_cents": 1, "positions": positions})

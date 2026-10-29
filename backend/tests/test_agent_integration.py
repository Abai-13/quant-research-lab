from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.agent_gateway.core import GatewayService
from app.agent_runtime.store import AgentStore
from app.main import create_app
from app.portfolio.rule_bundles import create_rule_bundle
from app.practice.runtime import practice_settings


class _TestProtector:
    """Reversible protection is test-only; production uses the OS protector."""

    _key = b"integration-test-only"

    def protect(self, plaintext: bytes) -> bytes:
        return bytes(value ^ self._key[index % len(self._key)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self.protect(ciphertext)


class _GatewayAdapter:
    """Bind the real A gateway implementation to its explicit test protector."""

    def __init__(self, settings):
        self.service = GatewayService(settings, protector=_TestProtector())

    def get_public_config(self, _settings):
        return self.service.get_public_config()

    def save_config(self, _settings, body):
        return self.service.save_config(body)

    async def test_config(self, _settings, body):
        return await self.service.test_config(body)

    async def request_completion(self, _settings, config_version, messages, tools, **kwargs):
        return await self.service.request_completion(config_version, messages, tools, **kwargs)


def _model_response(*, content=None, tool_calls=None):
    calls = tool_calls or []
    message = {"role": "assistant", "content": content, "tool_calls": calls}
    return {"object": "chat.completion", "choices": [{"index": 0, "message": message,
                          "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 48, "completion_tokens": 12, "total_tokens": 60}}


def test_real_agent_stack_over_synthetic_http_model(temp_settings, caplog):
    """Exercise the real A/B/C paths against an explicit loopback model fixture."""
    requests: list[dict] = []
    auth_headers: list[str] = []
    trade_code: list[str] = []
    budget_summary_requests: list[dict] = []
    invalid_argument_calls = 0
    invalid_argument_call_ids: list[str] = []
    cancel_started = threading.Event()
    cancel_release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_POST(self):
            nonlocal invalid_argument_calls
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            auth_headers.append(self.headers.get("Authorization", ""))
            offered = [item["function"]["name"] for item in payload.get("tools", [])]
            messages = payload.get("messages", [])
            tool_messages = [item for item in messages if item.get("role") == "tool"]
            goal = ""
            for item in messages:
                if item.get("role") == "user" and isinstance(item.get("content"), str):
                    try:
                        goal = str(json.loads(item["content"]).get("goal", ""))
                    except (TypeError, ValueError):
                        pass
            if offered == ["gateway_capability_probe"]:
                response = _model_response(tool_calls=[{"id": "probe-1", "type": "function", "function": {
                    "name": "gateway_capability_probe", "arguments": '{"value":"ok"}'}}])
            elif goal.startswith("A8: unknown tool"):
                response = _model_response(tool_calls=[{"id": "unknown-1", "type": "function", "function": {
                    "name": "run_shell", "arguments": "{}"}}])
            elif goal.startswith("A8: invalid tool arguments"):
                invalid_argument_calls += 1
                call_id = f"invalid-{invalid_argument_calls}"
                invalid_argument_call_ids.append(call_id)
                response = _model_response(tool_calls=[{"id": call_id, "type": "function", "function": {
                    "name": "get_source_quality", "arguments": json.dumps({"sql": "PRIVATE_INVALID_ARGUMENT_VALUE"})}}])
            elif goal.startswith("A8: usage unknown"):
                response = _model_response(tool_calls=[{"id": "unknown-usage-1", "type": "function", "function": {
                    "name": "get_source_quality", "arguments": "{}"}}])
                response.pop("usage")
            elif goal.startswith("A8: future record visibility"):
                if not tool_messages:
                    response = _model_response(tool_calls=[{"id": "future-search-1", "type": "function", "function": {
                        "name": "search_research_records", "arguments": '{"query":"FUTURE-CANARY-8","limit":20}'}}])
                else:
                    response = _model_response(content="当前模拟时点没有可见的匹配研究记录。")
            elif goal.startswith("A8: insufficient source"):
                if not tool_messages:
                    response = _model_response(tool_calls=[{"id": "source-limits-1", "type": "function", "function": {
                        "name": "get_source_quality", "arguments": "{}"}}])
                else:
                    response = _model_response(content="来源限制不足以支持更强结论。")
            elif offered == ["prepare_setup_draft"] and not tool_messages:
                response = _model_response(tool_calls=[{"id": "setup-1", "type": "function", "function": {
                    "name": "prepare_setup_draft", "arguments": json.dumps({"name": "本地合成验证",
                        "start_date": "20250102", "end_date": "20250131", "cash_cents": 100000}, ensure_ascii=False)}}])
            elif "交易草稿" in goal and "preview_trade" in offered and not tool_messages and trade_code:
                response = _model_response(tool_calls=[{"id": "trade-draft-1", "type": "function", "function": {
                    "name": "preview_trade", "arguments": json.dumps({"ts_code": trade_code[0],
                        "direction": "buy", "planned_shares": 100})}}])
            elif "等待受控响应" in goal:
                cancel_started.set()
                cancel_release.wait(timeout=5)
                response = _model_response(content="请求已完成。")
            elif (not offered and any(item.get("role") == "system"
                    and "Summarize only the supplied captured tool evidence" in item.get("content", "")
                    for item in messages)):
                summary_payload = json.loads(messages[-1]["content"])
                if summary_payload["goal"].startswith("\u6838\u5bf9\u5f53\u524d\u5408\u6210\u6570\u636e\u6765\u6e90"):
                    budget_summary_requests.append(summary_payload)
                included_ids = [item["evidence_id"] for item in summary_payload["evidence"]]
                included_tools = [item["tool_name"] for item in summary_payload["evidence"]]
                omitted_text = ("No evidence records were omitted."
                    if summary_payload["omitted_evidence"] == 0 else "Some captured evidence records were omitted.")
                response = _model_response(content=(
                    "Earlier conversation was omitted. This bounded summary uses only included evidence "
                    f"{', '.join(included_ids)} from tools {', '.join(included_tools)}. {omitted_text}"
                ))
            elif "get_source_quality" in offered and not tool_messages:
                response = _model_response(tool_calls=[{"id": "simulation-1", "type": "function", "function": {
                    "name": "get_source_quality", "arguments": "{}"}}])
            elif ("compare_review" in offered
                    and "对照" in goal
                    and any(item.get("name") == "get_source_quality" for item in tool_messages)
                    and not any(item.get("name") == "compare_review" for item in tool_messages)):
                response = _model_response(tool_calls=[{"id": "simulation-2", "type": "function", "function": {
                    "name": "compare_review", "arguments": "{}"}}])
            else:
                conclusion = "已依据合成数据来源和模拟对照完成检查。" if any(
                    item.get("name") == "compare_review" for item in tool_messages
                ) else "已依据绑定的合成数据来源完成检查。"
                response = _model_response(content=conclusion)
            raw = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(503 if goal.startswith("A8: provider 503") else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                # The cancellation scenario intentionally closes this request.
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = _GatewayAdapter(temp_settings)
    api_key = "sk-integration-canary-" + uuid.uuid4().hex
    try:
        with TestClient(create_app(temp_settings, agent_gateway=gateway)) as client:
            saved = client.put("/api/agent/config", json={
                "name": "Temporary local model", "protocol": "chat_completions",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "fixture-model",
                "api_key": api_key, "allow_loopback_http": True,
            })
            assert saved.status_code == 200, saved.text
            config_version = saved.json()["config"]["config_version"]
            assert api_key not in saved.text
            assert client.get("/api/agent/config").json()["config"]["key_configured"] is True

            tested = client.post("/api/agent/config/test", json={
                "config_version": config_version, "consent": True, "test_tools": True,
            })
            assert tested.status_code == 200, tested.text
            assert tested.json()["tool_supported"] is True, tested.json()
            assert auth_headers[-1] == f"Bearer {api_key}"

            invalid = client.put("/api/agent/config", json={
                "name": "bad", "protocol": "chat_completions", "base_url": "http://127.0.0.1",
                "model": "x", "api_key": api_key, "unexpected": api_key,
            })
            assert invalid.status_code == 422
            assert api_key not in invalid.text

            setup_disclosure = client.get("/api/agent/disclosure")
            assert setup_disclosure.status_code == 200
            setup = setup_disclosure.json()
            assert setup["limits"]["tokens"] == 4096
            assert not practice_settings(temp_settings).db_path.exists()
            setup_request = {
                "practice_id": None, "expected_revision": None, "goal": "整理本地模拟条件草稿",
                "config_version": config_version, "consent": True,
                "consent_digest": setup["consent_digest"], "idempotency_key": "setup-flow-001",
            }
            setup_created = client.post("/api/agent/tasks", json=setup_request)
            assert setup_created.status_code == 202, setup_created.text
            setup_task_id = setup_created.json()["task_id"]
            for _ in range(200):
                setup_task = client.get(f"/api/agent/tasks/{setup_task_id}").json()
                if setup_task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                    break
                time.sleep(0.01)
            assert setup_task["status"] == "completed", setup_task
            assert setup_task["result"]["drafts"][0]["kind"] == "setup"
            assert not practice_settings(temp_settings).db_path.exists()
            assert requests[1]["max_tokens"] >= 1
            assert [item["function"]["name"] for item in requests[1]["tools"]] == ["prepare_setup_draft"]

            demo = client.post("/api/practice/demo").json()
            version = next(item for item in demo["versions"] if item["version_id"] == demo["demo"]["version_id"])
            code = demo["symbols"][0]["ts_code"]
            trade_code.append(code)
            with sqlite3.connect(practice_settings(temp_settings).db_path) as conn:
                rules = create_rule_bundle(conn, kind="parameter", version="agent-integration-market-rules-1",
                    assumption_scope="synthetic_simulation", content={"bundle_kind": "parameter",
                    "market_rules": {"verification_status": "verified", "board_lot_shares": 100}})
            created = client.post("/api/practice/sessions", json={
                "name": "agent integration synthetic", "start_date": demo["demo"]["start_date"],
                "end_date": demo["demo"]["end_date"], "cash_cents": 10_000_000,
                "positions": [{"ts_code": code, "shares": 200, "sellable_shares": 200}],
                "data_version_id": version["version_id"], "source_code": version["source_code"],
                "settings": {"hide_system_hints": True, "simulation_rule_bundle_id": rules["bundle_id"],
                             "fee_bundle_id": demo["defaults"]["fee_bundle_id"]},
                "idempotency_key": "agent-integration-synthetic-session",
            })
            assert created.status_code == 200, created.text
            session = created.json()["session"]
            practice_id = session["practice_id"]
            disclosure_response = client.get("/api/agent/disclosure", params={
                "practice_id": practice_id, "expected_revision": session["revision"],
            })
            disclosure = disclosure_response.json()
            task_request = {
                "practice_id": practice_id, "expected_revision": session["revision"],
                "goal": "核对当前合成数据来源，不提交订单",
                "config_version": config_version, "consent": True,
                "consent_digest": disclosure["consent_digest"],
                "idempotency_key": "simulation-flow-001",
            }
            before = client.get(f"/api/practice/sessions/{practice_id}").json()["session"]
            created_task = client.post("/api/agent/tasks", json=task_request)
            assert created_task.status_code == 202, created_task.text
            task_id = created_task.json()["task_id"]
            duplicate = client.post("/api/agent/tasks", json=task_request)
            assert duplicate.status_code == 202
            assert duplicate.json()["task_id"] == task_id
            conflict = client.post("/api/agent/tasks", json={**task_request, "goal": "different payload"})
            assert conflict.status_code == 409

            for _ in range(300):
                task = client.get(f"/api/agent/tasks/{task_id}").json()
                if task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                    break
                time.sleep(0.01)
            if task["status"] != "completed":
                events = client.get(f"/api/agent/tasks/{task_id}/events").json()["events"]
                pytest.fail(json.dumps({"status": task["status"], "stop_reason": task["stop_reason"],
                    "usage": task["usage"], "events": events}, ensure_ascii=False))
            evidence = task["result"]["evidence"]
            assert [item["tool_name"] for item in evidence] == ["get_source_quality"]
            assert evidence[0]["data"]["data_version_id"] == version["version_id"]
            assert evidence[0]["as_of"] == session["revealed_date"]
            assert all(item["version"]["practice_id"] == practice_id for item in evidence)
            assert client.get(f"/api/agent/tasks/{task_id}/events").json()["events"]

            budget_request = {
                **task_request, "goal": "核对当前合成数据来源，再读取模拟对照，不提交订单",
                "idempotency_key": "simulation-budget-stop-001",
            }
            budget_created = client.post("/api/agent/tasks", json=budget_request)
            assert budget_created.status_code == 202, budget_created.text
            budget_task_id = budget_created.json()["task_id"]
            for _ in range(300):
                budget_task = client.get(f"/api/agent/tasks/{budget_task_id}").json()
                if budget_task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                    break
                time.sleep(0.01)
            assert budget_task["status"] == "completed", budget_task
            assert budget_task["stop_reason"] is None
            assert budget_task["usage"]["tool_calls"] == 2
            assert [item["tool_name"] for item in budget_task["result"]["evidence"]] == ["get_source_quality", "compare_review"]
            assert budget_task["usage"]["model_calls"] == 3
            assert budget_task["usage"]["model_calls"] <= setup["limits"]["rounds"]
            assert budget_task["usage"]["tool_calls"] <= setup["limits"]["tool_calls"]
            assert budget_task["usage"]["total_tokens"] is not None
            assert 0 < budget_task["usage"]["total_tokens"] <= setup["limits"]["tokens"]
            assert len(budget_summary_requests) == 1
            summary_payload = budget_summary_requests[0]
            assert summary_payload["notice"].startswith("Earlier model/tool conversation was omitted")
            assert summary_payload["omitted_evidence"] == len(budget_task["result"]["evidence"]) - len(summary_payload["evidence"])
            assert summary_payload["omitted_drafts"] == 0
            assert summary_payload["evidence"]
            included_ids = {item["evidence_id"] for item in summary_payload["evidence"]}
            assert {item["tool_name"] for item in summary_payload["evidence"]} == {"get_source_quality", "compare_review"}
            persisted_evidence = budget_task["result"]["evidence"]
            persisted_ids = {item["evidence_id"] for item in persisted_evidence}
            assert included_ids <= persisted_ids
            summary_text = budget_task["result"]["summary"]
            assert "Earlier conversation was omitted" in summary_text
            assert all(evidence_id in summary_text for evidence_id in included_ids)
            omitted_ids = persisted_ids - included_ids
            assert len(omitted_ids) == summary_payload["omitted_evidence"]
            assert all(evidence_id not in summary_text for evidence_id in omitted_ids)
            assert any(f"omitted earlier conversation; omitted evidence: {len(omitted_ids)}, drafts: 0" in limitation
                       for limitation in budget_task["result"]["limitations"])
            assert len(persisted_evidence) == 2
            assert client.get(f"/api/practice/sessions/{practice_id}").json()["session"]["revision"] == before["revision"]
            with sqlite3.connect(practice_settings(temp_settings).db_path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM practice_order WHERE practice_id=?", (practice_id,)).fetchone()[0] == 0

            reconfigured = client.put("/api/agent/config", json={
                "name": "Temporary local model", "protocol": "chat_completions",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "fixture-model",
                "api_key": api_key, "allow_loopback_http": True,
                "limits": {"tokens": 8192},
                "expected_config_version": config_version,
            })
            assert reconfigured.status_code == 200
            assert reconfigured.json()["config"]["limits"]["tokens"] == 8192
            assert client.get("/api/agent/config").json()["config"]["limits"]["tokens"] == 8192
            consent_stale = client.post("/api/agent/tasks", json={
                **task_request, "idempotency_key": "simulation-flow-stale",
            })
            assert consent_stale.status_code == 409
            assert consent_stale.json()["reason"] == "consent_stale", consent_stale.json()

            multistep_disclosure = client.get("/api/agent/disclosure", params={
                "practice_id": practice_id, "expected_revision": session["revision"],
            }).json()
            assert multistep_disclosure["limits"]["tokens"] == 8192
            multistep_request = {
                **task_request, "goal": "先核对当前合成数据来源，再读取模拟对照，不提交订单",
                "config_version": reconfigured.json()["config"]["config_version"],
                "consent_digest": multistep_disclosure["consent_digest"],
                "idempotency_key": "simulation-multistep-001",
            }
            multistep_created = client.post("/api/agent/tasks", json=multistep_request)
            assert multistep_created.status_code == 202, multistep_created.text
            multistep_id = multistep_created.json()["task_id"]
            for _ in range(300):
                multistep = client.get(f"/api/agent/tasks/{multistep_id}").json()
                if multistep["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                    break
                time.sleep(0.01)
            if multistep["status"] != "completed":
                events = client.get(f"/api/agent/tasks/{multistep_id}/events").json()["events"]
                pytest.fail(json.dumps({"status": multistep["status"], "stop_reason": multistep["stop_reason"],
                    "usage": multistep["usage"], "events": events}, ensure_ascii=False))
            assert [item["tool_name"] for item in multistep["result"]["evidence"]] == ["get_source_quality", "compare_review"]

            trade_request = {
                **multistep_request, "goal": "生成一个只读交易草稿供我检查，不下单",
                "idempotency_key": "simulation-trade-draft-001",
            }
            trade_created = client.post("/api/agent/tasks", json=trade_request)
            assert trade_created.status_code == 202, trade_created.text
            trade_task_id = trade_created.json()["task_id"]
            for _ in range(300):
                trade_task = client.get(f"/api/agent/tasks/{trade_task_id}").json()
                if trade_task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                    break
                time.sleep(0.01)
            assert trade_task["status"] == "completed", trade_task
            assert trade_task["result"]["drafts"][0]["kind"] == "trade"
            assert trade_task["result"]["drafts"][0]["payload"] == {
                "ts_code": code, "direction": "buy", "planned_shares": 100,
            }

            cancel_request = {
                **multistep_request, "goal": "等待受控响应以检查取消围栏",
                "idempotency_key": "simulation-cancel-001",
            }
            requests_before_cancel = len(requests)
            cancel_created = client.post("/api/agent/tasks", json=cancel_request)
            assert cancel_created.status_code == 202, cancel_created.text
            cancel_task_id = cancel_created.json()["task_id"]
            assert cancel_started.wait(timeout=3), "the local model fixture did not receive the cancellation task"
            cancelled = client.post(f"/api/agent/tasks/{cancel_task_id}/cancel", json={
                "idempotency_key": "simulation-cancel-request-001",
            })
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            cancel_release.set()
            for _ in range(100):
                cancelled_task = client.get(f"/api/agent/tasks/{cancel_task_id}").json()
                if cancelled_task["status"] == "cancelled":
                    break
                time.sleep(0.01)
            assert cancelled_task["status"] == "cancelled"
            assert len(requests) == requests_before_cancel + 1

            after = client.get(f"/api/practice/sessions/{practice_id}").json()["session"]
            assert (after["revision"], after["revealed_date"]) == (before["revision"], before["revealed_date"])
            with sqlite3.connect(practice_settings(temp_settings).db_path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM practice_order WHERE practice_id=?", (practice_id,)).fetchone()[0] == 0

            # Bind research revisions to the server-read simulation clock; the JSON
            # lookalike field cannot grant any additional visibility.
            from app.agent_tools.research import invoke_research_tool
            from app.agent_tools.service import capture_context, execute_tool

            second_session_response = client.post("/api/practice/sessions", json={
                "name": "agent integration second synthetic", "start_date": demo["demo"]["start_date"],
                "end_date": demo["demo"]["end_date"], "cash_cents": 10_000_000,
                "positions": [], "data_version_id": version["version_id"], "source_code": version["source_code"],
                "settings": {"hide_system_hints": True, "simulation_rule_bundle_id": rules["bundle_id"],
                             "fee_bundle_id": demo["defaults"]["fee_bundle_id"]},
                "idempotency_key": "agent-integration-second-session",
            })
            assert second_session_response.status_code == 200, second_session_response.text
            second_session = second_session_response.json()["session"]
            clock_practice_id = second_session["practice_id"]
            context_before_revision = capture_context(temp_settings, clock_practice_id, second_session["revision"])
            simulation_record = client.post("/api/research/records", json={
                "idempotency_key": "agent-simulation-clock-record-001",
                "kind": "observation", "goal": "CLOCK-RECORD original revision",
                "hypothesis": "visible on the captured simulation day",
                "disconfirming_condition": "not visible to another practice",
                "scope": "simulation", "evidence_as_of": second_session["revealed_date"],
                "source_refs": [{"owner_type": "practice", "owner_id": clock_practice_id}],
                "simulation_visible_as_of": "20991231",
            })
            assert simulation_record.status_code == 200, simulation_record.text
            simulation_record = simulation_record.json()["record"]
            with sqlite3.connect(temp_settings.runtime_dir / "research" / "research.sqlite3") as research_conn:
                visibility = research_conn.execute(
                    "SELECT owner_type,owner_id,visible_as_of,practice_revision "
                    "FROM research_revision_visibility WHERE revision_id=?",
                    (simulation_record["revision_id"],),
                ).fetchone()
            assert visibility == ("practice", clock_practice_id, second_session["revealed_date"], second_session["revision"])
            assert "simulation_visible_as_of" not in simulation_record
            found = execute_tool(temp_settings, context_before_revision, "search_research_records", {
                "query": "CLOCK-RECORD", "limit": 10,
            })
            assert [item["record_id"] for item in found["evidence"][0]["data"]["records"]] == [simulation_record["record_id"]]
            detail = execute_tool(temp_settings, context_before_revision, "read_research_record", {
                "record_id": simulation_record["record_id"],
            })
            assert detail["evidence"][0]["data"]["body"]["goal"] == "CLOCK-RECORD original revision"

            second_context = capture_context(temp_settings, practice_id, before["revision"])
            foreign_search = invoke_research_tool(temp_settings, second_context, "search_research_records", {
                "query": "CLOCK-RECORD", "limit": 10,
            })
            assert foreign_search["records"] == []

            advanced_clock = client.post(f"/api/practice/sessions/{clock_practice_id}/advance", json={
                "expected_revision": second_session["revision"], "idempotency_key": "agent-advance-for-research-clock",
            })
            assert advanced_clock.status_code == 200, advanced_clock.text
            advanced_session = advanced_clock.json()["session"]
            revised_record = client.post(f"/api/research/records/{simulation_record['record_id']}/revisions", json={
                "idempotency_key": "agent-simulation-clock-record-revision-001",
                "kind": "observation", "goal": "CLOCK-RECORD revised after advance",
                "hypothesis": "revision is visible only from its bound date",
                "disconfirming_condition": "past simulation must not see revision",
                "scope": "simulation", "evidence_as_of": advanced_session["revealed_date"],
                "source_refs": [{"owner_type": "practice", "owner_id": clock_practice_id}],
                "simulation_visible_as_of": "20991231",
            })
            assert revised_record.status_code == 200, revised_record.text
            past_list = invoke_research_tool(temp_settings, context_before_revision, "search_research_records", {
                "query": "CLOCK-RECORD", "limit": 10,
            })["records"]
            assert len(past_list) == 1 and past_list[0]["body"]["goal"] == "CLOCK-RECORD original revision"
            past_detail = invoke_research_tool(temp_settings, context_before_revision, "read_research_record", {
                "record_id": simulation_record["record_id"],
            })
            assert past_detail["body"]["goal"] == "CLOCK-RECORD original revision"
            advanced_context = capture_context(temp_settings, clock_practice_id, advanced_session["revision"])
            latest_detail = invoke_research_tool(temp_settings, advanced_context, "read_research_record", {
                "record_id": simulation_record["record_id"],
            })
            assert latest_detail["body"]["goal"] == "CLOCK-RECORD revised after advance"

            # Fixed hostile and incomplete provider cases travel through the real API,
            # task manager, AgentEngine, deterministic tools, and loopback HTTP gateway.
            future_simulation = client.post("/api/research/records", json={
                "kind": "observation", "goal": "future simulation write must be rejected",
                "hypothesis": "new API cannot bind future evidence",
                "disconfirming_condition": "none", "scope": "simulation",
                "evidence_as_of": "20991231", "source_refs": [{"owner_id": practice_id}],
                "idempotency_key": "agent-future-simulation-record-acceptance-001",
            })
            assert future_simulation.status_code == 422, future_simulation.text
            assert future_simulation.json()["reason"] == "simulation_evidence_in_future"
            future = client.post("/api/research/records", json={
                "kind": "observation", "goal": "FUTURE-CANARY-8 future-only record",
                "hypothesis": "future evidence must remain hidden",
                "disconfirming_condition": "none", "scope": "global", "evidence_as_of": "20991231",
                "idempotency_key": "agent-future-record-acceptance-001",
            })
            assert future.status_code == 200, future.text
            record_id = future.json()["record"]["record_id"]

            requests_before_stale_revision = len(requests)
            stale_revision = client.post("/api/agent/tasks", json={
                **multistep_request, "expected_revision": session["revision"] - 1,
                "idempotency_key": "agent-stale-revision-acceptance-001",
            })
            assert stale_revision.status_code == 409
            assert len(requests) == requests_before_stale_revision

            def run_case(goal: str, key: str) -> dict:
                request = {**multistep_request, "goal": goal, "idempotency_key": key}
                created_case = client.post("/api/agent/tasks", json=request)
                assert created_case.status_code == 202, created_case.text
                case_id = created_case.json()["task_id"]
                for _ in range(300):
                    result = client.get(f"/api/agent/tasks/{case_id}").json()
                    if result["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                        return result
                    time.sleep(0.01)
                pytest.fail(f"Agent fixture case timed out: {goal}")

            insufficient = run_case("A8: insufficient source; explain limits", "agent-insufficient-source-001")
            assert insufficient["status"] == "completed"
            assert [item["tool_name"] for item in insufficient["result"]["evidence"]] == ["get_source_quality"]
            source_evidence = insufficient["result"]["evidence"][0]["data"]
            assert source_evidence["synthetic"] is True
            assert source_evidence["limitations"]
            assert "不足以支持" in insufficient["result"]["summary"]

            future_result = run_case("A8: future record visibility", "agent-future-record-001")
            assert future_result["status"] == "completed"
            assert len(future_result["result"]["evidence"]) == 1
            future_data = future_result["result"]["evidence"][0]["data"]
            assert future_data["records"] == []
            assert future_data["as_of"] == session["revealed_date"]
            assert record_id not in json.dumps(future_data)
            assert "FUTURE-CANARY-8" not in json.dumps(future_data)

            unknown_tool = run_case("A8: unknown tool", "agent-unknown-tool-001")
            assert unknown_tool["status"] == "failed" and unknown_tool["stop_reason"] == "unknown_tool"
            invalid_arguments = run_case("A8: invalid tool arguments", "agent-invalid-arguments-001")
            assert invalid_arguments["status"] == "failed" and invalid_arguments["stop_reason"] == "tool_arguments_invalid"
            assert invalid_argument_call_ids == ["invalid-1", "invalid-2"]
            assert invalid_arguments["usage"]["tool_calls"] == 2
            assert invalid_arguments["result"]["evidence"] == []
            assert "PRIVATE_INVALID_ARGUMENT_VALUE" not in json.dumps(invalid_arguments, ensure_ascii=False)
            provider_503 = run_case("A8: provider 503", "agent-provider-503-001")
            assert provider_503["status"] == "failed" and provider_503["stop_reason"] == "provider_http_error"
            requests_before_unknown_usage = len(requests)
            unknown_usage = run_case("A8: usage unknown", "agent-usage-unknown-001")
            assert unknown_usage["status"] == "failed" and unknown_usage["stop_reason"] == "usage_unknown"
            assert unknown_usage["usage"]["total_tokens"] is None
            assert unknown_usage["usage"]["model_calls"] == 1
            assert unknown_usage["usage"]["tool_calls"] == 1
            assert len(requests) == requests_before_unknown_usage + 1

            assert client.get(f"/api/practice/sessions/{practice_id}").json()["session"]["revision"] == before["revision"]
            with sqlite3.connect(practice_settings(temp_settings).db_path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM practice_order WHERE practice_id=?", (practice_id,)).fetchone()[0] == 0

            store = AgentStore(temp_settings.runtime_dir / "agent" / "tasks.sqlite3")
            assert store.delete(task_id)
            assert store.delete(budget_task_id)
            assert store.delete(multistep_id)
            assert store.delete(trade_task_id)
            assert store.delete(cancel_task_id)
            for finished in (insufficient, future_result, unknown_tool, invalid_arguments, provider_503, unknown_usage):
                assert store.delete(finished["task_id"])
            assert client.get(f"/api/agent/tasks/{task_id}").status_code == 404
            assert client.get(f"/api/agent/tasks/{budget_task_id}").status_code == 404
            assert client.get(f"/api/agent/tasks/{multistep_id}").status_code == 404
            assert client.get(f"/api/agent/tasks/{trade_task_id}").status_code == 404
            assert client.get(f"/api/agent/tasks/{cancel_task_id}").status_code == 404
            serialized_store = store.path.read_bytes()
            assert api_key.encode() not in serialized_store
            assert api_key not in caplog.text
            assert api_key not in json.dumps(requests, ensure_ascii=False)
            tracked_scan = subprocess.run(
                ["git", "grep", "-F", "--", api_key], cwd=Path(__file__).resolve().parents[2],
                capture_output=True, text=True, check=False,
            )
            assert tracked_scan.returncode == 1
            for root in (temp_settings.runtime_dir, temp_settings.backup_dir):
                if root.exists():
                    for path in root.rglob("*"):
                        if path.is_file():
                            assert api_key.encode() not in path.read_bytes(), f"credential canary found at {path}"
    finally:
        cancel_release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

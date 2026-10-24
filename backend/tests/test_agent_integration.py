from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    cancel_started = threading.Event()
    cancel_release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_POST(self):
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
            self.send_response(200)
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
    api_key = "sk-integration-secret-must-not-leak"
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
            assert budget_task["status"] == "failed"
            assert budget_task["stop_reason"] == "token_budget_exhausted"
            assert budget_task["usage"]["tool_calls"] == 2
            assert [item["tool_name"] for item in budget_task["result"]["evidence"]] == ["get_source_quality", "compare_review"]

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

            after = client.get(f"/api/practice/sessions/{practice_id}").json()["session"]
            assert (after["revision"], after["revealed_date"]) == (before["revision"], before["revealed_date"])
            with sqlite3.connect(practice_settings(temp_settings).db_path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM practice_order WHERE practice_id=?", (practice_id,)).fetchone()[0] == 0

            store = AgentStore(temp_settings.runtime_dir / "agent" / "tasks.sqlite3")
            assert store.delete(task_id)
            assert store.delete(budget_task_id)
            assert store.delete(multistep_id)
            assert store.delete(trade_task_id)
            assert store.delete(cancel_task_id)
            assert client.get(f"/api/agent/tasks/{task_id}").status_code == 404
            assert client.get(f"/api/agent/tasks/{budget_task_id}").status_code == 404
            assert client.get(f"/api/agent/tasks/{multistep_id}").status_code == 404
            assert client.get(f"/api/agent/tasks/{trade_task_id}").status_code == 404
            assert client.get(f"/api/agent/tasks/{cancel_task_id}").status_code == 404
            serialized_store = store.path.read_bytes()
            assert api_key.encode() not in serialized_store
            assert api_key not in caplog.text
    finally:
        cancel_release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

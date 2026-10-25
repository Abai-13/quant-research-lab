from __future__ import annotations

import asyncio
import pytest

from app.agent_runtime.engine import AgentEngine, RuntimeErrorCode, _estimate_input_tokens, validate_arguments
from app.agent_runtime.store import AgentStore, IdempotencyConflict, canonical


class FakeTools:
    def __init__(self):
        self.calls=[]
    def tool_definitions(self):
        return [{"type":"function","function":{"name":"lookup","description":"lookup","parameters":{
            "type":"object","properties":{"symbol":{"type":"string","maxLength":12}},"required":["symbol"],"additionalProperties":False}}}]
    def validate_context(self,settings,context):
        if context.get("stale"): raise RuntimeError("context_stale")
    def execute_tool(self,settings,context,name,arguments):
        self.calls.append((name,arguments))
        return {"data":{"symbol":arguments["symbol"]},"evidence":[{"evidence_id":"ev-1","as_of":"2026-01-01"}],"drafts":[],"limitations":[],"context_fingerprint":"fp"}


class ResearchTools(FakeTools):
    def tool_definitions(self):
        return super().tool_definitions()+[{
            "type":"function","function":{"name":"search_research_records","description":"search visible research",
                "parameters":{"type":"object","properties":{"query":{"type":"string"}},
                              "required":["query"],"additionalProperties":False}},
        }]

    def execute_tool(self, settings, context, name, arguments):
        if name == "search_research_records":
            self.calls.append((name, arguments))
            return {"data":{"records":[]},"evidence":[],"drafts":[],"limitations":[],"context_fingerprint":"fp"}
        return super().execute_tool(settings,context,name,arguments)


class FakeGateway:
    def __init__(self,responses):
        self.responses=list(responses)
        self.requests=[]
    async def request_completion(self,*args,**kwargs):
        self.requests.append((args,kwargs))
        response=self.responses.pop(0)
        if isinstance(response,asyncio.Event):
            await response.wait()
            return {"message":{"role":"assistant","content":"late","tool_calls":[]},"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}
        return response


def model_response(*,content=None,calls=None,total=20):
    return {"message":{"role":"assistant","content":content,"tool_calls":calls or []},
            "usage":{"input_tokens":min(10,total),"output_tokens":max(0,total-min(10,total)),"total_tokens":total}}


def make_task(store):
    context={"scope":"simulation","practice_id":"practice-1","revision":1,"context_fingerprint":"fp","model_context":{"scope":"simulation"},"limitations":[]}
    view,created=store.create(key="idem-12345678",payload={"goal":"goal"},practice_id="practice-1",scope="simulation",goal="goal",
        config_version="v1",context=context,consent_digest="d"*64,limits={"rounds":8,"tool_calls":20,"timeout_seconds":120,"tokens":4096})
    assert created
    store.start(view["task_id"])
    return store.get(view["task_id"]),context


def test_dynamic_tool_branch_and_evidence(tmp_path):
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize()
    task,context=make_task(store)
    tools=FakeTools()
    gateway=FakeGateway([
        model_response(calls=[{"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\"symbol\":\"600000.SH\"}"}}]),
        model_response(content="The result is supported by ev-1.")])
    result,reason,usage=asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=tools).run(task,context=context,cancel_event=asyncio.Event()))
    assert reason is None
    assert [c[0] for c in tools.calls]==["lookup"]
    assert len(gateway.requests)==2
    assert gateway.requests[0][0][3][0]["function"]["description"]=="lookup"
    assert result["summary"].endswith("ev-1.")
    assert result["evidence"][0]["evidence_id"]=="ev-1"
    assert usage["model_calls"]==2 and usage["tool_calls"]==1


def test_input_preflight_counts_ascii_bytes_and_cjk_codepoints():
    ascii_payload={"schema":"function properties additionalProperties false"}
    assert _estimate_input_tokens(ascii_payload)==len(canonical(ascii_payload).encode("utf-8"))

    cjk_payload={"text":"研究"}
    serialized=canonical(cjk_payload)
    expected=len(serialized.encode("utf-8"))-len("研究".encode("utf-8"))+len("研究")
    assert _estimate_input_tokens(cjk_payload)==expected


def test_model_tool_descriptions_are_compact_and_research_tools_remain_available(tmp_path):
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize()
    task,context=make_task(store)
    tools=ResearchTools()
    gateway=FakeGateway([
        model_response(calls=[{"id":"research-call-1","type":"function","function":{
            "name":"search_research_records","arguments":"{\"query\":\"supporting evidence\"}"}}]),
        model_response(content="Done."),
    ])
    asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=tools).run(
        task,context=context,cancel_event=asyncio.Event()))
    offered=[item["function"]["name"] for item in gateway.requests[0][0][3]]
    assert offered==["lookup","search_research_records"]
    model_research_schema=gateway.requests[0][0][3][1]["function"]
    source_research_schema=ResearchTools().tool_definitions()[1]["function"]
    assert model_research_schema["description"]=="搜索可见研究记录"
    assert model_research_schema["parameters"]==source_research_schema["parameters"]
    assert tools.calls==[("search_research_records",{"query":"supporting evidence"})]

    research_store=AgentStore(tmp_path/"research.sqlite3"); research_store.initialize()
    research_context={"scope":"simulation","practice_id":"practice-1","revision":1,
                      "context_fingerprint":"fp","model_context":{"scope":"simulation"},"limitations":[]}
    research_task,_=research_store.create(key="idem-research-123",payload={"goal":"整理研究记录"},
        practice_id="practice-1",scope="simulation",goal="整理研究记录",config_version="v1",
        context=research_context,consent_digest="e"*64,
        limits={"rounds":8,"tool_calls":20,"timeout_seconds":120,"tokens":4096})
    research_store.start(research_task["task_id"])
    research_task=research_store.get(research_task["task_id"])
    research_gateway=FakeGateway([model_response(content="Done.")])
    asyncio.run(AgentEngine(settings=object(),store=research_store,gateway=research_gateway,tools=ResearchTools()).run(
        research_task,context=research_context,cancel_event=asyncio.Event()))
    research_offered=[item["function"]["name"] for item in research_gateway.requests[0][0][3]]
    assert research_offered==["lookup","search_research_records"]


def test_chinese_multiround_prompt_uses_budget_estimate_and_actual_usage(tmp_path):
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize()
    task,context=make_task(store)
    context["model_context"]={"已知研究背景":"研究资料需要按已揭晓日期核对。"*120}
    tools=FakeTools()
    gateway=FakeGateway([
        model_response(calls=[{"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\"symbol\":\"600000.SH\"}"}}]),
        model_response(content="已依据证据 ev-1 完成核对。"),
    ])
    result,reason,usage=asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=tools).run(
        task,context=context,cancel_event=asyncio.Event()))
    assert reason is None
    assert result["summary"]=="已依据证据 ev-1 完成核对。"
    assert len(gateway.requests)==2
    assert all(0 < request[1]["max_output_tokens"] <= 1_024 for request in gateway.requests)
    assert usage["input_tokens"]==20
    assert usage["output_tokens"]==20
    assert usage["total_tokens"]==40 <= task["limits"]["tokens"]


def test_reported_usage_still_enforces_hard_cumulative_limit(tmp_path):
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize()
    task,context=make_task(store)
    task["limits"]["tokens"]=1_000
    gateway=FakeGateway([model_response(content="This response exceeds the actual usage budget.",total=1_001)])
    with pytest.raises(RuntimeErrorCode) as caught:
        asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=FakeTools()).run(
            task,context=context,cancel_event=asyncio.Event()))
    assert caught.value.reason=="token_budget_exhausted"
    assert len(gateway.requests)==1
    assert store.get(task["task_id"])["usage"]["total_tokens"]==1_001


@pytest.mark.parametrize("call,reason",[
    ({"id":"c1","type":"function","function":{"name":"missing","arguments":"{}"}},"unknown_tool"),
    ({"id":"c1","type":"function","function":{"name":"lookup","arguments":"not-json"}},"tool_arguments_invalid"),
    ({"id":"c1","type":"function","function":{"name":"lookup","arguments":"{}"}},"tool_arguments_invalid"),
])
def test_rejects_unknown_or_invalid_tool_call(tmp_path,call,reason):
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize(); task,context=make_task(store)
    engine=AgentEngine(settings=object(),store=store,gateway=FakeGateway([model_response(calls=[call])]),tools=FakeTools())
    with pytest.raises(RuntimeErrorCode) as caught:
        asyncio.run(engine.run(task,context=context,cancel_event=asyncio.Event()))
    assert caught.value.reason==reason


def test_cancel_fence_discards_late_model_result(tmp_path):
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize(); task,context=make_task(store)
    gate=asyncio.Event(); gateway=FakeGateway([gate]); cancel=asyncio.Event()
    async def scenario():
        running=asyncio.create_task(AgentEngine(settings=object(),store=store,gateway=gateway,tools=FakeTools()).run(task,context=context,cancel_event=cancel))
        await asyncio.sleep(0)
        cancel.set(); gate.set()
        with pytest.raises(asyncio.CancelledError): await running
    asyncio.run(scenario())
    assert store.cancel(task["task_id"])["status"]=="cancelled"
    assert store.transition(task["task_id"],("running",),"completed",result={"summary":"late"}) is False


def test_preflight_reserves_serialized_input_and_tool_schema(tmp_path):
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize(); task,context=make_task(store)
    context["model_context"]={"bounded":"x"*9_000}
    gateway=FakeGateway([model_response(content="should not be requested")])
    engine=AgentEngine(settings=object(),store=store,gateway=gateway,tools=FakeTools())
    with pytest.raises(RuntimeErrorCode) as caught:
        asyncio.run(engine.run(task,context=context,cancel_event=asyncio.Event()))
    assert caught.value.reason=="token_budget_exhausted"
    assert not gateway.requests


def test_unknown_usage_stops_before_second_model_request(tmp_path):
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize(); task,context=make_task(store)
    first={"message":{"role":"assistant","content":None,"tool_calls":[{"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\"symbol\":\"600000.SH\"}"}}]},"usage":{}}
    gateway=FakeGateway([first,model_response(content="must not be requested")])
    with pytest.raises(RuntimeErrorCode) as caught:
        asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=FakeTools()).run(task,context=context,cancel_event=asyncio.Event()))
    assert caught.value.reason=="usage_unknown"
    assert len(gateway.requests)==1


def test_restart_marks_running_interrupted_and_idempotency_isolated(tmp_path):
    path=tmp_path/"tasks.sqlite3"; store=AgentStore(path); store.initialize(); task,context=make_task(store)
    same,created=store.create(key="idem-12345678",payload={"goal":"goal"},practice_id=None,scope="setup",goal="goal",
        config_version="v1",context=context,consent_digest="d"*64,limits={"rounds":8,"tool_calls":20,"timeout_seconds":120,"tokens":4096})
    assert not created and same["task_id"]==task["task_id"]
    with pytest.raises(IdempotencyConflict):
        store.create(key="idem-12345678",payload={"goal":"different"},practice_id=None,scope="setup",goal="goal",
            config_version="v1",context=context,consent_digest="d"*64,limits={})
    assert AgentStore(path).initialize()==1
    assert store.get(task["task_id"])["status"]=="interrupted"
    assert store.delete(task["task_id"])
    assert store.get(task["task_id"]) is None


def test_json_schema_union_null_and_pattern_for_setup_tool():
    schema={"type":"object","properties":{"start_date":{"type":"string","pattern":"^\\d{8}$"},
        "sellable_shares":{"type":["integer","null"],"minimum":0}},"required":["start_date","sellable_shares"],"additionalProperties":False}
    validate_arguments(schema,{"start_date":"20260102","sellable_shares":None})
    with pytest.raises(RuntimeErrorCode) as caught:
        validate_arguments(schema,{"start_date":"2026-01-02","sellable_shares":None})
    assert caught.value.reason=="tool_arguments_invalid"

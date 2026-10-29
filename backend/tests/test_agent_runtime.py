from __future__ import annotations

import asyncio
import json
import pytest

from app.agent_runtime.engine import (AgentEngine, MIN_SUMMARY_OUTPUT_TOKENS, RuntimeErrorCode,
                                     SUMMARY_VERSION_BINDING_FIELDS, SUMMARY_VERSION_PROJECTION_LIMITATION,
                                     _estimate_input_tokens, _summary_messages,
                                     _filter_numeric_summary, _numeric_claim_diagnostic,
                                     _unsupported_numeric_claims, _build_structured_evidence_facts,
                                     validate_arguments)
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
            records = [{"record_id":"visible-hit"}] if arguments.get("query") == "hit" else []
            return {"data":{"records":records,"notice":"当前可见范围内没有找到匹配记录。可以换个关键词或说法再试。"},
                    "evidence":[],"drafts":[],"limitations":[],"context_fingerprint":"fp"}
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


@pytest.mark.parametrize(("queries", "expected_notice"), [
    (("empty", "hit"), None),
    (("hit", "empty"), "最后一次搜索未找到匹配记录。"),
    (("empty",), "最后一次搜索未找到匹配记录。"),
    (("hit",), None),
])
def test_research_empty_notice_tracks_latest_search(tmp_path, queries, expected_notice):
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize()
    task,context=make_task(store)
    calls=[model_response(calls=[{"id":f"research-{index}","type":"function","function":{
        "name":"search_research_records","arguments":json.dumps({"query":query})}}])
        for index,query in enumerate(queries)]
    gateway=FakeGateway([*calls, model_response(content="Search completed.")])
    result,reason,_=asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=ResearchTools()).run(
        task,context=context,cancel_event=asyncio.Event()))
    assert reason is None
    assert (expected_notice in result["summary"]) if expected_notice else "最后一次搜索未找到匹配记录。" not in result["summary"]


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
        model_response(content="没有检索到匹配内容。"),
    ])
    result,reason,_=asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=tools).run(
        task,context=context,cancel_event=asyncio.Event()))
    assert reason is None
    assert "最后一次搜索未找到匹配记录。可以换个关键词或说法再试。" in result["summary"]
    assert "notice" in gateway.requests[1][0][2][-1]["content"]
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


def test_growing_tool_history_falls_back_to_bounded_evidence_summary(tmp_path):
    class GrowingTools(FakeTools):
        def execute_tool(self,settings,context,name,arguments):
            self.calls.append((name,arguments))
            return {"data":{"blob":"x"*6_000},"evidence":[{"evidence_id":"ev-captured-1","as_of":"2026-01-01","summary":"captured"}],
                    "drafts":[],"limitations":[],"context_fingerprint":"fp"}

    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize(); task,context=make_task(store)
    task["limits"]["tokens"]=8_192
    gateway=FakeGateway([
        model_response(calls=[{"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\"symbol\":\"600000.SH\"}"}}],total=2_800),
        model_response(content="Evidence ev-captured-1 supports the result.",total=800),
    ])
    result,reason,usage=asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=GrowingTools()).run(
        task,context=context,cancel_event=asyncio.Event()))
    assert reason is None and usage["total_tokens"]==3_600
    assert len(gateway.requests)==2
    assert gateway.requests[1][0][3]==[]
    compact=json.loads(gateway.requests[1][0][2][1]["content"])
    assert "omitted" in compact["notice"].lower()
    assert compact["evidence"][0]["evidence_id"]=="ev-captured-1"
    assert result["evidence"][0]["evidence_id"]=="ev-captured-1"


def test_tight_summary_budget_fairly_keeps_evidence_and_draft_with_minimum_reserve():
    evidence=[{"evidence_id":"ev-1","summary":"complete key evidence"},
              {"evidence_id":"ev-2","summary":"complete secondary evidence"}]
    drafts=[{"draft_id":"dr-1","payload":{"kind":"setup"}}]
    messages=_summary_messages("summarize",{},[],evidence,drafts,remaining=10_000)
    assert messages is not None
    two_records={"messages":messages,"tools":[]}
    tight_remaining=_estimate_input_tokens(two_records)+MIN_SUMMARY_OUTPUT_TOKENS
    compact=_summary_messages("summarize",{},[],evidence,drafts,remaining=tight_remaining)
    assert compact is not None
    payload=json.loads(compact[1]["content"])
    assert len(payload["evidence"])==1
    assert payload["drafts"]==drafts
    assert payload["omitted_drafts"]==0
    assert payload["omitted_evidence"]==1
    assert _estimate_input_tokens({"messages":compact,"tools":[]})+MIN_SUMMARY_OUTPUT_TOKENS<=tight_remaining


def _archive_shaped_evidence(evidence_id, tool_name, *, size=220):
    binding = {"practice_id":"practice-synthetic-1", "revision":7,
               "data_version_id":"dv-synthetic-1", "source_code":"synthetic-fixture"}
    return {"evidence_id":evidence_id, "tool_name":tool_name, "as_of":"2026-10-27",
            "version":{**binding, "context_fingerprint":"c"*640,
                       "algorithm_version":"algorithm-2026-01", "algorithm_fingerprint":"a"*640,
                       "fee_version":"fee-cn-a", "fee_fingerprint":"f"*640},
            "summary":f"Captured {tool_name} evidence for the synthetic case.",
            "data":{"metrics":{"return_bps":125,"drawdown_bps":-80}, "payload":"d"*size}}


def test_archive_sized_summary_projects_redundant_versions_and_keeps_later_comparison():
    evidence=[_archive_shaped_evidence("ev-state", "get_simulation_state"),
              _archive_shaped_evidence("ev-source", "get_source_quality"),
              _archive_shaped_evidence("ev-review", "compare_review")]
    original=json.loads(json.dumps(evidence))
    messages=_summary_messages("Compare the revealed simulation with its source", {"scope":"simulation"},
                               [], evidence, [], remaining=8_192)
    assert messages is not None
    packed=json.loads(messages[1]["content"])
    by_id={item["evidence_id"]:item for item in packed["evidence"]}
    assert {"ev-source", "ev-review"} <= set(by_id)
    assert packed["omitted_evidence"] == len(evidence)-len(by_id)
    assert packed["omitted_drafts"] == 0
    assert set(by_id["ev-review"]["version"]) == set(SUMMARY_VERSION_BINDING_FIELDS)
    assert by_id["ev-review"]["data"] == evidence[2]["data"]
    assert SUMMARY_VERSION_PROJECTION_LIMITATION in packed["limitations"]
    assert evidence == original
    assert _estimate_input_tokens({"messages":messages,"tools":[]})+MIN_SUMMARY_OUTPUT_TOKENS <= 8_192


def test_archive_sized_trade_summary_fairly_keeps_draft_and_exact_omission_counts():
    evidence=[_archive_shaped_evidence("ev-state", "get_simulation_state"),
              _archive_shaped_evidence("ev-source", "get_source_quality"),
              _archive_shaped_evidence("ev-price", "preview_trade")]
    draft={"draft_id":"dr-synthetic-trade", "kind":"trade", "practice_id":"practice-synthetic-1",
           "expected_revision":7, "payload":{"ts_code":"600000.SH", "direction":"buy", "planned_shares":100},
           "limitations":["Draft only; no order was written."]}
    messages=_summary_messages("Review the trade draft against the revealed state", {"scope":"simulation"},
                               [], evidence, [draft], remaining=8_192)
    assert messages is not None
    packed=json.loads(messages[1]["content"])
    assert packed["drafts"] == [draft]
    assert packed["omitted_drafts"] == 0
    assert packed["omitted_evidence"] == len(evidence)-len(packed["evidence"])
    assert any(item["evidence_id"] == "ev-state" for item in packed["evidence"])
    assert _estimate_input_tokens({"messages":messages,"tools":[]})+MIN_SUMMARY_OUTPUT_TOKENS <= 8_192


def test_unfittable_projected_record_is_omitted_whole_and_keeps_binding_fields():
    evidence=[_archive_shaped_evidence("ev-fit", "get_simulation_state", size=30),
              _archive_shaped_evidence("ev-too-large", "compare_review", size=12_000)]
    messages=_summary_messages("Summarize the captured state", {}, [], evidence, [], remaining=2_500)
    assert messages is not None
    packed=json.loads(messages[1]["content"])
    assert [item["evidence_id"] for item in packed["evidence"]] == ["ev-fit"]
    assert packed["omitted_evidence"] == 1
    assert set(packed["evidence"][0]["version"]) == set(SUMMARY_VERSION_BINDING_FIELDS)
    assert packed["evidence"][0]["data"] == evidence[0]["data"]
    assert _summary_messages("Summarize", {}, [], evidence[1:], [], remaining=2_500) is None


def test_bounded_summary_model_input_and_durable_evidence_are_separate(tmp_path):
    evidence=[_archive_shaped_evidence("ev-state-1", "get_simulation_state", size=120),
              _archive_shaped_evidence("ev-source-2", "get_source_quality", size=120),
              _archive_shaped_evidence("ev-review-3", "compare_review", size=120)]
    draft={"draft_id":"dr-trade-4", "kind":"trade", "practice_id":"practice-synthetic-1",
           "expected_revision":7, "payload":{"ts_code":"600000.SH", "direction":"buy", "planned_shares":100},
           "limitations":["Draft only; no order was written."]}
    original_evidence=json.loads(json.dumps(evidence))
    original_draft=json.loads(json.dumps(draft))

    class SyntheticArchiveTools(FakeTools):
        writes=[]
        def execute_tool(self,settings,context,name,arguments):
            self.calls.append((name,arguments))
            return {"data":{"transcript":"x"*6_000}, "evidence":evidence, "drafts":[draft],
                    "limitations":[], "context_fingerprint":"fp"}

    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize(); task,context=make_task(store)
    task["limits"]["tokens"]=8_192
    tools=SyntheticArchiveTools()
    gateway=FakeGateway([
        model_response(calls=[{"id":"call-1","type":"function","function":{
            "name":"lookup","arguments":"{\"symbol\":\"600000.SH\"}"}}],total=2_800),
        model_response(content="Compare the evidence in ev-review-3 with source ev-source-2 and retain draft dr-trade-4.",total=800),
    ])
    result,reason,usage=asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=tools).run(
        task,context=context,cancel_event=asyncio.Event()))
    assert reason is None
    assert usage["total_tokens"]==3_600
    assert len(gateway.requests)==2 and gateway.requests[1][0][3]==[]
    compact=json.loads(gateway.requests[1][0][2][1]["content"])
    assert {item["evidence_id"] for item in compact["evidence"]}=={"ev-state-1","ev-source-2","ev-review-3"}
    assert compact["drafts"]==[draft]
    assert compact["omitted_evidence"]==0 and compact["omitted_drafts"]==0
    assert all(set(item["version"])==set(SUMMARY_VERSION_BINDING_FIELDS) for item in compact["evidence"])
    assert SUMMARY_VERSION_PROJECTION_LIMITATION in compact["limitations"]
    durable=result
    assert durable["evidence"]==original_evidence
    assert durable["drafts"]==[original_draft]
    assert SUMMARY_VERSION_PROJECTION_LIMITATION in durable["limitations"]
    assert any("omitted evidence: 0, drafts: 0" in value for value in durable["limitations"])
    assert tools.writes==[]


def test_summary_stops_without_request_when_no_complete_record_fits(tmp_path):
    class OversizedEvidenceTools(FakeTools):
        def execute_tool(self,settings,context,name,arguments):
            self.calls.append((name,arguments))
            return {"data":{"blob":"x"*6_000},"evidence":[{"evidence_id":"ev-full","summary":"important "*1_000}],
                    "drafts":[],"limitations":[],"context_fingerprint":"fp"}

    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize(); task,context=make_task(store)
    task["limits"]["tokens"]=8_192
    gateway=FakeGateway([model_response(calls=[{"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\"symbol\":\"600000.SH\"}"}}],total=8_000)])
    with pytest.raises(RuntimeErrorCode) as caught:
        asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=OversizedEvidenceTools()).run(
            task,context=context,cancel_event=asyncio.Event()))
    assert caught.value.reason=="token_budget_exhausted"
    assert len(gateway.requests)==1
    saved=store.get(task["task_id"])["result"]
    assert saved["evidence"][0]["evidence_id"]=="ev-full"
    assert any("minimum 128-token" in value for value in saved["limitations"])


def test_numeric_guard_accepts_exact_uuid_and_evidenced_dates_but_blocks_unverified_amounts():
    evidence=[{"evidence_id":"ev_12345678-1234-4234-8234-123456789abc","as_of":"20241119",
               "data":{"price_cents":1000,"fee_cents":510,"planned_shares":100,
                       "source_id":"00000000-0000-4000-8000-000000000000",
                       "return_pct":"1000%",
                       "research_record_id":"rr_6eda5966cf8946f4a8771ecf58031a7d",
                       "revision_id":"rev_8f6aed90857d419b9ebff204522ebbb3"}}]
    assert not _unsupported_numeric_claims(
        "As of 2024-11-19, evidence ev_12345678-1234-4234-8234-123456789abc reports price 1,000 cents, "
        "record rr_6eda5966cf8946f4a8771ecf58031a7d revision rev_8f6aed90857d419b9ebff204522ebbb3 "
        "and source 00000000-0000-4000-8000-000000000000.",evidence,[])
    assert _unsupported_numeric_claims("The price is 1,001 cents.",evidence,[])
    assert not _unsupported_numeric_claims("已知价格10元。",evidence,[])
    assert not _unsupported_numeric_claims("已知价格1000分。",evidence,[])
    assert not _unsupported_numeric_claims("已知价格10元，手续费5.10元。",evidence,[])
    assert not _unsupported_numeric_claims("计划买入100股。",evidence,[])
    assert _unsupported_numeric_claims("计划买入101股。",evidence,[])
    assert _unsupported_numeric_claims("利润1000元",evidence,[])
    assert _unsupported_numeric_claims("价格1000元",evidence,[])
    assert _unsupported_numeric_claims("手续费1000分",evidence,[])
    assert _unsupported_numeric_claims("利润999元，收益-2.5%",evidence,[])
    assert _unsupported_numeric_claims("利润1000元，收益+1000%",evidence,[])
    assert not _unsupported_numeric_claims("收益+1000%",evidence,[])
    assert _unsupported_numeric_claims("未来价格10元",evidence,[])
    assert _unsupported_numeric_claims("As of 2024-11-20 the result was 1,000.",evidence,[])
    assert _unsupported_numeric_claims("See 00000000-0000-4000-8000-000000000001 for details.",evidence,[])
    assert _unsupported_numeric_claims("See ev_87654321 for details.",evidence,[])
    assert _unsupported_numeric_claims("See rr_6eda5966cf8946f4a8771ecf58031a7dX.",evidence,[])


def test_numeric_guard_requires_explicit_percent_units_and_rejects_mixed_values():
    assert _unsupported_numeric_claims("价格1000%", [{"data": {"price": "1,000"}}], [])
    assert not _unsupported_numeric_claims("价格1000%", [{"data": {"price_pct": "1,000%"}}], [])
    assert not _unsupported_numeric_claims("价格10元", [{"data": {"price_yuan": "10"}}], [])
    assert _unsupported_numeric_claims("价格10%", [{"data": {"price_yuan": "10"}}], [])
    assert _unsupported_numeric_claims("价格10元", [{"data": {"price_yuan": "10 USD"}}], [])
    assert _unsupported_numeric_claims("价格1000%", [{"data": {"price_pct": "1,000% estimated"}}], [])
    assert not _unsupported_numeric_claims("收益250bps", [{"data": {"return_bps": 250}}], [])


def test_numeric_guard_verifies_m2_financial_units_and_punctuation_clauses():
    evidence=[{"evidence_id":"ev-m2","data":{
        "estimated_notional_cents":100_000,
        "fee_estimate":{"fee_cents":510},
        "slippage_cents":100,
        "known_price_cents":1_000,
        "planned_shares":100,
        "cash_cents":10_000_000,
    }}]
    assert not _unsupported_numeric_claims(
        "名义金额1000元，手续费5.10元，滑点1元，价格10元，计划买入100股。",evidence,[])
    assert _unsupported_numeric_claims("利润1000元",evidence,[])
    assert _unsupported_numeric_claims("收益10%",evidence,[])


def _path_comparison_evidence(nav_values=(10_000_000,) * 4, trade_counts=(0,) * 4):
    return [{"evidence_id": "ev-synthetic-review", "tool_name": "compare_review", "as_of": "20241119",
             "data": {"paths": [{"nav_cents": nav, "trade_count": count}
                                for nav, count in zip(nav_values, trade_counts)]}}]


def test_numeric_guard_accepts_evidenced_nav_trade_count_path_count_and_chinese_dates():
    evidence = _path_comparison_evidence()
    assert not _unsupported_numeric_claims("4条路径净值均为100000元。", evidence, [])
    assert not _unsupported_numeric_claims("4条路径交易0笔。", evidence, [])
    assert not _unsupported_numeric_claims("All 4 paths have a NAV of 100000 yuan.", evidence, [])
    assert not _unsupported_numeric_claims("All 4 paths completed 0 trades.", evidence, [])
    assert not _unsupported_numeric_claims("截至2024年11月19日，净值为100000元。", evidence, [])
    assert _unsupported_numeric_claims("截至2024年11月20日，净值为100000元。", evidence, [])
    assert _unsupported_numeric_claims("5条路径净值均为100000元。", evidence, [])
    assert _unsupported_numeric_claims("4条路径净值均为99999元。", evidence, [])
    assert _unsupported_numeric_claims("交易次数为1次。", evidence, [])
    assert _unsupported_numeric_claims("交易0。", evidence, [])


def test_numeric_guard_keeps_price_profit_and_future_semantics_separate():
    evidence = _path_comparison_evidence()
    assert _unsupported_numeric_claims("4条路径利润均为100000元。", evidence, [])
    assert _unsupported_numeric_claims("未来价格为100000元。", evidence, [])
    assert _unsupported_numeric_claims("收益为100000元。", evidence, [])


def test_numeric_guard_requires_every_path_to_support_universal_nav_claim():
    evidence = _path_comparison_evidence(nav_values=(10_000_000, 10_000_000, 10_000_001, 10_000_000))
    assert _unsupported_numeric_claims("4条路径净值均为100000元。", evidence, [])
    consistent = _path_comparison_evidence(nav_values=(10_000_000,) * 4)
    assert not _unsupported_numeric_claims("4条路径净值均为100000元。", consistent, [])
    inconsistent_counts = _path_comparison_evidence(trade_counts=(0, 0, 1, 0))
    assert _unsupported_numeric_claims("4条路径交易均为0笔。", inconsistent_counts, [])
    unrelated_values = [{"data": {"nav_cents": 10_000_000,
                                   "paths": [{"trade_count": 0} for _ in range(4)]}}]
    assert _unsupported_numeric_claims("4条路径净值均为100000元。", unrelated_values, [])
    ambiguous_arrays = [{"data": {"paths": [{"nav_cents": 10_000_000, "trade_count": 0} for _ in range(4)]}}]
    second_array = [{"paths": [{"nav_cents": 10_000_000, "trade_count": 0} for _ in range(4)]}]
    assert _unsupported_numeric_claims("4条路径净值均为100000元。", ambiguous_arrays, second_array)


def test_numeric_guard_strips_only_explicit_list_prefix_and_reports_fixed_diagnostics():
    evidence = _path_comparison_evidence()
    assert not _unsupported_numeric_claims("1. 4条路径净值均为100000元。", evidence, [])
    assert _numeric_claim_diagnostic("净值为99999元。", evidence, []) == "unit_or_semantic_mismatch"
    assert _numeric_claim_diagnostic("无证收益为1.25%。", evidence, []) == "amount_without_matching_evidence"
    assert _numeric_claim_diagnostic("截至2024年11月20日。", evidence, []) == "unverified_date"
    assert _numeric_claim_diagnostic("引用 ev-unknown。", evidence, []) == "unrecognized_identifier"


def test_numeric_summary_suppresses_failed_sentences_without_leaking_them():
    evidence = _path_comparison_evidence()
    source = "4条路径净值均为100000元。净值为99999元。未来价格为100000元。"
    filtered, diagnostics = _filter_numeric_summary(source, evidence, [])
    assert filtered == "4条路径净值均为100000元。"
    assert "99999" not in filtered and "未来价格" not in filtered and "100000元。" in filtered
    assert diagnostics == ["unit_or_semantic_mismatch", "future_or_out_of_scope_claim"]
    filtered, diagnostics = _filter_numeric_summary("Safe. Unsupported decimal 1.25 is not evidenced.", evidence, [])
    assert filtered == "Safe."
    assert "1.25" not in filtered and diagnostics == ["unrecognized_numeric_type"]


def test_chinese_path_counts_and_universal_claims_are_checked():
    consistent = _path_comparison_evidence()
    mixed = _path_comparison_evidence(nav_values=(10_000_000, 10_000_000, 10_000_001, 10_000_000))
    assert not _unsupported_numeric_claims("四条路径净值均为100000元。", consistent, [])
    assert _unsupported_numeric_claims("五条路径净值均为100000元。", consistent, [])
    for claim in ("四条路径净值都是100000元。", "所有四条路径净值为100000元。", "每条路径净值为100000元。", "四条路径每条净值为100000元。", "四条路径净值100000元全部一致。"):
        assert not _unsupported_numeric_claims(claim, consistent, [])
        assert _unsupported_numeric_claims(claim, mixed, [])
    assert _unsupported_numeric_claims("净值为十万元。", consistent, [])
    assert _unsupported_numeric_claims("净值为十万。", consistent, [])
    assert not _unsupported_numeric_claims("净值是一致的。", consistent, [])
    assert _unsupported_numeric_claims("收益百分之十。", consistent, [])
    assert not _unsupported_numeric_claims("一般情况下，路径信息可供参考。", consistent, [])
    filtered, diagnostics = _filter_numeric_summary("一般情况下可供参考。五条路径净值为100000元。四条路径净值均为100000元。", consistent, [])
    assert "五条路径" not in filtered and "四条路径" in filtered and "一般情况下" in filtered
    assert diagnostics


def _bound_model_context():
    return {"scope":"simulation", "practice_id":"practice-1", "revision":2,
            "revealed_date":"20241119", "data_version_id":"dv-synthetic-1",
            "source_code":"tushare-fixture"}


def _bound_evidence(tool_name, data, *, practice_id="practice-1", revision=2,
                    as_of="20241119", evidence_id="ev-synthetic-1"):
    return {"evidence_id":evidence_id, "tool_name":tool_name, "as_of":as_of, "data":data,
            "version":{"practice_id":practice_id, "revision":revision,
                       "data_version_id":"dv-synthetic-1", "source_code":"tushare-fixture"}}


def _comparison_evidence():
    return _bound_evidence("compare_review", {"as_of":"20241119", "limitations":["pool_not_as_of"],
        "paths":[{"key":key,"nav_cents":10_000_000,"trade_count":0,"restrictions":[]}
                 for key in ("user","incremental","independent","hold")]})


def _trade_evidence():
    return _bound_evidence("preview_trade", {"as_of":"20241119","direction":"buy","ts_code":"000001.SZ",
        "known_price_cents":1000,"planned_shares":100,"estimated_notional_cents":100000,
        "fee_estimate":{"fee_cents":510,"commission_cents":500,"slippage_cents":100},"known_constraints_pass":True,
        "known_checks":{"known_price_available":True,"board_lot_aligned":True,
            "cash_sufficient_at_known_price":True,"execution_date_available":True,
            "fee_schedule_available":True,"market_rules_available":True},
        "future_execution_price_cents":None,"writes_order":False})


def test_markdown_table_headers_bind_numeric_values_and_unknown_columns_fail_closed():
    evidence = _path_comparison_evidence()
    table = """| Path | NAV | Trade count |
| --- | --- | --- |
| User | 100000 yuan | 0 trades |
| Incremental | 100000 yuan | 0 trades |
| Independent | 100000 yuan | 0 trades |
| Hold | 100000 yuan | 0 trades |"""
    filtered, diagnostics = _filter_numeric_summary(table, evidence, [])
    assert "100000 yuan" in filtered and "0 trades" in filtered
    assert diagnostics == []
    wrong = table.replace("100000 yuan", "99999 yuan", 1)
    assert not _filter_numeric_summary(wrong, evidence, [])[0]
    unknown = table.replace("| Path | NAV | Trade count |", "| Path | NAV | Mystery |")
    assert not _filter_numeric_summary(unknown, evidence, [])[0]


def test_commission_and_total_fee_are_bound_to_their_own_evidence_fields():
    evidence = _trade_evidence()
    assert not _unsupported_numeric_claims("Preview commission is 500 cents.", evidence, [])
    assert _unsupported_numeric_claims("Preview total fee is 500 cents.", evidence, [])
    assert not _unsupported_numeric_claims("Preview total fee is 510 cents.", evidence, [])
    assert not _unsupported_numeric_claims("\u9884\u89c8\u4f63\u91d1\u4e3a500 cents.", evidence, [])
    assert _unsupported_numeric_claims("\u9884\u89c8\u603b\u8d39\u7528\u4e3a500 cents.", evidence, [])


def test_numeric_filter_fails_closed_for_table_cell_errors_and_preserves_safe_mixed_sentences():
    evidence = _path_comparison_evidence()
    table = """| Path | NAV | Trade count |
| --- | ---: | ---: |
| User | 100000 yuan | 0 trades |
| Incremental | 100000 yuan | 0 trades |
| Independent | 100000 yuan | 0 trades |
| Hold | 100000 yuan | 0 trades |"""
    # A single unsupported cell suppresses the whole table; matching numbers
    # in other columns or rows must not lend it meaning.
    wrong_cell = table.replace("| User | 100000 yuan | 0 trades |", "| User | 100000 yuan | 1 trades |")
    assert not _filter_numeric_summary(wrong_cell, evidence, [])[0]
    # An unknown numeric column cannot borrow the NAV category from its row.
    unknown_column = table.replace("| Path | NAV | Trade count |", "| Path | NAV | Mystery |")
    assert not _filter_numeric_summary(unknown_column, evidence, [])[0]
    # Bad numeric prose is suppressed while an independent safe sentence stays.
    mixed = "净值为99999元。净值为100000元。"
    filtered, diagnostics = _filter_numeric_summary(mixed, evidence, [])
    assert filtered == "净值为100000元。"
    assert diagnostics


def test_total_fee_requires_total_field_even_when_component_matches():
    evidence = _trade_evidence()
    assert not _unsupported_numeric_claims("Preview total fee is 510 cents.", evidence, [])
    assert _unsupported_numeric_claims("Preview total fee is 500 cents.", evidence, [])
    assert not _unsupported_numeric_claims("Preview commission is 500 cents.", evidence, [])
    assert _unsupported_numeric_claims("Preview commission is 510 cents.", evidence, [])


def _research_evidence(*, as_of="20241119", owner_id="practice-1", revision_id="rev_synthetic123"):
    return _bound_evidence("read_research_record", {"as_of":"20241119","scope":"simulation",
        "owner_id":owner_id,"revision_id":revision_id,"evidence_as_of":as_of,
        "body":{"scope":"simulation","evidence_as_of":as_of,"source_refs":[
            {"owner_type":"practice","owner_id":owner_id,"revision":1,"revealed_date":"20241119",
             "data_version_id":"dv-synthetic-1"}]}})


@pytest.mark.parametrize("evidence,expected", [
    (_comparison_evidence(), "NAV 10,000,000 cents"),
    (_trade_evidence(), "参考总成本为100610分"),
    (_research_evidence(), "revision_id=rev_synthetic123"),
])
def test_engine_appends_structured_current_evidence_facts_through_normal_filter(tmp_path, evidence, expected):
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize()
    task,context=make_task(store)
    context["model_context"]=_bound_model_context()
    tools=FakeTools()
    tools.execute_tool=lambda settings,ctx,name,args: {"data":evidence["data"],"evidence":[evidence],
        "drafts":[],"limitations":[],"context_fingerprint":"fp"}
    gateway=FakeGateway([
        model_response(calls=[{"id":"call-facts","type":"function","function":{
            "name":"lookup","arguments":"{\"symbol\":\"synthetic\"}"}}]),
        model_response(content="I checked the captured evidence."),
    ])
    result,reason,_=asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=tools).run(
        task,context=context,cancel_event=asyncio.Event()))
    assert reason is None
    assert result["evidence"] == [evidence]
    assert _build_structured_evidence_facts(result["evidence"], _bound_model_context())
    assert expected in result["summary"], result["limitations"]
    assert evidence["evidence_id"] in result["summary"]
    assert "I checked the captured evidence." in result["summary"]
    assert "20991231" not in result["summary"]
    if evidence["tool_name"] == "compare_review":
        for key in ("user", "incremental", "independent", "hold"):
            assert f"{key} NAV" in result["summary"]
        assert "pool_not_as_of" in result["summary"]
    elif evidence["tool_name"] == "preview_trade":
        for expected_fact in ("已知参考价格为1000分", "预览计划股数为100股", "预览名义金额为100000分",
                              "预览模拟费用为510分", "预览滑点估算为100分", "预览参考总成本为100610分",
                              "未来成交价格未知"):
            assert expected_fact in result["summary"]
    elif evidence["tool_name"] == "read_research_record":
        assert "evidence_as_of=20241119" in result["summary"]
    assert not any("Some complete summary sentences were omitted" in item for item in result["limitations"])


def test_structured_evidence_facts_reject_missing_future_mismatched_and_malformed_data():
    context=_bound_model_context()
    assert _build_structured_evidence_facts([],context)==[]
    assert _build_structured_evidence_facts([_bound_evidence("compare_review",{
        "as_of":"20241120","paths":_comparison_evidence()["data"]["paths"]})],context)==[]
    assert _build_structured_evidence_facts([_bound_evidence("compare_review",
        _comparison_evidence()["data"],practice_id="another-practice")],context)==[]
    missing_path=_comparison_evidence(); missing_path["data"]["paths"].pop()
    assert _build_structured_evidence_facts([missing_path],context)==[]
    inconsistent_trade=_trade_evidence(); inconsistent_trade["data"]["estimated_notional_cents"]=99_999
    assert _build_structured_evidence_facts([inconsistent_trade],context)==[]
    future_research=_research_evidence(as_of="20991231")
    assert _build_structured_evidence_facts([future_research],context)==[]
    wrong_owner=_research_evidence(owner_id="another-practice")
    assert _build_structured_evidence_facts([wrong_owner],context)==[]
    malformed_revision=_research_evidence(revision_id="rev-unknown")
    assert _build_structured_evidence_facts([malformed_revision],context)==[]
    for missing_binding in ("practice_id", "revision", "revealed_date", "data_version_id", "source_code"):
        unbound=dict(context); unbound.pop(missing_binding)
        assert _build_structured_evidence_facts([_comparison_evidence()],unbound)==[]
    wrong_source=_comparison_evidence(); wrong_source["version"]["source_code"]="untrusted-source"
    assert _build_structured_evidence_facts([wrong_source],context)==[]
    wrong_version=_comparison_evidence(); wrong_version["version"]["data_version_id"]="other-version"
    assert _build_structured_evidence_facts([wrong_version],context)==[]
    conflicting_as_of=_comparison_evidence(); conflicting_as_of["as_of"]="20241118"
    assert _build_structured_evidence_facts([conflicting_as_of],context)==[]
    bool_revision=_comparison_evidence(); bool_revision["version"]["revision"]=True
    assert _build_structured_evidence_facts([bool_revision],context)==[]


def test_runtime_records_only_fixed_diagnostic_and_keeps_safe_summary_sentences(tmp_path):
    store = AgentStore(tmp_path / "tasks.sqlite3"); store.initialize()
    task, context = make_task(store)
    gateway = FakeGateway([
        model_response(calls=[{"id": "lookup", "type": "function", "function": {
            "name": "lookup", "arguments": "{\"symbol\":\"600000.SH\"}"}}]),
        model_response(content="The lookup completed. Net asset value is 999999 yuan."),
    ])
    result, reason, _ = asyncio.run(AgentEngine(settings=object(), store=store, gateway=gateway,
                                               tools=FakeTools()).run(
        task, context=context, cancel_event=asyncio.Event()))
    assert reason is None
    assert result["summary"] == "The lookup completed."
    assert "999999" not in result["summary"]
    assert any("amount_without_matching_evidence" in item for item in result["limitations"])
    events, _ = store.events(task["task_id"], 0, 20)
    diagnostic = next(event for event in events if event["kind"] == "summary_content_suppressed")
    assert diagnostic["reason"] == "amount_without_matching_evidence"
    assert "999999" not in diagnostic["summary"]


def test_evidence_only_summary_cannot_execute_a_returned_tool_call(tmp_path):
    class GrowingTools(FakeTools):
        def execute_tool(self,settings,context,name,arguments):
            self.calls.append((name,arguments))
            return {"data":{"blob":"x"*6_000},"evidence":[{"evidence_id":"ev-captured-1","as_of":"2026-01-01","summary":"captured"}],
                    "drafts":[],"limitations":[],"context_fingerprint":"fp"}

    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize(); task,context=make_task(store)
    task["limits"]["tokens"]=8_192
    response=model_response(calls=[{"id":"call-summary","type":"function","function":{
        "name":"lookup","arguments":"{\"symbol\":\"600000.SH\"}"}}],total=800)
    gateway=FakeGateway([
        model_response(calls=[{"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\"symbol\":\"600000.SH\"}"}}],total=2_800),
        response,
    ])
    tools=GrowingTools()
    with pytest.raises(RuntimeErrorCode) as caught:
        asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=tools).run(
            task,context=context,cancel_event=asyncio.Event()))
    assert caught.value.reason=="model_tool_call_invalid"
    assert tools.calls==[("lookup",{"symbol":"600000.SH"})]
    assert "omitted earlier conversation" in " ".join(store.get(task["task_id"])["result"]["limitations"])


def test_known_tool_schema_failure_gets_one_safe_correction_and_executes_only_valid_args(tmp_path):
    invalid_value="PRIVATE_INVALID_ARGUMENT_VALUE"
    gateway=FakeGateway([
        model_response(calls=[{"id":"bad-call","type":"function","function":{"name":"lookup",
            "arguments":json.dumps({"symbol":invalid_value})}}]),
        model_response(calls=[{"id":"fixed-call","type":"function","function":{"name":"lookup",
            "arguments":"{\"symbol\":\"600000.SH\"}"}}]),
        model_response(content="The lookup is complete."),
    ])
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize(); task,context=make_task(store)
    tools=FakeTools()
    result,reason,usage=asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=tools).run(
        task,context=context,cancel_event=asyncio.Event()))
    assert reason is None and result["summary"]=="The lookup is complete."
    assert tools.calls==[("lookup",{"symbol":"600000.SH"})]
    assert usage["tool_calls"]==2 and usage["model_calls"]==3
    correction_messages=gateway.requests[1][0][2]
    safe_error=next(message["content"] for message in correction_messages if message.get("role")=="tool")
    assert "invalid_arguments" in safe_error and invalid_value not in safe_error
    checkpoint=store.get(task["task_id"])["checkpoint"]
    assert any(step.get("kind")=="argument_validation" and step.get("state")=="committed"
               for step in checkpoint["steps"])


def test_second_invalid_tool_arguments_stop_without_tool_execution(tmp_path):
    invalid_call=lambda call_id,arguments: {"id":call_id,"type":"function","function":{
        "name":"lookup","arguments":arguments}}
    gateway=FakeGateway([
        model_response(calls=[invalid_call("bad-1","{\"unexpected\":true}")]),
        model_response(calls=[invalid_call("bad-2","{\"symbol\":123}")]),
    ])
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize(); task,context=make_task(store)
    tools=FakeTools()
    with pytest.raises(RuntimeErrorCode) as caught:
        asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=tools).run(
            task,context=context,cancel_event=asyncio.Event()))
    assert caught.value.reason=="tool_arguments_invalid"
    assert len(gateway.requests)==2
    assert tools.calls==[]
    assert store.get(task["task_id"])["usage"]["tool_calls"]==2


def test_invalid_tool_arguments_cannot_be_silently_abandoned_as_success(tmp_path):
    gateway=FakeGateway([
        model_response(calls=[{"id":"bad-call","type":"function","function":{
            "name":"lookup","arguments":"{\"unexpected\":true}"}}]),
        model_response(content="Everything is complete."),
    ])
    store=AgentStore(tmp_path/"tasks.sqlite3"); store.initialize(); task,context=make_task(store)
    tools=FakeTools()
    with pytest.raises(RuntimeErrorCode) as caught:
        asyncio.run(AgentEngine(settings=object(),store=store,gateway=gateway,tools=tools).run(
            task,context=context,cancel_event=asyncio.Event()))
    assert caught.value.reason=="tool_arguments_invalid"
    assert tools.calls==[]


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
    tools=FakeTools()
    responses=[model_response(calls=[call])]
    if reason=="tool_arguments_invalid":
        responses.append(model_response(calls=[{"id":"retry-call","type":"function","function":{
            "name":"lookup","arguments":"{\"symbol\":123}"}}]))
    engine=AgentEngine(settings=object(),store=store,gateway=FakeGateway(responses),tools=tools)
    with pytest.raises(RuntimeErrorCode) as caught:
        asyncio.run(engine.run(task,context=context,cancel_event=asyncio.Event()))
    assert caught.value.reason==reason
    assert tools.calls==[]


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


def test_numeric_reference_total_requires_one_complete_valid_buy_preview():
    import copy
    data = {"direction": "buy", "known_constraints_pass": True, "known_checks": dict.fromkeys(("known_price_available", "market_rules_available", "board_lot_aligned", "fee_schedule_available", "cash_sufficient_at_known_price", "execution_date_available"), True),
            "known_price_cents": 1000, "planned_shares": 100, "estimated_notional_cents": 100000,
            "fee_estimate": {"fee_cents": 510, "slippage_cents": 100}}
    evidence = [{"tool_name": "preview_trade", "data": data}]
    assert not _unsupported_numeric_claims("\u53c2\u8003\u603b\u6210\u672c1006.10\u5143\u3002", evidence, [])
    assert not _unsupported_numeric_claims("\u53c2\u8003\u603b\u6210\u672c100610\u5206\u3002", evidence, [])
    assert _unsupported_numeric_claims("\u53c2\u8003\u603b\u6210\u672c1006.11\u5143\u3002", evidence, [])
    for change in (lambda d: d.update(direction="sell"), lambda d: d.update(known_constraints_pass=False),
                   lambda d: d.update(estimated_notional_cents=99999), lambda d: d["fee_estimate"].pop("slippage_cents"),
                   lambda d: d["fee_estimate"].update(fee_cents=True)):
        bad = copy.deepcopy(evidence)
        change(bad[0]["data"])
        assert _unsupported_numeric_claims("\u53c2\u8003\u603b\u6210\u672c1006.10\u5143\u3002", bad, [])
    split = copy.deepcopy(evidence)
    split[0]["data"]["fee_estimate"].pop("slippage_cents")
    split.append({"tool_name": "preview_trade", "data": {"fee_estimate": {"slippage_cents": 100}}})
    assert _unsupported_numeric_claims("\u53c2\u8003\u603b\u6210\u672c1006.10\u5143\u3002", split, [])
    assert _unsupported_numeric_claims("\u603b\u6210\u672c1006.10\u5143\u3002", evidence, [])


def test_synthetic_sentence_replay_is_local_and_opt_in():
    # Test-only, in-memory replay: caller supplies synthetic sentences; no runtime hook or persistence.
    def replay_synthetic(sentences, enabled=False):
        if not enabled:
            return []
        return [(sentence, _numeric_claim_diagnostic(sentence, [], [])) for sentence in sentences]
    sentences = ["\u65e0\u8bc1\u6536\u76ca\u4e3a1.25%\u3002", "\u666e\u901a\u8bf4\u660e\u3002"]
    assert replay_synthetic(sentences) == []
    assert replay_synthetic(sentences, enabled=True) == [(sentences[0], "amount_without_matching_evidence"), (sentences[1], None)]

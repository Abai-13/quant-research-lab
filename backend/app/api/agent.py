"""Frozen /api/agent task and configuration API; no implicit test provider."""
from __future__ import annotations

import hashlib
import asyncio
import importlib
import json
import re
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..agent_runtime.manager import AgentTaskManager
from ..agent_runtime.store import ActiveTaskError, AgentStore, IdempotencyConflict, canonical
from ..agent_runtime.operation import operation_view
from ..config import Settings


class ConfigBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name:str=Field(min_length=1,max_length=80)
    protocol:str
    base_url:str=Field(max_length=2_048)
    model:str=Field(min_length=1,max_length=200)
    api_key:str|None=Field(default=None,max_length=4_000)
    expected_config_version:str|None=None
    allow_loopback_http:bool=False
    trusted_private_https:bool=False
    limits:dict[str,Any]|None=None

class TestBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    config_version:str
    consent:bool
    test_tools:bool

class TaskBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    practice_id:str|None
    expected_revision:int|None=None
    goal:str=Field(min_length=1,max_length=8_000)
    config_version:str
    consent:bool
    consent_digest:str=Field(min_length=64,max_length=64)
    idempotency_key:str=Field(min_length=8,max_length=160)
    limits:dict[str,Any]|None=None
    parent_task_id:str|None=None
    plan_id:str|None=None
    expected_plan_version:int|None=Field(default=None,ge=1)

class CancelBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    idempotency_key:str=Field(min_length=8,max_length=160)
    expected_generation:int|None=Field(default=None,ge=1)

class ResumeBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_generation:int=Field(ge=1)
    consent:bool
    config_version:str
    consent_digest:str=Field(min_length=64,max_length=64)
    idempotency_key:str=Field(min_length=8,max_length=160)


def _service(name:str)->Any:
    try:return importlib.import_module(name)
    except ImportError: raise HTTPException(503,detail={"ok":False,"reason":"agent_service_unavailable","detail":"Agent gateway or tools service is unavailable."}) from None

def _failure(exc:Exception, reason:str, status:int=400)->HTTPException:
    candidate=str(getattr(exc,"reason",reason))
    safe_reason=candidate if re.fullmatch(r"[a-z][a-z0-9_]{0,79}",candidate) else reason
    return HTTPException(status_code=int(getattr(exc,"status_code",status)),detail={"ok":False,"reason":safe_reason,"detail":reason.replace("_"," ")[:200]})

def _public_config(gateway:Any,settings:Settings)->dict[str,Any]:
    return gateway.get_public_config(settings)

def _bind_plan(settings:Settings, context:dict[str,Any], plan_id:str|None, expected_version:int|None,
               practice_id:str|None, store:AgentStore|None=None, parent_task_id:str|None=None)->None:
    if plan_id is None:
        if expected_version is not None: raise HTTPException(422,detail={"ok":False,"reason":"plan_version_required","detail":"plan_id is required."})
        return
    if expected_version is None: raise HTTPException(422,detail={"ok":False,"reason":"plan_version_required","detail":"expected_plan_version is required."})
    from ..practice.runtime import practice_settings
    db_path=practice_settings(settings).db_path
    if not db_path.is_file(): raise HTTPException(409,detail={"ok":False,"reason":"plan_not_found","detail":"Plan is unavailable."})
    uri=db_path.resolve().as_uri()+"?mode=ro"
    try:
        with sqlite3.connect(uri,uri=True,timeout=3) as conn:
            conn.row_factory=sqlite3.Row
            head=conn.execute("SELECT current_version,practice_id FROM practice_plan WHERE plan_id=?",(plan_id,)).fetchone()
            row=conn.execute("SELECT status,payload_json,symbols_json,goal,practice_id,qualification_json FROM practice_plan_version WHERE plan_id=? AND version=?",(plan_id,expected_version)).fetchone()
            if head is None or row is None: raise HTTPException(409,detail={"ok":False,"reason":"plan_not_found","detail":"Plan version is unavailable."})
            if int(head["current_version"])!=expected_version: raise HTTPException(409,detail={"ok":False,"reason":"plan_version_stale","detail":"Review the latest plan version."})
            linked=row["practice_id"]
            if linked!=practice_id: raise HTTPException(409,detail={"ok":False,"reason":"plan_context_invalid","detail":"Plan belongs to a different practice."})
            qualification=json.loads(row["qualification_json"])
            if row["status"]!="confirmed" and not qualification.get("ready"):
                issues=qualification.get("issues") or []
                hint="；".join(str(item.get("detail") or item.get("code")) for item in issues[:3]) or "请修正日期、来源或证券范围后重新检查。"
                raise HTTPException(422,detail={"ok":False,"reason":"plan_not_qualified","detail":"方案尚未通过资格检查："+hint})
            full_payload=json.loads(row["payload_json"])
            from app.data.chunks import VersionView
            market=VersionView(conn,str(full_payload["data_version_id"]),expected_source_code=str(full_payload["source_code"]))
            names={str(item["ts_code"]):item["name"] for item in market.securities()}
            public_payload={k:full_payload.get(k) for k in ("name","start_date","end_date","cash_cents","positions","data_version_id","source_code")}
            settings=full_payload.get("settings")
            if isinstance(settings,dict):
                public_payload["settings"]={k:settings[k] for k in (
                    "rebalance_every_n_days","hide_system_hints","max_single_name_weight_bp",
                    "min_cash_reserve_bp","parameter_bundle_id","rule_bundle_id","fee_bundle_id",
                    "constraints","watchlist_symbols") if k in settings}
            plan_context={"plan_id":plan_id,"version":expected_version,"status":row["status"],
                          "payload":public_payload,
                          "symbols":json.loads(row["symbols_json"]),"symbol_metadata":[{"ts_code":code,"name":names.get(code)} for code in json.loads(row["symbols_json"])],"goal":row["goal"]}
    except HTTPException: raise
    except (sqlite3.Error,ValueError,TypeError): raise HTTPException(409,detail={"ok":False,"reason":"plan_unavailable","detail":"Plan context is unavailable."}) from None
    history=[]
    parent=store.get(parent_task_id) if store is not None and parent_task_id else None
    seen=set()
    for _ in range(3):
        if not parent or parent["task_id"] in seen: break
        seen.add(parent["task_id"])
        pc=parent.get("context") or {}
        prior_version=pc.get("plan_version")
        if (pc.get("plan_id")!=plan_id or (prior_version is not None and prior_version>expected_version)
                or parent.get("practice_id")!=practice_id
                or pc.get("context_fingerprint")!=context.get("context_fingerprint")): break
        if parent.get("status")=="completed":
            result=parent.get("result") or {}
            history.append({"goal":parent.get("goal","")[:300],"summary":str(result.get("summary") or "")[:800],
                            "drafts":(result.get("drafts") or [])[:5]})
        parent=store.get(parent.get("parent_task_id")) if parent.get("parent_task_id") else None
    context["plan_id"]=plan_id; context["plan_version"]=expected_version
    model_context=dict(context.get("model_context") or {})
    model_context["current_plan"]=plan_context
    model_context["prior_plan_turns"]=history
    context["model_context"]=model_context

def _config_payload(config_result:dict[str,Any])->tuple[str,dict[str,Any]]:
    config=config_result.get("config")
    if not config_result.get("configured") or not isinstance(config,dict):
        raise HTTPException(409,detail={"ok":False,"reason":"agent_not_configured","detail":"Configure an Agent provider first."})
    return str(config.get("config_version") or ""),config

def _credential_generation(settings:Settings, config:dict[str,Any])->str|None:
    try:
        credentials=importlib.import_module("app.credentials")
    except ModuleNotFoundError as exc:
        if exc.name in {"app.credentials","app"}:
            value=config.get("credential_generation")
            return str(value) if value is not None else None
        raise
    reader=getattr(credentials,"credential_generation",None)
    if callable(reader):
        return str(reader(settings.runtime_dir,"model"))
    value=config.get("credential_generation")
    return str(value) if value is not None else None

def _disclosure(settings:Settings,gateway:Any,tools:Any,practice_id:str|None,expected_revision:int|None,limits_override:dict[str,Any]|None=None,plan_id:str|None=None,expected_plan_version:int|None=None,store:AgentStore|None=None,parent_task_id:str|None=None)->tuple[dict[str,Any],dict[str,Any]]:
    try:
        public=_public_config(gateway,settings); config_version,config=_config_payload(public)
        credential_generation=_credential_generation(settings,config)
        context=tools.capture_context(settings,practice_id,expected_revision)
        tools.validate_context(settings,context)
        if not isinstance(context,dict) or context.get("scope") not in {"setup","simulation"}:
            raise ValueError("context_invalid")
        if context.get("practice_id")!=practice_id or (context.get("scope")=="setup")!=(practice_id is None):
            raise ValueError("context_invalid")
        if not isinstance(context.get("context_fingerprint"),str) or not context["context_fingerprint"]:
            raise ValueError("context_invalid")
        _bind_plan(settings,context,plan_id,expected_plan_version,practice_id,store,parent_task_id)
    except HTTPException:raise
    except Exception as exc: raise _failure(exc,"context_unavailable",409) from None
    limits={"rounds":8,"tool_calls":20,"timeout_seconds":120,"tokens":4_096}
    hard_limits={"rounds":8,"tool_calls":20,"timeout_seconds":120,"tokens":8_192}
    configured=config.get("limits")
    if isinstance(configured,dict):
        for k in limits:
            if k not in configured:continue
            value=configured[k]
            if not isinstance(value,(int,float)) or isinstance(value,bool) or value<=0 or (k!="timeout_seconds" and int(value)!=value):
                raise HTTPException(503,detail={"ok":False,"reason":"config_limits_invalid","detail":"Configured runtime limits are invalid."})
            limits[k]=min(hard_limits[k],value)
    if limits_override:
        for k,v in limits_override.items():
            if k not in limits or not isinstance(v,(int,float)) or isinstance(v,bool):
                raise HTTPException(422,detail={"ok":False,"reason":"limits_invalid","detail":"Only bounded runtime limits are accepted."})
            if v<=0 or (k!="timeout_seconds" and int(v)!=v):
                raise HTTPException(422,detail={"ok":False,"reason":"limits_invalid","detail":"Runtime limits must be positive and count limits must be integers."})
            limits[k]=min(limits[k],v)
    target={"base_url":config.get("base_url"),"model":config.get("model"),"name":config.get("name")}
    categories=["user_goal","current_simulation_context","tool_evidence","generated_drafts"]
    binding={"config_version":config_version,"target":target,"context_fingerprint":context.get("context_fingerprint"),
             "practice_id":context.get("practice_id"),"revision":context.get("revision"),"revealed_date":context.get("revealed_date"),
             "data_version_id":context.get("data_version_id"),"plan_id":context.get("plan_id"),"plan_version":context.get("plan_version"),"credential_generation":credential_generation,
             "data_categories":categories,"limits":limits}
    consent_digest=hashlib.sha256(canonical(binding).encode("utf-8")).hexdigest()
    disclosure={"config_version":config_version,"target":target,"context":{k:context.get(k) for k in
        ("scope","practice_id","revision","revealed_date","data_version_id","source_code","context_fingerprint","limitations","plan_id","plan_version","model_context")},
        "data_categories":categories,"credential_generation":credential_generation,
        "purpose":"Research assistance for the bound historical simulation; evidence and drafts only.",
        "possible_cost":True,"limits":limits,"consent_digest":consent_digest}
    return disclosure,context

def _lower_limits(base:dict[str,Any],override:dict[str,Any]|None)->dict[str,Any]:
    result=dict(base)
    if override is None:return result
    for key,value in override.items():
        if key not in result or not isinstance(value,(int,float)) or isinstance(value,bool) or value<=0:
            raise HTTPException(422,detail={"ok":False,"reason":"limits_invalid","detail":"Only positive bounded runtime limits are accepted."})
        if key!="timeout_seconds" and int(value)!=value:
            raise HTTPException(422,detail={"ok":False,"reason":"limits_invalid","detail":"Count limits must be integers."})
        result[key]=min(result[key],value)
    return result

def _refresh_stale(row:dict[str,Any],settings:Settings,store:AgentStore,tools:Any=None)->dict[str,Any]:
    if row["practice_id"] is None or row["stale"]:return row
    tl=tools
    if tl is None:
        try:tl=_service("app.agent_tools")
        except HTTPException:return row
    try:
        bound=row["context"]
        current=tl.capture_context(settings,row["practice_id"],bound.get("revision"))
        if current.get("context_fingerprint")!=bound.get("context_fingerprint"):
            store.mark_stale(row["task_id"])
            row["stale"]=True
    except Exception as exc:
        reason=getattr(exc,"reason",None)
        if reason in {"context_stale","practice_not_found","revision_conflict","context_invalid"}:
            store.mark_stale(row["task_id"])
            row["stale"]=True
    return row


def create_agent_router(settings:Settings,store:AgentStore|None=None,manager:AgentTaskManager|None=None,
                        gateway:Any=None,tools:Any=None)->APIRouter:
    router=APIRouter(prefix="/api/agent",tags=["agent"])
    agent_store=store or AgentStore(settings.runtime_dir/"agent"/"tasks.sqlite3")
    task_manager=manager or AgentTaskManager(settings,agent_store,gateway,tools)

    @router.get("/config")
    def get_config():
        gw=gateway or _service("app.agent_gateway")
        try:return _public_config(gw,settings)
        except HTTPException:raise
        except Exception as exc:raise _failure(exc,"agent_config_unavailable",503) from None

    @router.put("/config")
    def put_config(body:ConfigBody):
        gw=gateway or _service("app.agent_gateway")
        try:return gw.save_config(settings,body.model_dump(exclude_none=True))
        except Exception as exc:raise _failure(exc,"agent_config_invalid") from None

    @router.post("/config/test")
    async def test_config(body:TestBody):
        if body.consent is not True or body.test_tools is not True:
            raise HTTPException(400,detail={"ok":False,"reason":"consent_required","detail":"Explicit consent to the tool capability test is required."})
        gw=gateway or _service("app.agent_gateway")
        try:return await gw.test_config(settings,body.model_dump())
        except Exception as exc:raise _failure(exc,"agent_test_failed",502) from None

    @router.get("/disclosure")
    def get_disclosure(practice_id:str|None=None,expected_revision:int|None=None,plan_id:str|None=None,expected_plan_version:int|None=None):
        gw=gateway or _service("app.agent_gateway"); tl=tools or _service("app.agent_tools")
        return _disclosure(settings,gw,tl,practice_id,expected_revision,plan_id=plan_id,expected_plan_version=expected_plan_version,store=agent_store)[0]

    @router.post("/tasks",status_code=202)
    async def create_task(body:TaskBody):
        if body.consent is not True:raise HTTPException(400,detail={"ok":False,"reason":"consent_required","detail":"Explicit consent is required."})
        try:
            existing=agent_store.existing(body.idempotency_key,body.model_dump())
        except IdempotencyConflict:
            raise HTTPException(409,detail={"ok":False,"reason":"idempotency_conflict","detail":"Idempotency key was already used with a different payload."}) from None
        if existing is not None:
            return {k:v for k,v in existing.items() if k not in ("consent_digest","limits","attempt")}
        gw=gateway or _service("app.agent_gateway"); tl=tools or _service("app.agent_tools")
        disclosure,context=await asyncio.to_thread(_disclosure,settings,gw,tl,body.practice_id,body.expected_revision,None,body.plan_id,body.expected_plan_version,agent_store,body.parent_task_id)
        effective_limits=_lower_limits(disclosure["limits"],body.limits)
        current_config,config=_config_payload(await asyncio.to_thread(_public_config,gw,settings))
        if body.config_version!=current_config or body.consent_digest!=disclosure["consent_digest"]:
            raise HTTPException(409,detail={"ok":False,"reason":"consent_stale","detail":"Configuration or simulation context changed; review disclosure again."})
        if not body.idempotency_key.strip():raise HTTPException(422,detail={"ok":False,"reason":"idempotency_key_invalid","detail":"A non-empty idempotency key is required."})
        if body.parent_task_id:
            parent=agent_store.get(body.parent_task_id)
            if not parent or parent["practice_id"]!=body.practice_id or parent["context"].get("context_fingerprint")!=context.get("context_fingerprint"):
                raise HTTPException(409,detail={"ok":False,"reason":"parent_context_invalid","detail":"Parent task must exist and have the same bound simulation context."})
            parent_plan_id=parent["context"].get("plan_id")
            parent_plan_version=parent["context"].get("plan_version")
            if (parent_plan_id!=body.plan_id
                    or (body.plan_id is not None and (parent_plan_version is None or parent_plan_version>body.expected_plan_version))):
                raise HTTPException(409,detail={"ok":False,"reason":"parent_plan_invalid","detail":"Parent task must belong to this plan and no newer plan version."})
        try:
            view,created=agent_store.create(key=body.idempotency_key,payload=body.model_dump(),practice_id=body.practice_id,
                scope=str(context.get("scope")),goal=body.goal,config_version=body.config_version,context=context,
                consent_digest=body.consent_digest,limits=effective_limits,parent_task_id=body.parent_task_id,
                credential_generation=disclosure.get("credential_generation"),plan_id=body.plan_id)
        except IdempotencyConflict:
            raise HTTPException(409,detail={"ok":False,"reason":"idempotency_conflict","detail":"Idempotency key was already used with a different payload."}) from None
        except ValueError as exc:
            raise HTTPException(413,detail={"ok":False,"reason":str(exc),"detail":"Task request exceeds the allowed size."}) from None
        if created: await task_manager.launch(view["task_id"],context)
        return {k:v for k,v in view.items() if k not in ("consent_digest","limits","attempt")}

    @router.get("/tasks")
    def list_tasks(practice_id:str|None=None,plan_id:str|None=None,scope:Literal["setup","simulation"]|None=None,
                   limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0,le=1_000_000)):
        rows,total=agent_store.list(practice_id,limit,offset,plan_id=plan_id,scope=scope)
        rows=[_refresh_stale(row,settings,agent_store,tools) for row in rows]
        return {"tasks":[{k:v for k,v in row.items() if k not in ("consent_digest","limits","attempt")} for row in rows],"total":total}

    @router.get("/tasks/{task_id}")
    def get_task(task_id:str):
        row=agent_store.get(task_id)
        if not row:raise HTTPException(404,detail={"ok":False,"reason":"task_not_found","detail":"Task not found."})
        row=_refresh_stale(row,settings,agent_store,tools)
        return {k:v for k,v in row.items() if k not in ("consent_digest","limits","attempt")}

    @router.get("/tasks/{task_id}/events")
    def task_events(task_id:str,after_seq:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=200)):
        if not agent_store.get(task_id):raise HTTPException(404,detail={"ok":False,"reason":"task_not_found","detail":"Task not found."})
        events,next_seq=agent_store.events(task_id,after_seq,limit)
        return {"events":events,"next_seq":next_seq}

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id:str,body:CancelBody):
        current=agent_store.get(task_id)
        if current is None:raise HTTPException(404,detail={"ok":False,"reason":"task_not_found","detail":"Task not found."})
        expected=body.expected_generation or current["generation"]
        if current["generation"]!=expected:raise HTTPException(409,detail={"ok":False,"reason":"generation_conflict","detail":"Operation changed; refresh its status."})
        row=await task_manager.cancel(task_id,expected)
        if not row:raise HTTPException(404,detail={"ok":False,"reason":"task_not_found","detail":"Task not found."})
        return {k:v for k,v in row.items() if k not in ("consent_digest","limits","attempt")}

    @router.get("/operations/{operation_id}")
    def inspect_operation(operation_id:str):
        row=agent_store.get(operation_id)
        if not row:raise HTTPException(404,detail={"ok":False,"reason":"operation_not_found","detail":"Operation not found."})
        row=_refresh_stale(row,settings,agent_store,tools)
        return operation_view(row)

    @router.post("/operations/{operation_id}/cancel")
    async def cancel_operation(operation_id:str,body:CancelBody):
        row=agent_store.get(operation_id)
        if not row:raise HTTPException(404,detail={"ok":False,"reason":"operation_not_found","detail":"Operation not found."})
        if body.expected_generation is None or row["generation"]!=body.expected_generation:
            raise HTTPException(409,detail={"ok":False,"reason":"generation_conflict","detail":"Submit the current operation generation."})
        cancelled=await task_manager.cancel(operation_id,body.expected_generation)
        if not cancelled:raise HTTPException(409,detail={"ok":False,"reason":"generation_conflict","detail":"Operation changed; refresh its status."})
        return operation_view(cancelled)

    @router.post("/operations/{operation_id}/resume",status_code=202)
    async def resume_operation(operation_id:str,body:ResumeBody):
        if body.consent is not True:raise HTTPException(400,detail={"ok":False,"reason":"consent_required","detail":"Explicit renewed consent is required."})
        row=agent_store.get(operation_id)
        if not row:raise HTTPException(404,detail={"ok":False,"reason":"operation_not_found","detail":"Operation not found."})
        resume_payload=body.model_dump()
        try:
            duplicate=agent_store.resume_existing(operation_id,idempotency_key=body.idempotency_key,payload=resume_payload)
        except IdempotencyConflict:
            raise HTTPException(409,detail={"ok":False,"reason":"idempotency_conflict","detail":"Resume key was already used with a different request."}) from None
        if duplicate is not None:
            return operation_view(duplicate)
        if row["generation"]!=body.expected_generation or row["status"]!="interrupted":
            raise HTTPException(409,detail={"ok":False,"reason":"generation_conflict","detail":"Operation changed; refresh its status."})
        gw=gateway or _service("app.agent_gateway"); tl=tools or _service("app.agent_tools")
        disclosure,context=await asyncio.to_thread(_disclosure,settings,gw,tl,row["practice_id"],row["context"].get("revision"),row["limits"],row["context"].get("plan_id"),row["context"].get("plan_version"),agent_store,row.get("parent_task_id"))
        if (body.config_version!=row["config_version"] or disclosure["config_version"]!=body.config_version
                or body.consent_digest!=disclosure["consent_digest"]
                or (str(disclosure.get("credential_generation")) if disclosure.get("credential_generation") is not None else None)!=row.get("credential_generation")
                or context.get("context_fingerprint")!=row["context"].get("context_fingerprint")):
            agent_store.mark_stale(operation_id)
            raise HTTPException(409,detail={"ok":False,"reason":"resume_binding_stale","detail":"Context, provider configuration, or renewed disclosure changed."})
        try:
            claimed=agent_store.claim_resume(operation_id,body.expected_generation,idempotency_key=body.idempotency_key,payload=resume_payload)
        except IdempotencyConflict:
            raise HTTPException(409,detail={"ok":False,"reason":"idempotency_conflict","detail":"Resume key was already used with a different request."}) from None
        if claimed is None:
            raise HTTPException(409,detail={"ok":False,"reason":"resume_unsafe","detail":"This checkpoint cannot be resumed without risking duplicate work or resetting budgets."})
        claimed,created=claimed
        # The new generation is fenced into every subsequent persistence call.
        if created:await task_manager.launch(operation_id,context)
        return operation_view(claimed)

    @router.delete("/tasks/{task_id}")
    def delete_task(task_id:str):
        try:deleted=agent_store.delete(task_id)
        except ActiveTaskError:raise HTTPException(409,detail={"ok":False,"reason":"task_active","detail":"Cancel or wait for the task to finish before deleting it."}) from None
        if not deleted:raise HTTPException(404,detail={"ok":False,"reason":"task_not_found","detail":"Task not found."})
        return {"ok":True}

    router.state_manager=task_manager
    router.state_store=agent_store
    return router

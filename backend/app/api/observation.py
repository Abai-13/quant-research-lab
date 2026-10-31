from __future__ import annotations
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt
from typing import Any
from app.practice.runtime import practice_connect
from app.practice.observation import ensure_schema, create, mutate, get_plan, list_plans

class ObservationCreate(BaseModel):
    model_config=ConfigDict(extra="forbid")
    practice_id:str
    plan_id:str|None=None
    expected_revision:StrictInt
    idempotency_key:str
    consent:StrictBool
    symbols:list[str]
    steps:StrictInt
    max_order_value_cents:StrictInt
    max_position_shares:StrictInt
    deadline:str
    algorithm_id:str
    algorithm_version:str
    parameters:dict[str,Any]={}
    permissions:dict[str,StrictBool]

class Mutation(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_version:StrictInt
    idempotency_key:str
    action:str|None=None
    steps:StrictInt|None=None
    max_order_value_cents:StrictInt|None=None
    max_position_shares:StrictInt|None=None
    deadline:str|None=None
    permissions:dict[str,StrictBool]|None=None

class Start(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_version:StrictInt
    idempotency_key:str
    consent:StrictBool

def create_observation_router():
    router=APIRouter(prefix="/api/practice",tags=["observation"])
    def connect(request):
        conn=practice_connect(request.app.state.settings); ensure_schema(conn); conn.commit(); return conn
    @router.post("/observe-plans")
    def post_plan(body:ObservationCreate,request:Request):
        conn=connect(request)
        try:
            conn.execute("BEGIN IMMEDIATE"); result=create(conn,body.model_dump()); conn.commit(); return result
        except Exception: conn.rollback(); raise
        finally: conn.close()
    @router.get("/observe-plans")
    def plans(request:Request,practice_id:str=Query(...)):
        conn=connect(request)
        try:return {"plans":list_plans(conn,practice_id)}
        finally:conn.close()
    @router.get("/observe-plans/{execution_id}")
    def plan(execution_id:str,request:Request):
        conn=connect(request)
        try:return get_plan(conn,execution_id)
        finally:conn.close()
    def change(execution_id,body,request,action):
        conn=connect(request)
        try:
            conn.execute("BEGIN IMMEDIATE"); result=mutate(conn,execution_id,body.model_dump(exclude_none=True),action); conn.commit(); return result
        except Exception: conn.rollback(); raise
        finally:conn.close()
    @router.post("/observe-plans/{execution_id}/pause")
    def pause(execution_id:str,body:Mutation,request:Request): return change(execution_id,body,request,"pause")
    @router.post("/observe-plans/{execution_id}/revoke")
    def revoke(execution_id:str,body:Mutation,request:Request): return change(execution_id,body,request,"revoke")
    @router.post("/observe-plans/{execution_id}/start")
    def start(execution_id:str,body:Start,request:Request):
        return request.app.state.observation_runner.start(execution_id,body.model_dump())
    @router.post("/observe-plans/{execution_id}/resume")
    def resume(execution_id:str,body:Start,request:Request):
        return request.app.state.observation_runner.start(execution_id,body.model_dump(),resume=True)
    @router.patch("/observe-plans/{execution_id}")
    def tighten(execution_id:str,body:Mutation,request:Request):
        if body.action not in (None,"tighten"): from fastapi import HTTPException; raise HTTPException(422,"action must be tighten")
        return change(execution_id,body,request,"tighten")
    return router

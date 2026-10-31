"""Persistent bounded authorizations; callers own the SQLite transaction."""
from __future__ import annotations
import sqlite3, uuid
from typing import Any
from .core import PracticeError, digest, parse_date, require_session, utc_now
from .storage import decode_json, encode_json

DDL = (
"CREATE TABLE IF NOT EXISTS observation_execution (execution_id TEXT PRIMARY KEY, practice_id TEXT NOT NULL, plan_id TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL, completed_steps INTEGER NOT NULL DEFAULT 0, symbols_json TEXT NOT NULL, limits_json TEXT NOT NULL, permissions_json TEXT NOT NULL, algorithm_json TEXT NOT NULL, frozen_json TEXT NOT NULL, deadline TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, run_token TEXT)",
"CREATE INDEX IF NOT EXISTS observation_execution_practice ON observation_execution(practice_id,created_at)",
"CREATE TABLE IF NOT EXISTS observation_event (event_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, version INTEGER NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
"CREATE TABLE IF NOT EXISTS observation_idempotency (operation TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL, PRIMARY KEY(operation,idempotency_key))",
)
def ensure_schema(conn):
    for sql in DDL: conn.execute(sql)
    columns={r[1] for r in conn.execute("PRAGMA table_info(observation_execution)")}
    if "run_token" not in columns: conn.execute("ALTER TABLE observation_execution ADD COLUMN run_token TEXT")
    if "stop_reason" not in columns: conn.execute("ALTER TABLE observation_execution ADD COLUMN stop_reason TEXT")

def begin_run(conn, execution_id: str, body: dict[str, Any], *, resume=False) -> dict[str, Any]:
    """Start/resume an authorized plan inside the caller's BEGIN IMMEDIATE transaction."""
    op=f"{'resume' if resume else 'start'}:{execution_id}"
    cached,h=_idem(conn,op,body.get("idempotency_key"),body)
    if cached: return {"execution":_execution(conn,execution_id),"run_token":None}
    if body.get("consent") is not True: raise PracticeError("consent_required","必须明确 consent=true",422)
    row=conn.execute("SELECT * FROM observation_execution WHERE execution_id=?",(execution_id,)).fetchone()
    if row is None: raise PracticeError("execution_not_found","观察授权不存在",404)
    if type(body.get("expected_version")) is not int or row["version"] != body["expected_version"]: raise PracticeError("version_conflict","授权版本已变化",409)
    if row["state"] != ("paused" if resume else "ready"): raise PracticeError("execution_not_ready","请刷新状态后明确开始或继续",409)
    validate_frozen(conn,row)
    perms=decode_json(row["permissions_json"])
    if not perms.get("observe"): raise PracticeError("observation_not_authorized","观察权限已关闭",409)
    if row["deadline"] < require_session(conn,row["practice_id"])["revealed_date"]: raise PracticeError("deadline_expired","授权已过期",409)
    if conn.execute("SELECT 1 FROM observation_execution WHERE practice_id=? AND state='running' AND execution_id<>?",(row["practice_id"],execution_id)).fetchone(): raise PracticeError("execution_already_running","同练习已有运行计划",409)
    token=uuid.uuid4().hex; now=utc_now()
    conn.execute("UPDATE observation_execution SET state='running',version=version+1,run_token=?,stop_reason=NULL,updated_at=? WHERE execution_id=?",(token,now,execution_id))
    _event(conn,execution_id,row["version"]+1,"resumed" if resume else "started",{})
    result=_execution(conn,execution_id); _save(conn,op,body["idempotency_key"],h,result)
    return {"execution":result,"run_token":token}

def _execution(conn,eid):
    row=conn.execute("SELECT * FROM observation_execution WHERE execution_id=?",(eid,)).fetchone()
    if row is None: raise PracticeError("execution_not_found","观察授权不存在",404)
    d=dict(row)
    for col,key in (("symbols_json","symbols"),("limits_json","limits"),("permissions_json","permissions"),("algorithm_json","algorithm"),("frozen_json","frozen")):
        d[key]=decode_json(d.pop(col))
    d["events"]=[{**dict(e),"payload":decode_json(e["payload_json"])} for e in conn.execute("SELECT * FROM observation_event WHERE execution_id=? ORDER BY rowid",(eid,))]
    d.pop("run_token",None)
    d["limits"]["deadline"]=d["deadline"]
    d["algorithm"]["version"]=d["algorithm"]["algorithm_version"]
    return d
def get_plan(conn,eid): return _execution(conn,eid)
def list_plans(conn,pid): return [_execution(conn,r[0]) for r in conn.execute("SELECT execution_id FROM observation_execution WHERE practice_id=? ORDER BY created_at DESC",(pid,))]
def _idem(conn,op,key,request):
    if not isinstance(key,str) or not 1<=len(key.strip())<=120: raise PracticeError("idempotency_key_required","idempotency_key 必须为1..120字符")
    h=digest(request); r=conn.execute("SELECT request_sha256,response_json FROM observation_idempotency WHERE operation=? AND idempotency_key=?",(op,key)).fetchone()
    if r:
        if r[0]!=h: raise PracticeError("idempotency_conflict","idempotency_key请求内容冲突",409)
        return decode_json(r[1]),h
    return None,h
def _save(conn,op,key,h,response): conn.execute("INSERT INTO observation_idempotency VALUES(?,?,?,?)",(op,key,h,encode_json(response)))
def _event(conn,eid,v,kind,payload): conn.execute("INSERT INTO observation_event VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,eid,v,kind,encode_json(payload),utc_now()))

def bundle_hashes(conn, settings):
    from app.portfolio.rule_bundles import load_rule_bundle
    result = {}
    for key in ("parameter_bundle_id", "simulation_rule_bundle_id", "rule_bundle_id", "fee_bundle_id"):
        if settings.get(key):
            bundle = load_rule_bundle(conn, settings[key])
            if bundle is None: raise PracticeError("frozen_bundle_missing", "冻结规则不存在", 409)
            result[key] = digest(bundle)
    return result

def validate_frozen(conn, row):
    session = require_session(conn, row["practice_id"])
    frozen = decode_json(row["frozen_json"])
    if session["status"] != "active" or session["revision"] != frozen["practice_revision"]:
        raise PracticeError("practice_revision_changed", "练习已被修改，请重新检查并创建授权", 409)
    if (session["data_version_id"] != frozen["data_version_id"] or
            session["source_code"] != frozen["source_code"] or session["settings"] != frozen["settings"]):
        raise PracticeError("frozen_data_changed", "冻结数据或设置已变化", 409)
    current = bundle_hashes(conn, session["settings"])
    if current != frozen.get("bundle_hashes"):
        raise PracticeError("frozen_bundles_changed", "冻结规则或费用已变化，请重新授权", 409)
    from .algorithms import resolve_algorithm
    algorithm = decode_json(row["algorithm_json"])
    resolved = resolve_algorithm(algorithm["algorithm_id"], algorithm["algorithm_version"], algorithm["parameters"])
    if resolved != algorithm["configuration"]:
        raise PracticeError("frozen_algorithm_changed", "冻结算法配置已变化", 409)
    return session

def stop(conn, row, state, reason):
    conn.execute("UPDATE observation_execution SET state=?,run_token=NULL,stop_reason=?,updated_at=? WHERE execution_id=?",
                 (state, reason, utc_now(), row["execution_id"]))
    _event(conn, row["execution_id"], row["version"], state, {"reason": reason})

def create(conn,body):
    op="create"; cached,h=_idem(conn,op,body.get("idempotency_key"),body)
    if cached:return cached
    if body.get("consent") is not True: raise PracticeError("consent_required","必须明确 consent=true")
    s=require_session(conn,body.get("practice_id"))
    if type(body.get("expected_revision")) is not int or body["expected_revision"]!=s["revision"]: raise PracticeError("revision_conflict","练习版本已变化",409)
    if s["status"]!="active": raise PracticeError("practice_unavailable","练习已结束",409)
    from app.data.chunks import VersionView
    view=VersionView(conn,s["data_version_id"],expected_source_code=s["source_code"])
    if view.chunked:
        rows=conn.execute("SELECT DISTINCT m.ts_code FROM data_version_chunk vc JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id WHERE vc.version_id=? AND vc.category='security'",(s["data_version_id"],)).fetchall()
    else:
        rows=conn.execute("SELECT DISTINCT ts_code FROM security_info WHERE source_code=? AND id IN (SELECT record_id FROM data_version_member WHERE version_id=? AND category='security')",(s["source_code"],s["data_version_id"])).fetchall()
    allowed={str(r[0]) for r in rows}; symbols=body.get("symbols")
    if not isinstance(symbols,list) or not symbols or any(not isinstance(x,str) for x in symbols) or len(set(symbols))!=len(symbols) or not set(symbols)<=allowed: raise PracticeError("symbols_invalid","证券必须是练习数据版本证券的非空子集",422)
    steps=body.get("steps")
    if type(steps) is not int or not 1<=steps<=20: raise PracticeError("steps_invalid","steps必须为1..20整数",422)
    limits={}
    for k in ("max_order_value_cents","max_position_shares"):
        v=body.get(k)
        if type(v) is not int or v<=0: raise PracticeError("limit_invalid",f"{k}必须为正整数",422)
        limits[k]=v
    deadline=parse_date(body.get("deadline"),"deadline")
    if not s["revealed_date"]<=deadline<=s["end_date"]: raise PracticeError("deadline_invalid","deadline必须在当前可见日与练习结束日之间",422)
    perms=body.get("permissions")
    if not isinstance(perms,dict) or set(perms)!={"observe","orders","advance"} or any(type(perms.get(k)) is not bool for k in ("observe","orders","advance")): raise PracticeError("permissions_invalid","三项权限必须为布尔值",422)
    from app.practice.algorithms import resolve_algorithm
    try: algorithm=resolve_algorithm(body.get("algorithm_id"),body.get("algorithm_version"),body.get("parameters",{}))
    except Exception as exc: raise PracticeError("algorithm_invalid",str(exc),422) from exc
    settings=s["settings"]
    frozen={"data_version_id":s["data_version_id"],"source_code":s["source_code"],"revealed_date":s["revealed_date"],"practice_revision":s["revision"],"settings":settings,"rule_bundle_id":settings.get("rule_bundle_id"),"fee_bundle_id":settings.get("fee_bundle_id"),"bundle_hashes":bundle_hashes(conn,settings)}
    eid=uuid.uuid4().hex; now=utc_now(); plan=body.get("plan_id") or uuid.uuid4().hex
    conn.execute("INSERT INTO observation_execution(execution_id,practice_id,plan_id,state,version,completed_steps,symbols_json,limits_json,permissions_json,algorithm_json,frozen_json,deadline,created_at,updated_at,run_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",(eid,s["practice_id"],plan,"ready",1,0,encode_json(symbols),encode_json({**limits,"steps":steps}),encode_json(perms),encode_json({"algorithm_id":body["algorithm_id"],"algorithm_version":body["algorithm_version"],"parameters":body.get("parameters",{}),"configuration":algorithm}),encode_json(frozen),deadline,now,now))
    _event(conn,eid,1,"created",{"state":"ready"}); result=_execution(conn,eid); _save(conn,op,body["idempotency_key"],h,result); return result

def mutate(conn,eid,body,action):
    op=f"{action}:{eid}"; cached,h=_idem(conn,op,body.get("idempotency_key"),body)
    if cached:return cached
    row=conn.execute("SELECT * FROM observation_execution WHERE execution_id=?",(eid,)).fetchone()
    if row is None: raise PracticeError("execution_not_found","观察授权不存在",404)
    if type(body.get("expected_version")) is not int or body["expected_version"]!=row["version"]: raise PracticeError("version_conflict","授权版本已变化",409)
    limits=decode_json(row["limits_json"]); perms=decode_json(row["permissions_json"]); state=row["state"]; changed=False
    if action=="pause":
        if state in ("ready","running"): state="paused"; changed=True
    elif action=="revoke":
        if state!="revoked": state="revoked"; changed=True
    else:
        for k in ("steps","max_order_value_cents","max_position_shares"):
            if k in body:
                val=body[k]
                if type(val) is not int or val<=0 or val>limits[k]: raise PracticeError("restriction_expansion","限制只能收紧",409)
                if k=="steps" and val<row["completed_steps"]: raise PracticeError("steps_below_completed","steps不能低于已完成步数",409)
                if val!=limits[k]: limits[k]=val; changed=True
        if "deadline" in body:
            val=parse_date(body["deadline"],"deadline")
            if val>row["deadline"]: raise PracticeError("restriction_expansion","deadline只能提前",409)
            if val<row["deadline"]: conn.execute("UPDATE observation_execution SET deadline=? WHERE execution_id=?",(val,eid)); changed=True
        if "permissions" in body:
            np=body["permissions"]
            if not isinstance(np,dict) or not set(np)<=set(perms) or any(type(np.get(k,perms[k])) is not bool for k in perms): raise PracticeError("permissions_invalid","权限必须为布尔值",422)
            for k in perms:
                if np.get(k,perms[k]) and not perms[k]: raise PracticeError("restriction_expansion","权限只能关闭",409)
                if np.get(k,perms[k])!=perms[k]: perms[k]=np[k]; changed=True
    v=row["version"]+(1 if changed else 0); now=utc_now()
    if changed:
        cur=conn.execute("UPDATE observation_execution SET state=?,version=?,limits_json=?,permissions_json=?,updated_at=? WHERE execution_id=? AND version=?",(state,v,encode_json(limits),encode_json(perms),now,eid,row["version"]))
        if cur.rowcount!=1: raise PracticeError("version_conflict","授权版本已变化",409)
        if state in ("paused","revoked"):
            conn.execute("UPDATE observation_execution SET run_token=NULL,stop_reason=? WHERE execution_id=?",(action,eid))
        _event(conn,eid,v,action,{})
    result=_execution(conn,eid); _save(conn,op,body["idempotency_key"],h,result); return result

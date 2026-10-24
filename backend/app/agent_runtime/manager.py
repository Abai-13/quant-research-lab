"""Small lifecycle manager: bounded background tasks and cancellation fences."""
from __future__ import annotations

import asyncio
import importlib
from typing import Any

from .engine import AgentEngine, RuntimeErrorCode
from .store import AgentStore


def _service(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        raise RuntimeErrorCode("agent_service_unavailable", "Agent gateway or tools service is unavailable.") from None


class AgentTaskManager:
    def __init__(self, settings: Any, store: AgentStore, gateway: Any = None, tools: Any = None, max_active: int = 2):
        self.settings, self.store = settings, store
        self.gateway, self.tools = gateway, tools
        self.max_active = max(1,min(max_active,4))
        self._running: dict[str, tuple[asyncio.Task[Any], asyncio.Event]] = {}
        self._lock = asyncio.Lock()
        self._blocking_semaphore = asyncio.Semaphore(4)

    async def launch(self, task_id: str, context: dict[str, Any]) -> None:
        async with self._lock:
            if len(self._running) >= self.max_active:
                self.store.transition(task_id,("queued",),"failed",reason="agent_busy")
                self.store.append_event(task_id,"task_failed","Agent runtime is at its active task limit.",reason="agent_busy")
                return
            event=asyncio.Event()
            runner=asyncio.create_task(self._execute(task_id,context,event),name=f"agent:{task_id}")
            self._running[task_id]=(runner,event)
            runner.add_done_callback(lambda _done, tid=task_id: self._running.pop(tid,None))

    async def _execute(self, task_id: str, context: dict[str, Any], cancel_event: asyncio.Event) -> None:
        if not self.store.start(task_id): return
        self.store.append_event(task_id,"task_started","Task execution started.")
        task=self.store.get(task_id)
        if not task:return
        try:
            gateway=self.gateway if self.gateway is not None else _service("app.agent_gateway")
            tools=self.tools if self.tools is not None else _service("app.agent_tools")
            async with self._blocking_semaphore:
                await asyncio.to_thread(tools.validate_context,self.settings,context)
            engine=AgentEngine(settings=self.settings,store=self.store,gateway=gateway,tools=tools,blocking_semaphore=self._blocking_semaphore)
            result,_,usage=await engine.run(task,context=context,cancel_event=cancel_event)
            if cancel_event.is_set(): return
            if self.store.transition(task_id,("running",),"completed",result=result,usage=usage):
                self.store.append_event(task_id,"task_completed","Task completed with evidence and draft references.",
                                        evidence_ids=[str(e.get("evidence_id")) for e in result["evidence"] if isinstance(e,dict) and e.get("evidence_id")][:100])
        except asyncio.CancelledError:
            # Cancellation endpoint atomically writes cancelled first; shutdown leaves running for restart recovery.
            return
        except RuntimeErrorCode as exc:
            reason=exc.reason[:80]
            if self.store.transition(task_id,("running",),"failed",reason=reason):
                self.store.append_event(task_id,"task_failed","Task stopped with a bounded runtime error.",reason=reason)
        except Exception as exc:
            candidate=getattr(exc,"reason",None)
            reason=candidate if isinstance(candidate,str) and candidate in {"context_stale","practice_not_found","revision_conflict","context_invalid"} else "agent_internal_error"
            if reason=="context_stale":self.store.mark_stale(task_id)
            if self.store.transition(task_id,("running",),"failed",reason=reason):
                self.store.append_event(task_id,"task_failed","Task stopped because its binding changed or runtime service failed.",reason=reason)

    async def cancel(self, task_id: str) -> dict[str, Any] | None:
        view=self.store.cancel(task_id)
        if view and view["status"]=="cancelled":
            pair=self._running.get(task_id)
            if pair:
                runner,event=pair; event.set(); runner.cancel()
        return view

    async def shutdown(self) -> None:
        tasks=list(self._running.values())
        for runner,event in tasks:
            event.set(); runner.cancel()
        if tasks:
            await asyncio.gather(*(runner for runner,_ in tasks),return_exceptions=True)
        # Keep running records so the next process startup marks them interrupted.

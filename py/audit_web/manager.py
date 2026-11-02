"""Single-slot audit job manager: one active audit globally, no pending queue, bounded finished history.

All state is touched from one event loop. `submit` contains no `await`, so concurrent POSTs are serialized by the
loop: exactly one claims the slot and every other caller gets `Busy`. The slot is released only after the agent task
(and with it any Claude CLI process group, which `ClaudeJSON` kills in its own `finally`) has fully finished.
"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable, Protocol
import uuid
from . import limits
from .errors import ResultTooLarge, classify

log = logging.getLogger('audit_web')
TERMINAL = ('completed', 'failed', 'cancelled')


class Busy(Exception):
    def __init__(self, active_id: str):
        super().__init__('an audit is already running')
        self.active_id = active_id


class Closed(Exception):
    pass


class AuditRunner(Protocol):
    async def run(self, prompt: str) -> Any: ...


@dataclass(eq=False)
class Job:
    id: str
    prompt: str
    created_at: float                  # wall clock, display only
    created: float                     # monotonic
    status: str = 'pending'
    started: float | None = None
    finished: float | None = None
    cancel_requested: bool = False
    error: dict[str, str] | None = None
    result_bytes: bytes | None = None  # the ONLY retained copy of the result (serialized once)
    supervisor: asyncio.Task | None = field(default=None, repr=False)
    agent_task: asyncio.Task | None = field(default=None, repr=False)


class AuditManager:
    def __init__(self, agent: AuditRunner, project: Callable[[Any], bytes], *, job_timeout: float = limits.JOB_TIMEOUT_MAX_SECONDS,
                 max_history: int = limits.MAX_HISTORY, ttl: float = limits.HISTORY_TTL_SECONDS,
                 max_result_bytes: int = limits.MAX_RESULT_BYTES, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, shutdown_warning: float = limits.SHUTDOWN_WARNING_SECONDS):
        if not 1 <= job_timeout <= limits.JOB_TIMEOUT_MAX_SECONDS or not 1 <= max_history <= 64 or ttl <= 0 or max_result_bytes < 1024:
            raise ValueError('invalid audit manager limits')
        self.agent, self.project = agent, project
        self.job_timeout, self.max_history, self.ttl, self.max_result_bytes = job_timeout, max_history, ttl, max_result_bytes
        self.clock, self.wall, self.shutdown_warning = clock, wall, shutdown_warning
        self._active: Job | None = None
        self._finished: OrderedDict[str, Job] = OrderedDict()
        self._closed = False

    # ---- admission -------------------------------------------------------------------------------------------
    @property
    def active_id(self) -> str | None:
        return self._active.id if self._active else None

    def submit(self, prompt: str) -> Job:
        """Claim the single slot or raise. No await: atomic with respect to other requests."""
        if self._closed:
            raise Closed()
        if self._active is not None:
            raise Busy(self._active.id)
        job = Job(id=str(uuid.uuid4()), prompt=prompt, created_at=self.wall(), created=self.clock())
        self._active = job
        job.supervisor = asyncio.get_running_loop().create_task(self._supervise(job), name=f'audit-{job.id}')
        return job

    # ---- lookup ------------------------------------------------------------------------------------------------
    def _prune(self) -> None:
        now = self.clock()
        for key in [k for k, j in self._finished.items() if j.finished is not None and now - j.finished > self.ttl]:
            del self._finished[key]
        while len(self._finished) > self.max_history:
            self._finished.popitem(last=False)

    def get(self, job_id: str) -> Job | None:
        self._prune()
        if self._active is not None and self._active.id == job_id:
            return self._active
        return self._finished.get(job_id)

    def list(self) -> list[Job]:
        """Newest first: the active job (if any) then finished jobs."""
        self._prune()
        jobs = list(reversed(self._finished.values()))
        return ([self._active] if self._active else []) + jobs

    def elapsed(self, job: Job) -> float:
        if job.started is None:
            return 0.0
        return max(0.0, (job.finished if job.finished is not None else self.clock()) - job.started)

    def retained_bytes(self) -> int:
        return sum(len(j.result_bytes or b'') for j in self._finished.values())

    # ---- cancellation ------------------------------------------------------------------------------------------
    def cancel(self, job_id: str) -> Job | None:
        """Request cancellation. The job stays `running` (cancel_requested) and keeps the slot until cleanup completes."""
        job = self.get(job_id)
        if job is None or job.status in TERMINAL:
            return job
        job.cancel_requested = True
        if job.agent_task is not None and not job.agent_task.done():
            job.agent_task.cancel()
        return job

    # ---- execution ---------------------------------------------------------------------------------------------
    async def _reap(self, task: asyncio.Task) -> None:
        """Cancel and fully await the agent task. A repeated cancellation of the supervisor never abandons the cleanup."""
        task.cancel()
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()  # mark retrieved

    async def _supervise(self, job: Job) -> None:
        agent_task: asyncio.Task | None = None
        try:
            try:
                if job.cancel_requested:
                    raise asyncio.CancelledError()
                job.status, job.started = 'running', self.clock()
                agent_task = job.agent_task = asyncio.get_running_loop().create_task(self.agent.run(job.prompt), name=f'agent-{job.id}')
                done, _ = await asyncio.wait({agent_task}, timeout=self.job_timeout)
                if not done:
                    await self._reap(agent_task)
                    self._finish(job, 'failed', error=dict(code='job_timeout', message=f'The audit exceeded the {int(self.job_timeout)}-second limit and was stopped.',
                                                           hint='Ask a narrower question and try again.'))
                elif agent_task.cancelled():
                    self._finish(job, 'cancelled')
                else:
                    result = agent_task.result()
                    raw = self.project(result)
                    del result
                    if len(raw) > self.max_result_bytes:
                        raise ResultTooLarge('serialized result exceeds the configured bound')
                    self._finish(job, 'completed', result=raw)
            except asyncio.CancelledError:
                if agent_task is not None:
                    await self._reap(agent_task)
                self._finish(job, 'cancelled')
            except Exception as exc:
                if agent_task is not None and not agent_task.done():
                    await self._reap(agent_task)
                if job.cancel_requested and agent_task is not None and agent_task.cancelled():
                    self._finish(job, 'cancelled')
                else:
                    log.error('audit %s failed', job.id, exc_info=exc)
                    self._finish(job, 'failed', error=classify(exc))
        finally:
            if agent_task is not None and not agent_task.done():  # defensive: never release the slot with live work
                await self._reap(agent_task)
            if job.status not in TERMINAL:
                self._finish(job, 'failed', error=classify(RuntimeError('internal')))
            self._active = None  # slot released only here, after every cleanup above
            self._prune()

    def _finish(self, job: Job, status: str, *, error: dict[str, str] | None = None, result: bytes | None = None) -> None:
        if job.status in TERMINAL:
            return
        job.status, job.error, job.finished = status, error, self.clock()
        job.result_bytes = result if status == 'completed' else None  # failed/cancelled jobs never carry a result
        self._finished[job.id] = job
        self._finished.move_to_end(job.id)
        job.agent_task = None

    async def shutdown(self) -> None:
        """Cancel and await the active audit (agent task and model process groups) until cleanup has really finished, then drop all history."""
        self._closed = True
        job = self._active
        if job is not None:
            job.cancel_requested = True
            if job.supervisor is not None and not job.supervisor.done():
                job.supervisor.cancel()
                # The interval is a WARNING deadline only. Resources stay held (slot occupied, callers' backend/registry open)
                # until the agent task and its model process groups have actually finished; an outer supervisor may SIGKILL.
                while True:
                    done, _ = await asyncio.wait({job.supervisor}, timeout=self.shutdown_warning)
                    if done:
                        break
                    log.warning('audit %s cleanup still running %ss into shutdown; continuing to wait', job.id, self.shutdown_warning)
                if job.status not in TERMINAL:  # supervisor cancelled before its first step: its body (and finally) never ran
                    self._finish(job, 'cancelled')
                    self._active = None
        self._finished.clear()

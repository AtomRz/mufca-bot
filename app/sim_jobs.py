"""
Background simulation jobs (!tp1sim, !compsim, /api/tp1sim, /api/compsim).

Why this exists: at 6000 bars a full-scope simulation takes minutes. The web
dashboard sits behind a Cloudflare Tunnel, which drops any HTTP request that
does not answer within ~100 s - the simulation finished server-side, but the
response had nowhere to go, so the page just showed nothing. A job runs in the
background instead: the POST returns a job id at once and the page polls
GET /api/sim-jobs/{id} for progress and, finally, the result. Each poll is
short, so no proxy or browser timeout can lose the result, and the result is
kept for a while, so it survives a page reload or a tab switch.

Only one simulation runs at a time (they are CPU-heavy and each already uses
every core); a second request is refused with SimBusy instead of silently
doubling the load, which is what happened when a timed-out run was retried.

Everything here runs on the event loop thread - no locks needed.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_KEEP_FINISHED = 10          # finished jobs kept for later reads
_FINISHED_TTL_SECONDS = 6 * 3600


class SimBusy(Exception):
    """Raised by SimJobs.start() while another simulation is running."""

    def __init__(self, job: "SimJob"):
        super().__init__(f"a {job.kind} simulation is already running ({job.done}/{job.total})")
        self.job = job


class SimJob:
    def __init__(self, kind: str, params: Dict[str, Any]):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.params = params
        self.status = "running"          # running | done | error | cancelled
        self.done = 0
        self.total = 0
        self.message = "starting"
        self.result: Any = None
        self.error: Optional[str] = None
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.task: Optional["asyncio.Task"] = None

    def progress(self, done: int, total: int, message: str) -> None:
        self.done, self.total, self.message = done, total, message

    def to_dict(self, include_result: bool = True) -> Dict[str, Any]:
        end = self.finished_at or time.time()
        out: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "params": self.params,
            "status": self.status,
            "progress": {"done": self.done, "total": self.total, "message": self.message},
            "elapsed_seconds": round(end - self.started_at, 1),
            "error": self.error,
        }
        if include_result and self.status == "done":
            out["result"] = self.result
        return out

    async def wait(self) -> Any:
        """Awaits the job and returns its result (or raises its error). Shielded:
        cancelling the *waiter* (e.g. a Discord command) leaves the job running."""
        assert self.task is not None
        await asyncio.wait({self.task})
        if self.status == "done":
            return self.result
        raise RuntimeError(self.error or f"simulation {self.status}")


class SimJobs:
    def __init__(self) -> None:
        self._jobs: Dict[str, SimJob] = {}

    # ── queries ───────────────────────────────────────────────────────
    def active(self) -> Optional[SimJob]:
        return next((j for j in self._jobs.values() if j.status == "running"), None)

    def get(self, job_id: str) -> Optional[SimJob]:
        self._purge()
        return self._jobs.get(job_id)

    # ── control ───────────────────────────────────────────────────────
    def start(
        self,
        kind: str,
        params: Dict[str, Any],
        run: Callable[[Callable[[int, int, str], None]], Awaitable[Any]],
    ) -> SimJob:
        """Starts `run(progress)` in the background. Raises SimBusy if a job is
        already running. `progress(done, total, message)` feeds the job's state."""
        busy = self.active()
        if busy is not None:
            raise SimBusy(busy)
        self._purge()
        job = SimJob(kind, params)
        self._jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, run))
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status != "running" or job.task is None:
            return False
        job.task.cancel()
        return True

    # ── internals ─────────────────────────────────────────────────────
    async def _run(self, job: SimJob, run) -> None:
        try:
            job.result = await run(job.progress)
            job.status = "done"
            job.message = "finished"
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error = "cancelled"
            logger.info(f"[SIM] job {job.id} ({job.kind}) cancelled")
        except Exception as e:
            job.status = "error"
            job.error = str(e) or repr(e)
            logger.error(f"[SIM] job {job.id} ({job.kind}) failed: {e}", exc_info=True)
        finally:
            job.finished_at = time.time()

    def _purge(self) -> None:
        now = time.time()
        finished = sorted(
            (j for j in self._jobs.values() if j.status != "running"),
            key=lambda j: j.finished_at or 0,
            reverse=True,
        )
        for i, j in enumerate(finished):
            if i >= _KEEP_FINISHED or now - (j.finished_at or now) > _FINISHED_TTL_SECONDS:
                self._jobs.pop(j.id, None)


jobs = SimJobs()

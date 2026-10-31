"""
Process pool for CPU-bound simulations (!tp1sim, !compsim and their web
counterparts).

Why processes: a backtest is a long pure-Python/pandas loop. Threads share one
GIL, so asyncio.to_thread() ran every backtest on a single core and, worse,
starved the event loop (Discord heartbeats, the web API, the live scanner) for
minutes. Separate processes use every core and leave the bot responsive.

Usage:
    async with SimPool() as pool:
        futures = [pool.submit(kwargs) for kwargs in jobs]   # kwargs for backtest_history()
        results = await asyncio.gather(*futures)

Network I/O stays in the parent: callers fetch OHLCV first and pass DataFrames
in kwargs, so workers never touch the exchange (no extra rate-limit pressure,
no ccxt objects crossing a process boundary).

Workers are started lazily (one per submitted job, up to the worker limit),
live only as long as the `async with` block, and read the persisted signal
history/config from disk when they start - so every job sees current data.

Worker count: SIM_WORKERS env var, default = usable CPU cores (max 8).
SIM_WORKERS=1 keeps the previous behaviour (one backtest at a time, in a
thread of the main process). If a worker dies or can't start, the job is
retried once on a fresh worker and then run in-process, so a result is
produced either way.
"""

import asyncio
import logging
import os
import pickle
import struct
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HEADER = struct.Struct(">Q")
_WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_worker.py")
_MAX_AUTO_WORKERS = 8
_STOP_GRACE_SECONDS = 5.0


def default_workers() -> int:
    """SIM_WORKERS if set (>= 1), else the number of usable CPU cores (max 8)."""
    raw = os.getenv("SIM_WORKERS", "").strip()
    if raw:
        try:
            return max(1, min(64, int(raw)))
        except ValueError:
            logger.warning(f"[SIM] SIM_WORKERS={raw!r} is not an integer — using the automatic value")
    try:
        cores = len(os.sched_getaffinity(0))  # respects cpuset limits (docker --cpuset-cpus)
    except AttributeError:
        cores = os.cpu_count() or 1
    return max(1, min(_MAX_AUTO_WORKERS, cores))


def _run_in_process(kwargs: Dict[str, Any]):
    """Blocking: run one backtest in the current process (thread fallback)."""
    from signals import backtest_history  # lazy: signals imports this module
    return backtest_history(None, **kwargs)


class _Worker:
    """One `python sim_worker.py` subprocess, driven over its stdin/stdout."""

    def __init__(self, name: str):
        self.name = name
        self.proc: Optional[asyncio.subprocess.Process] = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", _WORKER_SCRIPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,  # inherit: the worker's log lines land in the container log
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        logger.info(f"[SIM] {self.name} started (pid {self.proc.pid})")

    async def call(self, request: Dict[str, Any]):
        assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None
        payload = pickle.dumps(request, protocol=pickle.HIGHEST_PROTOCOL)
        self.proc.stdin.write(_HEADER.pack(len(payload)) + payload)
        await self.proc.stdin.drain()
        header = await self.proc.stdout.readexactly(_HEADER.size)
        (length,) = _HEADER.unpack(header)
        status, value = pickle.loads(await self.proc.stdout.readexactly(length))
        if status != "ok":
            raise RuntimeError(f"{self.name} failed:\n{value}")
        return value

    async def stop(self, kill: bool = False) -> None:
        proc = self.proc
        self.proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            if not kill and proc.stdin is not None:
                proc.stdin.close()  # EOF -> the worker exits its loop by itself
                try:
                    await asyncio.wait_for(proc.wait(), _STOP_GRACE_SECONDS)
                    return
                except asyncio.TimeoutError:
                    pass
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning(f"[SIM] {self.name}: stop failed: {e}")


class SimPool:
    """Runs backtest_history() jobs on up to `workers` processes."""

    def __init__(self, workers: Optional[int] = None):
        self.workers = max(1, workers if workers else default_workers())
        # workers == 1 -> no subprocess at all, same as the old single-thread behaviour
        self.in_process = self.workers == 1
        self._queue: "asyncio.Queue" = asyncio.Queue()
        self._drivers: List["asyncio.Task"] = []
        self._pending: List["asyncio.Future"] = []
        self._closed = False

    async def __aenter__(self) -> "SimPool":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close(cancel=exc_type is not None)

    # ── public API ────────────────────────────────────────────────────
    def submit(self, kwargs: Dict[str, Any]) -> "asyncio.Future":
        """Queue one backtest_history() call (kwargs, without `exchange`).
        Returns a future for its (count, history, bars_fetched) tuple."""
        if self._closed:
            raise RuntimeError("SimPool is closed")
        fut: "asyncio.Future" = asyncio.get_running_loop().create_future()
        self._pending.append(fut)
        self._queue.put_nowait((kwargs, fut))
        if len(self._drivers) < self.workers:  # lazy: one worker per job until the limit
            self._drivers.append(asyncio.create_task(self._drive(len(self._drivers) + 1)))
        return fut

    async def close(self, cancel: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        if cancel:
            for task in self._drivers:
                task.cancel()
        else:
            for _ in self._drivers:
                self._queue.put_nowait(None)
        await asyncio.gather(*self._drivers, return_exceptions=True)
        # whatever never ran must not leave a future dangling
        for fut in self._pending:
            if not fut.done():
                fut.cancel()
            elif not fut.cancelled():
                fut.exception()  # mark retrieved: no "never retrieved" noise on early exits

    # ── internals ─────────────────────────────────────────────────────
    async def _drive(self, idx: int) -> None:
        worker = _Worker(f"sim-worker-{idx}")
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                kwargs, fut = item
                if fut.cancelled():
                    continue
                try:
                    result = await self._execute(worker, kwargs)
                except asyncio.CancelledError:
                    raise
                except BaseException as e:
                    if not fut.done():
                        fut.set_exception(e)
                else:
                    if not fut.done():
                        fut.set_result(result)
        finally:
            await worker.stop(kill=True)

    async def _execute(self, worker: _Worker, kwargs: Dict[str, Any]):
        if self.in_process:
            return await asyncio.to_thread(_run_in_process, kwargs)

        last_err: Optional[BaseException] = None
        for attempt in (1, 2):  # a crashed worker (e.g. OOM kill) gets one fresh start
            try:
                if not worker.alive:
                    await worker.start()
                return await worker.call({"kind": "backtest", "kwargs": kwargs})
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                last_err = e
                logger.warning(f"[SIM] {worker.name}: attempt {attempt} failed: {e!r}")
                await worker.stop(kill=True)

        logger.warning(f"[SIM] {worker.name}: running this job in-process instead ({last_err!r})")
        return await asyncio.to_thread(_run_in_process, kwargs)

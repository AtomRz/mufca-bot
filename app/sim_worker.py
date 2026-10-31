"""
Worker process for CPU-bound simulations (see sim_pool.py).

Run as a plain script: `python sim_worker.py`. It is deliberately NOT started
through multiprocessing: multiprocessing's "spawn" re-imports the parent's
__main__ module (main.py -> bot -> discord, web, ...) in every child, which is
slow and drags the whole bot into each worker. A plain subprocess imports only
what the simulation needs (signals.py and its indicator modules).

Protocol (binary frames on stdin/stdout, both directions):
    8-byte big-endian length, then that many bytes of pickle.
Request : {"kind": "backtest", "kwargs": {...}}  |  {"kind": "ping"}
Response: ("ok", result)  |  ("err", traceback_text)

Anything the imported modules print goes to stderr, never into the protocol
channel: fd 1 is moved aside before any import happens.
"""

import logging
import os
import pickle
import struct
import sys
import traceback

_HEADER = struct.Struct(">Q")


def _read_exact(stream, n: int):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None  # parent closed the pipe -> time to exit
        buf += chunk
    return bytes(buf)


def read_frame(stream):
    header = _read_exact(stream, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    payload = _read_exact(stream, length)
    if payload is None:
        return None
    return pickle.loads(payload)


def write_frame(stream, obj) -> None:
    payload = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(_HEADER.pack(len(payload)))
    stream.write(payload)
    stream.flush()


def main() -> int:
    # Keep the real stdout for the protocol, point fd 1 (and sys.stdout) at
    # stderr so a stray print() in any imported module can't corrupt the frames.
    proto_out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    proto_in = sys.stdin.buffer

    # Run below the live scanner's priority: the simulation may use every core,
    # but must never starve the bot that is trading on this machine.
    try:
        os.nice(int(os.getenv("SIM_WORKER_NICE", "10")))
    except (AttributeError, OSError, ValueError):
        pass  # not available on this platform

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    log = logging.getLogger("sim_worker")

    import signals  # noqa: E402  (heavy import, after the stdout swap on purpose)

    while True:
        try:
            request = read_frame(proto_in)
        except Exception as e:  # corrupted frame: nothing sensible left to do
            log.error(f"[SIM-WORKER] unreadable request: {e}")
            return 1
        if request is None:
            return 0  # parent closed stdin: normal shutdown

        try:
            kind = request.get("kind")
            if kind == "ping":
                response = ("ok", "pong")
            elif kind == "backtest":
                # exchange=None: every network input was fetched by the parent
                # and arrives as DataFrames in kwargs (df / benchmark_df / htf_df).
                response = ("ok", signals.backtest_history(None, **request["kwargs"]))
            else:
                response = ("err", f"unknown request kind: {kind!r}")
        except BaseException:
            response = ("err", traceback.format_exc())

        try:
            write_frame(proto_out, response)
        except BrokenPipeError:
            return 0


if __name__ == "__main__":
    sys.exit(main())

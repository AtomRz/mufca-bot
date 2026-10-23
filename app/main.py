"""
MUFCA v4.0 — Multi-Timeframe Adaptive Trading Bot
Run: python main.py

🆕 The Discord bot and the web API (web_api.py) now run in ONE process and
ONE asyncio event loop — discord.py and uvicorn coexist perfectly fine via
asyncio.gather(). This means: no duplicate fetch/recomputation of
indicators — the web UI reads the same bot.state and config that the
scanner sees.

🆕 Discord is now fully optional (config.DISCORD_ENABLED is false if
DISCORD_TOKEN isn't set, or DISCORD_ENABLED=false is explicitly set in
.env). Without Discord, only the scanner and the web dashboard start: no
gateway connection comes up, !commands are unavailable, but signals/TP1/
statistics keep working as usual via the web/WS/Android push. Before, the
scanner's startup was hard-tied to Discord's on_ready — without it, nothing
worked at all (see bot.ensure_engine_started()).

🆕 Graceful shutdown: on SIGTERM (`docker stop`) or SIGINT (Ctrl+C), the bot
flushes signals_history.json AND a snapshot of active positions
(bot_state_snapshot.json) to disk before exiting — before, on any restart
(including a routine deploy of a new version), bot.state was rebuilt from
scratch via make_state(), and every active, not-yet-closed position simply
vanished from Status/the chart (see state.save_bot_state).
"""

import asyncio
import logging
import os
import signal

from bot import bot, ensure_engine_started
from config import DISCORD_TOKEN, DISCORD_ENABLED

logger = logging.getLogger(__name__)

WEB_API_PORT = int(os.getenv("WEB_API_PORT", "8585"))
WEB_API_HOST = os.getenv("WEB_API_HOST", "0.0.0.0")

# 🆕 kept so shutdown can ask uvicorn to exit gracefully (should_exit) instead
# of cancelling serve() mid-request, which dropped open WebSocket/HTTP clients
# without a proper close.
_web_server = None


async def _run_web_api():
    """Brings up FastAPI (web_api.py) via uvicorn in the current event loop."""
    import uvicorn
    from web_api import app as fastapi_app  # imported here: web_api does `import bot as core`,
                                             # bot must already be fully loaded by this point

    global _web_server
    config = uvicorn.Config(
        fastapi_app,
        host=WEB_API_HOST,
        port=WEB_API_PORT,
        log_level="info",
        loop="none",  # already inside a running loop — don't let uvicorn create its own
    )
    server = uvicorn.Server(config)
    _web_server = server
    logger.info(f"[WEB] Starting web API on {WEB_API_HOST}:{WEB_API_PORT}")
    await server.serve()


def _flush_state_to_disk():
    """Forces a save of everything that's accumulated in memory but hasn't
    made it to disk yet.

    🆕 FIX: all steps used to share one try/except, so if the first one failed
    (e.g. signals_history.json), the position snapshot — the one that actually
    keeps open trades alive across a restart — was never written. Each step is
    now independent."""
    def _step(label, fn):
        try:
            fn()
            logger.info(f"[SHUTDOWN] {label} flushed to disk")
        except Exception as e:
            logger.error(f"[SHUTDOWN] Failed to flush {label}: {e}", exc_info=True)

    import bot as _bot_module
    from state import load_signals_history, save_signals_history, save_bot_state
    import derivatives
    import spread

    # position snapshot first: it's the most valuable and the cheapest
    _step("bot_state_snapshot.json", lambda: save_bot_state(_bot_module.state))
    # same dict already cached in memory; saving forces a write
    _step("signals_history.json", lambda: save_signals_history(load_signals_history()))
    _step("derivatives OI baseline", derivatives.flush_oi_baseline)
    _step("spread history", spread.flush_spread_history)


async def _stop_scanner(timeout: float = 5.0):
    """🆕 FIX: the scanner used to keep running while (and after) state was
    flushed, so a trade opened or closed in that window was missing from the
    snapshot — after the restart it was either lost or announced twice. Stop
    the scanner first (Loop.stop() is graceful: the current iteration
    finishes), but only wait a few seconds: `docker stop` gives 10 s in total
    before SIGKILL, and the flush must still fit in it."""
    import bot as _bot_module
    restart = getattr(_bot_module, "_scanner_restart_task", None)
    if restart is not None and not restart.done():
        restart.cancel()  # a pending crash-recovery restart must not revive the loop
    scanner = _bot_module.market_scanner
    if not scanner.is_running():
        return
    scanner.stop()
    deadline = asyncio.get_running_loop().time() + timeout
    while scanner.is_running() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.1)
    if scanner.is_running():
        logger.warning("[SHUTDOWN] Scanner iteration did not finish in time — cancelling it")
        scanner.cancel()
        await asyncio.sleep(0.2)


async def _main():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_shutdown(sig_name: str):
        logger.info(f"[SHUTDOWN] Received {sig_name}, shutting down gracefully...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except NotImplementedError:
            # Windows' event loop has no add_signal_handler — fall back to the
            # plain signal module (still routes into the same stop_event).
            signal.signal(sig, lambda *_a, _n=sig.name: loop.call_soon_threadsafe(_request_shutdown, _n))

    tasks_to_wait = {asyncio.create_task(stop_event.wait(), name="stop-event")}

    bot_task = None
    if DISCORD_ENABLED:
        bot_task = asyncio.create_task(bot.start(DISCORD_TOKEN), name="discord-bot")
        tasks_to_wait.add(bot_task)
    else:
        logger.info("[STARTUP] Discord disabled (no token or DISCORD_ENABLED=false) — running scanner + web dashboard only.")
        # on_ready never fires without a gateway connection, so kick off the
        # scanner/backtest engine directly here instead.
        # 🆕 FIX: the task object was dropped, so it could be garbage-collected
        # mid-run and an exception inside it was never reported.
        engine_task = asyncio.create_task(ensure_engine_started(), name="engine-start")

        def _log_engine_start(t: asyncio.Task):
            if not t.cancelled() and t.exception() is not None:
                logger.error(f"[STARTUP] Engine start failed: {t.exception()}", exc_info=t.exception())

        engine_task.add_done_callback(_log_engine_start)

    web_task = asyncio.create_task(_run_web_api(), name="web-api")
    tasks_to_wait.add(web_task)

    # Wait for either a stop signal, or (an unexpected) completion of one of the tasks
    done, pending = await asyncio.wait(tasks_to_wait, return_when=asyncio.FIRST_COMPLETED)

    # 🆕 FIX: this used to fall straight through to shutdown on ANY task
    # completing, with no distinction between "stop_event fired because of
    # a deliberate SIGTERM/SIGINT" (the expected case) and "discord-bot or
    # web-api actually crashed" — and no log line naming which task
    # finished or what exception it raised, so a crash and a clean shutdown
    # were indistinguishable after the fact. Log every task in `done`
    # (skipping the expected stop-event task itself) with its outcome.
    for task in done:
        if task.get_name() == "stop-event":
            continue
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            logger.warning(f"[SHUTDOWN] Task '{task.get_name()}' was cancelled unexpectedly.")
            continue
        if exc is not None:
            logger.error(f"[SHUTDOWN] Task '{task.get_name()}' crashed — shutting down: {exc}", exc_info=exc)
        else:
            logger.warning(f"[SHUTDOWN] Task '{task.get_name()}' finished unexpectedly (no error) — shutting down.")

    await _stop_scanner()
    _flush_state_to_disk()

    if _web_server is not None:
        _web_server.should_exit = True  # graceful uvicorn shutdown

    if bot_task is not None:
        logger.info("[SHUTDOWN] Closing Discord connection...")
        if not bot.is_closed():
            await bot.close()

    if not web_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(web_task), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
    for task in filter(None, (bot_task, web_task)):
        if not task.done():
            task.cancel()
    await asyncio.gather(*filter(None, (bot_task, web_task)), return_exceptions=True)

    logger.info("[SHUTDOWN] Done.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    if not DISCORD_TOKEN:
        logger.warning("[STARTUP] DISCORD_TOKEN not set — running without Discord (scanner + web dashboard only).")

    asyncio.run(_main())

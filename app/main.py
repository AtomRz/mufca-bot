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
import signal

from bot import bot, ensure_engine_started
from config import DISCORD_TOKEN, DISCORD_ENABLED

logger = logging.getLogger(__name__)

WEB_API_PORT = 8585


async def _run_web_api():
    """Brings up FastAPI (web_api.py) via uvicorn in the current event loop."""
    import uvicorn
    from web_api import app as fastapi_app  # imported here: web_api does `import bot as core`,
                                             # bot must already be fully loaded by this point

    config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=WEB_API_PORT,
        log_level="info",
        loop="none",  # already inside a running loop — don't let uvicorn create its own
    )
    server = uvicorn.Server(config)
    logger.info(f"[WEB] Starting web API on 0.0.0.0:{WEB_API_PORT}")
    await server.serve()


def _flush_state_to_disk():
    """Forces a save of everything that's accumulated in memory but hasn't
    made it to disk yet."""
    try:
        from state import load_signals_history, save_signals_history, save_bot_state
        import bot as _bot_module

        history = load_signals_history()  # this is the same dict already cached in memory
        save_signals_history(history)     # forces it to write to disk
        logger.info("[SHUTDOWN] signals_history.json flushed to disk")

        save_bot_state(_bot_module.state)  # snapshot of active positions — survives a restart
        logger.info("[SHUTDOWN] bot_state_snapshot.json flushed to disk")

        import derivatives
        derivatives.flush_oi_baseline()  # throttled OI baseline writes — force the last one out
        logger.info("[SHUTDOWN] derivatives OI baseline flushed to disk")

        import spread
        spread.flush_spread_history()  # throttled spread history writes — force the last one out
        logger.info("[SHUTDOWN] spread history flushed to disk")
    except Exception as e:
        logger.error(f"[SHUTDOWN] Failed to flush state: {e}", exc_info=True)


async def _main():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_shutdown(sig_name: str):
        logger.info(f"[SHUTDOWN] Received {sig_name}, shutting down gracefully...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown, sig.name)

    tasks_to_wait = {asyncio.create_task(stop_event.wait(), name="stop-event")}

    bot_task = None
    if DISCORD_ENABLED:
        bot_task = asyncio.create_task(bot.start(DISCORD_TOKEN), name="discord-bot")
        tasks_to_wait.add(bot_task)
    else:
        logger.info("[STARTUP] Discord disabled (no token or DISCORD_ENABLED=false) — running scanner + web dashboard only.")
        # on_ready never fires without a gateway connection, so kick off the
        # scanner/backtest engine directly here instead.
        asyncio.create_task(ensure_engine_started())

    web_task = asyncio.create_task(_run_web_api(), name="web-api")
    tasks_to_wait.add(web_task)

    # Wait for either a stop signal, or (an unexpected) completion of one of the tasks
    done, pending = await asyncio.wait(tasks_to_wait, return_when=asyncio.FIRST_COMPLETED)

    _flush_state_to_disk()

    if bot_task is not None:
        logger.info("[SHUTDOWN] Closing Discord connection...")
        if not bot.is_closed():
            await bot.close()

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

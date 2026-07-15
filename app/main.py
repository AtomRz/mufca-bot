"""
MUFCA v4.0 — Multi-Timeframe Adaptive Trading Bot
Запуск: python main.py

🆕 Discord-бот и веб-API (web_api.py) теперь работают в ОДНОМ процессе и
ОДНОМ asyncio event loop — discord.py и uvicorn прекрасно уживаются вместе
через asyncio.gather(). Это значит: никакого дублирующего fetch/пересчёта
индикаторов — веб-морда читает те же bot.state и config, что видит сканер.

🆕 Graceful shutdown: по SIGTERM (`docker stop`) или SIGINT (Ctrl+C) бот
досохраняет signals_history.json на диск перед выходом, вместо того чтобы
просто быть убитым и потерять несохранённый прогресс по MFE/MAE
(см. update_signal_mae_mfe в state.py).
"""

import asyncio
import logging
import signal

from bot import bot
from config import DISCORD_TOKEN

logger = logging.getLogger(__name__)

WEB_API_PORT = 8585


async def _run_web_api():
    """Поднимает FastAPI (web_api.py) через uvicorn в текущем event loop."""
    import uvicorn
    from web_api import app as fastapi_app  # импорт здесь: web_api делает `import bot as core`,
                                             # bot должен быть уже полностью загружен к этому моменту

    config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=WEB_API_PORT,
        log_level="info",
        loop="none",  # уже внутри работающего loop — не даём uvicorn создавать свой
    )
    server = uvicorn.Server(config)
    logger.info(f"[WEB] Starting web API on 0.0.0.0:{WEB_API_PORT}")
    await server.serve()


def _flush_state_to_disk():
    """Форсит сохранение всего, что накопилось в памяти, но ещё не долетело до диска."""
    try:
        from state import load_signals_history, save_signals_history
        history = load_signals_history()  # это тот же закэшированный в памяти dict
        save_signals_history(history)     # принудительно пишет его на диск
        logger.info("[SHUTDOWN] signals_history.json flushed to disk")
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

    bot_task = asyncio.create_task(bot.start(DISCORD_TOKEN), name="discord-bot")
    web_task = asyncio.create_task(_run_web_api(), name="web-api")

    # Ждём либо сигнала остановки, либо (аварийного) завершения одной из задач
    done, pending = await asyncio.wait(
        {asyncio.create_task(stop_event.wait(), name="stop-event"), bot_task, web_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    _flush_state_to_disk()

    logger.info("[SHUTDOWN] Closing Discord connection...")
    if not bot.is_closed():
        await bot.close()

    for task in (bot_task, web_task):
        if not task.done():
            task.cancel()
    await asyncio.gather(bot_task, web_task, return_exceptions=True)

    logger.info("[SHUTDOWN] Done.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in .env file!")

    asyncio.run(_main())

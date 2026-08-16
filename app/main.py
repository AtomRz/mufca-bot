"""
MUFCA v4.0 — Multi-Timeframe Adaptive Trading Bot
Запуск: python main.py

🆕 Discord-бот и веб-API (web_api.py) теперь работают в ОДНОМ процессе и
ОДНОМ asyncio event loop — discord.py и uvicorn прекрасно уживаются вместе
через asyncio.gather(). Это значит: никакого дублирующего fetch/пересчёта
индикаторов — веб-морда читает те же bot.state и config, что видит сканер.

🆕 Discord теперь полностью опционален (config.DISCORD_ENABLED — false, если
DISCORD_TOKEN не задан, либо явно DISCORD_ENABLED=false в .env). Без Discord
запускаются только сканер и веб-дашборд: gateway-соединение не поднимается,
!команды недоступны, но сигналы/TP1/статистика продолжают работать как обычно
через веб/WS/Android push. Раньше запуск сканера был жёстко завязан на
Discord'овский on_ready — без него не работало вообще ничего (см.
bot.ensure_engine_started()).

🆕 Graceful shutdown: по SIGTERM (`docker stop`) или SIGINT (Ctrl+C) бот
досохраняет signals_history.json И снапшот активных позиций (bot_state_snapshot.json)
на диск перед выходом — раньше при любом рестарте (в том числе обычном деплое
новой версии) bot.state строился с нуля через make_state(), и все активные,
ещё не закрытые позиции просто исчезали из Status/графика (см. state.save_bot_state).
"""

import asyncio
import logging
import signal

from bot import bot, ensure_engine_started
from config import DISCORD_TOKEN, DISCORD_ENABLED

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
        from state import load_signals_history, save_signals_history, save_bot_state
        import bot as _bot_module

        history = load_signals_history()  # это тот же закэшированный в памяти dict
        save_signals_history(history)     # принудительно пишет его на диск
        logger.info("[SHUTDOWN] signals_history.json flushed to disk")

        save_bot_state(_bot_module.state)  # снапшот активных позиций — переживёт рестарт
        logger.info("[SHUTDOWN] bot_state_snapshot.json flushed to disk")
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

    # Ждём либо сигнала остановки, либо (аварийного) завершения одной из задач
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

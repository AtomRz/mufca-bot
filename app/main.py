"""
MUFCA v4.0 — Multi-Timeframe Adaptive Trading Bot
Запуск: python main.py

🆕 Discord-бот и веб-API (web_api.py) теперь работают в ОДНОМ процессе и
ОДНОМ asyncio event loop — discord.py и uvicorn прекрасно уживаются вместе
через asyncio.gather(). Это значит: никакого дублирующего fetch/пересчёта
индикаторов — веб-морда читает те же bot.state и config, что видит сканер.
"""

import asyncio
import logging

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


async def _main():
    await asyncio.gather(
        bot.start(DISCORD_TOKEN),
        _run_web_api(),
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in .env file!")

    asyncio.run(_main())

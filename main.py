"""
MUFCA v3.1 — Multi-Timeframe Adaptive Trading Bot
Запуск: python main.py
"""

import logging
from bot import bot
from config import DISCORD_TOKEN

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in .env file!")
    
    bot.run(DISCORD_TOKEN)
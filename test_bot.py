import asyncio
import logging
import sys

from fball_bot.bot import FballTradingBot
from fball_bot.database import init_database

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)

async def main():
    init_database()
    bot = FballTradingBot()
    await bot._init_polymarket()
    print("Running cycle...")
    await bot._cycle()
    print("Cycle completed")
    await bot.stop()

asyncio.run(main())

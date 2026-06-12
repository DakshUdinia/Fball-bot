import asyncio
from fball_bot.pm import GammaClient

async def main():
    c = GammaClient()
    ms = await c.search_markets('soccer', limit=20)
    for m in ms:
        print(m.get('question'))
    await c.close()

asyncio.run(main())

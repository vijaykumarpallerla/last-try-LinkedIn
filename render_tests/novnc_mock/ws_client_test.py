import asyncio
import websockets

async def t():
    async with websockets.connect('ws://localhost:6902/') as ws:
        await ws.send('hello')
        r = await ws.recv()
        print('recv:', r)

if __name__ == '__main__':
    asyncio.run(t())

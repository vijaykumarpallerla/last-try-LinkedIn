import asyncio
import os
import logging
import signal

import websockets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('ws-test')

PORT = int(os.environ.get('PORT', '8000'))

async def echo(websocket, path):
    logger.info('Client connected')
    try:
        async for message in websocket:
            logger.info(f'Received: {message}')
            await websocket.send(f'echo:{message}')
    except websockets.exceptions.ConnectionClosedOK:
        logger.info('Client disconnected')
    except Exception:
        logger.exception('Error in websocket')

async def main():
    server = await websockets.serve(echo, '0.0.0.0', PORT)
    logger.info(f'WebSocket echo server listening on 0.0.0.0:{PORT}')

    stop = asyncio.Future()

    def _on_sig(*_):
        if not stop.done():
            stop.set_result(True)

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _on_sig)
        except Exception:
            pass

    await stop
    server.close()
    await server.wait_closed()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

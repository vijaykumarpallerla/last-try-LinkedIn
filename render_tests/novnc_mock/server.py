from http.server import SimpleHTTPRequestHandler, HTTPServer
import threading
import os
import asyncio
import websockets
import logging

ROOT = os.path.dirname(__file__)
STATIC_DIR = os.path.join(ROOT, 'static')
WS_PORT = int(os.environ.get('NOVNC_MOCK_WS_PORT', '6902'))
HTTP_PORT = int(os.environ.get('NOVNC_MOCK_HTTP_PORT', '6901'))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('novnc-mock')

class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

def run_http():
    httpd = HTTPServer(('0.0.0.0', HTTP_PORT), _Handler)
    log.info(f'HTTP static server listening on http://0.0.0.0:{HTTP_PORT}')
    httpd.serve_forever()

async def ws_echo(websocket, path):
    log.info('WS client connected')
    try:
        async for msg in websocket:
            log.info('WS received: %s', msg)
            await websocket.send(f'echo:{msg}')
    except Exception:
        log.exception('WS handler error')

def run_ws():
    # Run the websockets server in the main thread asyncio event loop
    async def _main():
        async with websockets.serve(ws_echo, '0.0.0.0', WS_PORT):
            log.info(f'WebSocket echo server listening on ws://0.0.0.0:{WS_PORT}')
            await asyncio.Future()  # run forever
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    t1 = threading.Thread(target=run_http, daemon=True)
    t2 = threading.Thread(target=run_ws, daemon=True)
    t1.start()
    t2.start()
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        pass

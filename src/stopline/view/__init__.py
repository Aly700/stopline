"""The local page. stopline.app wires this factory to the gate, the stage and the targets."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .feed import ChangeFeed
from .records import read_chain, read_state, serial

ASSETS = Path(__file__).parent
OPERATIONS = {'1': 'start_run', '2': 'start_run', 'S': 'press_stop', 'K': 'cut_and_kill',
              'W': 'resume_worker', 'R': 'restore_and_reconcile', 'U': 'undo', 'V': 'validation_rejection'}


def create_app(db, stage, targets, numbers=None):
    """Create without opening any connection; streams belong to the app lifespan.

    Serve with uvicorn on 127.0.0.1:8785. The view only reads records and target
    state; key commands go through the supplied StageAPI.
    """
    feed = ChangeFeed(db)

    @asynccontextmanager
    async def lifespan(app):
        feed.start()
        try:
            yield
        finally:
            await run_in_threadpool(feed.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.feed = feed

    @app.middleware('http')
    async def local_page(request, call_next):
        # Browser scripts may observe this page; only this page can issue keys.
        origin = request.headers.get('origin')
        if request.method == 'POST' and origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Use the keys on this page.'}, status_code=403)
        response = await call_next(request)
        # Search is optional. Keep an absent route from becoming a browser error.
        if request.method == 'GET' and request.url.path == '/api/ask' and response.status_code == 404:
            response = JSONResponse({'ok': False, 'rows': [], 'message': 'Search is not enabled.'})
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    @app.get('/')
    def page():
        return FileResponse(ASSETS / 'index.html')

    @app.get('/app.js')
    def script():
        return FileResponse(ASSETS / 'app.js', media_type='text/javascript')

    @app.get('/style.css')
    def stylesheet():
        return FileResponse(ASSETS / 'style.css', media_type='text/css')

    @app.get('/favicon.ico')
    def favicon():
        return Response(status_code=204)

    last_key = {'n': 0}

    def current_state():
        # The last key pressed is part of what every open page shows, whoever pressed it.
        return dict(read_state(db), last_key=dict(last_key) if last_key['n'] else None)

    def remember(key, result):
        last_key.update(n=last_key['n'] + 1, key=key, message=result.get('message', ''),
                        pending=bool(result.get('pending')), lines=result.get('lines'))
        feed.revision += 1
        return result

    @app.get('/api/state')
    def state():
        try:
            return current_state()
        except Exception:
            return JSONResponse({'pending': True, 'message': 'Atlas pending. Keeping the last confirmed state.'}, status_code=503)

    @app.get('/api/targets')
    async def target_state():
        async def read(name, target):
            try:
                return name, serial(await run_in_threadpool(target.state))
            except Exception:
                return name, {'system': name, 'reachable': False}
        return dict(await asyncio.gather(*(read(name, target) for name, target in targets.items())))

    @app.get('/api/events')
    async def events(request: Request):
        async def stream():
            yield 'retry: 1000\nevent: rebuild\ndata: {}\n\n'
            last = -1
            heartbeat = 0
            while not await request.is_disconnected():
                if feed.revision != last:
                    last = feed.revision
                    payload = {'revision': last, 'connected': feed.connected}
                    try:
                        payload['state'] = await run_in_threadpool(current_state)
                    except Exception:
                        payload['pending'] = True
                    yield f'event: change\ndata: {json.dumps(payload)}\n\n'
                await asyncio.sleep(.15)
                heartbeat += 1
                if heartbeat >= 65:
                    yield ': keepalive\n\n'
                    heartbeat = 0
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.post('/api/key/{key}')
    def keypress(key: str):
        key = key.upper()
        commands = {'1': lambda: stage.start_run('flag'), '2': lambda: stage.start_run('stopline'),
                    'S': stage.press_stop, 'K': stage.cut_and_kill, 'W': stage.resume_worker,
                    'R': stage.restore_and_reconcile, 'U': stage.undo, 'V': stage.validation_rejection,
                    'X': lambda: stage.overreach()}
        if key == 'M':
            try:
                lines = list(numbers()) if numbers is not None else []
                return remember(key, {'ok': True, 'lines': [str(line) for line in lines[:3]],
                                      'message': 'Measured results.' if lines else 'Numbers have not been measured.'})
            except Exception:
                return {'ok': False, 'lines': [], 'message': 'Numbers are unavailable.'}
        if key not in commands:
            raise HTTPException(404, 'Unknown key.')
        try:
            return remember(key, serial(asdict(commands[key]())))
        except Exception:
            return JSONResponse({'ok': False, 'pending': False, 'message': 'Request failed. Check the confirmed state before trying again.'}, status_code=503)

    @app.get('/api/operation/{key}')
    def operation(key: str):
        name = OPERATIONS.get(key.upper())
        if name is None:
            raise HTTPException(404, 'Unknown key.')
        try:
            snapshot = stage.status()
            if not snapshot.get('pending'):
                result = snapshot.get('operations', {}).get(name)
                if result is not None:
                    return serial(result)
        except Exception:
            pass
        return {'pending': True, 'message': 'pending'}

    @app.get('/api/chain/{action_id}')
    def chain(action_id: str):
        try:
            result = read_chain(db, action_id)
        except Exception:
            return JSONResponse({'pending': True, 'message': 'Evidence pending.'}, status_code=503)
        if result is None:
            raise HTTPException(404, 'Action not found.')
        return result

    return app

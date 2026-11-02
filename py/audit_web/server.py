"""Bind policy and uvicorn configuration. Loopback only; hard ingress bounds."""
import asyncio
import ipaddress
from pathlib import Path
import webbrowser
import uvicorn
from . import limits
from .app import allowed_hosts_for, create_app
from .runtime import Runtime

UI_DIR = Path(__file__).resolve().parents[2] / 'ui'


def validate_bind(host: str, port: int, tunnel_ports: tuple[int, ...] = ()) -> str:
    """Return the canonical bind host or raise. Non-loopback binds are refused outright."""
    canonical = '127.0.0.1' if host == 'localhost' else host
    try:
        loopback = ipaddress.ip_address(canonical).is_loopback
    except ValueError:
        loopback = False
    if canonical not in ('127.0.0.1', '::1') or not loopback:
        raise ValueError('refusing to bind: this workbench has no authentication and is loopback-only (127.0.0.1 or ::1)')
    if not 1024 <= port <= 65535 or any(not 1024 <= p <= 65535 for p in tunnel_ports):
        raise ValueError('ports must be 1024..65535')
    return canonical


def uvicorn_config(app, host: str, port: int) -> uvicorn.Config:
    return uvicorn.Config(app, host=host, port=port, http='h11', ws='none', lifespan='on', log_level='warning', access_log=False,
                          server_header=False, limit_concurrency=limits.UVICORN_LIMIT_CONCURRENCY, backlog=limits.UVICORN_BACKLOG,
                          timeout_keep_alive=5, h11_max_incomplete_event_size=limits.MAX_HEADER_BYTES)


async def serve(runtime: Runtime, host: str, port: int, *, tunnel_ports: tuple[int, ...] = (), open_browser: bool = False) -> None:
    host = validate_bind(host, port, tunnel_ports)
    app = create_app(manager=runtime.manager, catalog=runtime.catalog, probe=runtime.probe, ui_dir=UI_DIR,
                     allowed_hosts=allowed_hosts_for(host, port, tunnel_ports), closers=[runtime.close])
    server = uvicorn.Server(uvicorn_config(app, host, port))
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.05)
    if server.started:
        url = f'http://{"[::1]" if host == "::1" else host}:{port}/'
        print(f'FinFilings audit workbench ready at {url}  (loopback only, no authentication)', flush=True)
        if open_browser:
            webbrowser.open(url)
    await task

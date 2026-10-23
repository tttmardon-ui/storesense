"""StoreSense API: принимает события от камер/симулятора, отдаёт метрики и дашборд."""
import contextlib
import copy
import os
import threading
import time
from datetime import date
from pathlib import Path

import yaml
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import db, stats

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config/config.yaml"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
API_KEY = os.environ.get("API_KEY", "")

_cfg_cache: dict = {"mtime": 0.0, "cfg": None}


def config() -> dict:
    """Читает config.yaml и перечитывает его сам, если файл изменился."""
    mtime = CONFIG_PATH.stat().st_mtime
    if _cfg_cache["cfg"] is None or mtime != _cfg_cache["mtime"]:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            _cfg_cache["cfg"] = yaml.safe_load(f)
        _cfg_cache["mtime"] = mtime
    return _cfg_cache["cfg"]


def public_config() -> dict:
    """Конфиг без RTSP-ссылок: в них логины и пароли камер."""
    cfg = copy.deepcopy(config())
    for b in cfg.get("branches", []):
        for c in b.get("cameras", []):
            c.pop("rtsp", None)
    return cfg


def _check_key(request: Request) -> JSONResponse | None:
    if API_KEY and request.headers.get("x-api-key") != API_KEY:
        return JSONResponse({"error": "invalid api key"}, status_code=401)
    return None


# ── Эндпоинты ─────────────────────────────────────────────────

PAGE_HEAD = ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
             '<style>body{margin:0}[hidden]{display:none!important}</style></head><body>')


def index(request: Request):
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(PAGE_HEAD + html + "</body></html>")


def health(request: Request):
    db.query("SELECT 1")
    return JSONResponse({"status": "ok", "time": time.time()})


def get_config(request: Request):
    return JSONResponse(public_config())


def get_dashboard(request: Request):
    cfg = config()
    branch = request.query_params.get("branch") or cfg["branches"][0]["id"]
    day_s = request.query_params.get("date")
    try:
        day = date.fromisoformat(day_s) if day_s else None
        return JSONResponse(stats.dashboard(cfg, branch, day))
    except KeyError:
        return JSONResponse({"error": f"unknown branch '{branch}'"}, status_code=404)
    except ValueError:
        return JSONResponse({"error": "date must be YYYY-MM-DD"}, status_code=400)


def get_meta(request: Request):
    branch = request.query_params.get("branch")
    if branch:
        n = db.query("SELECT COUNT(*) AS n FROM events WHERE branch=?", (branch,))[0]["n"]
    else:
        n = db.query("SELECT COUNT(*) AS n FROM events")[0]["n"]
    return JSONResponse({"events": n})


async def post_events(request: Request):
    if (err := _check_key(request)):
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    events = body.get("events") if isinstance(body, dict) else body
    if not isinstance(events, list):
        return JSONResponse({"error": "expected {'events': [...]}"}, status_code=400)
    saved = await run_in_threadpool(db.insert_events, events)
    return JSONResponse({"saved": saved})


async def post_report(request: Request):
    if (err := _check_key(request)):
        return err
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    db.add_report(body.get("branch"), body.get("kind", "daily"), text)
    return JSONResponse({"ok": True})


def get_reports(request: Request):
    limit = min(int(request.query_params.get("limit", 20)), 200)
    rows = db.query("SELECT ts, branch, kind, text FROM reports ORDER BY ts DESC LIMIT ?", (limit,))
    return JSONResponse([dict(r) for r in rows])


# ── Фоновая уборка старых событий ─────────────────────────────

def _cleanup_loop():
    while True:
        try:
            db.cleanup(int(os.environ.get("KEEP_DAYS", 400)))
        except Exception as e:  # уборка не должна ронять сервис
            print("cleanup error:", e, flush=True)
        time.sleep(6 * 3600)


def on_startup():
    db.init()
    config()  # падаем сразу, если конфиг сломан — так ошибку видно в логах
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    print(f"StoreSense API запущен. Конфиг: {CONFIG_PATH}, база: {db.DB_PATH}", flush=True)


routes = [
    Route("/", index),
    Route("/api/health", health),
    Route("/api/config", get_config),
    Route("/api/dashboard", get_dashboard),
    Route("/api/meta", get_meta),
    Route("/api/events", post_events, methods=["POST"]),
    Route("/api/reports", post_report, methods=["POST"]),
    Route("/api/reports", get_reports, methods=["GET"]),
    Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
]

@contextlib.asynccontextmanager
async def lifespan(app):
    on_startup()
    yield


app = Starlette(routes=routes, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)

"""FastAPI 入口：薄路由层，业务逻辑都在 service.py。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import CN_TZ, load_settings
from app.service import Service, ServiceError
from app.store import default_store

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="事件证据时间线", version="0.1.0")
service = Service(load_settings(), default_store())


@app.exception_handler(ServiceError)
async def _service_error(_: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status, content={"error": exc.message})


def _parse_as_of(as_of: Optional[str]) -> Optional[datetime]:
    if not as_of:
        return None
    try:
        dt = datetime.fromisoformat(as_of)
    except ValueError:
        raise ServiceError("as_of 时间格式不正确")
    return dt if dt.tzinfo else dt.replace(tzinfo=CN_TZ)


@app.get("/api/health")
def health():
    return service.health()


@app.get("/api/events")
def events():
    return {"items": service.list_events()}


@app.get("/api/events/{event_id}")
def event_detail(event_id: str, as_of: Optional[str] = Query(None)):
    return service.event_detail(event_id, _parse_as_of(as_of))


@app.post("/api/events/{event_id}/ask")
def ask(event_id: str, payload: dict = Body(...)):
    return service.ask(event_id, payload.get("question", ""), _parse_as_of(payload.get("as_of")))


@app.get("/api/events/{event_id}/market")
def market(event_id: str, thscode: str = Query(...)):
    return service.market(event_id, thscode)


@app.get("/api/watchlist")
def watchlist():
    return service.watchlist()


@app.post("/api/watchlist")
def add_watch(payload: dict = Body(...)):
    return service.add_watch(payload.get("query", ""))


@app.delete("/api/watchlist/{thscode}")
def remove_watch(thscode: str):
    return service.remove_watch(thscode)


@app.post("/api/ingest")
def ingest(payload: dict = Body(...)):
    return service.ingest(payload)


@app.get("/api/ifind/tools")
def ifind_tools():
    return service.ifind_tools()


@app.get("/api/ifind/news")
def ifind_news(q: str = Query("")):
    return service.ifind_news(q)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")

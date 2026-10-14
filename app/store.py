"""内存数据仓库：启动时加载构造样例，运行中导入的来源/主张追加到内存（可选落盘）。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from app.models import Claim, Event, Source, WatchItem

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class Store:
    def __init__(self, seed_file: Optional[Path] = None, runtime_file: Optional[Path] = None):
        self.lock = threading.RLock()
        self.events: dict[str, Event] = {}
        self.sources: dict[str, Source] = {}
        self.claims: dict[str, Claim] = {}
        self.watchlist: dict[str, WatchItem] = {}
        self.extraction_log: list[dict] = []   # 抽取校验记录（含被丢弃的主张）
        self.runtime_file = runtime_file
        if seed_file:
            self._load(seed_file)
        if runtime_file and runtime_file.exists():
            self._load(runtime_file)

    def _load(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for e in raw.get("events", []):
            ev = Event(**e)
            self.events[ev.id] = ev
        for s in raw.get("sources", []):
            src = Source(**s)
            self.sources[src.id] = src
        for c in raw.get("claims", []):
            c = dict(c)
            if "event_id" not in c:
                c["event_id"] = self.sources[c["source_id"]].event_id
            cl = Claim(**c)
            self.claims[cl.id] = cl
        for w in raw.get("watchlist", []):
            wi = WatchItem(**w)
            self.watchlist[wi.thscode] = wi
        self.extraction_log.extend(raw.get("extraction_log", []))

    def event_sources(self, event_id: str) -> list[Source]:
        return [s for s in self.sources.values() if s.event_id == event_id]

    def event_claims(self, event_id: str) -> list[Claim]:
        return [c for c in self.claims.values() if c.event_id == event_id]

    def add(self, event: Optional[Event], source: Source, claims: list[Claim], log: Optional[dict] = None) -> None:
        with self.lock:
            if event:
                self.events[event.id] = event
            self.sources[source.id] = source
            for c in claims:
                self.claims[c.id] = c
            if log:
                self.extraction_log.append(log)
            self._persist()

    def add_watch(self, item: WatchItem) -> None:
        with self.lock:
            self.watchlist[item.thscode] = item
            self._persist()

    def remove_watch(self, thscode: str) -> bool:
        with self.lock:
            ok = self.watchlist.pop(thscode, None) is not None
            self._persist()
            return ok

    def _persist(self) -> None:
        """只把运行中新增的数据写入 runtime 文件（构造样例不重复写）。失败不影响主流程。"""
        if not self.runtime_file:
            return
        try:
            data = {
                "events": [e.model_dump(mode="json") for e in self.events.values() if not e.is_constructed],
                "sources": [s.model_dump(mode="json") for s in self.sources.values() if s.origin != "demo"],
                "claims": [
                    c.model_dump(mode="json") for c in self.claims.values()
                    if self.sources.get(c.source_id) and self.sources[c.source_id].origin != "demo"
                ],
                "watchlist": [w.model_dump(mode="json") for w in self.watchlist.values() if not w.is_constructed],
                "extraction_log": self.extraction_log[-200:],
            }
            self.runtime_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.runtime_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.runtime_file)
        except OSError:
            pass


def default_store() -> Store:
    runtime = os.environ.get("RUNTIME_DATA_FILE", str(DATA_DIR / "runtime.json"))
    return Store(DATA_DIR / "demo_events.json", Path(runtime) if runtime else None)

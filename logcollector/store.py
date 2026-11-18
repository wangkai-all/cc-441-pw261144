"""Append-only JSONL stores with idempotent upserts.

Events and dead-letter errors are persisted as JSON Lines so the state
directory stays inspectable with plain unix tools. Re-adding an object with
an id that already exists is a no-op, which is what makes duplicate reads
and replayed events safe.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, Iterable, List, Optional


class JsonlStore:
    """Base store: dict in memory, JSONL on disk."""

    def __init__(self, path: str, key: str):
        self.path = path
        self.key = key
        self._items: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn last line must not kill the store
                k = obj.get(self.key)
                if k is not None:
                    self._items[k] = obj

    def add(self, obj: dict) -> bool:
        """Insert if the id is new. Returns True when actually appended."""
        k = obj[self.key]
        with self._lock:
            if k in self._items:
                return False
            self._items[k] = obj
            self._append_line(obj)
            return True

    def add_many(self, objs: Iterable[dict]) -> int:
        return sum(1 for o in objs if self.add(o))

    def _append_line(self, obj: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _rewrite(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for obj in self._items.values():
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def get(self, k: str) -> Optional[dict]:
        return self._items.get(k)

    def all(self) -> List[dict]:
        return list(self._items.values())

    def __len__(self) -> int:
        return len(self._items)


class EventStore(JsonlStore):
    def __init__(self, path: str):
        super().__init__(path, "event_id")

    def timeline(self, source: Optional[str] = None,
                 level: Optional[str] = None,
                 limit: int = 1000) -> List[dict]:
        evs = self.all()
        if source:
            evs = [e for e in evs if e["source"] == source]
        if level:
            lv = level.upper()
            evs = [e for e in evs if (e.get("level") or "") == lv]
        evs.sort(key=lambda e: (e.get("ts_utc") or 0, e["event_id"]))
        return evs[-limit:]


class ErrorStore(JsonlStore):
    def __init__(self, path: str):
        super().__init__(path, "error_id")

    def unresolved(self) -> List[dict]:
        return [e for e in self.all() if not e.get("resolved")]

    def mark_resolved(self, error_ids: Iterable[str], replay_id: str) -> int:
        n = 0
        with self._lock:
            for eid in error_ids:
                err = self._items.get(eid)
                if err and not err.get("resolved"):
                    err["resolved"] = True
                    err["resolved_by"] = replay_id
                    n += 1
            if n:
                self._rewrite()
        return n

from __future__ import annotations

"""Lightweight TTL cache for tool results.

Keys are (tool_name, normalized_args) tuples.  Entries expire after
CACHE_TTL_SECONDS and are evicted lazily on the next access.
"""

import re
import time
from threading import RLock
from typing import Any


CACHE_TTL_SECONDS = 600  # 10 minutes — sensible for weather / news
_MAX_ENTRIES = 400


class ToolResultCache:
    def __init__(self, ttl: float = CACHE_TTL_SECONDS, maxsize: int = _MAX_ENTRIES) -> None:
        self._ttl = ttl
        self._maxsize = maxsize
        self._store: dict[tuple[str, str], tuple[float, Any]] = {}
        self._lock = RLock()

    def _key(self, tool: str, args: dict[str, Any]) -> tuple[str, str]:
        """Normalise args so cosmetically-different calls share a cache slot."""
        def _norm(value: Any) -> Any:
            if isinstance(value, str):
                return re.sub(r"\s+", " ", value).strip().casefold()
            if isinstance(value, list):
                return [_norm(item) for item in value]
            if isinstance(value, dict):
                return {k: _norm(value[k]) for k in sorted(value)}
            return value

        import json
        return tool, json.dumps(_norm(args), ensure_ascii=False, sort_keys=True)

    def get(self, tool: str, args: dict[str, Any]) -> Any | None:
        key = self._key(tool, args)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, tool: str, args: dict[str, Any], value: Any) -> None:
        key = self._key(tool, args)
        with self._lock:
            if len(self._store) >= self._maxsize:
                # evict oldest entries first
                now = time.monotonic()
                expired = [k for k, (exp, _) in self._store.items() if exp <= now]
                for k in expired:
                    del self._store[k]
                if len(self._store) >= self._maxsize:
                    oldest_key = min(self._store, key=lambda k: self._store[k][0])
                    del self._store[oldest_key]
            self._store[key] = (time.monotonic() + self._ttl, value)

    def invalidate(self, tool: str | None = None) -> int:
        with self._lock:
            if tool is None:
                n = len(self._store)
                self._store.clear()
                return n
            keys = [k for k in self._store if k[0] == tool]
            for k in keys:
                del self._store[k]
            return len(keys)

    def stats(self) -> dict[str, int]:
        now = time.monotonic()
        with self._lock:
            total = len(self._store)
            live = sum(1 for exp, _ in self._store.values() if exp > now)
        return {"total": total, "live": live, "expired": total - live}


# Module-level singleton used by the agent executor
_cache = ToolResultCache()


def get_cache() -> ToolResultCache:
    return _cache

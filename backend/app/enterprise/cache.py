"""A small in-process cache with an expiry, for results that are expensive to compute and cheap to be a few seconds old (the dashboard's aggregate figures).

Per process: the API and the worker are separate processes, so nothing here is relied on for correctness. A short lifetime is the invalidation: a figure is never more than `ttl` seconds old.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    def __init__(self, ttl_seconds: float = 15.0, max_entries: int = 256):
        self.ttl, self.max = ttl_seconds, max_entries
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self.hits = self.misses = 0

    def get(self, key):
        item = self._data.get(key)
        if item is not None and time.monotonic() - item[0] < self.ttl:
            self._data.move_to_end(key)
            self.hits += 1
            return item[1]
        self._data.pop(key, None)
        self.misses += 1
        return None

    def put(self, key, value) -> None:
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

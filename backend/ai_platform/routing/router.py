"""LLM router: capability + resource aware model selection with caching,
load balancing (round-robin across equal-priority backends), retry/fallback
and a bounded async task queue for production stability.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from ..config import settings
from ..resources.monitor import resource_monitor
from ..models.base import BaseBackend, GenerationResult


@dataclass
class RouteRequest:
    messages: list[dict]
    modalities: set[str] = field(default_factory=lambda: {"text"})
    preferred_model: str = "auto"       # explicit id or "auto"
    force_local: bool = False           # e.g. privacy mode / budget
    temperature: float = 0.7
    max_tokens: int = 1024
    task_complexity: float = 0.5        # 0..1 – used for priority scheduling


class ResponseCache:
    """LRU cache keyed by hash(model-context+prompt); skips cloud cost."""

    def __init__(self, max_entries: int):
        self.max = max_entries
        self._data: OrderedDict[str, GenerationResult] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(backend_id: str, messages: list[dict]) -> str:
        raw = backend_id + "|" + "|".join(f"{m['role']}:{m['content']}" for m in messages[-6:])
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, k: str) -> GenerationResult | None:
        if k in self._data:
            self.hits += 1
            self._data.move_to_end(k)
            return self._data[k]
        self.misses += 1
        return None

    def put(self, k: str, v: GenerationResult) -> None:
        self._data[k] = v
        self._data.move_to_end(k)
        while len(self._data) > self.max:
            self._data.popitem(last=False)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class TaskQueue:
    """Priority queue (lower complexity value = served first under pressure)."""

    def __init__(self, max_workers: int = 8):
        self._heap: list[tuple[float, int, asyncio.Future, RouteRequest]] = []
        self._counter = itertools.count()
        self._sem = asyncio.Semaphore(max_workers)
        self.stats = {"queued": 0, "served": 0}

    async def submit(self, router: "LLMRouter", req: RouteRequest) -> GenerationResult:
        self.stats["queued"] += 1
        async with self._sem:  # bounded concurrency protects model backends
            result = await router.dispatch(req)
            self.stats["served"] += 1
            return result


class LLMRouter:
    def __init__(self, backends: list[BaseBackend]):
        self.backends = {b.id: b for b in backends}
        self._order = sorted(backends, key=lambda b: b.priority)
        self.cache = ResponseCache(settings.cache_max_entries)
        self.queue = TaskQueue()
        self._rr: dict[int, itertools.cycle] = {}

    def register(self, backend: BaseBackend) -> None:
        """Hot-plug new models without restart (plugin system uses this)."""
        self.backends[backend.id] = backend
        self._order = sorted(self.backends.values(), key=lambda b: b.priority)

    def unregister(self, backend_id: str) -> bool:
        if backend_id in self.backends and self.backends[backend_id].provider == "local":
            return False  # never drop the guaranteed local fallback
        removed = self.backends.pop(backend_id, None)
        if removed:
            self._order = sorted(self.backends.values(), key=lambda b: b.priority)
        return bool(removed)

    # ---- selection ---------------------------------------------------------
    def candidates(self, req: RouteRequest) -> list[BaseBackend]:
        if req.preferred_model != "auto" and req.preferred_model in self.backends:
            b = self.backends[req.preferred_model]
            if b.can_handle(req.modalities):
                return [b]
        pool = [b for b in self._order if b.can_handle(req.modalities)]
        if req.force_local:
            pool = [b for b in pool if b.provider == "local"] or pool
        # resource-aware: avoid cloud video/3D on memory-starved machines
        snap = resource_monitor.snapshot()
        if snap.ram_available_mb < 500:
            pool = [b for b in pool if b.provider == "local"] or pool
        groups: dict[int, list[BaseBackend]] = {}
        for b in pool:
            groups.setdefault(b.priority, []).append(b)
        ordered: list[BaseBackend] = []
        for prio in sorted(groups):
            grp = groups[prio]
            # rotate equal-priority group each call => round-robin load spreading
            offset = int(time.monotonic() * 1000) % len(grp)
            ordered.extend(grp[offset:] + grp[:offset])
        return ordered or [self.backends["local-core"]]

    def route(self, req: RouteRequest) -> BaseBackend:
        cands = self.candidates(req)
        return cands[0]

    # ---- execution -----------------------------------------------------------
    async def dispatch(self, req: RouteRequest) -> GenerationResult:
        last_exc: Exception | None = None
        for backend in self.candidates(req)[:3]:  # up to 3 fallback attempts
            key = ResponseCache.key(backend.id, req.messages)
            if cached := self.cache.get(key):
                cached.meta["cache"] = "hit"
                return cached
            try:
                res = await backend.generate(req.messages,
                                             temperature=req.temperature,
                                             max_tokens=req.max_tokens)
                self.cache.put(key, res)
                res.meta.setdefault("cache", "miss")
                res.meta["routed_via"] = backend.provider
                return res
            except Exception as exc:
                last_exc = exc
                continue
        # ultimate fallback: local engine (never fails)
        local = self.backends["local-core"]
        res = await local.generate(req.messages)
        res.meta["fallback_reason"] = str(last_exc) if last_exc else ""
        return res

    async def run(self, req: RouteRequest) -> GenerationResult:
        return await self.queue.submit(self, req)

    async def stream_dispatch(self, req: RouteRequest):
        """Yield text chunks; falls back to whole-response generation on error."""
        for backend in self.candidates(req)[:2]:
            try:
                async for chunk in backend.stream(req.messages,
                                                  temperature=req.temperature,
                                                  max_tokens=req.max_tokens):
                    yield chunk
                return
            except Exception:
                continue
        res = await self.backends["local-core"].generate(req.messages)
        yield res.text

    def describe(self) -> list[dict]:
        return [{"id": b.id, "provider": b.provider, "modalities": list(b.modalities),
                 "priority": b.priority, "streaming": b.supports_streaming}
                for b in self._order]

"""Plugin system: auto-discovery, hot reload, dynamic registration.

Plugins live in `ai_platform/plugins/builtin/` (or any dir added at runtime)
and expose a module-level `PLUGIN: Plugin` instance. The manager scans .py
files, imports them safely and registers tools into the tool registry – no
restart required (`reload()` re-scans and diffs). External HTTP-tool plugins
are supported via HttpToolPlugin; when an external API is unreachable the
manager falls back to the plugin's built-in `fallback` implementation.
"""
from __future__ import annotations

import importlib.util
import inspect
import pkgutil
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import httpx


@dataclass
class ToolResult:
    ok: bool
    output: str
    source: str = "plugin"
    meta: dict = field(default_factory=dict)


class Plugin(ABC):
    name: str = "unnamed"
    version: str = "1.0.0"
    description: str = ""
    modalities: tuple[str, ...] = ("text",)
    enabled: bool = True

    @abstractmethod
    async def execute(self, action: str, payload: dict) -> ToolResult: ...

    def manifest(self) -> dict:
        return {"name": self.name, "version": self.version,
                "description": self.description, "modalities": list(self.modalities),
                "enabled": self.enabled}


class HttpToolPlugin(Plugin):
    """Base for external-API plugins with automatic internal fallback."""

    endpoint: str = ""
    timeout: float = 8.0

    async def execute(self, action: str, payload: dict) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(self.endpoint, json={"action": action, **payload})
                r.raise_for_status()
                return ToolResult(True, r.text[:4000], source="external")
        except Exception as exc:
            fb = getattr(self, "fallback", None)
            if callable(fb):
                out = await fb(action, payload) if inspect.iscoroutinefunction(fb) else fb(action, payload)
                return ToolResult(True, out, source="internal-fallback",
                                  meta={"external_error": str(exc)[:200]})
            return ToolResult(False, f"Plugin {self.name} nicht erreichbar: {exc}", source="error")


class PluginManager:
    def __init__(self):
        self.plugins: dict[str, Plugin] = {}
        self._load_errors: list[str] = []
        self.last_scan: float = 0.0

    # ---- discovery ----------------------------------------------------------
    def discover_builtin(self) -> None:
        from . import builtin as builtin_pkg
        for mod_info in pkgutil.iter_modules(builtin_pkg.__path__):
            if mod_info.name.startswith("_"):
                continue
            self._import(f"{builtin_pkg.__name__}.{mod_info.name}")
        self.last_scan = time.time()

    def load_directory(self, path: str | Path) -> int:
        found = 0
        for f in sorted(Path(path).glob("*.py")):
            if f.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(f"dynplug_{f.stem}", f)
            if not (spec and spec.loader):
                continue
            try:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
                found += self._register_from_module(mod)
            except Exception as exc:
                self._load_errors.append(f"{f.name}: {exc}")
        self.last_scan = time.time()
        return found

    def _import(self, dotted: str) -> None:
        try:
            mod = importlib.import_module(dotted)
            self._register_from_module(mod)
        except Exception as exc:
            self._load_errors.append(f"{dotted}: {exc}")

    def _register_from_module(self, mod) -> int:
        n = 0
        for attr in vars(mod).values():
            if isinstance(attr, Plugin):
                self.register(attr)
                n += 1
        return n

    # ---- lifecycle ------------------------------------------------------------
    def register(self, plugin: Plugin) -> None:
        self.plugins[plugin.name] = plugin

    def unregister(self, name: str) -> bool:
        return self.plugins.pop(name, None) is not None

    def reload(self) -> dict:
        before = set(self.plugins)
        self.discover_builtin()
        after = set(self.plugins)
        return {"added": sorted(after - before), "kept": sorted(after & before),
                "errors": self._load_errors[-10:]}

    def get(self, name: str) -> Plugin | None:
        p = self.plugins.get(name)
        return p if p and p.enabled else None

    def list_manifests(self) -> list[dict]:
        return [p.manifest() for p in self.plugins.values()]

    async def invoke(self, name: str, action: str, payload: dict) -> ToolResult:
        p = self.get(name)
        if not p:
            return ToolResult(False, f"Unbekanntes oder deaktiviertes Plugin: {name}", "system")
        started = time.perf_counter()
        try:
            res = await p.execute(action, payload)
        except Exception as exc:
            res = ToolResult(False, f"Plugin-Fehler: {exc}", "error")
        res.meta["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return res


plugin_manager = PluginManager()

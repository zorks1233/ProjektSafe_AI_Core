"""Built-in plugin: weather via external API with offline internal fallback.

Demonstrates the HttpToolPlugin contract: if wttr.in is unreachable, an
internal deterministic estimator answers instead of failing the request.
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse

from ..manager import HttpToolPlugin, ToolResult


class WeatherPlugin(HttpToolPlugin):
    name = "weather"
    version = "1.0.0"
    description = "Wetterdaten (externes API mit internem Ersatzmodul)"
    modalities = ("text",)
    endpoint = ""  # built per-request below

    async def execute(self, action: str, payload: dict) -> ToolResult:
        if action != "current":
            return ToolResult(False, f"Unbekannte Aktion: {action}")
        city = str(payload.get("city", ""))[:80]
        if not city or not city.replace(" ", "").replace("-", "").isalnum():
            return ToolResult(False, "Ungültiger Ortsname")
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
        try:
            import httpx
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(url, headers={"User-Agent": "ai-platform/1.0"})
                r.raise_for_status()
                data = r.json()
            cur = data["current_condition"][0]
            out = (f"{city}: {cur['temp_C']}°C, {cur['weatherDesc'][0]['value']}, "
                   f"Luftfeuchte {cur['humidity']}%, Wind {cur['windspeedKmph']} km/h")
            return ToolResult(True, out, source="external")
        except Exception as exc:
            out = self.fallback(action, {"city": city})
            return ToolResult(True, out, source="internal-fallback",
                              meta={"external_error": str(exc)[:150]})

    @staticmethod
    def fallback(action: str, payload: dict) -> str:
        city = payload.get("city", "unbekannt")
        h = int(hashlib.sha256(city.lower().encode()).hexdigest(), 16)
        temp = -5 + h % 36                       # deterministic pseudo-climate
        conditions = ["sonnig", "bewölkt", "leichter Regen", "klar", "neblig"]
        cond = conditions[h % len(conditions)]
        return (f"{city}: ~{temp}°C, {cond} "
                f"(interne Schätzung – externes Wetter-API nicht erreichbar)")


PLUGIN = WeatherPlugin()

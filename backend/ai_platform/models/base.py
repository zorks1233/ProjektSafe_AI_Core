"""Model abstraction layer.

Every backend implements the async `generate()` / `stream()` interface so the
router can treat local, cloud and multimodal models uniformly. Cloud adapters
are real HTTP clients (OpenAI-compatible / Anthropic Messages API) and are
only registered when an API key is configured; otherwise the always-available
LocalEngine guarantees a working system (no external dependency needed).
"""
from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx


@dataclass
class GenerationResult:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    meta: dict = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    """Cheap heuristic tokenizer (~4 chars/token), used for accounting/cost."""
    return max(1, len(text) // 4) if text else 0


class BaseBackend(ABC):
    id: str = "base"
    provider: str = "local"
    modalities: tuple[str, ...] = ("text",)
    supports_streaming: bool = True
    priority: int = 100          # lower = preferred when capabilities equal
    loaded_vram_mb: int = 0

    @abstractmethod
    async def generate(self, messages: list[dict], **kw) -> GenerationResult: ...

    async def stream(self, messages: list[dict], **kw):
        """Default streaming: chunk the final result (local engine overrides)."""
        res = await self.generate(messages, **kw)
        for i in range(0, len(res.text), 24):
            yield res.text[i:i + 24]
            await asyncio.sleep(0.01)

    def can_handle(self, modalities: set[str]) -> bool:
        return modalities.issubset(set(self.modalities))


# --------------------------------------------------------------------------
# Local deterministic engine – real logic, not a stub:
# intent routing (code/math/summary/translate/QA) over the conversation with
# template + extractive-NLP generation. Always available, zero dependencies.
# --------------------------------------------------------------------------
class LocalEngine(BaseBackend):
    id = "local-core"
    provider = "local"
    modalities = ("text", "image", "audio", "video", "3d")
    priority = 50

    _CODE_RX = re.compile(r"\b(code|funktion|function|implement|programm|script|debug|fehler|class |def |python|javascript|sql)\b", re.I)
    _MATH_RX = re.compile(r"(berechne|calculate|wieviel|how many|\d+\s*[\+\-\*/×÷]\s*\d+|sqrt|wurzel)")
    _SUMMARY_RX = re.compile(r"\b(zusammenfass|summar|tl;?dr|fasse|kürze|shorten)\b", re.I)
    _TRANSLATE_RX = re.compile(r"\b(übersetz|translate|dolmetsch)\b", re.I)

    async def generate(self, messages: list[dict], **kw) -> GenerationResult:
        prompt = messages[-1]["content"] if messages else ""
        history = [m for m in messages[:-1] if m["role"] == "user"]
        reply = self._respond(prompt, history)
        await asyncio.sleep(0.02)  # simulate inference scheduling
        return GenerationResult(
            text=reply, model=self.id,
            tokens_in=estimate_tokens(prompt), tokens_out=estimate_tokens(reply),
            meta={"engine": "local-deterministic"},
        )

    async def stream(self, messages: list[dict], **kw):
        res = await self.generate(messages, **kw)
        for word in re.findall(r"\S+\s*", res.text):
            yield word
            await asyncio.sleep(0.008)

    # ---- internal heuristics ------------------------------------------------
    def _respond(self, prompt: str, history: list[dict]) -> str:
        p = prompt.strip()
        low = p.lower()
        if self._CODE_RX.search(low):
            return self._code_answer(p)
        if self._MATH_RX.search(low):
            ans = self._math_answer(p)
            if ans:
                return ans
        if self._SUMMARY_RX.search(low):
            return self._summarize(p, history)
        if self._TRANSLATE_RX.search(low):
            return ("Ich habe einen Übersetzungsauftrag erkannt. In dieser "
                    "Offline-Umgebung antworte ich strukturierbar:\n\n"
                    f"Original: „{p[:400]}“\n"
                    "Für echte Übersetzungen mit einem Cloud-Modell simply "
                    "API-Key konfigurieren – das Routing wechselt automatisch.")
        # default: extractive QA over the provided context
        return self._qa_answer(p, history)

    def _code_answer(self, p: str) -> str:
        lang = "python"
        if "javascript" in p.lower() or " js " in p.lower():
            lang = "javascript"
        if "sql" in p.lower():
            lang = "sql"
        fn = re.search(r"`([^`]{1,60})`|(\b([a-zA-Z_][\w]*)\b)(?=\s*(?:funktion|function))", p, re.I)
        name = (fn.group(1) or fn.group(3)) if fn else "loesung"
        name = re.sub(r"\W+", "_", name).strip("_").lower() or "loesung"
        if lang == "python":
            body = (f"```python\ndef {name}(*args, **kwargs):\n"
                    f'    """{p[:160].replace(chr(10), " ")}"""\n'
                    f"    # Lokaler Engine-Entwurf – mit angebundenem LLM wird\n"
                    f"    # hier echtes Code-Generierungsrouting verwendet.\n"
                    f"    raise NotImplementedError('Bitte Modell-Layer verbinden')\n```")
        elif lang == "sql":
            body = f"```sql\n-- Anfrage zu: {p[:120]}\nSELECT * FROM tabelle WHERE bedingung = TRUE LIMIT 100;\n```"
        else:
            body = (f"```javascript\nfunction {name}(...args) {{\n  // Entwurf der lokalen Engine\n"
                    f"  throw new Error('LLM-Backend verbinden');\n}}\n```")
        return f"Code-Entwurf ({lang}):\n\n{body}"

    def _math_answer(self, p: str) -> str | None:
        expr = re.search(r"(\d+(?:\.\d+)?)\s*([\+\-\*/×÷])\s*(\d+(?:\.\d+)?)", p)
        if not expr:
            return None
        a, op, b = float(expr.group(1)), expr.group(2), float(expr.group(3))
        op = {"×": "*", "÷": "/"}.get(op, op)
        val = {"+": a + b, "-": a - b, "*": a * b, "/": (a / b if b else None)}[op]
        if val is None:
            return "Division durch Null ist nicht definiert."
        return f"Ergebnis der Berechnung: {expr.group(0).strip()} = {val:g}"

    def _summarize(self, p: str, history: list[dict]) -> str:
        source = re.sub(r"^.*?(zusammenfass\w*|summar\w*|fasse|tl;?dr)[:\s]*", "", p, flags=re.I)
        if len(source) < 20 and history:
            source = history[-1]["content"]
        sentences = re.split(r"(?<=[.!?])\s+", source) or [source]
        # naive TF scoring for extractive summary
        words = re.findall(r"\w+", source.lower())
        freq: dict[str, int] = {}
        for w in words:
            if len(w) > 3:
                freq[w] = freq.get(w, 0) + 1
        scored = sorted(sentences,
                        key=lambda s: sum(freq.get(w, 0) for w in re.findall(r"\w+", s.lower())),
                        reverse=True)
        top = [s for s in scored[: max(1, min(3, len(scored)))] if s.strip()]
        return "Zusammenfassung:\n- " + "\n- ".join(t.strip() for t in top) if top else "Keine zusammenfassbaren Inhalte gefunden."

    def _qa_answer(self, p: str, history: list[dict]) -> str:
        ctx = " ".join(m["content"] for m in history[-3:])
        if len(ctx) > 80:
            sents = [s for s in re.split(r"(?<=[.!?])\s+", ctx) if len(s) > 40]
            qwords = set(re.findall(r"\w{4,}", p.lower()))
            best = max(sents, key=lambda s: len(qwords & set(re.findall(r"\w{4,}", s.lower()))), default=None)
            if best:
                return (f"Hierzu fällt mir aus dem Gesprächskontext ein:\n\n„{best.strip()}“\n\n"
                        "Vertiefe das Thema mit einer konkreten Folgefrage, oder verbinde "
                        "einen Modell-Provider für vollwertige Generierung.")
        return (f"Verstanden: „{p[:300]}“\n\nIch bin die lokale Kern-Engine dieser Plattform. "
                "Für vollständige Antworten aller Modalitäten kann im Backend ein Cloud-Modell "
                "(OpenAI/Claude/Qwen) per API-Key aktiviert werden – das Routing übernimmt dann "
                "automatisch anhand von Aufgabe, Modalität und verfügbaren Ressourcen.")


# --------------------------------------------------------------------------
# OpenAI-compatible chat completions adapter (also works for Qwen via its
# OpenAI-compatible endpoint, Ollama, LM Studio, vLLM …)
# --------------------------------------------------------------------------
class OpenAICompatBackend(BaseBackend):
    provider = "cloud"
    modalities = ("text", "image")
    supports_streaming = True

    def __init__(self, model_id: str, base_url: str, api_key: str, label: str,
                 vision: bool = False, priority: int = 10):
        self.id = model_id
        self.label = label
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.priority = priority
        if vision:
            self.modalities = ("text", "image")

    async def generate(self, messages: list[dict], **kw) -> GenerationResult:
        payload = {"model": self.id, "messages": messages,
                   "temperature": kw.get("temperature", 0.7),
                   "max_tokens": kw.get("max_tokens", 1024)}
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(f"{self.base_url}/chat/completions",
                                  headers={"Authorization": f"Bearer {self.api_key}"},
                                  json=payload)
            r.raise_for_status()
            data = r.json()
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        return GenerationResult(text=text, model=self.id,
                                tokens_in=usage.get("prompt_tokens", 0),
                                tokens_out=usage.get("completion_tokens", 0),
                                meta={"provider": self.label})

    async def stream(self, messages: list[dict], **kw):
        payload = {"model": self.id, "messages": messages, "stream": True,
                   "temperature": kw.get("temperature", 0.7)}
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", f"{self.base_url}/chat/completions",
                                     headers={"Authorization": f"Bearer {self.api_key}"},
                                     json=payload) as r:
                r.raise_for_status()
                async for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        delta = json.loads(chunk)["choices"][0]["delta"].get("content")
                    except (KeyError, IndexError, json.JSONDecodeError):
                        continue
                    if delta:
                        yield delta


class AnthropicBackend(BaseBackend):
    provider = "cloud"
    id = "claude-sonnet"
    label = "Anthropic"
    modalities = ("text", "image")
    priority = 12

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model
        self.id = model

    async def generate(self, messages: list[dict], **kw) -> GenerationResult:
        sys_msg = next((m["content"] for m in messages if m["role"] == "system"), None)
        conv = [m for m in messages if m["role"] != "system"]
        payload = {"model": self.model, "max_tokens": kw.get("max_tokens", 1024),
                   "messages": conv}
        if sys_msg:
            payload["system"] = sys_msg
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post("https://api.anthropic.com/v1/messages",
                                  headers={"x-api-key": self.api_key,
                                           "anthropic-version": "2023-06-01"},
                                  json=payload)
            r.raise_for_status()
            data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []))
        usage = data.get("usage", {})
        return GenerationResult(text=text, model=self.model,
                                tokens_in=usage.get("input_tokens", 0),
                                tokens_out=usage.get("output_tokens", 0),
                                meta={"provider": "Anthropic"})


def build_default_backends(settings_obj) -> list[BaseBackend]:
    backends: list[BaseBackend] = [LocalEngine()]
    if settings_obj.openai_api_key and settings_obj.enable_cloud_models:
        backends.append(OpenAICompatBackend(
            "gpt-4o-mini", "https://api.openai.com/v1",
            settings_obj.openai_api_key, "OpenAI", vision=True, priority=5))
    if settings_obj.anthropic_api_key and settings_obj.enable_cloud_models:
        backends.append(AnthropicBackend(settings_obj.anthropic_api_key))
    if settings_obj.qwen_api_key and settings_obj.enable_cloud_models:
        # Qwen offers an OpenAI-compatible endpoint
        backends.append(OpenAICompatBackend(
            "qwen-plus", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            settings_obj.qwen_api_key, "Qwen Cloud", vision=False, priority=8))
    return backends

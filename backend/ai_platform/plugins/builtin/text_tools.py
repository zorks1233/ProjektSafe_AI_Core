"""Built-in plugin: text utilities (word frequency, readability, diff-ish)."""
from __future__ import annotations

import re
from collections import Counter

from ..manager import Plugin, ToolResult


class TextToolsPlugin(Plugin):
    name = "text-tools"
    version = "1.0.0"
    description = "Worthäufigkeit, Lesbarkeit, Statistiken"
    modalities = ("text",)

    async def execute(self, action: str, payload: dict) -> ToolResult:
        text = str(payload.get("text", ""))[:200_000]
        if not text:
            return ToolResult(False, "Kein Text übergeben")
        words = re.findall(r"\w+", text.lower())
        if action == "stats":
            sentences = max(1, len(re.split(r"[.!?]+", text.strip())))
            syll = sum(max(1, len(re.findall(r"[aeiouäöü]+", w))) for w in words)
            flesch = round(206.84 - 1.015 * (len(words) / sentences)
                           - 84.6 * (syll / max(1, len(words))), 1)
            return ToolResult(True, f"Wörter: {len(words)}, Sätze: {sentences}, "
                                    f"Ø Silben/Wort: {syll/max(1,len(words)):.2f}, "
                                    f"Flesch-Lesbarkeit: {flesch}",
                              meta={"words": len(words), "sentences": sentences})
        if action == "frequency":
            top = Counter(words).most_common(int(payload.get("top", 10)))
            lines = "\n".join(f"- {w}: {c}" for w, c in top)
            return ToolResult(True, f"Häufigste Wörter:\n{lines}")
        if action == "slugify":
            slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:120]
            return ToolResult(True, slug)
        return ToolResult(False, f"Unbekannte Aktion: {action}")


PLUGIN = TextToolsPlugin()

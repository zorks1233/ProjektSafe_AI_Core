"""Session management: context window, memory graph, summarising routing.

- ContextWindow keeps the last N messages plus a rolling summary of older
  turns so long chats stay inside token budgets (memory routing).
- MemoryGraph stores entity/relation triples extracted from conversation to
  give the model persistent facts across sessions (per user).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..config import settings


@dataclass
class Turn:
    role: str
    content: str
    meta: dict = field(default_factory=dict)


class ContextWindow:
    """Sliding window + hierarchical summary of evicted history."""

    def __init__(self, max_messages: int | None = None):
        self.max_messages = max_messages or settings.max_context_messages
        self.summary: str = ""

    def build_messages(self, system_prompt: str, history: list[Turn]) -> list[dict]:
        recent = history[-self.max_messages:]
        evicted = history[:-self.max_messages]
        if evicted:
            self._extend_summary(evicted)
        msgs: list[dict] = [{"role": "system", "content": system_prompt}]
        if self.summary:
            msgs.append({"role": "system",
                         "content": f"Zusammenfassung früherer Gespräche:\n{self.summary}"})
        msgs.extend({"role": t.role, "content": t.content} for t in recent)
        return msgs

    def _extend_summary(self, evicted: list[Turn], max_len: int = 1200) -> None:
        lines = []
        for t in evicted[-12:]:
            snippet = re.sub(r"\s+", " ", t.content)[:140]
            lines.append(f"{t.role}: {snippet}")
        combined = (self.summary + "\n" if self.summary else "") + "\n".join(lines)
        self.summary = combined[-max_len:]


ENTITY_RX = re.compile(r"\b([A-ZÄÖÜ][\wäöüß-]{2,}(?:\s+[A-ZÄÖÜ][\wäöüß-]{2,}){0,3})\b")
REL_RX = re.compile(r"\b(ist|sind|arbeitet|lebt|mag|braucht|hat|is|are|works|likes|needs|has)\b", re.I)


class MemoryGraph:
    """Lightweight triple store per user – persisted via caller callbacks."""

    MAX_TRIPLES_PER_USER = 500

    def __init__(self):
        self._store: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    def ingest(self, user_id: int, text: str) -> int:
        added = 0
        sentences = re.split(r"(?<=[.!?])\s+", text)
        for s in sentences:
            ents = ENTITY_RX.findall(s)
            rel = REL_RX.search(s)
            if not ents or not rel:
                continue
            subject = ents[0].strip()
            predicate = rel.group(1).lower()
            obj = s.split(rel.group(1), 1)[-1].strip(" .,;:")[:120]
            if subject and obj:
                bucket = self._store[user_id][subject.lower()]
                triple = f"{predicate} -> {obj}"
                if triple not in bucket:
                    bucket.add(triple)
                    added += 1
        # cap growth
        u = self._store[user_id]
        total = sum(len(v) for v in u.values())
        while total > self.MAX_TRIPLES_PER_USER and u:
            k = next(iter(u))
            total -= len(u.pop(k))
        return added

    def recall(self, user_id: int, query: str, limit: int = 6) -> list[str]:
        qwords = {w.lower() for w in re.findall(r"\w{4,}", query)}
        scored: list[tuple[float, str]] = []
        for ent, triples in self._store[user_id].items():
            overlap = len(qwords & set(re.findall(r"\w{4,}", ent)))
            for tr in triples:
                score = overlap + 0.1 * len(qwords & set(re.findall(r"\w{4,}", tr.lower())))
                scored.append((score, f"{ent} {tr}"))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:limit] if _ > 0]

    def context_block(self, user_id: int, query: str) -> str:
        facts = self.recall(user_id, query)
        return ("Bekannte Fakten über den Nutzer:\n" + "\n".join(f"- {f}" for f in facts)) if facts else ""

    def load(self, user_id: int, data: dict[str, list[str]]) -> None:
        for ent, trs in data.items():
            self._store[user_id][ent] = set(trs)

    def dump(self, user_id: int) -> dict[str, list[str]]:
        return {k: sorted(v) for k, v in self._store.get(user_id, {}).items()}


context_window = ContextWindow()
memory_graph = MemoryGraph()

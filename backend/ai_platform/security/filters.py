"""Security layer: injection detection, payload analysis, anomaly heuristics.

All findings are returned as Finding objects with a severity so the router
can decide to block, flag or allow. Unknown/suspicious patterns are marked
via statistical heuristics (entropy, token density) instead of only regexes.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Finding:
    category: str
    severity: Severity
    description: str
    evidence: str = ""

    def to_dict(self) -> dict:
        return {"category": self.category, "severity": self.severity.value,
                "description": self.description, "evidence": self.evidence[:120]}


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def max_severity(self) -> Severity | None:
        if not self.findings:
            return None
        order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        return max((f.severity for f in self.findings), key=order.index)

    @property
    def blocked(self) -> bool:
        return self.max_severity in (Severity.HIGH, Severity.CRITICAL)

    def to_dict(self) -> dict:
        return {"blocked": self.blocked,
                "max_severity": self.max_severity.value if self.max_severity else None,
                "findings": [f.to_dict() for f in self.findings]}


# ---------------- signature-based detectors ----------------
_SQL_PATTERNS = [
    re.compile(r"\b(union\s+select|select\s+.*\s+from\s+information_schema)\b", re.I),
    re.compile(r"\b(drop|truncate|alter)\s+table\b", re.I),
    re.compile(r"\b(insert|update|delete)\s+.*\b(where|set)\b.*(--|;)", re.I),
    re.compile(r"('\s*(or|and)\s*'?\d+'?\s*=\s*'?\d+')", re.I),
    re.compile(r"\bexec(\s|\+)+(s|x)p\w+", re.I),
    re.compile(r";\s*(shutdown|go|drop)\b", re.I),
]
_XSS_PATTERNS = [
    re.compile(r"<\s*script[^>]*>", re.I),
    re.compile(r"javascript\s*:", re.I),
    re.compile(r"on(load|error|click|mouseover)\s*=", re.I),
    re.compile(r"<\s*iframe[^>]+src\s*=", re.I),
]
_PATH_TRAVERSAL = re.compile(r"(\.\./){2,}|(%2e%2e%2f){2,}", re.I)
_COMMAND_INJECTION = [
    re.compile(r"[;&|`]\s*(rm|cat|curl|wget|nc|bash|sh|powershell|cmd)\b", re.I),
    re.compile(r"\$\((?:\s*\w+.{1,60})\)"),
    re.compile(r">\s*/dev/(sd|hd|nvme)", re.I),
]
_PROMPT_INJECTION = [
    re.compile(r"\b(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)\b", re.I),
    re.compile(r"\byou\s+are\s+now\s+(in\s+)?(developer|dan|jailbreak|god)\s*mode\b", re.I),
    re.compile(r"\b(reveal|print|show)\s+(your\s+)?(system\s+prompt|hidden\s+instructions?)\b", re.I),
    re.compile(r"\bnew\s+system\s+prompt\s*:", re.I),
    re.compile(r"\b(?:act|pretend)\s+as\s+(if\s+you\s+(are|were)\s+)?unrestricted\b", re.I),
]
_PICKLE_DANGER = re.compile(r"(gASV|\bcpickle\b|\bpickle\.loads\b|__reduce__)", re.I)


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


class RequestFilter:
    """Full request scanner combining signatures + statistical heuristics."""

    MAX_TEXT_LEN = 200_000

    def scan_text(self, text: str) -> ScanResult:
        result = ScanResult()
        if not text:
            return result
        text = text[: self.MAX_TEXT_LEN]

        for rx in _SQL_PATTERNS:
            if m := rx.search(text):
                result.findings.append(Finding("sql_injection", Severity.CRITICAL,
                                               "SQL injection signature detected", m.group(0)))
        for rx in _XSS_PATTERNS:
            if m := rx.search(text):
                result.findings.append(Finding("xss", Severity.HIGH,
                                               "Cross-site-scripting payload detected", m.group(0)))
        if m := _PATH_TRAVERSAL.search(text):
            result.findings.append(Finding("path_traversal", Severity.HIGH,
                                           "Path traversal sequence detected", m.group(0)))
        for rx in _COMMAND_INJECTION:
            if m := rx.search(text):
                result.findings.append(Finding("command_injection", Severity.CRITICAL,
                                               "Shell command injection pattern", m.group(0)))
        for rx in _PROMPT_INJECTION:
            if m := rx.search(text):
                result.findings.append(Finding("prompt_injection", Severity.MEDIUM,
                                               "Prompt-injection phrasing detected", m.group(0)))
        if m := _PICKLE_DANGER.search(text):
            result.findings.append(Finding("unsafe_deserialization", Severity.CRITICAL,
                                           "Dangerous deserialization marker", m.group(0)))

        # ---- statistical heuristics for UNKNOWN indicators ----
        words = re.findall(r"\S+", text)
        if len(words) > 50:
            ent = _shannon_entropy(text)
            symbol_ratio = sum(not w.isalnum() for w in "".join(words)) / max(len(text), 1)
            # high entropy + many non-alnum tokens => encoded/obfuscated payload
            if ent > 4.9 and symbol_ratio > 0.35:
                result.findings.append(Finding("anomaly_high_entropy", Severity.MEDIUM,
                                               f"Unknown obfuscation pattern (entropy={ent:.2f})",
                                               text[:80]))
            repeated = max((words.count(w) for w in set(words)), default=0)
            if repeated > 200:
                result.findings.append(Finding("anomaly_repeat_flood", Severity.MEDIUM,
                                               "Token repetition flood (possible DoS/poisoning)", ""))
            control_chars = sum(1 for ch in text if ord(ch) < 9 or 11 <= ord(ch) < 32)
            if control_chars > 20:
                result.findings.append(Finding("anomaly_control_chars", Severity.HIGH,
                                               "Binary/control-character smuggling detected", ""))
        return result

    def scan_payload(self, raw: bytes) -> ScanResult:
        result = ScanResult()
        from ..config import settings
        if len(raw) > settings.max_payload_bytes:
            result.findings.append(Finding("payload_size", Severity.HIGH,
                                           f"Payload exceeds limit ({len(raw)} bytes)", ""))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            result.findings.append(Finding("binary_payload", Severity.HIGH,
                                           "Non-UTF8 binary payload in text field", ""))
            text = raw.decode("utf-8", errors="replace")
        result.findings.extend(self.scan_text(text).findings)
        return result


request_filter = RequestFilter()

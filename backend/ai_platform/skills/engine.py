"""Skill engine: the platform's learning layer.

Two kinds of skills:

1. TASK SKILLS – deterministic, real implementations that run locally and
   answer common chat intents without any cloud model: math (AST-safe),
   word/sentence/char statistics, text transformations (rot13/caesar/base64/
   hex/reverse/case), roman numerals, date & age calculation, BMI, password
   generation, regex testing, markdown tables, colour conversion.

2. LEARNED CONVERSATIONAL SKILLS – the bot *learns what people write in
   chats*: greetings, thanks, farewells, smalltalk and user preferences are
   recognised via multilingual vocabularies, matched with a token-overlap
   classifier and answered with quality-scored templates. Every assistant
   reply carries feedback buttons; 👍/👎 adjust each skill's confidence
   (exponential bandit-style scoring). Skills whose confidence crosses a
   threshold get promoted into the system prompt so stronger models reuse
   what has proven to work ("self-optimisation").

The whole store is persisted encrypted through the repository layer
(`skill_store` table) so learning survives restarts – no plaintext on disk.
"""
from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import json
import math
import random
import re
import secrets
import string
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ===========================================================================
# helpers
# ===========================================================================
def _norm(text: str) -> str:
    return re.sub(r"[^a-zäöüß0-9\s]", "", text.lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w{2,}", _norm(text)))


# ===========================================================================
# 1) task skills – real deterministic implementations
# ===========================================================================
_ARITH_OPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b, ast.USub: lambda a: -a, ast.UAdd: lambda a: +a,
}
_ARITH_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max,
    "int": int, "float": float, "log": math.log, "log10": math.log10,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "exp": math.exp,
    "pi": lambda: math.pi, "floor": math.floor, "ceil": math.ceil,
}


def _arith_eval(node):
    if isinstance(node, ast.Expression):
        return _arith_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITH_OPS:
        left, right = _arith_eval(node.left), _arith_eval(node.right)
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise ZeroDivisionError("Division durch 0")
        if isinstance(node.op, ast.Pow) and abs(right) > 512:
            raise ValueError("Exponent zu groß")
        return _ARITH_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ARITH_OPS:
        return _ARITH_OPS[type(node.op)](_arith_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ARITH_FUNCS:
        return _ARITH_FUNCS[node.func.id](*[_arith_eval(a) for a in node.args])
    raise ValueError("nicht erlaubter Ausdruck")


def _extract_math_expr(text: str) -> Optional[str]:
    t = text.replace(",", ".").replace("×", "*").replace("·", "*").replace("÷", "/")
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s*%\s*von\b", r"(\1/100)*", t, flags=re.I)
    t = re.sub(r"\bquadratwurzel aus\b|\bsqrt von\b|√", "sqrt", t, flags=re.I)
    t = re.sub(r"\bplus\b", "+", t, flags=re.I)
    t = re.sub(r"\bminus\b", "-", t, flags=re.I)
    t = re.sub(r"\b(mal|x)\b", "*", t, flags=re.I)
    t = re.sub(r"\b(durch|geteilt durch)\b", "/", t, flags=re.I)
    t = re.sub(r"\bhoch\b|\^", "**", t, flags=re.I)
    m = re.search(r"[-+*/().,\d\s]*?\d+(?:\.\d+)?(?:\s*[-+*/^]\s*(?:[-+*/().,\d\s]*?\d+(?:\.\d+)?))+[-+*/().,\d\s%]*", t)
    return m.group(0) if m else None


def skill_math(text: str) -> Optional[str]:
    expr = _extract_math_expr(text)
    if not expr:
        return None
    expr = expr.strip().rstrip("%").strip()
    try:
        tree = ast.parse(expr, mode="eval")
        val = _arith_eval(tree)
    except Exception:
        return None
    if isinstance(val, float) and val.is_integer():
        val = int(val)
    pretty = expr.replace("**", "^")
    return f"```text\n{pretty} = {val}\n```\nDie Berechnung lief lokal im Skill-Engine-Layer (AST-validiert, kein eval)."


def skill_textstats(text: str) -> Optional[str]:
    m = re.search(r"(?:wie viele|anzahl)\s*(wörter|zeichen|sätze|words|characters)", text, re.I)
    if not m:
        return None
    body = re.sub(r"^.*?(?:wie viele|anzahl)\s*\w+\s*(?:hat|enthält|in)?[:：]?\s*", "", text, count=1, flags=re.I)
    words = re.findall(r"\S+", body)
    chars = len(body.replace("\n", ""))
    sentences = len([s for s in re.split(r"[.!?]+", body) if s.strip()])
    freq: dict[str, int] = {}
    for w in re.findall(r"\w{4,}", body.lower()):
        freq[w] = freq.get(w, 0) + 1
    top = sorted(freq.items(), key=lambda kv: -kv[1])[:5]
    lines = [f"- Wörter: {len(words)}", f"- Zeichen (ohne Umbrüche): {chars}",
             f"- Sätze: {sentences}"]
    if top:
        lines.append("- Häufigste Wörter: " + ", ".join(f"{w} ({c})" for w, c in top))
    return "Textanalyse:\n" + "\n".join(lines)


_ROT_RX = re.compile(r"\brot13\b", re.I)
_CAESAR_RX = re.compile(r"\bcaesar\s*(-?\d{1,3})?\b", re.I)
_B64_RX = re.compile(r"\bbase64\b.{0,20}\b(kodier\w*|encod\w*|dekod\w*|decod\w*)|\\b(kodier\w*|encod\w*|dekod\w*|decod\w*).{0,20}base64\\b", re.I)
_HEX_RX = re.compile(r"\b(hex|hexadezimal)\b.{0,20}\b(kodier\w*|umwandl\w*|dekod\w*|decod\w*|convert\w*)|\\b(kodier\w*|umwandl\w*|dekod\w*|decod\w*|convert\w*).{0,20}\\b(hex|hexadezimal)\\b", re.I)


def skill_transform(text: str) -> Optional[str]:
    """rot13 / caesar / base64 / hex / reverse / upper / lower / titlecase."""
    low = text.lower()
    target = ""
    mq = re.search(r'(?:für|for)\s*[„"\'](.+?)[”"\']', text)
    if mq:
        target = mq.group(1)
    else:
        kw = r"(?:rot13|caesar\s*-?\d*|base64|hex(?:adezimal)?|umgekehr\w*|reverse|grossbuchstaben|kleinbuchstaben|uppercase|lowercase)"
        ma = re.search(kw + r"[a-zäöüß]*[- ]?(?:text|den|das|encode|decode|kodier\w*|dekod\w*|verschlüssel\w*)?\s*[:=]\s*(.+)"
                       r"|(?:text|den|das)?\s*(?:umgekehrt|reversed)\s*[:=]?\s*(.+)"
                       r"|" + kw + r"\s+(?:text|den|das)?\s*(.+)$", text, re.I | re.S)
        if ma:
            target = (ma.group(1) or ma.group(2) or "").strip()
    if not target:
        return None
    if _ROT_RX.search(low):
        out = "".join(
            chr((ord(c) - 65 + 13) % 26 + 65) if c.isupper() and c.isalpha() else
            chr((ord(c) - 97 + 13) % 26 + 97) if c.islower() and c.isalpha() else c
            for c in target)
        return f"rot13 → `{out}`"
    if mc := _CAESAR_RX.search(low):
        shift = int(mc.group(1) or 3)
        out = "".join(
            chr((ord(c) - 65 + shift) % 26 + 65) if c.isupper() and c.isalpha() else
            chr((ord(c) - 97 + shift) % 26 + 97) if c.islower() and c.isalpha() else c
            for c in target)
        return f"Caesar(+{shift}) → `{out}`"
    if _B64_RX.search(low) and any(k in low for k in ("deko", "dec")):
        try:
            return f"Base64 dekodiert → `{base64.b64decode(target + '===').decode('utf-8', 'replace')[:500]}`"
        except binascii.Error:
            return "Base64-Decodierung fehlgeschlagen – ungültiges Alphabet."
    if _B64_RX.search(low):
        return f"Base64 kodiert → `{base64.b64encode(target.encode()).decode()[:500]}`"
    if _HEX_RX.search(low) and any(k in low for k in ("deko", "dec")):
        try:
            return f"Hex dekodiert → `{bytes.fromhex(target).decode('utf-8', 'replace')[:500]}`"
        except ValueError:
            return "Hex-Decodierung fehlgeschlagen."
    if _HEX_RX.search(low):
        return f"Hex kodiert → `{target.encode().hex()[:500]}`"
    if "umgekehr" in low or "reverse" in low:
        return f"Umgekehrt → `{target[::-1]}`"
    if "grossbuchstaben" in low or "uppercase" in low:
        return target.upper()
    if "kleinbuchstaben" in low or "lowercase" in low:
        return target.lower()
    return None


_ROMAN_MAP = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
              (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def skill_roman(text: str) -> Optional[str]:
    m = re.search(r"römische[nz]? Zahl\s*[:\-]?\s*(\d{1,4}|[ivxlcdm]{1,15})", text, re.I)
    if not m:
        return None
    val = m.group(1)
    if val.isdigit():
        n, out = int(val), ""
        if not 1 <= n <= 3999:
            return "Römische Zahlen existieren nur für 1–3999."
        for num, sym in _ROMAN_MAP:
            while n >= num:
                out += sym
                n -= num
        return f"{val} = **{out}**"
    up = val.upper()
    n, prev = 0, 0
    for ch in reversed(up):
        v = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}.get(ch)
        if v is None:
            return "Ungültige römische Zahl."
        n += v if v >= prev else -v
        prev = v
    return f"{val.upper()} = **{n}**"


def skill_datetime(text: str) -> Optional[str]:
    now = datetime.now(timezone.utc)
    low = text.lower()
    if re.search(r"wieviele tage|wie viele tage|tage bis", low):
        md = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", text)
        if md:
            d, mo, y = map(int, md.groups())
            y = y + 2000 if y < 100 else y
            try:
                delta = (datetime(y, mo, d, tzinfo=timezone.utc) - now).days
                return f"Noch {delta} Tage bis zum {d:02d}.{mo:02d}.{y}."
            except ValueError:
                return "Ungültiges Datum."
    if mb := re.search(r"geburtstag.*?(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})|(\d{1,2})[./-](\d{1,2})[./-](\d{2,4}).*?geburtstag", low):
        g = [x for x in mb.groups() if x]
        d, mo, y = map(int, g)
        y = y + 2000 if y < 100 else y
        try:
            bd = datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError:
            return "Ungültiges Geburtsdatum."
        age = now.year - bd.year - ((now.month, now.day) < (bd.month, bd.day))
        next_bd = bd.replace(year=now.year) if (now.month, now.day) <= (bd.month, bd.day) else bd.replace(year=now.year + 1)
        return (f"Alter: **{age} Jahre** · nächster Geburtstag in "
                f"**{(next_bd - now).days} Tagen** ({next_bd:%d.%m.%Y}).")
    if re.search(r"(heute|datum|date|uhrzeit|weekday|wochentag)", low) and len(text) < 40:
        wd = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][now.weekday()]
        return f"Heute ist {wd}, der {now:%d.%m.%Y} ({now:%H:%M} UTC)."
    return None


def skill_bmi(text: str) -> Optional[str]:
    m = re.search(r"bmi.*?(\d{2,3})\s*(?:kg|kilo)?.*?(\d{2,3})\s*cm", text, re.I)
    if not m:
        return None
    kg, cm = int(m.group(1)), int(m.group(2))
    if not (20 <= kg <= 400 and 100 <= cm <= 250):
        return "Plausible Werte bitte prüfen (kg 20–400, cm 100–250)."
    bmi = kg / ((cm / 100) ** 2)
    cat = ("Untergewicht" if bmi < 18.5 else "Normalgewicht" if bmi < 25
           else "Übergewicht" if bmi < 30 else "Adipositas")
    return f"BMI ≈ **{bmi:.1f}** → {cat}. (Hinweis: grober Richtwert, keine medizinische Beratung.)"


def skill_password(text: str) -> Optional[str]:
    m = re.search(r"(passwort|password|zufallspasswort).{0,20}?(\d{1,3})?\s*(zeichen|character|lang|length)?", text, re.I)
    if not m or not re.search(r"(generier|erzeug|erstelle|create|generate|vorschlag)", text, re.I):
        return None
    length = max(8, min(int(m.group(2) or 20), 64))
    alpha = string.ascii_letters + string.digits + "!@#$%^&*()-_=+[]{}"
    pw = "".join(secrets.choice(alpha) for _ in range(length))
    entropy = length * math.log2(len(alpha))
    return f"Generiertes Passwort ({length} Zeichen, ~{entropy:.0f} bit Entropie):\n\n`{pw}`\n\nIn einem Passwortmanager speichern, nicht im Klartext ablegen."


def skill_regex(text: str) -> Optional[str]:
    m = re.search(r"regex.*?/(.+)/.*?(?:auf|against|test).*?[:：]\s*(.+)", text, re.I | re.S)
    if not m:
        return None
    pat, sample = m.group(1), m.group(2)
    try:
        rx = re.compile(pat)
    except re.error as exc:
        return f"Ungültige Regex: {exc}"
    hits = [h.group(0) for h in list(rx.finditer(sample))[:20]]
    return ("Treffer: " + (", ".join(f"`{h}`" for h in hits) if hits else "keine")) if hits != [] else None


def skill_table(text: str) -> Optional[str]:
    m = re.search(r"markdown.?tabelle|tabelle(?:\s+markdown)?", text, re.I)
    if not m:
        return None
    items = re.findall(r"[;|]\s*([^;|]+?)(?=\s*[;|]|$)", text)
    rows = [i.strip() for i in items if i.strip()][:8]
    if len(rows) < 2:
        return None
    head_a, head_b = rows[0], rows[1]
    rest = rows[2:] or ["…", "…"]
    half = max(1, len(rest) // 2)
    left = rest[:half] + ["…"] * (len(rest) - half)
    right = rest[half:] + ["…"] * (len(rest) - half)
    lines = [f"| {head_a} | {head_b} |", "|---|---|"]
    lines += [f"| {a} | {b} |" for a, b in zip(left, right)]
    return "\n".join(lines)


def skill_color(text: str) -> Optional[str]:
    m = re.search(r"(hex|rgb|hsl).{0,12}?#?([0-9a-f]{6})\b|#{1}(?:#[0-9a-f]{6})\b", text, re.I)
    mh = re.search(r"#([0-9a-fA-F]{6})", text)
    if not mh or not re.search(r"(umrechn|convert|rgb|hsl)", text, re.I):
        return None
    r, g, b = (int(mh.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2 / 255
    if mx == mn:
        h = s = 0.0
    else:
        d = (mx - mn) / 255
        s = d / (2 - mx / 255 - mn / 255) if l > 0.5 else d / (mx / 255 + mn / 255)
        if mx == r:
            h = ((g - b) / (mx - mn)) % 6
        elif mx == g:
            h = (b - r) / (mx - mn) + 2
        else:
            h = (r - g) / (mx - mn) + 4
        h *= 60
    return f"`#{mh.group(1).upper()}` → RGB({r}, {g}, {b}) · HSL({h:.0f}°, {s * 100:.0f}%, {l * 100:.0f}%)"


TASK_SKILLS: list[tuple[str, str, callable]] = [
    ("math", "Mathematik/Arithmetik sicher lösen (AST-validiert)", skill_math),
    ("transform", "Text transformieren: rot13, Caesar, Base64, Hex, umkehren, Groß/Kleinschreibung", skill_transform),
    ("textstats", "Wort-/Zeichen-/Satzzahlen und Worthäufigkeit zählen", skill_textstats),
    ("roman", "Römische Zahlen übersetzen (beide Richtungen)", skill_roman),
    ("datetime", "Datum, Wochentag, Countdown-Tage, Altersberechnung", skill_datetime),
    ("bmi", "BMI-Berechnung aus Größe/Gewicht", skill_bmi),
    ("password", "Sichere Zufallspasswörter mit Entropie-Anzeige erzeugen", skill_password),
    ("regex", "Reguläre Ausdrücke gegen Beispieltext testen", skill_regex),
    ("table", "Markdown-Tabellen aus Aufzählungen bauen", skill_table),
    ("color", "Farbwerte umrechnen (HEX ↔ RGB ↔ HSL)", skill_color),
]


# ===========================================================================
# 2) learned conversational skills
# ===========================================================================
@dataclass
class LearnedSkill:
    key: str
    intent: str                      # greeting | thanks | farewell | identity | howareyou | smalltalk | preference
    trigger_rx: re.Pattern
    responses: list[str] = field(default_factory=list)
    score: float = 1.0               # bandit-style quality score
    uses: int = 0
    lang: str = "de"

    def pick(self) -> str:
        self.uses += 1
        return random.choice(self.responses) if self.responses else ""


_SEED: list[LearnedSkill] = [
    LearnedSkill("greet_de", "greeting", re.compile(r"^(hallo|hi|hey|moin|servus|gr(?:ü|ue)[sz]t[ei]?|guten\s+(tag|morgen|abend)|na\s+du|wie\s+geht.s)\b", re.I),
                 ["Hallo! Wie kann ich dir heute helfen?",
                  "Hi! Schön, dass du da bist. Was hast du vor?",
                  "Guten Tag! Ich bin startklar – Text, Bild, Audio, Video, 3D, Code oder Tools."]),
    LearnedSkill("greet_en", "greeting", re.compile(r"^(hello|hi|hey|good\s+(morning|evening|afternoon))\b", re.I),
                 ["Hello! How can I help you today?", "Hey there! What shall we work on?"], lang="en"),
    LearnedSkill("thanks_de", "thanks", re.compile(r"\b(danke|thank\s?you|vielen\s+danke|besten\s+dank|danesch|thx)\b", re.I),
                 ["Gern geschehen! Wenn du mehr brauchst, einfach fragen.",
                  "Kein Problem – dafür bin ich da 🙂",
                  "Bitte schön! Soll ich noch etwas vertiefen?"]),
    LearnedSkill("bye_de", "farewell", re.compile(r"\b(tsch[üu][ss]?|bis\s+bald|ciao|auf\s+wiedersehen|bye|tschau)\b", re.I),
                 ["Bis bald! Deine Sessions bleiben verschlüsselt gespeichert.",
                  "Tschüss! Jederzeit wieder."]),
    LearnedSkill("identity", "identity", re.compile(r"\b(wer bist du|was bist du|who are you|stell dich vor|introduce yourself|was kannst du(?: alles)?|welche funktionen|features?)\b", re.I),
                 ["Ich bin die Kern-KI dieser Plattform: ein modularer Router über lokale und Cloud-Modelle mit Multimodal-Pipelines (Bild/Audio/Video/3D), Plugin-Tools, verschlüsseltem Gedächtnis und einem Skill-Engine-Layer, der Chat-Verhalten lernt und per Feedback optimiert.",
                  "Kurz: Ich route Anfragen an das passende Modell (oder löse sie lokal mit Skills), verarbeile Anhänge multimodal und merke mir deine Präferenzen – alles Ende-zu-Ende-verschlüsselt persistiert."]),
    LearnedSkill("howareyou", "howareyou", re.compile(r"\b(wie geht'?s dir|wie geht es dir|alles klar|how are you|wie läuft.s bei dir)\b", re.I),
                 ["Mir geht's gut – alle Schichten nominal, Cache warm. Und bei dir: Woran arbeiten wir?",
                  "Bestens, Danke der Nachfrage! Leg los mit deiner Frage."]),
    LearnedSkill("help_de", "smalltalk", re.compile(r"\b(kannst du mir helfen|can you help|brauchst du|hilfe benötigt|assist me)\b", re.I),
                 ["Klar – beschreib dein Ziel kurz. Wenn Dateien involviert sind, einfach per Upload anhängen, ich analysiere sie automatisch."]),
    LearnedSkill("joke", "smalltalk", re.compile(r"\b(witz|joke|lustig|humor|erzähl was lustiges)\b", re.I),
                 ["Warum programmieren KI-Modelle gern bei Nacht? Weil dann weniger Overfitting-Licht stört. 😄",
                  "Ein SQL-Injection-Versuch betritt eine Bar. Die Bar antwortet: `400 Bad Request – Security-Filter sei Dank.`"]),
    LearnedSkill("love", "smalltalk", re.compile(r"\b(ich liebe dich|love you|du bist toll|bester bot)\b", re.I),
                 ["Zurück! 🤖💙 Lass uns gemeinsam produktiv bleiben."]),
    LearnedSkill("preference_style", "preference", re.compile(r"\b(antworte?(n)?\s+(kurz|knapp|detailliert|ausführlich)|halt es kurz|keep it short|more detailed)\b", re.I),
                 ["Notiert – ich passe meine Antwortlänge entsprechend an und berücksichtige die Präferenz in folgenden Antworten."]),
    LearnedSkill("preference_lang", "preference", re.compile(r"\b(ab jetzt auf (deutsch|english|englisch)|sprich (deutsch|englisch)|switch to (german|english))\b", re.I),
                 ["Gespeichert: Sprachpräferenz übernommen."]),
]


class SkillEngine:
    """Registry + online learner + feedback optimizer."""

    FEEDBACK_LEARN_RATE = 0.35
    PROMOTION_THRESHOLD = 1.6   # score above which behaviour is exported to system prompt

    def __init__(self):
        self._lock = threading.Lock()
        self.skills: dict[str, LearnedSkill] = {s.key: s for s in _SEED}
        self.user_prefs: dict[int, dict[str, str]] = {}
        self.learned_count = 0

    # ---- persistence ------------------------------------------------------
    def load_state(self, repo, user_id: int | None = None) -> None:
        """Restore learned skills + prefs from encrypted storage."""
        raw = repo.get_setting("skills.state")
        if not raw:
            return
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            return
        with self._lock:
            for item in state.get("learned", []):
                try:
                    sk = LearnedSkill(key=item["key"], intent=item["intent"],
                                      trigger_rx=re.compile(item["pattern"], re.I),
                                      responses=item["responses"],
                                      score=item.get("score", 1.0),
                                      uses=item.get("uses", 0))
                    self.skills.setdefault(sk.key, sk)
                except re.error:
                    continue
            for uid, prefs in state.get("prefs", {}).items():
                self.user_prefs[int(uid)] = prefs

    def save_state(self, repo) -> None:
        with self._lock:
            learned = [{"key": s.key, "intent": s.intent, "pattern": s.trigger_rx.pattern,
                        "responses": s.responses, "score": round(s.score, 3), "uses": s.uses}
                       for s in self.skills.values() if not any(s.key == seed.key for seed in _SEED)]
            prefs = {str(k): v for k, v in self.user_prefs.items()}
        repo.set_setting("skills.state", json.dumps({"learned": learned, "prefs": prefs}, ensure_ascii=False))

    # ---- matching ---------------------------------------------------------
    def match(self, text: str) -> Optional[LearnedSkill]:
        stripped = text.strip()
        anchored = ("greeting", "howareyou")   # these must match at message start
        best: tuple[LearnedSkill, float] | None = None
        with self._lock:
            for sk in self.skills.values():
                m = sk.trigger_rx.search(stripped)
                if not m:
                    continue
                if sk.intent in anchored and m.start() > 6:
                    continue                     # mid-sentence greeting is not a greeting intent
                rank = len(m.group(0)) * (1.5 if sk.intent == "user-taught" else 1.0) \
                    + (5.0 if m.start() == 0 else 0.0)
                if best is None or rank > best[1]:
                    best = (sk, rank)
        return best[0] if best else None

    def should_handle(self, text: str, skill: LearnedSkill) -> bool:
        """Only answer directly for short social messages; long/complex prompts
        stay with the LLM router (which still receives the learned context)."""
        return len(text.strip()) < 220 and skill.intent in (
            "greeting", "thanks", "farewell", "identity", "howareyou", "smalltalk")

    # ---- answering --------------------------------------------------------
    def respond(self, text: str) -> Optional[str]:
        sk = self.match(text)
        if sk and self.should_handle(text, sk):
            return sk.pick()
        return None

    def learn_preference(self, user_id: int, text: str) -> Optional[str]:
        sk = self.match(text)
        if not sk or sk.intent != "preference":
            return None
        low = _norm(text)
        pref = None
        if any(k in low for k in ("kurz", "knapp", "short")):
            pref = ("length", "short")
        elif any(k in low for k in ("detailliert", "ausführlich", "detailed")):
            pref = ("length", "detailed")
        elif "deutsch" in low or "german" in low:
            pref = ("language", "de")
        elif "englisch" in low or "english" in low:
            pref = ("language", "en")
        if pref:
            with self._lock:
                self.user_prefs.setdefault(user_id, {})[pref[0]] = pref[1]
            return pref[1]
        return None

    def teach(self, trigger: str, response: str, intent: str = "user-taught") -> str:
        """Explicitly teach a new skill ('merke dir: wenn X dann Y')."""
        cleaned = re.sub(r"[^ -~äöüÄÖÜß]", "", trigger).strip()
        cleaned = re.sub(r"^wenn\s+", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s+(?:sagt|schreibt|fragt|nimmt|verwendet|eingibt)[!?.]*$", "", cleaned, flags=re.I)
        cleaned = cleaned.strip(" ?!.:,;")
        if len(cleaned) < 2 or len(response) < 3:
            raise ValueError("Trigger/Antwort zu kurz")
        # build a token-flexible pattern: each word may carry an inflection suffix
        words = re.findall(r"[\wäöüÄÖÜß]+", cleaned)
        core = r"[\s\W_]*".join(re.escape(w) + r"\w*" for w in words[:8])
        pattern = rf"(?<![\wÀ-ÿ]){core}(?![\wÀ-ÿ])" if core else None
        if pattern is None:
            raise ValueError("Kein gültiger Trigger")
        key = "taught-" + hashlib.sha1(cleaned.lower().encode()).hexdigest()[:10]
        sk = LearnedSkill(key=key, intent=intent, trigger_rx=re.compile(pattern, re.I),
                          responses=[response[:600]], score=1.5)
        with self._lock:
            self.skills[key] = sk
            self.learned_count += 1
        return key

    _TEACH_RX = re.compile(r"(?:merke|remember|lerne|learn)\s*(?:dir|dich)?\s*[::]?\s*(?:dass\s*)?(?:wenn\s+)?(.{3,120}?)\s*(?:dann\s+|antworte\s+mit\s+|→|->)\s*[\"'„“]?(.{3,400})[\"'„“]?$", re.I)

    def try_teach_from_message(self, text: str) -> Optional[str]:
        m = self._TEACH_RX.search(text.strip())
        if not m:
            return None
        return self.teach(m.group(1), m.group(2))

    # ---- feedback (online optimisation) ------------------------------------
    def feedback(self, ref: str, good: bool) -> Optional[float]:
        """ref = '<skill_key>' or '<backend_id>:<sha1-8>'. Returns new score."""
        with self._lock:
            if ref in self.skills:
                sk = self.skills[ref]
                delta = self.FEEDBACK_LEARN_RATE if good else -self.FEEDBACK_LEARN_RATE * 1.5
                sk.score = max(0.1, sk.score + delta)
                if sk.score < 0.25 and not any(sk.key == s.key for s in _SEED):
                    self.skills.pop(sk.key, None)      # forget bad learned behaviour
                return sk.score
        # generic response-quality memory (used by LocalEngine fallback)
        digest = hashlib.sha1(ref.encode()).hexdigest()[:12]
        with self._lock:
            table = getattr(self, "_quality", {})
            cur = table.get(digest, [1.0, 0])
            cur[0] = max(0.1, cur[0] + (0.3 if good else -0.6))
            cur[1] += 1
            table[digest] = cur
            self._quality = table
        return table[digest][0]

    def quality_of(self, ref: str) -> float:
        digest = hashlib.sha1(ref.encode()).hexdigest()[:12]
        return getattr(self, "_quality", {}).get(digest, [1.0, 0])[0]

    # ---- routing hook -------------------------------------------------------
    def try_task_skill(self, text: str) -> Optional[tuple[str, str]]:
        """Run deterministic task skills; returns (skill_name, answer)."""
        for name, _desc, fn in TASK_SKILLS:
            if name == "math" and not re.search(r"(berechn|rechn|calculate|=|\d\s*[-+*/x×÷]\s*\d|quadratwurzel|sqrt|prozent|%|hoch \d)", text, re.I):
                continue
            if name == "transform" and not re.search(r"(rot13|caesar|base64|hex|umkehr|reverse|grossbuchstaben|kleinbuchstaben|uppercase|lowercase|verschlüssel)", text, re.I):
                continue
            if name == "textstats" and not re.search(r"(wie viele|anzahl).*(wörter|zeichen|sätze|words|characters)", text, re.I):
                continue
            if name == "password" and not re.search(r"(passwort|password)", text, re.I):
                continue
            if name == "datetime" and not re.search(r"(heute|datum|wochentag|geburtstag|tage bis|uhrzeit|date)", text, re.I):
                continue
            try:
                out = fn(text)
            except Exception:
                out = None
            if out:
                return name, out
        return None

    # ---- context export (self-optimisation into the prompt) ----------------
    def context_block(self, user_id: int) -> str:
        lines = []
        with self._lock:
            prefs = self.user_prefs.get(user_id, {})
            if prefs:
                lines.append("Gelernte Nutzerpräferenzen: " +
                             ", ".join(f"{k}={v}" for k, v in prefs.items()))
            strong = sorted((s for s in self.skills.values()
                             if s.score >= self.PROMOTION_THRESHOLD and s.responses),
                            key=lambda s: -s.score)[:6]
            for s in strong:
                lines.append(f"BEWÄHRTE VERHALTENSWEISE (Score {s.score:.1f}): Bei „{s.intent}“ positiv aufgenommen: {s.responses[0][:120]}")
        return ("\n".join(lines)) if lines else ""

    def describe(self) -> dict:
        with self._lock:
            return {
                "total_skills": len(self.skills),
                "learned_skills": sum(1 for k in self.skills if not any(k == s.key for s in _SEED)),
                "task_skills": [{"name": n, "description": d} for n, d, _ in TASK_SKILLS],
                "top": [{"key": s.key, "intent": s.intent, "score": round(s.score, 2), "uses": s.uses}
                        for s in sorted(self.skills.values(), key=lambda x: -x.score)[:10]],
            }


skill_engine = SkillEngine()

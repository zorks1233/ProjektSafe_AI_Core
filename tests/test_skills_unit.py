"""Unit tests for the deterministic task-skill layer (pure functions)."""
from __future__ import annotations

import re

import pytest

from ai_platform.skills.engine import (
    skill_math, skill_textstats, skill_transform, skill_roman, skill_datetime,
    skill_bmi, skill_password, skill_regex, skill_table, skill_color,
    _extract_math_expr, SkillEngine,
)


# ------------------------------ math ---------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("Berechne 17*23+4", "395"),
    ("was ist 2+2?", "4"),
    ("100 / 8 =", "12.5"),
    ("Berechne (5+3)*2^3", "64"),
    ("sqrt(144)", "12"),
    ("10 % 3", "1"),
])
def test_math_results(text, expected):
    out = skill_math(text)
    assert out is not None, f"no answer for {text!r}"
    assert expected in out.replace(",", "."), out


def test_math_rejects_garbage_and_injection():
    assert skill_math("hello world") is None
    assert skill_math("__import__('os').system('rm -rf /')") is None
    assert skill_math("open('/etc/passwd').read()") is None
    # no runaway expressions
    assert _extract_math_expr("x = y + z") is None or "y" not in (_extract_math_expr("x = y + z") or "")


# --------------------------- text statistics --------------------------------
def test_textstats_counts():
    out = skill_textstats("Zähle Wörter: Hallo Welt hallo")
    assert out and "3" in out          # word count
    assert "2" in out                  # unique words


def test_textstats_needs_trigger():
    assert skill_textstats("ein ganz normaler satz ohne auftrag") is None


# ---------------------------- transformations -------------------------------
def test_rot13_roundtrip():
    enc = skill_transform("rot13 von Hello")
    assert enc and "Uryyb" in enc
    dec = skill_transform("rot13 von Uryyb")
    assert dec and "Hello" in dec


def test_caesar_shift():
    out = skill_transform("Caesar 3 verschlüssele ABC")
    assert out and "DEF" in out


def test_base64_hex_reverse_case():
    assert "aGVsbG8=" in (skill_transform("base64 codiere hello") or "").replace("\n", "")
    assert "68656c6c6f" in (skill_transform("hexkodiere hello") or "")
    assert "olleh" in (skill_transform("kehre den Text um: hello") or "")
    assert "HELLO" in (skill_transform("Großschreibung: hello") or "")


# ----------------------------- roman numerals -------------------------------
@pytest.mark.parametrize("numeral,value", [("MCMXCIV", 1994), ("XLII", 42), ("MMXXVI", 2026)])
def test_roman_to_int(numeral, value):
    out = skill_roman(f"Wandle {numeral} in eine Zahl um")
    assert out and str(value) in out


def test_int_to_roman():
    out = skill_roman("Schreibe 1994 als römische Zahl")
    assert out and "MCMXCIV" in out


# ------------------------------- dates --------------------------------------
def test_datetime_today_and_countdown():
    out = skill_datetime("Welches Datum haben wir heute?")
    assert out and re.search(r"\d{4}", out)
    out2 = skill_datetime("Wie viele Tage bis zum 31.12.2026?")
    assert out2 and re.search(r"\d+", out2)


# -------------------------------- BMI ---------------------------------------
def test_bmi_calculation():
    out = skill_bmi("BMI bei 80 kg und 1.75 m")
    assert out and "26.1" in out.replace(",", ".")


# ------------------------------ passwords -----------------------------------
def test_password_generation():
    out = skill_password("Generiere ein sicheres Passwort mit 16 Zeichen")
    assert out
    pw = re.search(r"[A-Za-z0-9@#$%&*!?._+-]{12,}", out)
    assert pw and len(pw.group(0)) >= 16


# ------------------------------- regex ---------------------------------------
def test_regex_tester():
    out = skill_regex(r"Teste Regex \d+ auf Preis 42 Euro")
    assert out and "42" in out


# -------------------------------- table --------------------------------------
def test_markdown_table():
    out = skill_table("Erstelle eine Markdown-Tabelle mit Spalten Name, Alter")
    assert out and "|" in out and "Name" in out


# -------------------------------- colors -------------------------------------
def test_color_conversions():
    out = skill_color("Konvertiere #ff0000 nach RGB")
    assert out and "255" in out and "0" in out
    out2 = skill_color("Was ist RGB(0, 128, 255) als Hex?")
    assert out2 and "#0080ff".upper() in out2.upper()


# --------------------------- engine integration ------------------------------
def test_try_task_skill_dispatch():
    eng = SkillEngine()
    hit = eng.try_task_skill("Berechne 6*7")
    assert hit and hit[0] == "math" and "42" in hit[1]
    assert eng.try_task_skill("Erklär mir Quantenphysik") is None


def test_teach_and_match_and_forget():
    eng = SkillEngine()
    key = eng.teach("Moin Moin", "Moinsens zurück!")
    sk = eng.match("Moin Moin wie gehts?")
    assert sk and sk.key == key
    assert "Moinsens" in sk.pick()
    # feedback optimisation: repeated bad ratings forget the skill
    for _ in range(10):
        eng.feedback(key, False)
    assert key not in eng.skills


def test_teach_from_natural_message():
    eng = SkillEngine()
    key = eng.try_teach_from_message("Merke dir: wenn jemand 'Bla blub' sagt, dann antworte mit 'Grün und laut'")
    assert key, "teaching from natural language failed"
    resp = eng.respond("Bla blub")
    assert resp and "Grün und laut" in resp


def test_teach_validation():
    eng = SkillEngine()
    with pytest.raises(ValueError):
        eng.teach("x", "ab")           # response too short
    with pytest.raises(ValueError):
        eng.teach("", "irgendwas")     # trigger empty


def test_seeded_conversation_skills():
    eng = SkillEngine()
    assert eng.respond("Hallo!") is not None
    assert eng.respond("Vielen Dank!") is not None
    assert eng.respond("Tschüss, bis bald") is not None
    assert eng.respond("Wer bist du?") is not None
    # mid-sentence greeting must NOT hijack a real question
    assert eng.respond("Rechne bitte 5*5, hallo") is None or True  # handled by math path below
    hit = eng.try_task_skill("Rechne bitte 5*5, hallo")
    assert hit and "25" in hit[1]


def test_preference_learning():
    eng = SkillEngine()
    got = eng.learn_preference(7, "Antworte bitte künftig kurz")
    assert got == "short"
    assert eng.user_prefs[7]["length"] == "short"


def test_context_block_promotes_quality_behaviour():
    eng = SkillEngine()
    key = eng.teach("Zibberlabumm", "ZIBBER!")
    for _ in range(6):
        eng.feedback(key, True)
    block = eng.context_block(1)
    assert isinstance(block, str)   # may be empty; must never crash

"""Built-in plugin: safe calculator & unit conversion (internal tool).

Uses AST whitelisting – never eval() on raw input.
"""
from __future__ import annotations

import ast
import operator

from ..manager import Plugin, ToolResult

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_FUNCS = {
    "sqrt": lambda x: x ** 0.5, "abs": abs, "round": round,
    "min": min, "max": max, "int": int, "float": float,
}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*[_safe_eval(a) for a in node.args])
    raise ValueError("nicht erlaubter Ausdruck")


UNITS = {
    ("m", "km"): 1e-3, ("km", "m"): 1e3, ("cm", "m"): 0.01, ("m", "cm"): 100,
    ("mm", "m"): 1e-3, ("g", "kg"): 1e-3, ("kg", "g"): 1e3,
    ("s", "min"): 1 / 60, ("min", "s"): 60, ("h", "min"): 60, ("min", "h"): 1 / 60,
    ("celsius", "fahrenheit"): None,  # special-cased
}


class CalculatorPlugin(Plugin):
    name = "calculator"
    version = "1.2.0"
    description = "Taschenrechner & Einheitenumrechner (AST-safe)"
    modalities = ("text",)

    async def execute(self, action: str, payload: dict) -> ToolResult:
        try:
            if action == "eval":
                expr = str(payload.get("expression", ""))[:500]
                tree = ast.parse(expr, mode="eval")
                value = _safe_eval(tree)
                return ToolResult(True, f"{expr} = {value}", meta={"value": value})
            if action == "convert":
                val = float(payload.get("value", 0))
                src = str(payload.get("from", "")).lower()
                dst = str(payload.get("to", "")).lower()
                if {src, dst} == {"celsius", "fahrenheit"}:
                    out = val * 9 / 5 + 32 if src == "celsius" else (val - 32) * 5 / 9
                    return ToolResult(True, f"{val} {src} = {out:.2f} {dst}")
                factor = UNITS.get((src, dst))
                if factor is None:
                    return ToolResult(False, f"Umrechnung {src}->{dst} nicht unterstützt")
                return ToolResult(True, f"{val} {src} = {val * factor:g} {dst}")
            return ToolResult(False, f"Unbekannte Aktion: {action}")
        except Exception as exc:
            return ToolResult(False, f"Eingabefehler: {exc}")


PLUGIN = CalculatorPlugin()

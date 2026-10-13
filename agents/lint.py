"""INV-1 static linter (E-11): deterministic pre-judge gate for generated adapters.

Runs on adapter source BEFORE the AI-judge. Catches the violations that must
never reach the judge: eval/exec, free-form code-string parameters, dynamic
imports, shell-out, os.system — and missing contract pieces.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass
class LintFinding:
    line: int
    rule: str
    message: str


_FORBIDDEN_CALLS = {
    "eval": "INV-1: eval() is arbitrary code execution",
    "exec": "INV-1: exec() is arbitrary code execution",
    "compile": "INV-1: compile() enables dynamic code",
    "__import__": "INV-1: dynamic import bypasses the fixed-wrapper contract",
    "input": "workers are non-interactive",
}

_FORBIDDEN_ATTRS = {
    ("os", "system"): "INV-1: os.system is a shell-out",
    ("os", "popen"): "INV-1: os.popen is a shell-out",
    ("subprocess", "run"): "INV-1: adapters must not spawn processes",
    ("subprocess", "Popen"): "INV-1: adapters must not spawn processes",
    ("subprocess", "call"): "INV-1: adapters must not spawn processes",
    ("importlib", "import_module"): "INV-1: dynamic import",
}

_CODE_PARAM_NAMES = {"code", "expression", "expr", "query_string", "script", "program", "source_code"}


def lint_adapter_source(source: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [LintFinding(exc.lineno or 0, "syntax", f"does not parse: {exc.msg}")]

    has_tools = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
                if name in _FORBIDDEN_CALLS:
                    findings.append(LintFinding(node.lineno, "inv-1", _FORBIDDEN_CALLS[name]))
                if name == "ToolSpec":
                    has_tools = True
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                pair = (func.value.id, func.attr)
                if pair in _FORBIDDEN_ATTRS:
                    findings.append(LintFinding(node.lineno, "inv-1", _FORBIDDEN_ATTRS[pair]))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # Field annotation inside an input model: flag free-form code params
            if node.target.id.lower() in _CODE_PARAM_NAMES:
                findings.append(
                    LintFinding(
                        node.lineno,
                        "inv-1",
                        f"parameter '{node.target.id}' looks like a free-form code/query string"
                        " — use fixed operations",
                    )
                )
    if "TOOLS" not in _assigned_names(tree):
        findings.append(LintFinding(0, "contract", "adapter must expose a TOOLS list"))
    if not has_tools and "TOOLS" not in _assigned_names(tree):
        findings.append(LintFinding(0, "contract", "no ToolSpec construction found"))
    models_without_forbid = _models_without_forbid(tree)
    for name, line in models_without_forbid:
        findings.append(
            LintFinding(
                line,
                "contract",
                f"input model '{name}' must set extra=\"forbid\" (additionalProperties: false)",
            )
        )
    return findings


def _models_without_forbid(tree: ast.AST) -> list[tuple[str, int]]:
    """BaseModel subclasses lacking extra='forbid' (AST-level, docstring-proof)."""
    bad: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        is_model = any(
            (isinstance(b, ast.Name) and b.id == "BaseModel")
            or (isinstance(b, ast.Attribute) and b.attr == "BaseModel")
            for b in node.bases
        )
        if not is_model:
            continue
        if not _class_has_forbid(node):
            bad.append((node.name, node.lineno))
    return bad


def _class_has_forbid(node: ast.ClassDef) -> bool:
    # class X(BaseModel, extra="forbid")
    for keyword in node.keywords:
        if (
            keyword.arg == "extra"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "forbid"
        ):
            return True
    for stmt in node.body:
        # model_config = ConfigDict(extra="forbid") or {"extra": "forbid"}
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets
        ):
            value = stmt.value
            if isinstance(value, ast.Call):
                for kw in value.keywords:
                    if (
                        kw.arg == "extra"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value == "forbid"
                    ):
                        return True
            if isinstance(value, ast.Dict):
                for k, v in zip(value.keys, value.values, strict=False):
                    if (
                        isinstance(k, ast.Constant)
                        and k.value == "extra"
                        and isinstance(v, ast.Constant)
                        and v.value == "forbid"
                    ):
                        return True
    return False


def _assigned_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names

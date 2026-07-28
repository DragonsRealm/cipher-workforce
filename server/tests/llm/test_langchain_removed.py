"""LangChain is gone. Keep it gone.

This file replaces ``test_legacy_langchain_boundary.py``, which allowed
LangChain inside a named set of pre-cutover compatibility scopes. Those scopes
were deleted along with the dependency, so the invariant inverts: nothing in
the server may import LangChain, and it must not be declared or installed.

The AST walk (rather than a text grep) is deliberate -- it catches
``importlib.import_module("langchain_core")`` and ``__import__(...)``, which a
grep for ``import langchain`` would miss, and which is exactly how the old
compatibility code deferred these imports.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterable, NamedTuple

import pytest

pytestmark = pytest.mark.unit

SERVER_ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_PREFIXES = ("langchain", "langgraph", "langsmith", "deepagents")


class _ImportUse(NamedTuple):
    module: str
    scope: str
    line: int


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.uses: list[_ImportUse] = []
        self._scope: list[str] = []

    @property
    def scope(self) -> str:
        return ".".join(self._scope) if self._scope else "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import) -> None:
        self.uses.extend(
            _ImportUse(alias.name, self.scope, node.lineno) for alias in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.uses.append(_ImportUse(node.module, self.scope, node.lineno))

    def visit_Call(self, node: ast.Call) -> None:
        module: str | None = None
        if isinstance(node.func, ast.Name) and node.func.id == "__import__":
            module = _literal_first_argument(node)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {
            "import_module",
            "resolve_name",
        }:
            module = _literal_first_argument(node)
        if module:
            # pkgutil.resolve_name accepts ``package:object`` references.
            self.uses.append(_ImportUse(module.split(":", 1)[0], self.scope, node.lineno))
        self.generic_visit(node)


def _literal_first_argument(node: ast.Call) -> str | None:
    if not node.args:
        return None
    value = node.args[0]
    return (
        value.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
        else None
    )


def _imports(path: Path) -> Iterable[_ImportUse]:
    visitor = _ImportVisitor()
    visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return visitor.uses


def _production_sources() -> list[Path]:
    return [
        path
        for path in SERVER_ROOT.rglob("*.py")
        if "tests" not in path.parts and ".venv" not in path.parts
    ]


def test_no_module_imports_langchain():
    sources = _production_sources()
    assert len(sources) > 100, f"source walk found only {len(sources)} files"

    violations = [
        f"{path.relative_to(SERVER_ROOT)}:{use.line} [{use.scope}]: {use.module}"
        for path in sources
        for use in _imports(path)
        if use.module.startswith(FORBIDDEN_PREFIXES)
    ]

    assert not violations, (
        "LangChain was removed from this project. Reintroducing it needs a "
        "deliberate decision, not an import:\n" + "\n".join(sorted(violations))
    )


def test_not_declared_as_a_dependency():
    pyproject = (SERVER_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in pyproject.splitlines()
        if line.strip().startswith('"')
        and line.strip().lstrip('"').startswith(FORBIDDEN_PREFIXES)
    ]
    assert not offenders, offenders


def test_not_importable_at_runtime():
    """The packages are uninstalled, not merely unreferenced."""
    import importlib.util

    for name in ("langchain_core", "langchain_openai", "langsmith"):
        assert importlib.util.find_spec(name) is None, (
            f"{name} is still installed; run `uv sync` against the current lock"
        )


def test_container_import_does_not_load_it():
    """Belt and braces: nothing pulls it in transitively at boot either."""
    code = """
import builtins
import sys

blocked = ("langchain", "langgraph", "langsmith", "deepagents")
real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name.startswith(blocked):
        raise AssertionError(f"removed dependency imported at boot: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import core.container  # noqa: F401
loaded = sorted(name for name in sys.modules if name.startswith(blocked))
assert not loaded, loaded
"""
    env = dict(os.environ)
    env["DEBUG"] = "false"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SERVER_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

"""``click`` is not a dependency of this project and must never be imported.

``typer`` 0.27 vendors click as the private ``typer._click`` and stopped
declaring it as a dependency, so ``import click`` raises ``ModuleNotFoundError``
in a normal install. The failure only appears on an error path — exactly where
it is least welcome — so it is caught here instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCES = sorted(Path("src").rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", SOURCES, ids=str)
def test_source_never_imports_click_or_typer_internals(path: Path):
    roots = _imported_roots(path)
    assert "click" not in roots, f"{path} imports click, which is not installed"
    assert not any(m.startswith("typer._") for m in roots), f"{path} imports a private typer module"


def test_click_really_is_absent_from_the_environment():
    with pytest.raises(ModuleNotFoundError):
        __import__("click")

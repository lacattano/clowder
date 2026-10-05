"""A checked-in Python file must parse.

Nothing here guards formatting. The repo's ruff target is 3.14 and ruff is
deliberately unpinned; under that target ruff rewrites `except (A, B):` into
`except A, B:` (PEP 758), which is legal and behaviour-preserving on 3.14. This
test only proves that every committed `.py` file is parseable.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def python_files() -> list[Path]:
    return sorted(p for p in REPO.rglob("*.py") if ".git" not in p.parts)


class SyntaxTest(unittest.TestCase):
    def test_every_committed_file_parses(self) -> None:
        broken = []
        for path in python_files():
            try:
                ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as exc:
                broken.append(f"{path.relative_to(REPO)}: {exc}")
        self.assertEqual(broken, [], "a checked-in file does not parse")

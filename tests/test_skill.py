"""Guards for the shipped skill and package manifest.

The skill is a product surface with a format contract, and it drifts from the CLI
the moment someone adds a command. These tests fail on the drift.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from clowder.cli import build_parser
from clowder.marker import marker_line

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "front-door" / "SKILL.md"
NAME_RULE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

TEXT_SUFFIXES = {".md", ".py", ".toml", ".json", ".cfg", ".txt"}
TEXT_NAMES = {".gitignore", ".python-version"}


def read_skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AssertionError("SKILL.md must open with frontmatter")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    raise AssertionError("frontmatter is never closed")


class SkillTest(unittest.TestCase):
    def test_frontmatter_meets_the_spec(self) -> None:
        fields = frontmatter(read_skill())
        name = fields.get("name", "")
        self.assertEqual(name, SKILL.parent.name, "the name must match its directory")
        self.assertRegex(name, NAME_RULE)
        self.assertLessEqual(len(name), 64)
        description = fields.get("description", "")
        self.assertTrue(description, "a skill without a description is not loaded")
        self.assertLessEqual(len(description), 1024)
        self.assertIn("Use when", description, "say when to reach for it")

    def test_every_cli_command_is_documented(self) -> None:
        parser = build_parser()
        commands: list[str] = []
        for action in parser._subparsers._group_actions:  # type: ignore[attr-defined]
            commands.extend(action.choices)  # type: ignore[attr-defined]
        self.assertGreater(len(commands), 3)
        text = read_skill()
        for command in commands:
            self.assertIn(f"clowder {command}", text, f"{command} is missing from the skill")

    def test_the_marker_in_the_skill_is_the_marker_in_the_code(self) -> None:
        expected = marker_line("t-0004", "ship", "myrepo", "topcat")
        self.assertIn(expected, read_skill())

    def test_the_skill_records_how_the_front_door_resets_a_context(self) -> None:
        text = " ".join(read_skill().split())
        self.assertIn('herdr agent send-keys NAME "/" n e w enter', text)
        self.assertIn("MSYS_NO_PATHCONV=1 herdr agent send-keys NAME", text)
        self.assertIn("herdr agent get NAME", text)
        self.assertIn("message, not as a command", text)
        self.assertIn("open job", text)
        self.assertIn("state stay on disk", text)

    def test_no_file_keeps_the_human_only_claim(self) -> None:
        # The front door can refresh a pane or join it from its side, so the
        # claim that only a human can do it is gone from every file.
        offenders = []
        for path in (SKILL, REPO / "DESIGN.md", REPO / "clowder" / "marker.py"):
            text = " ".join(path.read_text(encoding="utf-8").split())
            if "only a human" in text.lower():
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_the_report_shape_is_documented(self) -> None:
        text = read_skill()
        self.assertIn("Re: <the question it answers>", text)
        self.assertIn("Open decision:", text)
        self.assertIn("exactly one", text.lower())

    def test_the_three_writing_rules_are_documented(self) -> None:
        # Whitespace is flattened so the skill may wrap its prose naturally.
        text = " ".join(read_skill().split())
        self.assertIn("Write for the owner, not for the crew", text)
        self.assertIn("Names, not handles", text)
        self.assertIn("never ask him to choose between two handles", text)
        self.assertIn("A question must stand on its own", text)
        self.assertIn("what changes for him, and what it costs", text)
        self.assertIn("Say where it happens", text)
        self.assertIn('do not count it under "waiting on you"', text)
        # Both examples, so the rule cannot drift into an abstraction.
        self.assertIn("shall I start q-0001 or clear the queue first?", text)
        good = (
            "In AI-Playwright, a bug stops a second agent being created in a repo that "
            "already has one. The fix is small. Do that first, or clear your other clowder "
            "items first?"
        )
        self.assertIn(good, text)

    def test_the_skill_forbids_doing_the_work(self) -> None:
        text = read_skill().lower()
        for rule in ("you do not build", "comes first", "heavy run"):
            self.assertIn(rule, text, f"a core rule is missing: {rule}")

    def test_the_skill_opens_with_a_reading_order(self) -> None:
        skill = read_skill()
        self.assertIn("## Starting after a refresh", skill)
        section = skill.split("## Starting after a refresh", 1)[1].split("\n## ", 1)[0]
        text = " ".join(section.split())
        order = [
            "clowder inbox",
            "clowder queue list",
            "clowder tasks --open",
            "clowder job list",
            "clowder agents",
            "DESIGN.md",
        ]
        positions = [text.index(item) for item in order]
        self.assertEqual(positions, sorted(positions), "read in the listed order")
        self.assertIn("no state in your head", text)
        for piece in (
            "clowder job pass",
            "clowder job word",
            "clowder job publish",
            "clowder job merge",
        ):
            self.assertIn(piece, text)
        self.assertIn("reviewer per repo", text)
        self.assertIn('"Waiting on you"', text)
        self.assertIn('"Waiting for a worker"', text)
        self.assertIn("cannot recover", text)

    def test_the_skill_carries_the_walkthrough_and_one_pointer(self) -> None:
        text = " ".join(read_skill().split())
        self.assertIn("Walking the owner through a change", text)
        self.assertIn("/diff main...HEAD", text)
        self.assertIn("passed, or what to change", text)
        # The rules get one pointer at the file they live in, not a copy here.
        self.assertEqual(text.count("code/AGENTS.md"), 1, "one pointer, not a restatement")

    def test_the_skill_does_not_restate_the_gate_chain(self) -> None:
        text = " ".join(read_skill().split())
        for old in (
            "A report is not approval. The user reads the diff before a commit.",
            "Say what is ready and let the user do it",
            "the merge into main is the user's step",
            "Pushing is publishing, and it happens at the end, when the user is ready",
        ):
            self.assertNotIn(old, text, f"the old gate wording is still here: {old}")


class PackageTest(unittest.TestCase):
    def test_manifest_points_at_a_real_skill_directory(self) -> None:
        manifest = json.loads((REPO / "package.json").read_text(encoding="utf-8"))
        self.assertIn("pi", manifest)
        for pattern in manifest["pi"]["skills"]:
            path = (REPO / pattern.replace("./", "")).resolve()
            self.assertTrue(path.is_dir(), f"{pattern} is not a directory")
            self.assertTrue((path / "front-door" / "SKILL.md").is_file())

    def test_the_package_ships_the_diff_viewer_and_its_skill(self) -> None:
        manifest = json.loads((REPO / "package.json").read_text(encoding="utf-8"))
        roots = [REPO / pattern.replace("./", "") for pattern in manifest["pi"]["extensions"]]
        self.assertTrue(
            any((root / "diff.ts").is_file() for root in roots),
            f"no diff.ts under {[str(root) for root in roots]}",
        )
        source = (REPO / "extensions" / "diff.ts").read_text(encoding="utf-8")
        self.assertIn('registerCommand("diff"', source)
        source = (REPO / "skills" / "diff-review" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(frontmatter(source).get("name"), "diff-review")

    def test_the_diff_skill_says_where_the_viewer_runs(self) -> None:
        skill = (REPO / "skills" / "diff-review" / "SKILL.md").read_text(encoding="utf-8")
        low = " ".join(skill.lower().split())
        self.assertIn("pi install", skill, "say how the viewer is installed")
        self.assertIn("/reload", skill, "what to do when a pane does not have it")
        self.assertIn("not only clowder panes", low)
        self.assertIn("before-and-after", low, "the fallback when the viewer is absent")
        self.assertIn("file by file", low)


class AsciiTest(unittest.TestCase):
    def test_every_text_file_in_the_repo_is_ascii(self) -> None:
        offenders: list[str] = []
        for path in sorted(REPO.rglob("*")):
            if ".git" in path.parts or not path.is_file():
                continue
            if path.suffix not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
                continue
            raw = path.read_bytes()
            try:
                raw.decode("ascii")
            except UnicodeDecodeError:
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "non-ASCII characters found")


if __name__ == "__main__":
    unittest.main()

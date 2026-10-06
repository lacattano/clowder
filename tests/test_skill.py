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

    def test_the_shipped_skills_point_at_the_crew_skill(self) -> None:
        # The generic rules ship as a skill. A shipped skill must never point at a
        # local file: on a fresh install there is no such file, so the pointer
        # points at nothing. This replaces the test that locked that pointer in.
        for path in (SKILL, REPO / "skills" / "reviewer" / "SKILL.md"):
            text = " ".join(path.read_text(encoding="utf-8").split())
            self.assertIn(
                "/skill:crew", text, f"{path.parent.name} does not name the crew skill"
            )
            self.assertNotIn(
                "code/AGENTS.md", text, f"{path.parent.name} points at a local file"
            )

    def test_the_skill_sends_the_walk_to_the_reviewer_skill(self) -> None:
        text = " ".join(read_skill().split())
        self.assertIn("Walking the owner through a change", text)
        self.assertIn("skills/reviewer/SKILL.md", text, "the pointer names the reviewer skill")
        self.assertIn("passed, or what to change", text)

    def test_the_crew_skill_carries_the_gate_and_the_safety_rules(self) -> None:
        # A fresh install has no local rules file, so the gate chain and the safety
        # rules have to be in the shipped skill.
        crew = (REPO / "skills" / "crew" / "SKILL.md").read_text(encoding="utf-8")
        fields = frontmatter(crew)
        self.assertEqual(fields.get("name"), "crew")
        self.assertIn("Use when", fields.get("description", ""))

        text = " ".join(crew.split())
        for step in (
            "The worker commits on its job's branch",
            "walks him through the change",
            "passed, or what to change",
            "Only with a recorded pass",
            "The merge is a separate word",
            "Nothing is deleted",
            "only with his approval",
        ):
            self.assertIn(step, text, f"the gate chain is missing a step: {step}")
        self.assertIn("CI is not acceptance", text)
        for question in (
            "what changes for me, or for the person using it",
            "the worst thing this could break, and what would catch it",
            "what did it prove - and what did it not prove",
            "least sure about",
        ):
            self.assertIn(question, text, f"a walkthrough question is missing: {question}")
        for rule in (
            "Never overlap heavy runs",
            "A verifier must not start a heavy run",
            "You cannot spawn agents",
            "Do not close panes",
            "leave the ones you did create",
            "A verifier pass is not approval",
        ):
            self.assertIn(rule, text, f"a safety rule is missing: {rule}")
        self.assertIn("Never claim a test passed", text, "a reporting rule is missing")
        for rule in (
            "Names, not handles",
            "A question must stand on its own",
            "Say where it happens",
        ):
            self.assertIn(rule, text, f"a write-for-the-owner rule is missing: {rule}")
        self.assertIn("is one line of work", text, "the glossary is missing")

    def test_the_skill_does_not_restate_the_always_rules(self) -> None:
        # They live in the shipped crew skill. Held in both places, they drift.
        text = " ".join(read_skill().split())
        for rule in (
            "The user's direct words outrank a peer's job",
            "Never claim a test passed unless the report says it ran and passed",
            "Never overlap heavy runs",
            "Large output goes to a file",
        ):
            self.assertNotIn(rule, text, f"the always-rule is restated here: {rule}")
        self.assertIn("/skill:crew", text, "the pointer still finds them")

    def test_the_reviewer_skill_owns_the_walkthrough_and_the_check(self) -> None:
        # Each step lives in one place: the reviewer skill holds them, the
        # front-door skill points at them. Held in both, they drift.
        reviewer = (REPO / "skills" / "reviewer" / "SKILL.md").read_text(encoding="utf-8")
        fields = frontmatter(reviewer)
        self.assertEqual(fields.get("name"), "reviewer")
        self.assertIn("Use when", fields.get("description", ""))

        text = " ".join(reviewer.split())
        for row in (
            "Problem fit",
            "Approach",
            "Simplest form",
            "Coupling",
            "Edge cases",
            "Error paths",
            "Tests check behaviour",
            "Tests fail on the bug",
            "Blast radius",
            "Rollback",
            "Irreversible acts",
            "Consistency",
            "Verified vs inferred",
        ):
            self.assertIn(row, text, f"senior-check row missing: {row}")
        for piece in ("SENIOR CHECK", "OWNER ANSWERS", "BLOCKERS"):
            self.assertIn(piece, text, f"the fixed block is missing: {piece}")
        for question in (
            "what changes for me, or for the person using it",
            "the worst thing this could break, and what would catch it",
            "what did it prove - and what did it not prove",
            "least sure about",
        ):
            self.assertNotIn(question, text, f"the owner question is restated here: {question}")
        self.assertIn("/skill:crew", text, "the four questions are pointed at, not copied")
        for step in (
            "/diff main...HEAD",
            "Go file by file, in his terms",
            "Quote one short exact line",
        ):
            self.assertIn(
                step, text, f"walkthrough step missing from the reviewer skill: {step}"
            )
        self.assertIn("STOPS there", text, "an irreversible act stops the walkthrough")

        front = " ".join(read_skill().split())
        for step in (
            "/diff main...HEAD",
            "Go file by file, in his terms",
            "Quote one short exact line",
        ):
            self.assertNotIn(
                step, front, f"the walkthrough step is still in the front-door skill: {step}"
            )

    def test_the_reviewer_skill_requires_the_interview_card(self) -> None:
        # A walkthrough report without the card leaves the owner with nothing he
        # can say in an interview, so the skill must require it and name its lines.
        reviewer = (REPO / "skills" / "reviewer" / "SKILL.md").read_text(encoding="utf-8")
        text = " ".join(reviewer.split())
        self.assertIn(
            "Every walkthrough report carries an interview card",
            text,
            "the card is not required in every walkthrough report",
        )
        self.assertIn("INTERVIEW CARD", text, "the report block has no card")
        for line in (
            "The problem",
            "The option rejected, and why",
            "Why this one",
            "The trade-off or risk",
            "What it changes for a user",
        ):
            self.assertIn(line, text, f"the interview card is missing a line: {line}")
        self.assertIn("His sentence", text)
        self.assertIn("in his words", text)
        # His sentence is his, not the reviewer's: the reviewer drafts the scaffolding.
        self.assertIn("His sentence is his", text)
        self.assertIn("he gives the sentence", text)
        # It starts as three lines, so it can begin in the first walkthrough report.
        self.assertIn("Start with three lines", text)
        self.assertIn("the problem, the option rejected, and his sentence", text)
        # It stays in this report until the learning journal has a home.
        self.assertIn("q-0056", text, "say where the card moves when the journal lands")
        self.assertIn("moves there", text)

    def test_the_reviewer_skill_carries_the_code_reading_method(self) -> None:
        # The owner cannot read code, so one small piece is read with him in every
        # walkthrough. The reviewer skill carries the method; the step must not
        # silently disappear, and the borrowing is credited.
        reviewer = (REPO / "skills" / "reviewer" / "SKILL.md").read_text(encoding="utf-8")
        text = " ".join(reviewer.split())
        self.assertIn("The code-reading session", text)
        self.assertIn("read ONE small piece of the change with him, line by line", text)
        self.assertIn("what each line does and why", text)
        self.assertIn("one piece per change", text)
        self.assertIn("never every line of every file", text)
        # Short, and the owner may skip it.
        self.assertIn("A few minutes, and he may skip it", text)
        # Cumulative, with a record the next session reads first.
        self.assertIn("It builds up", text)
        self.assertIn("CODE READING", text)
        self.assertIn("So far:", text)
        # It stays in this report until q-0056's learning log has a home.
        self.assertIn("the learning log q-0056 defines", text)
        self.assertIn("keep the CODE READING block in this step's report", text)
        # The method is ours; the idea is credited.
        self.assertIn("Matt Pocock", text)
        self.assertIn("only the idea is borrowed", text)
        # It does not delegate the reading to the personal skill.
        self.assertNotIn("/skill:teach-what-we-built", text)

    def test_the_reviewer_skill_states_the_recall_check_and_its_order(self) -> None:
        # The recall check sits between the file walk and the pass: his four questions
        # with the reviewer's answers hidden, his attempt in his own words, then the
        # reveal. It gates nothing - no score, no pass mark.
        reviewer = (REPO / "skills" / "reviewer" / "SKILL.md").read_text(encoding="utf-8")
        text = " ".join(reviewer.split())
        for piece in (
            "The recall check, after the walk and before the pass",
            "OWNER ANSWERS A-D hidden",
            "in his own words",
            "only then do you reveal",
            "no other set",
            "It gates nothing",
            "No score, no pass mark",
            "may pass having missed every question",
            "re-teaches",
            "no new field or store",
        ):
            self.assertIn(piece.lower(), text.lower(), f"the recall step is missing: {piece}")
        # The reveal order, as the brief states it.
        low = text.lower()
        self.assertLess(
            low.index("a-d hidden"),
            low.index("in his own words"),
            "his attempt comes after the answers are hidden",
        )
        self.assertLess(
            low.index("in his own words"),
            low.index("only then do you reveal"),
            "the reveal comes after his attempt",
        )
        # It uses his own four questions, and names no second set.
        self.assertIn("the ones above", text, "the recall uses the four questions above")
        self.assertIn("no other set", text, "the recall names no second question set")
        self.assertIn("A-D", text, "the reveal names the OWNER ANSWERS block")

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

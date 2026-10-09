from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clowder.sessions import extract_text, read_answer, read_model, read_usage
from tests.support import default_usage, write_session

# 2026-09-27T10:00:03Z and two later instants, in epoch milliseconds.
T0 = 1790512803000
T1 = T0 + 5000
T2 = T0 + 10000
T3 = T0 + 90000


class UsageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "session.jsonl"

    def test_missing_file_reads_as_nothing(self) -> None:
        self.assertIsNone(read_usage(self.path))
        self.assertIsNone(read_answer(self.path))

    def test_sums_tokens_cost_and_identity(self) -> None:
        write_session(
            self.path,
            cwd="C:/code/myrepo",
            turns=[
                {
                    "at": T1,
                    "text": "working",
                    "usage": default_usage(1000, 200, cost_total=0.002),
                },
                {"at": T2, "text": "done", "usage": default_usage(500, 100, cost_total=0.001)},
            ],
        )
        usage = read_usage(self.path)
        assert usage is not None
        self.assertEqual(usage.turns, 2)
        self.assertEqual(usage.input_tokens, 1500)
        self.assertEqual(usage.output_tokens, 300)
        self.assertEqual(usage.total_tokens, 1800)
        self.assertAlmostEqual(usage.cost_total, 0.003, places=6)
        self.assertEqual(usage.provider, "test-provider")
        self.assertEqual(usage.model, "test-model")
        self.assertEqual(usage.session_id, "sess-0001")
        self.assertEqual(usage.cwd, "C:/code/myrepo")
        self.assertAlmostEqual(usage.duration_seconds or 0, 5.0, places=3)

    def test_since_excludes_an_earlier_task_in_the_same_pane(self) -> None:
        write_session(
            self.path,
            cwd="C:/code/myrepo",
            turns=[
                {"at": T1, "text": "old task answer", "usage": default_usage(9000, 900)},
                {"at": T3, "text": "this task answer", "usage": default_usage(1000, 100)},
            ],
        )
        usage = read_usage(self.path, since=T2 / 1000)
        assert usage is not None
        self.assertEqual(usage.turns, 1)
        self.assertEqual(usage.input_tokens, 1000)

        answer = read_answer(self.path, since=T2 / 1000)
        self.assertEqual(answer, "this task answer")
        self.assertEqual(read_answer(self.path), "this task answer")

    def test_answer_is_prose_not_thinking_or_tool_calls(self) -> None:
        write_session(
            self.path,
            cwd="C:/code/myrepo",
            turns=[
                {"at": T1, "text": "first pass", "usage": default_usage()},
                {"at": T2, "text": "final pass", "usage": default_usage()},
            ],
        )
        self.assertEqual(read_answer(self.path), "final pass")

    def test_turn_with_no_text_is_not_an_answer(self) -> None:
        write_session(
            self.path,
            cwd="C:/code/myrepo",
            turns=[
                {"at": T1, "text": "the answer", "usage": default_usage()},
                {"at": T2, "usage": default_usage()},
            ],
        )
        usage = read_usage(self.path)
        assert usage is not None
        self.assertEqual(usage.turns, 2, "a textless turn still counts as usage")
        self.assertEqual(read_answer(self.path), "the answer")

    def test_a_torn_last_line_is_survivable(self) -> None:
        write_session(
            self.path,
            cwd="C:/code/myrepo",
            turns=[{"at": T1, "text": "good", "usage": default_usage()}],
        )
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write('{"type":"message","message":{"role":"assist')
        usage = read_usage(self.path)
        assert usage is not None
        self.assertEqual(usage.turns, 1)
        self.assertEqual(read_answer(self.path), "good")

    def test_empty_and_blank_lines_are_skipped(self) -> None:
        self.path.write_text("\n\n   \n", encoding="utf-8")
        self.assertIsNone(read_usage(self.path))

    def test_messages_without_usage_still_count_as_turns(self) -> None:
        record = {
            "type": "message",
            "timestamp": "2026-09-27T10:00:03.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        }
        self.path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        usage = read_usage(self.path)
        assert usage is not None
        self.assertEqual(usage.turns, 1)
        self.assertEqual(usage.total_tokens, 0)


class ModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "session.jsonl"

    def write_records(self, *records: dict[str, object]) -> None:
        self.path.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )

    def test_missing_file_reads_as_nothing(self) -> None:
        self.assertIsNone(read_model(self.path))

    def test_the_opening_model_change_is_read(self) -> None:
        self.write_records(
            {"type": "session", "id": "s", "cwd": "C:/code/myrepo"},
            {"type": "model_change", "provider": "prov", "modelId": "big-model"},
            {"type": "thinking_level_change", "thinkingLevel": "high"},
        )
        info = read_model(self.path)
        assert info is not None
        self.assertEqual(info.provider, "prov")
        self.assertEqual(info.model, "big-model")
        self.assertEqual(info.thinking, "high")
        self.assertEqual(info.label, "prov/big-model (thinking high)")

    def test_an_old_session_falls_back_to_its_assistant_turns(self) -> None:
        self.write_records(
            {
                "type": "message",
                "message": {"role": "assistant", "provider": "a", "model": "one"},
            },
            {
                "type": "message",
                "message": {"role": "assistant", "provider": "b", "model": "two"},
            },
        )
        info = read_model(self.path)
        assert info is not None
        self.assertEqual(info.label, "b/two")

    def test_a_later_record_wins(self) -> None:
        self.write_records(
            {
                "type": "message",
                "message": {"role": "assistant", "provider": "a", "model": "one"},
            },
            {"type": "model_change", "provider": "c", "modelId": "three"},
        )
        info = read_model(self.path)
        assert info is not None
        self.assertEqual(info.label, "c/three")

    def test_a_message_without_a_level_keeps_the_last_one(self) -> None:
        self.write_records(
            {"type": "thinking_level_change", "thinkingLevel": "high"},
            {
                "type": "message",
                "message": {"role": "assistant", "provider": "a", "model": "one"},
            },
        )
        info = read_model(self.path)
        assert info is not None
        self.assertEqual(info.thinking, "high")

    def test_label_without_a_provider(self) -> None:
        self.write_records({"type": "model_change", "modelId": "solo"})
        info = read_model(self.path)
        assert info is not None
        self.assertEqual(info.label, "solo")


class ExtractTextTest(unittest.TestCase):
    def test_plain_string_content(self) -> None:
        self.assertEqual(extract_text("hello"), "hello")
        self.assertIsNone(extract_text("   "))

    def test_only_text_parts_are_joined(self) -> None:
        content = [
            {"type": "thinking", "thinking": "hidden"},
            {"type": "text", "text": "one"},
            {"type": "toolCall", "name": "read"},
            {"type": "text", "text": "two"},
        ]
        self.assertEqual(extract_text(content), "one\ntwo")

    def test_junk_content_is_none(self) -> None:
        self.assertIsNone(extract_text(None))
        self.assertIsNone(extract_text(42))
        self.assertIsNone(extract_text([{"type": "text"}]))
        self.assertIsNone(extract_text([]))


if __name__ == "__main__":
    unittest.main()

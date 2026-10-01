from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clowder.config import load_config
from clowder.errors import ConfigError
from tests.support import clean_env, working_dir, write_config


class LoadConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        (self.workspace / "myrepo").mkdir(parents=True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config_path = write_config(
            self.root / "clowder.config.toml",
            workspace={"root": str(self.workspace)},
            mux={"bin": "herdr"},
            sessions={"root": str(self.root / "sessions")},
        )

    def test_explicit_config_is_used(self) -> None:
        config = load_config(self.config_path)
        assert config.workspace_root is not None
        self.assertEqual(config.workspace_root, self.workspace)
        self.assertEqual(config.mux_bin, "herdr")
        self.assertEqual(config.mux_prompt_argv, ("agent", "prompt", "{agent}", "{brief}"))
        self.assertEqual(config.source, self.config_path)

    def test_state_defaults_beside_the_config(self) -> None:
        config = load_config(self.config_path)
        self.assertEqual(config.state_path, self.config_path.parent / "state.json")

    def test_env_state_beats_the_file(self) -> None:
        with clean_env(CLOWDER_STATE=str(self.root / "elsewhere.json")):
            config = load_config(self.config_path)
        self.assertEqual(config.state_path, self.root / "elsewhere.json")

    def test_env_mux_bin_beats_the_file(self) -> None:
        with clean_env(CLOWDER_MUX_BIN="other-mux"):
            config = load_config(self.config_path)
        self.assertEqual(config.mux_bin, "other-mux")

    def test_repo_name_resolves_under_the_workspace_root(self) -> None:
        config = load_config(self.config_path)
        self.assertEqual(config.resolve_repo("myrepo"), self.workspace / "myrepo")

    def test_repo_name_with_no_workspace_root_is_an_error(self) -> None:
        path = write_config(self.root / "bare.toml")
        config = load_config(path)
        with self.assertRaises(ConfigError) as caught:
            config.resolve_repo("myrepo")
        self.assertIn("workspace root", str(caught.exception))

    def test_missing_repo_directory_names_the_path(self) -> None:
        config = load_config(self.config_path)
        with self.assertRaises(ConfigError) as caught:
            config.resolve_repo("not-there")
        self.assertIn("not-there", str(caught.exception))

    def test_repos_table_pins_a_name_to_a_path(self) -> None:
        other = self.root / "elsewhere"
        other.mkdir()
        path = write_config(
            self.root / "pinned.toml",
            workspace={"root": str(self.workspace)},
            repos={"pinned": str(other)},
        )
        config = load_config(path)
        self.assertEqual(config.resolve_repo("pinned"), other)

    def test_a_path_is_taken_as_a_path(self) -> None:
        config = load_config(self.config_path)
        self.assertEqual(
            config.resolve_repo(str(self.workspace / "myrepo")),
            self.workspace / "myrepo",
        )
        with working_dir(self.workspace):
            self.assertEqual(config.resolve_repo("./myrepo"), self.workspace / "myrepo")

    def test_missing_explicit_config_is_an_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.root / "nope.toml")

    def test_bad_toml_is_an_error(self) -> None:
        path = self.root / "bad.toml"
        path.write_text("[workspace\nroot = ", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            load_config(path)
        self.assertIn("not valid TOML", str(caught.exception))

    def test_prompt_argv_must_be_strings(self) -> None:
        path = write_config(self.root / "odd.toml", mux={"prompt_argv": [1, 2]})
        with self.assertRaises(ConfigError) as caught:
            load_config(path)
        self.assertIn("prompt_argv", str(caught.exception))

    def test_section_must_be_a_table(self) -> None:
        path = self.root / "shape.toml"
        path.write_text('mux = "herdr"\n', encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_search_finds_a_config_in_the_current_directory(self) -> None:
        directory = self.root / "project"
        directory.mkdir()
        found = write_config(directory / "clowder.toml")
        with clean_env(), working_dir(directory):
            config = load_config()
        self.assertEqual(config.source, found)

    def test_no_config_anywhere_is_not_an_error(self) -> None:
        directory = self.root / "empty"
        directory.mkdir()
        with clean_env(CLOWDER_HOME=str(self.home)), working_dir(directory):
            config = load_config()
        self.assertIsNone(config.source)
        self.assertEqual(config.state_path, self.home / "state.json")

    def test_mux_prompt_argv_can_be_overridden(self) -> None:
        path = write_config(
            self.root / "mux.toml", mux={"bin": "tmux", "prompt_argv": ["send", "{brief}"]}
        )
        config = load_config(path)
        self.assertEqual(config.mux_prompt_argv, ("send", "{brief}"))

    def test_as_dict_is_json_ready(self) -> None:
        import json

        config = load_config(self.config_path)
        json.dumps(config.as_dict())

    def test_marker_is_on_by_default(self) -> None:
        config = load_config(self.config_path)
        self.assertTrue(config.dispatch_marker)
        self.assertIsNone(config.front_door_name)

    def test_marker_can_be_turned_off(self) -> None:
        path = write_config(self.root / "off.toml", dispatch={"marker": False})
        self.assertFalse(load_config(path).dispatch_marker)

    def test_marker_must_be_a_boolean(self) -> None:
        path = write_config(self.root / "odd.toml", dispatch={"marker": "yes"})
        with self.assertRaises(ConfigError) as caught:
            load_config(path)
        self.assertIn("dispatch.marker", str(caught.exception))

    def test_front_door_name_is_read(self) -> None:
        path = write_config(self.root / "fd.toml", front_door={"name": "topcat"})
        self.assertEqual(load_config(path).front_door_name, "topcat")

    def test_front_door_name_must_be_a_string(self) -> None:
        path = write_config(self.root / "fd2.toml", front_door={"name": 7})
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_the_git_identity_has_a_default(self) -> None:
        config = load_config(self.config_path)
        self.assertEqual(config.git_name, "clowder-bot")
        self.assertEqual(config.git_email, "94532220+lacattano@users.noreply.github.com")

    def test_the_git_identity_can_be_overridden(self) -> None:
        path = write_config(
            self.root / "git.toml", git={"name": "my-bot", "email": "bot@example.com"}
        )
        config = load_config(path)
        self.assertEqual(config.git_name, "my-bot")
        self.assertEqual(config.git_email, "bot@example.com")

    def test_git_identity_env_beats_the_file(self) -> None:
        with clean_env(CLOWDER_GIT_EMAIL="env@example.com"):
            config = load_config(self.config_path)
        self.assertEqual(config.git_email, "env@example.com")

    def test_an_unknown_git_key_is_refused(self) -> None:
        path = write_config(self.root / "gitbad.toml", git={"nmae": "typo"})
        with self.assertRaises(ConfigError) as caught:
            load_config(path)
        self.assertIn("unknown git keys", str(caught.exception))

    def test_a_git_value_must_be_a_string(self) -> None:
        path = write_config(self.root / "gitnum.toml", git={"email": 7})
        with self.assertRaises(ConfigError) as caught:
            load_config(path)
        self.assertIn("git.email", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

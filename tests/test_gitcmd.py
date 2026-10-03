"""Git helpers, against a real repository made in a temp directory.

No multiplexer, no network: `git init` in a scratch dir and drive it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clowder import gitcmd
from clowder.errors import GitError


class ScratchRepo:
    """A real repository with one commit on `main`."""

    def __init__(self, root: Path) -> None:
        self.root = root / "repo"
        self.root.mkdir(parents=True)
        gitcmd.run_git(self.root, "init", "-b", "main")
        gitcmd.run_git(self.root, "config", "user.email", "t@example.com")
        gitcmd.run_git(self.root, "config", "user.name", "Test")
        (self.root / "readme.md").write_text("hello\n", encoding="utf-8")
        gitcmd.run_git(self.root, "add", "readme.md")
        gitcmd.run_git(self.root, "commit", "-m", "first")

    def commit(self, name: str, text: str = "x\n") -> str:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        gitcmd.run_git(self.root, "add", name)
        gitcmd.run_git(self.root, "commit", "-m", f"add {name}")
        return gitcmd.head_commit(self.root) or ""

    def commit_as(self, name: str, identity: gitcmd.Identity, text: str = "x\n") -> str:
        """A commit made the way a crew pane makes one: identity in the env."""
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        gitcmd.run_git(self.root, "add", name)
        gitcmd.run_git(self.root, "commit", "-m", f"add {name}", env=identity.env())
        return gitcmd.head_commit(self.root, short=False) or ""


class GitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = ScratchRepo(Path(self.tmp.name))

    # -- reading -----------------------------------------------------------

    def test_is_repo_and_toplevel(self) -> None:
        self.assertTrue(gitcmd.is_repo(self.repo.root))
        self.assertEqual(Path(gitcmd.toplevel(self.repo.root) or ""), self.repo.root)
        self.assertFalse(gitcmd.is_repo(self.repo.root / "nope"))

    def test_a_fresh_checkout_is_clean(self) -> None:
        self.assertTrue(gitcmd.is_clean(self.repo.root))
        self.assertEqual(gitcmd.status_entries(self.repo.root), [])

    def test_an_untracked_file_makes_it_dirty(self) -> None:
        (self.repo.root / "scratch.txt").write_text("x", encoding="utf-8")
        self.assertFalse(gitcmd.is_clean(self.repo.root))
        entries = gitcmd.status_entries(self.repo.root)
        self.assertTrue(any("scratch.txt" in entry for entry in entries))

    def test_the_panes_pi_state_does_not_make_it_dirty(self) -> None:
        # The tool writes `.pi/remote-pi/config.json` so a new pane joins the
        # relay. That is local state, not the agent's work, so it is ignored.
        config = self.repo.root / ".pi" / "remote-pi" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}", encoding="utf-8")
        self.assertTrue(gitcmd.is_clean(self.repo.root))
        self.assertEqual(gitcmd.status_entries(self.repo.root), [])

    def test_pi_state_does_not_hide_real_work(self) -> None:
        config = self.repo.root / ".pi" / "remote-pi" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}", encoding="utf-8")
        (self.repo.root / "scratch.txt").write_text("x", encoding="utf-8")
        entries = gitcmd.status_entries(self.repo.root)
        self.assertTrue(any("scratch.txt" in entry for entry in entries))
        self.assertFalse(any(".pi" in entry for entry in entries))

    def test_a_tracked_change_under_pi_still_shows(self) -> None:
        # The exclusion is the tool's own file, not all of `.pi/`. A repo that
        # tracks a file there keeps git honest about its changes.
        path = self.repo.root / ".pi" / "tracked.json"
        path.parent.mkdir(parents=True)
        path.write_text("one\n", encoding="utf-8")
        gitcmd.run_git(self.repo.root, "add", ".pi/tracked.json")
        gitcmd.run_git(self.repo.root, "commit", "-m", "track a pi file")
        path.write_text("two\n", encoding="utf-8")
        entries = gitcmd.status_entries(self.repo.root)
        self.assertTrue(any("tracked.json" in entry for entry in entries))
        self.assertFalse(gitcmd.is_clean(self.repo.root))

    def test_another_file_under_pi_still_shows(self) -> None:
        other = self.repo.root / ".pi" / "other.json"
        other.parent.mkdir(parents=True)
        other.write_text("x\n", encoding="utf-8")
        entries = gitcmd.status_entries(self.repo.root)
        # git collapses an untracked directory, so the entry is `.pi/` itself.
        self.assertTrue(any(".pi" in entry for entry in entries))

    def test_a_nested_pi_still_shows(self) -> None:
        nested = self.repo.root / "sub" / ".pi" / "nested.json"
        nested.parent.mkdir(parents=True)
        nested.write_text("x\n", encoding="utf-8")
        entries = gitcmd.status_entries(self.repo.root)
        self.assertTrue(any("sub" in entry for entry in entries))

    def test_an_edited_file_makes_it_dirty(self) -> None:
        (self.repo.root / "readme.md").write_text("changed\n", encoding="utf-8")
        self.assertFalse(gitcmd.is_clean(self.repo.root))

    def test_branch_and_commit(self) -> None:
        self.assertEqual(gitcmd.current_branch(self.repo.root), "main")
        short = gitcmd.head_commit(self.repo.root)
        assert short is not None
        self.assertEqual(len(short), 7)
        full = gitcmd.head_commit(self.repo.root, short=False)
        assert full is not None
        self.assertEqual(len(full), 40)

    def test_detached_head_has_no_branch(self) -> None:
        commit = gitcmd.head_commit(self.repo.root, short=False)
        assert commit is not None
        gitcmd.run_git(self.repo.root, "checkout", "--detach", commit)
        self.assertIsNone(gitcmd.current_branch(self.repo.root))

    def test_branch_exists_and_local_branches(self) -> None:
        self.assertTrue(gitcmd.branch_exists(self.repo.root, "main"))
        self.assertFalse(gitcmd.branch_exists(self.repo.root, "task/nope"))
        self.assertEqual(gitcmd.local_branches(self.repo.root), ["main"])

    def test_not_a_repo_reads_as_nothing_not_an_error(self) -> None:
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertIsNone(gitcmd.current_branch(empty))
        self.assertIsNone(gitcmd.head_commit(empty))
        self.assertEqual(gitcmd.status_entries(empty), [])

    # -- base branch -------------------------------------------------------

    def test_base_falls_back_to_the_usual_names(self) -> None:
        self.assertEqual(gitcmd.resolve_base(self.repo.root), "main")

    def test_base_uses_master_when_that_is_what_exists(self) -> None:
        repo = Path(self.tmp.name) / "old"
        repo.mkdir()
        gitcmd.run_git(repo, "init", "-b", "master")
        gitcmd.run_git(repo, "config", "user.email", "t@example.com")
        gitcmd.run_git(repo, "config", "user.name", "Test")
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        gitcmd.run_git(repo, "add", "a.txt")
        gitcmd.run_git(repo, "commit", "-m", "first")
        self.assertEqual(gitcmd.resolve_base(repo), "master")

    def test_an_explicit_base_must_exist(self) -> None:
        self.assertEqual(gitcmd.resolve_base(self.repo.root, "main"), "main")
        with self.assertRaises(GitError) as caught:
            gitcmd.resolve_base(self.repo.root, "release")
        self.assertIn("no branch 'release'", str(caught.exception))
        self.assertIn("main", str(caught.exception))

    def test_base_falls_back_to_the_checked_out_branch(self) -> None:
        gitcmd.run_git(self.repo.root, "branch", "-m", "main", "work")
        self.assertEqual(gitcmd.resolve_base(self.repo.root), "work")

    def test_no_branches_and_no_commits_is_an_error(self) -> None:
        bare = Path(self.tmp.name) / "bare"
        bare.mkdir()
        gitcmd.run_git(bare, "init", "-b", "main")
        with self.assertRaises(GitError):
            gitcmd.resolve_base(bare)

    # -- moving between branches -------------------------------------------

    def test_switching_to_a_new_branch(self) -> None:
        gitcmd.switch_new_branch(self.repo.root, "task/refund", "main")
        self.assertEqual(gitcmd.current_branch(self.repo.root), "task/refund")
        self.assertTrue(gitcmd.branch_exists(self.repo.root, "task/refund"))
        gitcmd.switch_branch(self.repo.root, "main")
        self.assertEqual(gitcmd.current_branch(self.repo.root), "main")

    def test_switching_to_a_missing_branch_is_an_error(self) -> None:
        with self.assertRaises(GitError):
            gitcmd.switch_branch(self.repo.root, "task/nope")

    def test_switching_with_uncommitted_work_is_refused_by_git(self) -> None:
        # Why the dirty check exists: git carries the change over, silently.
        (self.repo.root / "readme.md").write_text("half done\n", encoding="utf-8")
        gitcmd.switch_new_branch(self.repo.root, "task/refund", "main")
        self.assertFalse(gitcmd.is_clean(self.repo.root), "the edit followed the switch")

    def test_detach_at_a_branch_frees_a_folder(self) -> None:
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        self.repo.commit("work.txt")
        gitcmd.switch_branch(self.repo.root, "main")
        gitcmd.detach_at(self.repo.root, "main")
        self.assertIsNone(gitcmd.current_branch(self.repo.root))
        self.assertTrue(gitcmd.is_detached(self.repo.root))
        self.assertEqual(gitcmd.head_commit(self.repo.root), gitcmd.head_commit(self.repo.root))

    # -- reachability ------------------------------------------------------

    def test_a_commit_on_a_branch_is_reachable(self) -> None:
        commit = gitcmd.head_commit(self.repo.root, short=False) or ""
        self.assertTrue(gitcmd.is_reachable(self.repo.root, commit))
        self.assertIn("main", gitcmd.branches_containing(self.repo.root, commit))

    def test_a_detached_commit_is_on_no_branch(self) -> None:
        # The case the gate exists for: work sitting only in one folder's HEAD.
        gitcmd.run_git(self.repo.root, "checkout", "--detach")
        commit = self.repo.commit("only-here.txt")
        self.assertTrue(gitcmd.is_detached(self.repo.root))
        self.assertEqual(
            gitcmd.branches_containing(self.repo.root, commit),
            [],
            "git prints '(no branch)' here, which is not a branch",
        )
        self.assertFalse(gitcmd.is_reachable(self.repo.root, commit))

    def test_a_commit_not_on_any_branch_is_unreachable(self) -> None:
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        commit = self.repo.commit("work.txt")
        gitcmd.switch_branch(self.repo.root, "main")
        gitcmd.run_git(self.repo.root, "checkout", "--detach")
        self.assertTrue(gitcmd.is_reachable(self.repo.root, commit), "task/x holds it")

        orphan = self.repo.commit("orphan.txt")
        self.assertFalse(gitcmd.is_reachable(self.repo.root, orphan))

    # -- a change on a branch, by content ----------------------------------

    def test_patch_id_survives_a_rebase(self) -> None:
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        self.repo.commit("b.txt")
        old = self.repo.commit("c.txt")
        gitcmd.switch_branch(self.repo.root, "main")
        self.repo.commit("d.txt")
        gitcmd.switch_branch(self.repo.root, "task/x")
        gitcmd.run_git(self.repo.root, "rebase", "main")
        new = gitcmd.head_commit(self.repo.root, short=False) or ""
        gitcmd.switch_branch(self.repo.root, "main")

        self.assertNotEqual(old, new)
        self.assertIsNotNone(gitcmd.patch_id(self.repo.root, old))
        self.assertEqual(
            gitcmd.patch_id(self.repo.root, old), gitcmd.patch_id(self.repo.root, new)
        )

    def test_a_superseded_commit_is_content_on_a_branch(self) -> None:
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        self.repo.commit("b.txt")
        old = self.repo.commit("c.txt")
        gitcmd.switch_branch(self.repo.root, "main")
        self.repo.commit("d.txt")
        gitcmd.switch_branch(self.repo.root, "task/x")
        gitcmd.run_git(self.repo.root, "rebase", "main")
        gitcmd.switch_branch(self.repo.root, "main")

        self.assertFalse(gitcmd.is_reachable(self.repo.root, old), "the old hash is gone")
        self.assertEqual(gitcmd.commit_risk(self.repo.root, old), gitcmd.ON_BRANCH)

    def test_a_lost_commit_is_still_at_risk(self) -> None:
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        lost = self.repo.commit("lost.txt")
        gitcmd.run_git(self.repo.root, "reset", "--hard", "HEAD~1")
        gitcmd.switch_branch(self.repo.root, "main")

        self.assertFalse(gitcmd.is_reachable(self.repo.root, lost))
        self.assertEqual(gitcmd.commit_risk(self.repo.root, lost), gitcmd.LOST)

    def test_an_empty_change_has_nothing_to_lose(self) -> None:
        gitcmd.run_git(self.repo.root, "checkout", "--detach")
        gitcmd.run_git(self.repo.root, "commit", "--allow-empty", "-m", "empty")
        commit = gitcmd.head_commit(self.repo.root, short=False) or ""
        self.assertFalse(gitcmd.is_reachable(self.repo.root, commit))
        self.assertEqual(gitcmd.commit_risk(self.repo.root, commit), gitcmd.EMPTY)

    def test_a_merge_commit_is_named_not_unchecked(self) -> None:
        root = self.repo.root
        gitcmd.switch_new_branch(root, "side", "main")
        self.repo.commit("side.txt")
        gitcmd.switch_branch(root, "main")
        self.repo.commit("main.txt")
        gitcmd.run_git(root, "merge", "--no-ff", "-m", "merge side", "side")
        merge = gitcmd.head_commit(root, short=False) or ""
        gitcmd.run_git(root, "reset", "--hard", "HEAD~1")
        gitcmd.run_git(root, "branch", "-D", "side")

        self.assertFalse(gitcmd.is_reachable(root, merge))
        self.assertEqual(gitcmd.commit_risk(root, merge), gitcmd.MERGE)

    def test_a_missing_commit_cannot_be_checked(self) -> None:
        self.assertEqual(gitcmd.commit_risk(self.repo.root, "0" * 40), gitcmd.UNKNOWN)

    # -- worktrees ---------------------------------------------------------

    def test_add_a_worktree_on_a_new_branch(self) -> None:
        target = Path(self.tmp.name) / "wt" / "maker"
        gitcmd.add_worktree(self.repo.root, target, "crew/maker", "main")
        self.assertTrue((target / "readme.md").is_file())
        self.assertEqual(gitcmd.current_branch(target), "crew/maker")

        listed = {Path(w.path): w for w in gitcmd.list_worktrees(self.repo.root)}
        self.assertIn(target, listed)
        self.assertEqual(listed[target].branch, "crew/maker")
        self.assertFalse(listed[target].detached)

    def test_a_second_worktree_cannot_hold_the_same_branch(self) -> None:
        first = Path(self.tmp.name) / "wt" / "one"
        second = Path(self.tmp.name) / "wt" / "two"
        gitcmd.add_worktree(self.repo.root, first, "crew/maker", "main")
        with self.assertRaises(GitError) as caught:
            gitcmd.add_worktree(self.repo.root, second, "crew/maker", "main")
        self.assertIn("already used by worktree", str(caught.exception))

    def test_a_worktree_can_switch_to_a_job_branch(self) -> None:
        target = Path(self.tmp.name) / "wt" / "maker"
        gitcmd.add_worktree(self.repo.root, target, "crew/maker", "main")
        gitcmd.switch_new_branch(target, "task/refund", "main")
        self.assertEqual(gitcmd.current_branch(target), "task/refund")
        gitcmd.switch_branch(target, "crew/maker")
        self.assertEqual(gitcmd.current_branch(target), "crew/maker")

    def test_remove_a_worktree(self) -> None:
        target = Path(self.tmp.name) / "wt" / "maker"
        gitcmd.add_worktree(self.repo.root, target, "crew/maker", "main")
        gitcmd.remove_worktree(self.repo.root, target)
        self.assertFalse(target.is_dir())
        # The branch outlives the checkout, which is the point of keeping it.
        self.assertEqual(set(gitcmd.local_branches(self.repo.root)), {"main", "crew/maker"})

    def test_a_free_worktree_has_no_branch_on_it(self) -> None:
        target = Path(self.tmp.name) / "wt" / "space"
        gitcmd.add_worktree_free(self.repo.root, target, "main")
        self.assertTrue((target / "readme.md").is_file(), "it holds the base code")
        self.assertIsNone(gitcmd.current_branch(target))
        self.assertTrue(gitcmd.is_detached(target))
        listed = {Path(w.path): w for w in gitcmd.list_worktrees(self.repo.root)}
        self.assertTrue(listed[target].detached)
        self.assertIsNone(listed[target].branch)

    def test_two_free_worktrees_can_share_the_base(self) -> None:
        # Which is the whole point of the space model: no branch is held, so
        # several spaces sit on the base at once.
        one = Path(self.tmp.name) / "wt" / "one"
        two = Path(self.tmp.name) / "wt" / "two"
        gitcmd.add_worktree_free(self.repo.root, one, "main")
        gitcmd.add_worktree_free(self.repo.root, two, "main")
        self.assertTrue(one.is_dir() and two.is_dir())
        self.assertEqual(gitcmd.head_commit(one), gitcmd.head_commit(two))

    def test_delete_a_branch(self) -> None:
        gitcmd.switch_new_branch(self.repo.root, "task/refund", "main")
        gitcmd.switch_branch(self.repo.root, "main")
        gitcmd.delete_branch(self.repo.root, "task/refund")
        self.assertFalse(gitcmd.branch_exists(self.repo.root, "task/refund"))

    def test_an_unmerged_branch_is_not_deleted(self) -> None:
        # `git branch -d` protects unmerged work, and that protection is kept.
        gitcmd.switch_new_branch(self.repo.root, "task/refund", "main")
        self.repo.commit("work.txt")
        gitcmd.switch_branch(self.repo.root, "main")
        with self.assertRaises(GitError):
            gitcmd.delete_branch(self.repo.root, "task/refund")

    # -- setup commands ----------------------------------------------------

    def test_run_command_writes_something(self) -> None:
        import sys

        gitcmd.run_command(
            f"\"{sys.executable}\" -c \"open('made.txt', 'w').write('x')\"", self.repo.root
        )
        self.assertTrue((self.repo.root / "made.txt").is_file())

    def test_a_failing_command_is_an_error(self) -> None:
        import sys

        with self.assertRaises(GitError) as caught:
            gitcmd.run_command(f'"{sys.executable}" -c "raise SystemExit(3)"', self.repo.root)
        self.assertIn("setup command failed", str(caught.exception))

    def test_a_missing_command_is_an_error(self) -> None:
        with self.assertRaises(GitError):
            gitcmd.run_command("definitely-not-a-real-program --go", self.repo.root)

    def test_an_empty_command_does_nothing(self) -> None:
        gitcmd.run_command("   ", self.repo.root)

    # -- a stale base ------------------------------------------------------

    def test_fetch_is_false_without_a_remote(self) -> None:
        self.assertFalse(gitcmd.fetch(self.repo.root))
        self.assertIsNone(gitcmd.branch_behind(self.repo.root, "main"))

    def test_branch_behind_counts_commits_after_a_fetch(self) -> None:
        origin = Path(self.tmp.name) / "origin.git"
        gitcmd.run_git(self.tmp.name, "init", "--bare", "-b", "main", str(origin))
        gitcmd.run_git(self.repo.root, "remote", "add", "origin", str(origin))
        gitcmd.run_git(self.repo.root, "push", "-u", "origin", "main")
        other = Path(self.tmp.name) / "other"
        gitcmd.run_git(self.tmp.name, "clone", str(origin), str(other))
        gitcmd.run_git(other, "config", "user.email", "t@example.com")
        gitcmd.run_git(other, "config", "user.name", "Test")
        (other / "ahead.txt").write_text("x\n", encoding="utf-8")
        gitcmd.run_git(other, "add", "ahead.txt")
        gitcmd.run_git(other, "commit", "-m", "ahead")
        gitcmd.run_git(other, "push", "origin", "main")

        self.assertTrue(gitcmd.fetch(self.repo.root))
        self.assertEqual(gitcmd.branch_behind(self.repo.root, "main"), 1)
        self.assertEqual(gitcmd.branch_behind(self.repo.root, "task/nope"), None)

    # -- the crew identity -------------------------------------------------

    def test_identity_env_sets_author_and_committer(self) -> None:
        identity = gitcmd.Identity("clowder-bot", "bot@example.com")
        self.assertEqual(
            identity.env(),
            {
                "GIT_AUTHOR_NAME": "clowder-bot",
                "GIT_AUTHOR_EMAIL": "bot@example.com",
                "GIT_COMMITTER_NAME": "clowder-bot",
                "GIT_COMMITTER_EMAIL": "bot@example.com",
            },
        )

    def test_a_commit_under_the_identity_ignores_a_foreign_local_config(self) -> None:
        # The checkout's own config names Test <t@example.com>. The environment
        # still decides, which is the fix: the config is not trusted.
        identity = gitcmd.Identity("clowder-bot", "bot@example.com")
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        self.repo.commit_as("crew.txt", identity)
        shown = gitcmd.run_git(self.repo.root, "log", "-1", "--format=%an|%ae|%cn|%ce").strip()
        self.assertEqual(shown, "clowder-bot|bot@example.com|clowder-bot|bot@example.com")

    def test_the_guard_refuses_a_foreign_author(self) -> None:
        identity = gitcmd.Identity("clowder-bot", "bot@example.com")
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        foreign = self.repo.commit("foreign.txt")  # Test <t@example.com>
        found = gitcmd.foreign_commits(self.repo.root, "main..task/x", identity)
        self.assertEqual([item.commit[:7] for item in found], [foreign])
        with self.assertRaises(GitError) as caught:
            gitcmd.refuse_foreign_authors(self.repo.root, "main..task/x", identity)
        self.assertIn("refusing to publish", str(caught.exception))
        self.assertIn("t@example.com", str(caught.exception))

    def test_the_guard_passes_a_crew_commit(self) -> None:
        identity = gitcmd.Identity("clowder-bot", "bot@example.com")
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        commit = self.repo.commit_as("crew.txt", identity)
        self.assertEqual(gitcmd.foreign_commits(self.repo.root, "main..task/x", identity), [])
        gitcmd.refuse_foreign_authors(self.repo.root, "main..task/x", identity)
        self.assertEqual(gitcmd.commit_of(self.repo.root, "task/x"), commit)

    def test_the_guard_ignores_commits_already_on_the_base(self) -> None:
        # The base commit is Test's. It is not being published, so it is not the
        # crew's business; only the branch's own commits are checked.
        identity = gitcmd.Identity("clowder-bot", "bot@example.com")
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        self.repo.commit_as("crew.txt", identity)
        gitcmd.refuse_foreign_authors(self.repo.root, "main..task/x", identity)

    def test_the_guard_checks_the_committer_too(self) -> None:
        identity = gitcmd.Identity("clowder-bot", "bot@example.com")
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        (self.repo.root / "mixed.txt").write_text("x\n", encoding="utf-8")
        gitcmd.run_git(self.repo.root, "add", "mixed.txt")
        gitcmd.run_git(
            self.repo.root,
            "commit",
            "-m",
            "mixed",
            env={
                "GIT_AUTHOR_NAME": "clowder-bot",
                "GIT_AUTHOR_EMAIL": "bot@example.com",
                "GIT_COMMITTER_NAME": "Someone",
                "GIT_COMMITTER_EMAIL": "someone@example.com",
            },
        )
        found = gitcmd.foreign_commits(self.repo.root, "main..task/x", identity)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].committer_email, "someone@example.com")


class RiskMemoTest(unittest.TestCase):
    """The memo of at-risk answers: it must save work and never lie."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = ScratchRepo(Path(self.tmp.name))

    def a_lost_commit(self) -> str:
        """A commit that is on no branch: alive only in this folder's history."""
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        lost = self.repo.commit("lost.txt")
        gitcmd.run_git(self.repo.root, "reset", "--hard", "HEAD~1")
        gitcmd.switch_branch(self.repo.root, "main")
        return lost

    def test_a_refs_move_invalidates_the_memo(self) -> None:
        # One write remembers the answer. The next write must not trust it once a
        # branch holds the commit - the fingerprint is what makes it recompute.
        with tempfile.TemporaryDirectory() as tmp:
            memo_path = Path(tmp) / "risk-memo.json"
            lost = self.a_lost_commit()
            cold = gitcmd.RiskMemo(memo_path)
            self.assertEqual(cold.risk(self.repo.root, lost), gitcmd.LOST)
            cold.save()

            gitcmd.run_git(self.repo.root, "branch", "hold", lost)

            warm = gitcmd.RiskMemo(memo_path)
            warm.load()
            calls: list[str] = []
            real = gitcmd.commit_risk

            def counting(path: str, commit: str) -> str:
                calls.append(commit)
                return real(path, commit)

            with mock.patch.object(gitcmd, "commit_risk", counting):
                answer = warm.risk(self.repo.root, lost)

            self.assertEqual(answer, gitcmd.ON_BRANCH)
            self.assertEqual(len(calls), 1, "the stale answer was not reused")

    def test_a_commit_that_stops_being_reachable_is_reported_lost(self) -> None:
        # The other direction, and the one that matters most: a memo must never
        # hide a genuinely lost commit.
        with tempfile.TemporaryDirectory() as tmp:
            memo_path = Path(tmp) / "risk-memo.json"
            gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
            commit = self.repo.commit("work.txt")
            cold = gitcmd.RiskMemo(memo_path)
            self.assertEqual(cold.risk(self.repo.root, commit), gitcmd.ON_BRANCH)
            cold.save()

            gitcmd.switch_branch(self.repo.root, "main")
            gitcmd.run_git(self.repo.root, "branch", "-D", "task/x")

            warm = gitcmd.RiskMemo(memo_path)
            warm.load()
            self.assertEqual(warm.risk(self.repo.root, commit), gitcmd.LOST)

    def test_a_refs_move_within_one_write_is_not_seen(self) -> None:
        # Stated, not implied: a memo covers one write, and refs cannot move
        # inside one. The guard is the file, so the next write recomputes.
        memo = gitcmd.RiskMemo()
        lost = self.a_lost_commit()
        self.assertEqual(memo.risk(self.repo.root, lost), gitcmd.LOST)
        gitcmd.run_git(self.repo.root, "branch", "hold", lost)
        self.assertEqual(memo.risk(self.repo.root, lost), gitcmd.LOST)

    def test_two_tasks_sharing_a_commit_cost_one_look(self) -> None:
        memo = gitcmd.RiskMemo()
        commit = self.repo.commit("shared.txt")
        calls: list[tuple[str, ...]] = []
        real = gitcmd.try_git

        def counting(cwd: object, *args: str, **kwargs: object) -> str | None:
            calls.append(args)
            return real(cwd, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(gitcmd, "try_git", counting):
            first = memo.risk(self.repo.root, commit)
            after_first = len(calls)
            second = memo.risk(self.repo.root, commit)

        self.assertEqual(first, gitcmd.ON_BRANCH)
        self.assertEqual(second, first)
        self.assertGreater(after_first, 0, "the first look reads git")
        self.assertEqual(len(calls), after_first, "the second task spawns no git at all")

    def test_the_memo_survives_a_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memo_path = Path(tmp) / "risk-memo.json"
            memo = gitcmd.RiskMemo(memo_path)
            commit = self.repo.commit("kept.txt")
            self.assertEqual(memo.risk(self.repo.root, commit), gitcmd.ON_BRANCH)
            memo.save()
            self.assertTrue(memo_path.is_file())

            warm = gitcmd.RiskMemo(memo_path)
            warm.load()
            with mock.patch.object(
                gitcmd, "commit_risk", side_effect=AssertionError("recomputed")
            ):
                self.assertEqual(warm.risk(self.repo.root, commit), gitcmd.ON_BRANCH)

    def test_the_fingerprint_covers_every_ref_not_just_branches(self) -> None:
        # `commit_risk` runs `git log --all` when a commit is not reachable, so a
        # tag can change its answer. The fingerprint has to see that too.
        before = gitcmd.refs_fingerprint(self.repo.root)
        gitcmd.run_git(self.repo.root, "tag", "v1")
        self.assertNotEqual(before, gitcmd.refs_fingerprint(self.repo.root))

    def test_a_corrupt_memo_file_is_a_cold_memo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memo_path = Path(tmp) / "risk-memo.json"
            memo_path.write_text("{not json at all", encoding="utf-8")
            memo = gitcmd.RiskMemo(memo_path)
            memo.load()
            commit = self.repo.commit("corrupt.txt")
            self.assertEqual(memo.risk(self.repo.root, commit), gitcmd.ON_BRANCH)

    def test_a_missing_memo_file_is_a_cold_memo(self) -> None:
        memo = gitcmd.RiskMemo(Path(self.tmp.name) / "nowhere" / "risk-memo.json")
        memo.load()
        commit = self.repo.commit("cold.txt")
        self.assertEqual(memo.risk(self.repo.root, commit), gitcmd.ON_BRANCH)

    def test_an_unwritable_memo_never_raises(self) -> None:
        # A memo that cannot be written costs one slow write later. It must never
        # fail the command it follows.
        blocked = Path(self.tmp.name) / "blocked"
        blocked.write_text("not a folder", encoding="utf-8")
        memo = gitcmd.RiskMemo(blocked / "risk-memo.json")
        commit = self.repo.commit("unwritable.txt")
        self.assertEqual(memo.risk(self.repo.root, commit), gitcmd.ON_BRANCH)
        memo.save()


if __name__ == "__main__":
    unittest.main()

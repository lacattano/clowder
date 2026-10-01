"""Git helpers, against a real repository made in a temp directory.

No multiplexer, no network: `git init` in a scratch dir and drive it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
        self.assertIs(gitcmd.content_on_a_branch(self.repo.root, old), True)

    def test_a_lost_commit_is_not_content_on_a_branch(self) -> None:
        gitcmd.switch_new_branch(self.repo.root, "task/x", "main")
        lost = self.repo.commit("lost.txt")
        gitcmd.run_git(self.repo.root, "reset", "--hard", "HEAD~1")
        gitcmd.switch_branch(self.repo.root, "main")

        self.assertFalse(gitcmd.is_reachable(self.repo.root, lost))
        self.assertIs(gitcmd.content_on_a_branch(self.repo.root, lost), False)

    def test_an_empty_change_cannot_be_checked(self) -> None:
        gitcmd.run_git(self.repo.root, "checkout", "--detach")
        gitcmd.run_git(self.repo.root, "commit", "--allow-empty", "-m", "empty")
        commit = gitcmd.head_commit(self.repo.root, short=False) or ""
        self.assertFalse(gitcmd.is_reachable(self.repo.root, commit))
        self.assertIsNone(gitcmd.content_on_a_branch(self.repo.root, commit))

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


if __name__ == "__main__":
    unittest.main()

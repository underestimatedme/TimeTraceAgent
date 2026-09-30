import subprocess
import tempfile
import unittest
from pathlib import Path

from timetrace import worktree


def git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.home = root / "home"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("config", "user.email", "t@example.com", cwd=self.repo)
        git("config", "user.name", "t", cwd=self.repo)
        (self.repo / "README.md").write_text("hi\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        git("remote", "add", "origin", "https://example.com/x.git", cwd=self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ensure_creates_isolated_worktree_with_push_blocked(self):
        path, branch = worktree.ensure(str(self.repo), 1, self.home)
        self.assertEqual(branch, "timetrace/1")
        self.assertTrue((Path(path) / "README.md").exists())
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=path), "timetrace/1")
        self.assertEqual(git("config", "remote.origin.pushurl", cwd=path), "no_push://blocked")
        # main checkout keeps its normal push url
        r = subprocess.run(["git", "config", "remote.origin.pushurl"], cwd=self.repo,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        # push from the worktree must fail fast
        r = subprocess.run(["git", "push", "origin", "timetrace/1"], cwd=path, capture_output=True,
                           text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_ensure_is_idempotent(self):
        p1, _ = worktree.ensure(str(self.repo), 2, self.home)
        p2, _ = worktree.ensure(str(self.repo), 2, self.home)
        self.assertEqual(p1, p2)

    def test_ensure_can_start_from_another_task_branch(self):
        p1, b1 = worktree.ensure(str(self.repo), 1, self.home)
        (Path(p1) / "step1.txt").write_text("one\n")
        git("add", ".", cwd=p1)
        git("commit", "-q", "-m", "step 1", cwd=p1)
        p2, b2 = worktree.ensure(str(self.repo), 2, self.home, base=b1)
        self.assertTrue((Path(p2) / "step1.txt").exists())
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=p2), "timetrace/2")
        with self.assertRaisesRegex(ValueError, "base branch"):
            worktree.ensure(str(self.repo), 3, self.home, base="timetrace/does-not-exist")

    def test_is_git_repo(self):
        self.assertTrue(worktree.is_git_repo(str(self.repo)))
        self.assertFalse(worktree.is_git_repo(self.tmp.name))

    def test_snapshot_detects_assume_unchanged_tracked_bytes(self):
        git("update-index", "--assume-unchanged", "README.md", cwd=self.repo)
        before = worktree.snapshot(str(self.repo))
        (self.repo / "README.md").write_text("changed while hidden from git diff\n")
        self.assertNotEqual(worktree.snapshot(str(self.repo)), before)

    def test_snapshot_binds_gitlink_commit_without_reading_child_worktree(self):
        child = Path(self.tmp.name) / "child"
        child.mkdir()
        git("init", "-q", "-b", "main", cwd=child)
        git("config", "user.email", "t@example.com", cwd=child)
        git("config", "user.name", "t", cwd=child)
        (child / "source.txt").write_text("first\n")
        git("add", ".", cwd=child)
        git("commit", "-qm", "child initial", cwd=child)
        git("-c", "protocol.file.allow=always", "submodule", "add", str(child), "deps/child", cwd=self.repo)
        git("commit", "-qm", "add submodule", cwd=self.repo)
        before = worktree.snapshot(str(self.repo))
        checked_out = self.repo / "deps" / "child"
        (checked_out / "source.txt").write_text("local child worktree changes\n")
        self.assertEqual(worktree.snapshot(str(self.repo)), before)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-am", "child next", cwd=checked_out)
        next_commit = git("rev-parse", "HEAD", cwd=checked_out)
        # The parent index owns the gitlink identity; child state alone is not
        # the parent's checkpoint evidence.
        self.assertEqual(worktree.snapshot(str(self.repo)), before)
        git("update-index", "--cacheinfo", "160000,%s,deps/child" % next_commit, cwd=self.repo)
        self.assertNotEqual(worktree.snapshot(str(self.repo)), before)

    # ---- metadata integrity (sandbox escape through git config) -------------
    def test_verify_metadata_accepts_a_fresh_worktree_and_the_main_checkout(self):
        path, _ = worktree.ensure(str(self.repo), 5, self.home)
        worktree.verify_metadata(path, str(self.repo))
        worktree.verify_metadata(str(self.repo), str(self.repo))

    def _admin(self, path):
        return Path(git("rev-parse", "--absolute-git-dir", cwd=path))

    def test_verify_metadata_rejects_extra_keys_in_config_worktree(self):
        # A sandboxed run can write the per-worktree admin dir (it must, to
        # commit). core.fsmonitor there would run a command the next time the
        # unsandboxed runner calls `git diff` in the worktree.
        path, _ = worktree.ensure(str(self.repo), 6, self.home)
        cfg = self._admin(path) / "config.worktree"
        cfg.write_text(cfg.read_text() + "[core]\n\tfsmonitor = touch /tmp/pwned\n")
        with self.assertRaises(worktree.MetadataTampered):
            worktree.verify_metadata(path, str(self.repo))

    def test_verify_metadata_rejects_a_redirected_dot_git_file(self):
        path, _ = worktree.ensure(str(self.repo), 7, self.home)
        evil = Path(path) / "evil"
        evil.mkdir()
        (Path(path) / ".git").write_text("gitdir: %s\n" % evil)
        with self.assertRaises(worktree.MetadataTampered):
            worktree.verify_metadata(path, str(self.repo))

    def test_verify_metadata_rejects_a_redirected_commondir(self):
        path, _ = worktree.ensure(str(self.repo), 8, self.home)
        (self._admin(path) / "commondir").write_text(str(Path(path) / "fake") + "\n")
        with self.assertRaises(worktree.MetadataTampered):
            worktree.verify_metadata(path, str(self.repo))

    def test_verify_metadata_rejects_a_worktree_of_another_repository(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        git("init", "-q", "-b", "main", cwd=other)
        git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "--allow-empty", "-qm", "i", cwd=other)
        path, _ = worktree.ensure(str(other), 9, self.home)
        with self.assertRaises(worktree.MetadataTampered):
            worktree.verify_metadata(path, str(self.repo))

    def test_ensure_refuses_to_reuse_a_tampered_worktree(self):
        path, _ = worktree.ensure(str(self.repo), 10, self.home)
        cfg = self._admin(path) / "config.worktree"
        cfg.write_text(cfg.read_text() + "[include]\n\tpath = /tmp/x\n")
        with self.assertRaises(worktree.MetadataTampered):
            worktree.ensure(str(self.repo), 10, self.home)

    def test_snapshot_ignores_an_fsmonitor_hook(self):
        marker = Path(self.tmp.name) / "fsmonitor-ran"
        git("config", "core.fsmonitor", "touch %s; false" % marker, cwd=self.repo)
        worktree.snapshot(str(self.repo))
        self.assertFalse(marker.exists())

    def test_sandbox_write_roots_are_the_minimum_needed_to_commit(self):
        path, _ = worktree.ensure(str(self.repo), 11, self.home)
        roots = [Path(r) for r in worktree.sandbox_write_roots(path)]
        common = (self.repo / ".git").resolve()
        self.assertIn(self._admin(path).resolve(), roots)
        self.assertIn(common / "objects", roots)
        self.assertIn(common / "refs" / "heads" / "timetrace", roots)
        # Never the whole .git: shared config and hooks run outside the sandbox.
        self.assertNotIn(common, roots)
        for root in roots:
            self.assertNotIn(root, (common / "config", common / "hooks"))
            self.assertTrue(root != common and common in root.parents, root)

    def test_sandbox_write_roots_empty_for_a_main_checkout(self):
        self.assertEqual(worktree.sandbox_write_roots(str(self.repo)), [])


if __name__ == "__main__":
    unittest.main()


class HooksTest(unittest.TestCase):
    def test_ensure_never_runs_repository_hooks(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                            "commit", "--allow-empty", "-qm", "init"], cwd=repo, check=True)
            marker = Path(d) / "hook-ran"
            hook = repo / ".git" / "hooks" / "post-checkout"
            hook.write_text("#!/bin/sh\necho ran >> '%s'\n" % marker)
            hook.chmod(0o755)
            subprocess.run(["git", "branch", "timetrace/existing"], cwd=repo, check=True)
            worktree.ensure(str(repo), 1, Path(d) / "home", "main")
            worktree.ensure(str(repo), 2, Path(d) / "home", "main", branch="timetrace/existing")
            self.assertFalse(marker.exists(), "post-checkout hook ran")

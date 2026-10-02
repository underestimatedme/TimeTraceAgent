"""The local permission rule table (deny > allow > ask the phone)."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from timetrace import approvals
from timetrace.approvals import ALLOW, ASK, DENY, evaluate


class RuleTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.home = base / "home"
        # Where task worktrees really are: under ~/.timetrace, itself a
        # credential location for everything outside the worktree.
        self.root = self.home / ".timetrace" / "worktrees" / "7"
        (self.root / "src").mkdir(parents=True)
        (self.home / ".ssh").mkdir(parents=True)
        self.outside = base / "elsewhere"
        self.outside.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, tool, tool_input, expected):
        verdict = evaluate(tool, tool_input, str(self.root), home=str(self.home))
        self.assertEqual(verdict.action, expected, (tool, tool_input, verdict))
        return verdict

    def test_edits_inside_the_worktree_are_allowed(self):
        for tool, key in (("Edit", "file_path"), ("Write", "file_path"), ("MultiEdit", "file_path"),
                          ("NotebookEdit", "notebook_path")):
            self.check(tool, {key: str(self.root / "src" / "a.py")}, ALLOW)
        self.check("Write", {"file_path": "src/new.py"}, ALLOW)  # relative to the worktree

    def test_writes_outside_the_worktree_are_denied(self):
        self.check("Write", {"file_path": str(self.outside / "x")}, DENY)
        self.check("Edit", {"file_path": str(self.root / ".." / "escape.txt")}, DENY)
        # A symlink inside the worktree that points outside is resolved first.
        (self.root / "link").symlink_to(self.outside)
        self.check("Write", {"file_path": str(self.root / "link" / "x")}, DENY)

    def test_git_metadata_and_ci_config_are_never_edited(self):
        self.check("Write", {"file_path": str(self.root / ".git")}, DENY)
        self.check("Edit", {"file_path": str(self.root / ".github" / "workflows" / "ci.yml")}, DENY)
        self.check("Write", {"file_path": str(self.root / ".gitlab-ci.yml")}, DENY)
        self.check("Write", {"file_path": str(self.root / "Jenkinsfile")}, DENY)

    def test_credential_locations_are_never_read(self):
        self.check("Read", {"file_path": str(self.home / ".ssh" / "id_ed25519")}, DENY)
        self.check("Read", {"file_path": "~/.aws/credentials"}, DENY)
        self.check("Bash", {"command": "cat ~/.ssh/id_rsa"}, DENY)
        self.check("Bash", {"command": "security find-generic-password -s x -w"}, DENY)
        self.check("Bash", {"command": "ls -la $HOME/.timetrace"}, DENY)

    def test_the_worktree_under_dot_timetrace_is_not_a_credential_location(self):
        self.check("Read", {"file_path": str(self.root / "src" / "a.py")}, ALLOW)
        self.check("Bash", {"command": "ls -la %s/src" % self.root}, ALLOW)
        self.check("Bash", {"command": "cat %s/../../config.json" % self.root}, DENY)
        self.check("Read", {"file_path": str(self.home / ".timetrace" / "timetrace.db")}, DENY)

    def test_reads_inside_are_allowed_and_outside_ask(self):
        self.check("Read", {"file_path": str(self.root / "src" / "a.py")}, ALLOW)
        self.check("Grep", {"pattern": "x", "path": str(self.root)}, ALLOW)
        self.check("Glob", {"pattern": "**/*.py"}, ALLOW)
        self.check("Read", {"file_path": str(self.outside / "notes.txt")}, ASK)

    def test_read_only_commands_are_allowed(self):
        for command in ("git status", "git diff --stat", "git log -5 --oneline", "ls -la src", "cat README.md",
                        "grep -rn TODO src", "find . -name '*.py'", "wc -l src/a.py", "pwd",
                        "git add -A", "git commit -m 'fix: x'"):
            self.check("Bash", {"command": command}, ALLOW)

    def test_git_push_and_branch_changes_are_denied_even_inside_compound_commands(self):
        for command in ("git push", "git push origin HEAD", "git commit -m x && git push",
                        "git -C . push", "FOO=1 git push --force", "git remote set-url origin x",
                        "git config core.hooksPath /tmp/h", "git branch -D main", "git checkout -b other",
                        "git switch main", "git worktree add ../x", "sudo rm -rf /tmp/x"):
            self.check("Bash", {"command": command}, DENY)

    def test_anything_else_asks(self):
        for command in ("npm install left-pad", "rm -rf build", "ls | head", "cat a > b", "echo $(whoami)",
                        "find . -delete", "find . -exec rm {} ;", "git -c core.pager=x log", "git diff --output=/tmp/x",
                        "cat /etc/hosts", "rg --pre ./x foo", "curl https://example.com"):
            self.check("Bash", {"command": command}, ASK)
        self.check("WebFetch", {"url": "https://example.com"}, ASK)
        self.check("mcp__server__tool", {"x": 1}, ASK)

    def test_git_config_reads_are_not_denied(self):
        self.check("Bash", {"command": "git config --get user.name"}, ASK)


class RememberAndSummaryTest(unittest.TestCase):
    def test_remember_key_is_tool_plus_first_two_words(self):
        self.assertEqual(approvals.remember_key("Bash", {"command": "npm install left-pad"}), "Bash:npm install")
        self.assertEqual(approvals.remember_key("Bash", {"command": "NODE_ENV=x npm test"}), "Bash:npm test")
        self.assertIsNone(approvals.remember_key("Bash", {"command": "npm install x && rm -rf ~"}))
        self.assertEqual(approvals.remember_key("WebFetch", {"url": "https://x"}), "WebFetch")

    def test_summary_is_redacted_and_bounded(self):
        text = approvals.summary("Bash", {"command": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123' x"})
        self.assertTrue(text.startswith("运行 curl"))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123", text)
        self.assertLessEqual(len(approvals.summary("Bash", {"command": "x" * 5000})), 300)
        self.assertEqual(approvals.summary("Edit", {"file_path": "/w/a.py"}), "修改文件 /w/a.py")

    def test_input_is_redacted_and_at_most_4kb(self):
        value = approvals.safe_input({"command": "export GITHUB_TOKEN=ghp_" + "a" * 36, "blob": "y" * 20000})
        raw = json.dumps(value)
        self.assertLessEqual(len(raw), 4096)
        self.assertNotIn("ghp_" + "a" * 36, raw)
        many = approvals.safe_input({"k%d" % i: "中" * 200 for i in range(100)})
        self.assertLessEqual(len(json.dumps(many)), 4096)


if __name__ == "__main__":
    unittest.main()

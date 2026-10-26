import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "commit-list-updates.sh"


class CommitUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.remote = root / "origin.git"
        self.git("init", "--bare", str(self.remote))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "list.txt").write_text("original\n")
        self.git("add", "list.txt")
        self.git("commit", "-m", "initial")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-u", "origin", "main")
        self.initial = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.STDOUT, text=True).strip()

    def run_script(self, *paths):
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        return subprocess.run(["bash", str(SCRIPT), "Update lists", *paths], cwd=self.repo, env=environment, capture_output=True, text=True)

    def test_unchanged_is_success_without_commit_or_push(self):
        # A removed remote makes an accidental push fail.
        self.git("remote", "remove", "origin")
        result = self.run_script("list.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No list changes", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_changed_output_is_committed_and_pushed(self):
        (self.repo / "list.txt").write_text("updated\n")
        result = self.run_script("list.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("--git-dir", str(self.remote), "rev-parse", "refs/heads/main"))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_first_created_output_is_committed(self):
        (self.repo / "new-list.txt").write_text("new\n")
        result = self.run_script("new-list.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show", "HEAD:new-list.txt"), "new")

    def test_unrelated_staged_file_is_not_committed(self):
        (self.repo / "list.txt").write_text("updated\n")
        (self.repo / "unrelated.txt").write_text("unrelated\n")
        self.git("add", "unrelated.txt")
        result = self.run_script("list.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "list.txt")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "unrelated.txt")

    def test_push_failure_is_not_hidden(self):
        (self.repo / "list.txt").write_text("updated\n")
        self.git("remote", "remove", "origin")
        self.assertNotEqual(self.run_script("list.txt").returncode, 0)


if __name__ == "__main__":
    unittest.main()

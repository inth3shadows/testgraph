"""hooks/pre-push, run as git would run it (audit M5, H3).

The hook is shell, so these tests run the real text under `sh` (dash on Debian)
in a throwaway git repo. PATH holds symlinks to only the tools the hook needs plus
stub `python3` / `codegraph`, and deliberately `timeout` (until H3 removes the need for it): that is macOS, and the
old hook silently did nothing there.
"""
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK_SRC = os.path.join(os.path.dirname(HERE), "hooks", "pre-push")
ZERO = "0" * 40
MARK = "31337"      # sleep duration that identifies a stub's stray process

_TOOLS = ("timeout", "sh", "git", "mktemp", "rm", "sleep", "cat", "env", "dirname", "basename")


def _stray_processes():
    """pids whose argv is `sleep 31337` (none if /proc is missing)."""
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue
        if argv[:2] == [b"sleep", MARK.encode()]:
            out.append(int(pid))
    return out


@unittest.skipUnless(shutil.which("sh") and shutil.which("git"), "needs sh and git")
class PrePushHookTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bin = os.path.join(self.root, "bin")
        os.makedirs(self.bin)
        for tool in _TOOLS:
            found = shutil.which(tool)
            if found:
                os.symlink(found, os.path.join(self.bin, tool))
        self.home = os.path.join(self.root, "tg-home")
        os.makedirs(self.home)
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        with open(HOOK_SRC) as fh:
            text = fh.read().replace("__TESTGRAPH_HOME__", self.home)
        self.hook = os.path.join(self.root, "pre-push")
        with open(self.hook, "w") as fh:
            fh.write(text)
        self.addCleanup(self._reap)

    def _reap(self):
        for pid in _stray_processes():
            os.kill(pid, signal.SIGKILL)

    def _stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def _git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", "-C", self.repo, *args], check=True, env=env,
                              capture_output=True, text=True).stdout.strip()

    def _commit(self, name):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(name)
        self._git("add", name)
        self._git("commit", "-q", "-m", name)
        return self._git("rev-parse", "HEAD")

    def _env(self, **extra):
        env = {"PATH": self.bin, "HOME": self.root, "TMPDIR": self.root}
        env.update(extra)
        return env

    def _stdin(self, local):
        return f"refs/heads/feat {local} refs/heads/feat {ZERO}\n"

    def _run(self, local, **env):
        return subprocess.run(["sh", self.hook], cwd=self.repo, env=self._env(**env),
                              input=self._stdin(local), capture_output=True,
                              text=True, timeout=60)

    def _new_branch_repo(self, default="master"):
        self._git("init", "-q", "-b", default)
        base = self._commit("base.txt")
        self._git("checkout", "-q", "-b", "feat")
        for n in ("a", "b", "c"):
            tip = self._commit(n)
        return base, tip

    # --- M5 ---------------------------------------------------------------

    def _base_arg(self, stdout):
        parts = stdout.split()
        return parts[parts.index("--base") + 1]

    def test_new_branch_in_a_master_repo_diffs_from_the_merge_base(self):
        base, tip = self._new_branch_repo(default="master")
        self._stub("python3", 'echo "$*"')
        r = self._run(tip)
        self.assertEqual(self._base_arg(r.stdout), base, r.stdout + r.stderr)

    def test_main_still_works_and_wt_base_wins(self):
        base, tip = self._new_branch_repo(default="main")
        self._stub("python3", 'echo "$*"')
        self.assertEqual(self._base_arg(self._run(tip).stdout), base)
        # An explicit wt.base outranks the guesses.
        self._git("branch", "mid", "feat~2")
        mid = self._git("rev-parse", "feat~2")
        self._git("config", "wt.base", "mid")
        self.assertEqual(self._base_arg(self._run(tip).stdout), mid)

    def test_a_dash_leading_wt_base_is_ignored_not_an_option(self):
        base, tip = self._new_branch_repo(default="master")
        self._git("config", "wt.base", "--output=/nope")
        self._stub("python3", 'echo "$*"')
        r = self._run(tip)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._base_arg(r.stdout), base)


if __name__ == "__main__":
    unittest.main()

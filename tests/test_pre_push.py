"""hooks/pre-push, run as git would run it (audit M5, H3).

The hook is shell, so these tests run the real text under `sh` (dash on Debian)
in a throwaway git repo. PATH holds symlinks to only the tools the hook needs plus
stub `python3` / `codegraph`, and deliberately NO `timeout`: that is macOS, and the
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

_TOOLS = ("sh", "git", "mktemp", "rm", "sleep", "cat", "env", "dirname", "basename")


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
        self.assertFalse(os.path.exists(os.path.join(self.bin, "timeout")))
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

    # --- H3 ---------------------------------------------------------------

    def test_selector_output_is_printed_without_gnu_timeout(self):
        _, tip = self._new_branch_repo()
        self._stub("python3", 'echo "SELECTOR $*"')
        r = self._run(tip)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SELECTOR -m testgraph.hook", r.stdout)

    def test_a_hung_selector_is_killed_with_heartbeat_and_exit_zero(self):
        _, tip = self._new_branch_repo()
        self._stub("python3", f"exec sleep {MARK}")
        t0 = time.time()
        r = self._run(tip, TESTGRAPH_HOOK_TIMEOUT="3", TESTGRAPH_HOOK_HEARTBEAT="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.time() - t0, 15)
        self.assertIn("testgraph: selector still running (1s) — Ctrl-C skips it", r.stderr)
        self.assertIn("testgraph: selector skipped (timed out after 3s)", r.stderr)
        if os.path.isdir("/proc"):
            self.assertEqual(_stray_processes(), [])

    def test_a_hung_codegraph_sync_is_bounded_and_the_selector_still_runs(self):
        _, tip = self._new_branch_repo()
        self._stub("codegraph", f"exec sleep {MARK}")
        self._stub("python3", 'echo "SELECTOR"')
        r = self._run(tip, TESTGRAPH_HOOK_SYNC_TIMEOUT="2", TESTGRAPH_HOOK_HEARTBEAT="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("testgraph: codegraph sync skipped (timed out after 2s)", r.stderr)
        self.assertIn("SELECTOR", r.stdout)

    def test_a_crashing_selector_shows_its_first_stderr_line(self):
        _, tip = self._new_branch_repo()
        self._stub("python3", 'echo "Traceback boom" >&2; echo second >&2; exit 1')
        r = self._run(tip)
        self.assertEqual(r.returncode, 0)
        self.assertIn("testgraph: selector: Traceback boom", r.stderr)
        self.assertNotIn("second", r.stderr)
        self.assertEqual([f for f in os.listdir(self.root) if f.startswith("testgraph-hook.")], [])

    def _interrupt(self, sig):
        _, tip = self._new_branch_repo()
        self._stub("python3", f"exec sleep {MARK}")
        p = subprocess.Popen(["sh", self.hook], cwd=self.repo, stdin=subprocess.PIPE,
                             stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             env=self._env(TESTGRAPH_HOOK_HEARTBEAT="1"),
                             start_new_session=True)
        p.stdin.write(self._stdin(tip))
        p.stdin.close()
        time.sleep(1.5)
        self.assertTrue(_stray_processes(), "the stub should be running by now")
        os.kill(p.pid, sig)          # the shell only: its child ignores SIGINT
        _, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(_stray_processes(), [])
        return err

    @unittest.skipUnless(os.path.isdir("/proc"), "needs /proc")
    def test_ctrl_c_kills_the_child_and_skips_the_step(self):
        self.assertIn("skipped (interrupted)", self._interrupt(signal.SIGINT))

    @unittest.skipUnless(os.path.isdir("/proc"), "needs /proc")
    def test_sigterm_kills_the_child_and_exits_zero(self):
        self._interrupt(signal.SIGTERM)

    def test_the_hook_parses_under_dash_and_bash(self):
        for shell in ("sh", "dash", "bash"):
            if shutil.which(shell):
                r = subprocess.run([shell, "-n", self.hook], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{shell}: {r.stderr}")

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

"""A throwaway Git repository, for the tests that need real Git plumbing.

Not a test module itself: `unittest discover` only collects `test*.py`.
"""

import pathlib
import subprocess
import tempfile


class Repo:
    """A repository in a temporary directory, with a base commit and a head."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "tester")
        self.git("config", "user.email", "tester@localhost")
        self.git("config", "commit.gpgsign", "false")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")

    def close(self):
        self.tmp.cleanup()

    def git(self, *args):
        done = subprocess.run(["git"] + list(args), cwd=str(self.path),
                              capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise AssertionError("git %s failed: %s" % (" ".join(args), done.stderr))
        return done.stdout

    def write(self, name, text):
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text, encoding="utf-8")

    def remove(self, name):
        (self.path / name).unlink()

    def move(self, old, new):
        self.git("mv", old, new)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def refs(self):
        return sorted(self.git("for-each-ref", "--format=%(refname) %(objectname)").splitlines())

    def dirty(self):
        return self.git("status", "--porcelain").strip()

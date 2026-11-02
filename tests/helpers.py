import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from mesh import core  # noqa: E402
from mesh.db import DB, Paths  # noqa: E402


def sh(*cmd, cwd=None, input=None, env=None):
    return subprocess.run(cmd, cwd=cwd, input=input, capture_output=True, text=True, env=env)


class Repo:
    """Throwaway git repo with mesh initialised."""

    def __init__(self, files=None, test_cmd="true", init=True):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        sh("git", "init", "-q", "-b", "main", cwd=self.root)
        for k, v in (files or {"README.md": "hi\n"}).items():
            p = self.root / k
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(v)
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        if init:
            self.mesh("init", "--no-sync")
            self.set_test(test_cmd)

    def git(self, *a, cwd=None):
        return sh("git", "-c", "user.email=t@t", "-c", "user.name=t", *a, cwd=cwd or self.root)

    def env(self):
        e = dict(os.environ)
        e["PYTHONPATH"] = str(SRC)
        e.pop("MESH_SESSION", None)
        e["MESH_ROOT"] = str(self.root)
        return e

    def mesh(self, *a, input=None, cwd=None):
        return sh(sys.executable, "-m", "mesh", *a, cwd=cwd or self.root, input=input, env=self.env())

    def set_test(self, cmd):
        p = self.root / ".mesh" / "mesh.toml"
        import re
        t = re.sub(r'^test = ".*?"', f'test = "{cmd}"', p.read_text(), flags=re.M)
        p.write_text(t)

    def db(self):
        d = DB(Paths(self.root))
        self._dbs = getattr(self, "_dbs", []) + [d]
        return d

    def session(self, cli, native, agent=None):
        db = self.db()
        sid = core.register_session(db, cli, agent, native, os.getpid())
        db.close()
        return sid

    def cleanup(self):
        for d in getattr(self, "_dbs", []):
            try:
                d.close()
            except Exception:
                pass
        self._tmp.cleanup()


class MeshCase(unittest.TestCase):
    def repo(self, **kw):
        r = Repo(**kw)
        self.addCleanup(r.cleanup)
        return r

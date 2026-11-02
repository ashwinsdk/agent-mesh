"""First-run / settings logic (UI-independent)."""
from __future__ import annotations

import getpass
import io
import time
from contextlib import redirect_stdout

from .. import adapters, core
from ..db import DB, MeshError, Paths


def installed_clis(paths: Paths) -> list[str]:
    return [n for n in ("claude", "copilot", "antigravity") if adapters.ADAPTERS[n](paths).available()]


def needs_setup(paths: Paths) -> bool:
    cfg = core.load_config(paths)
    return not cfg["app"]["cli"] or not cfg["autonomy"]["accepted_at"]


def apply_setup(paths: Paths, cli: str, billing: dict, plan_approval: bool, accepted: bool) -> list[str]:
    """Write config, regenerate client files, and commit the mesh-generated files so worker worktrees inherit the safety hooks."""
    if not accepted:
        raise MeshError("the autonomy policy must be accepted to run headless workers")
    if cli not in installed_clis(paths):
        raise MeshError(f"{cli} is not installed")
    cfg = core.load_config(paths)
    core.set_toml_section(paths.config, "app", {"cli": cli})
    b = dict(cfg["billing"]); b.update({k: v for k, v in billing.items() if v in ("subscription", "api", "credits", "unknown")})
    core.set_toml_section(paths.config, "billing", b)
    core.set_toml_section(paths.config, "autonomy", {
        "mode": "worktree-confined", "accepted_by": getpass.getuser(), "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "accepted_cli_flags": "claude:acceptEdits; copilot:allow-all-tools; antigravity:dangerously-skip-permissions", "plan_approval": plan_approval})
    log = adapters.sync_all(paths, ["claude", "copilot", "antigravity"])
    from .. import cli2
    db = DB(paths)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli2.cmd_commit_config(db, None, "human")
        db.event("human", "autonomy.accepted", None, {"via": "app setup", "cli": cli})
        log.append(buf.getvalue().strip())
    finally:
        db.close()
    return log

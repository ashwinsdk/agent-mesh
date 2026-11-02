"""Agent registry (14 reusable roles) and project-profile detection."""
from __future__ import annotations

import json
from pathlib import Path

from .db import MeshError, atomic_write

COMMON_COMM = ("Report via `mesh msg send`; ask questions with type=question; announce blockers with type=blocker; "
               "never edit files owned by another session; request ownership with type=file_request.")
COMMON_DONE = "Acceptance criteria met, tests run and results recorded, `mesh checkpoint create` written, task moved to REVIEW."


def _a(id_, role, resp, caps, restr, ctx, outs, accept, sub_tools="Read, Grep, Glob, Edit, Write, Bash"):
    return dict(id=id_, role=role, responsibilities=resp, capabilities=caps, restrictions=restr, required_context=ctx,
                expected_outputs=outs, communication=COMMON_COMM, acceptance=accept, completion=COMMON_DONE, tools=sub_tools)


AGENTS = [
    _a("orchestrator", "Orchestrator", "Analyse a request, decompose into epics/tasks with dependencies and file ownership, run independent tasks in parallel, monitor, integrate, validate. Spawn the fewest agents that work; do simple tasks yourself.",
       "task decomposition, delegation, progress monitoring, conflict resolution", "Does not implement large features directly; does not assign overlapping file ownership without worktree isolation.",
       "project memory, task board, active locks", "task tree in `mesh`, assignment messages, integration report", "Every subtask COMPLETED, integration branch green.", "Read, Grep, Glob, Bash, Agent"),
    _a("planner", "Project Planner", "Turn goals into a prioritised backlog with acceptance criteria and dependency order.", "estimation, scoping, risk listing",
       "No code changes.", "project memory, requirements", "tasks created via `mesh task create`, risks in memory", "Each task has acceptance criteria and file scope.", "Read, Grep, Glob, Bash"),
    _a("researcher", "Researcher", "Investigate libraries, APIs, prior art, and the existing codebase; report verified findings with sources.", "code search, documentation reading",
       "Read-only on source; mark unverified claims as assumed.", "question, relevant files", "findings added with `mesh mem fact --status verified|assumed --source`", "Findings cite sources.", "Read, Grep, Glob, Bash, WebFetch, WebSearch"),
    _a("architect", "System Architect", "Define structure, interfaces, data flow and record decisions as ADRs.", "design, trade-off analysis", "Does not implement features; does not overrule ADRs without a new ADR.",
       "project memory, ADRs, structure", "ADRs via `mesh mem adr`, interface specs", "Decision, context and consequences recorded.", "Read, Grep, Glob, Bash"),
    _a("frontend", "Frontend Developer", "Implement UI components, state, accessibility and client-side tests.", "UI frameworks, styling, a11y", "Only touches owned UI paths; no backend schema changes.",
       "task brief, UI conventions, API contracts", "components, tests, notes", "UI tests pass; contract respected."),
    _a("backend", "Backend Developer", "Implement services, APIs, business logic and tests.", "APIs, services, error handling", "Only touches owned backend paths; no schema migrations without database agent.",
       "task brief, API contracts", "endpoints, tests, notes", "Tests pass; contract respected."),
    _a("database", "Database Engineer", "Design schemas, migrations, queries and indexing; verify migration safety.", "SQL/NoSQL modelling, migrations", "Never runs destructive migrations against non-local data; migrations must be reversible or documented.",
       "schema, data access code", "migrations, schema docs, tests", "Migrations apply and roll back cleanly."),
    _a("devops", "DevOps Engineer", "Build, CI/CD, containers, infrastructure and deployment config.", "CI, containers, IaC", "No production deploys or credential changes without explicit human approval.",
       "build config, infra files", "pipeline/IaC changes, runbook notes", "Pipeline validated locally where possible."),
    _a("security", "Security Auditor", "Review for vulnerabilities, secrets, unsafe dependencies and permissions.", "threat modelling, static review", "Read-only unless fixing an assigned finding; never exfiltrates secrets.",
       "diff, dependency manifests", "findings with severity and fixes", "Findings triaged; no unaddressed high severity.", "Read, Grep, Glob, Bash"),
    _a("qa", "QA Engineer", "Write and run tests, find edge cases, report failures with repro steps.", "unit/integration/e2e tests", "Does not change production code except for testability, and says so.",
       "acceptance criteria, changed files", "tests, test report", "New tests fail before/pass after; report recorded."),
    _a("debugger", "Debugging Specialist", "Reproduce, isolate and fix defects; document root cause.", "tracing, bisecting, profiling", "Fix minimal; no refactors beyond the defect.",
       "failure report, logs", "fix, regression test, root-cause note in lessons", "Regression test added."),
    _a("reviewer", "Code Reviewer", "Review diffs for correctness, style, maintainability; give actionable feedback.", "diff review", "Does not edit reviewed code; sends review messages instead.",
       "diff, task acceptance", "review message (type=review) with must-fix / suggestions", "Verdict given.", "Read, Grep, Glob, Bash"),
    _a("docs", "Documentation Writer", "Write and update docs, READMEs, changelogs, usage examples.", "technical writing", "Documents verified behaviour only.",
       "changed code, ADRs", "docs", "Docs match code."),
    _a("integrator", "Integration Agent", "Merge completed task branches, resolve conflicts deliberately, rerun tests, update task and memory state.", "git merge, conflict resolution, validation",
       "Never discards another agent's changes; conflicts are resolved with the owners informed; does not promote to main without human approval.", "task branches, test commands", "integration branch, report", "Merged branch passes validation."),
]


def render_agent(a: dict) -> str:
    fm = "\n".join(f"{k}: {a[k]}" for k in ("id", "role", "capabilities", "restrictions", "required_context", "expected_outputs", "acceptance", "completion"))
    return (f"---\n{fm}\n---\n# {a['role']}\n\n## Responsibilities\n{a['responsibilities']}\n\n## Restrictions\n{a['restrictions']}\n\n"
            f"## Communication\n{a['communication']}\n\n## Completion\n{a['completion']}\n")


def parse_agent(text: str) -> dict:
    if not text.startswith("---\n"):
        raise MeshError("agent file missing frontmatter")
    head, _, body = text[4:].partition("\n---\n")
    d = {}
    for ln in head.splitlines():
        if ":" in ln:
            k, _, v = ln.partition(":")
            d[k.strip()] = v.strip()
    d["body"] = body
    return d


def install_agents(agents_dir: Path) -> list[str]:
    out = []
    for a in AGENTS:
        p = agents_dir / f"{a['id']}.md"
        if not p.exists():  # never overwrite human edits
            atomic_write(p, render_agent(a))
            out.append(a["id"])
    return out


def load_agents(agents_dir: Path) -> list[dict]:
    out = []
    for p in sorted(agents_dir.glob("*.md")):
        try:
            d = parse_agent(p.read_text())
            d["tools"] = next((x["tools"] for x in AGENTS if x["id"] == d.get("id")), "Read, Grep, Glob, Edit, Write, Bash")
            out.append(d)
        except MeshError:
            continue
    return out


def suggest(title: str, description: str = "") -> list[str]:
    """Pick the smallest sensible agent set by keyword; default to one generalist-by-domain agent."""
    t = f"{title} {description}".lower()
    rules = [("frontend", ("ui", "component", "css", "react", "page", "frontend")), ("backend", ("api", "endpoint", "service", "backend", "server")),
             ("database", ("schema", "migration", "sql", "database", "query")), ("devops", ("ci", "deploy", "docker", "pipeline", "infra")),
             ("security", ("security", "vulnerab", "secret", "auth", "audit")), ("qa", ("test", "coverage", "qa")),
             ("debugger", ("bug", "crash", "fix", "regression", "error")), ("docs", ("doc", "readme", "changelog")),
             ("researcher", ("research", "investigate", "compare", "evaluate")), ("architect", ("architecture", "design", "adr"))]
    hit = [a for a, kws in rules if any(k in t for k in kws)]
    return hit[:4] or ["backend"]


# ---------------------------------------------------------------- profiles

PROFILES = [
    # (profile, marker files/globs, test, lint)
    ("web3", ("hardhat.config.*", "foundry.toml", "truffle-config.js", "Anchor.toml"), "", ""),
    ("game", ("ProjectSettings/ProjectVersion.txt", "project.godot", "*.uproject"), "", ""),
    ("mobile", ("pubspec.yaml", "android/build.gradle", "ios/Podfile", "*.xcodeproj"), "", ""),
    ("ml", ("environment.yml", "mlruns", "*.ipynb", "dvc.yaml"), "", ""),
    ("infrastructure", ("*.tf", "cdk.json", "serverless.yml", "Pulumi.yaml", "helm"), "", ""),
    ("web", ("next.config.*", "vite.config.*", "angular.json", "svelte.config.*", "nuxt.config.*"), "", ""),
]
STACKS = [
    ("package.json", "npm test --silent", "npm run lint --silent"),
    ("pyproject.toml", "python3 -m pytest -q", "ruff check ."),
    ("setup.py", "python3 -m pytest -q", ""),
    ("requirements.txt", "python3 -m pytest -q", ""),
    ("Cargo.toml", "cargo test", "cargo clippy"),
    ("go.mod", "go test ./...", "go vet ./..."),
    ("pom.xml", "mvn -q test", ""),
    ("build.gradle", "gradle test", ""),
    ("Gemfile", "bundle exec rake test", ""),
    ("composer.json", "composer test", ""),
    ("mix.exs", "mix test", ""),
    ("Makefile", "make test", ""),
]


def detect(root: Path) -> dict:
    """Detect stack and profile from manifests; never assumes a technology."""
    def has(pat):
        return any(root.glob(pat)) or any(root.glob(f"*/{pat}"))
    stack = [(m, t, l) for m, t, l in STACKS if (root / m).exists()]
    test = stack[0][1] if stack else ""
    lint = stack[0][2] if stack else ""
    if (root / "package.json").exists():
        try:
            scripts = json.loads((root / "package.json").read_text()).get("scripts", {})
            test = "npm test --silent" if "test" in scripts else ""
            lint = "npm run lint --silent" if "lint" in scripts else ""
        except ValueError:
            pass
    profile = next((p for p, pats, *_ in PROFILES if any(has(x) for x in pats)), None)
    pkgs = [d for d in root.glob("*/package.json")] + [d for d in root.glob("packages/*/package.json")] + [d for d in root.glob("apps/*/package.json")]
    if len(pkgs) >= 2 or (root / "pnpm-workspace.yaml").exists() or (root / "lerna.json").exists() or (root / "nx.json").exists():
        profile = "monorepo"
    if profile is None:
        profile = "backend" if stack else "generic"
    return {"profile": profile, "manifests": [s[0] for s in stack], "test": test, "lint": lint}

"""Secret detection, destructive-command guards, redaction. Treats all inputs as untrusted text."""
from __future__ import annotations

import math
import re

SECRET_PATTERNS = [
    ("aws-access-key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("generic-assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret|token|passwd|password|private[_-]?key)\b\s*[:=]\s*['\"]?([A-Za-z0-9/+_\-]{16,})['\"]?")),
]


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    freq = {c: s.count(c) / len(s) for c in set(s)}
    return -sum(p * math.log2(p) for p in freq.values())


def scan_secrets(text: str) -> list[str]:
    hits = []
    for name, rx in SECRET_PATTERNS:
        for m in rx.finditer(text):
            if name == "generic-assignment" and _entropy(m.group(2)) < 3.0:
                continue
            hits.append(name)
    return hits


def redact(text: str) -> str:
    for name, rx in SECRET_PATTERNS:
        text = rx.sub(f"[REDACTED:{name}]", text)
    return text


SAFE_ARTIFACTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "dist", "build", ".cache", ".next", ".turbo", "coverage", ".venv", "venv", "target"}
_RM = re.compile(r"^\s*(?:sudo\s+)?rm\s+(.*)$")


def _safe_rm(args: str) -> bool:
    """True if `rm -rf <targets>` only removes disposable build artefacts by RELATIVE path (no absolute, ~, $VAR, .., bare * or .)."""
    import shlex
    try:
        toks = shlex.split(args)
    except ValueError:
        return False
    flags = [t for t in toks if t.startswith("-")]
    targets = [t for t in toks if not t.startswith("-")]
    if not targets or not any("r" in f.lower() for f in flags):
        return False
    for t in targets:
        if t.startswith(("/", "~", "$")) or ".." in t.split("/") or t.strip("/.*") == "":
            return False
        leaf = t.rstrip("/").split("/")[-1]
        if not (leaf in SAFE_ARTIFACTS or leaf.endswith(".pyc") or (leaf == "*.pyc")):
            return False
    return True


DESTRUCTIVE = [
    (re.compile(r"\bgit\s+push\b.*(--force\b|-f\b|--force-with-lease)"), "force push"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (re.compile(r"\bgit\s+clean\s+-[a-zA-Z]*f"), "git clean -f"),
    (re.compile(r"\bgit\s+checkout\s+--\s+\."), "discard all changes"),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "force delete branch"),
    (re.compile(r"\b(drop\s+(table|database)|truncate\s+table)\b", re.I), "destructive SQL"),
    (re.compile(r"\bmkfs\b|\bdd\s+if=.*\bof=/dev/"), "disk overwrite"),
    (re.compile(r">\s*\.mesh/runtime/mesh\.db"), "truncating mesh database"),
    (re.compile(r"\brm\b.*\.mesh\b"), "deleting mesh state"),
    (re.compile(r"\bchmod\s+-R\s+777\b"), "world-writable chmod"),
    (re.compile(r"\bcurl\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b"), "pipe remote script to shell"),
]


_RM_RF = re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-r\s+-f|-f\s+-r)\b")


_PROTECTED_SHELL = re.compile(r"\.mesh/(?:mesh\.toml|lib\b|memory/project\.md|instructions\b|agents\b)|(?:^|[\s/])\.mesh\b[^\s]*/(?:mesh\.toml)")
_WRITE_HINT = re.compile(r"(>>?|\btee\b|\bsed\s+-[a-z]*i|\bperl\s+-[a-z]*i|\bmv\b|\bcp\b|\brm\b|\btruncate\b|\bdd\b|\binstall\b|\bchmod\b|\bln\b|\bpython3?\s+-c\b|\bnode\s+-e\b|\bruby\s+-e\b|\bgit\s+(checkout|restore|reset)\b)")


def check_mesh_internals(cmd: str) -> str | None:
    """Agents must use `mesh` commands, never the coordination database or mesh's own config/library by hand."""
    if re.search(r"mesh\.db", cmd):
        return "direct access to the mesh database (use `mesh` commands)"
    if _PROTECTED_SHELL.search(cmd) and _WRITE_HINT.search(cmd):
        return "modifying mesh's own config/library/memory from the shell (use `mesh` commands)"
    return None


def check_command(cmd: str) -> str | None:
    """Return a human-readable reason if the command needs explicit human approval."""
    internal = check_mesh_internals(cmd)
    if internal:
        return internal
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        if _RM_RF.search(seg):
            m = _RM.match(seg.strip())
            if not (m and _safe_rm(m.group(1))):
                return "recursive force delete"
    for rx, why in DESTRUCTIVE:
        if rx.search(cmd):
            return why
    return None


PROTECTED = [".mesh/mesh.toml", ".mesh/instructions/", ".mesh/agents/", ".mesh/memory/project.md", ".git/"]
SECRET_FILES = re.compile(r"(^|/)(\.env(\.[^/]+)?|id_rsa|id_ed25519|credentials|.*\.pem|.*\.key)$")


def check_path_write(rel: str) -> str | None:
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if SECRET_FILES.search(rel) and not rel.endswith((".example", ".sample")):
        return "write to a secrets-bearing file"
    for p in PROTECTED:
        if rel == p.rstrip("/") or rel.startswith(p):
            return f"write to protected path {p} (use mesh commands / human approval)"
    return None

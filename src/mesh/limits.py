"""Daily / weekly / other usage-limit windows, taken ONLY from what each CLI itself reports.

Sources (official, no private endpoints, no model turn is run):
  antigravity : `agy -p "/usage"`  (the CLI prints quota with reset times; JSON/piped output uses UTC)
  copilot     : `copilot -p "/usage"`  (session AI credits plus usage-limit progress when limits apply)
  claude      : statusline rate_limits (5h / 7d) recorded by `mesh statusline` for interactive sessions
The output format of the first two is not documented, so parsing is tolerant (JSON or text) and the raw output is always kept
and shown: if a window cannot be parsed the user sees the CLI's own words instead of a guess. Nothing here is ever estimated.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import adapters
from .db import DB, now
from .safety import redact

WINDOW_WORDS = [
    ("daily", re.compile(r"\b(daily|per day|each day|24[- ]?h(?:ou)?rs?|today)\b", re.I)),
    ("weekly", re.compile(r"\b(weekly|per week|each week|7[- ]?day|this week)\b", re.I)),
    ("monthly", re.compile(r"\b(monthly|per month|each month|this month|billing (?:period|cycle))\b", re.I)),
    ("5-hour", re.compile(r"\b(?:5|five)[- ]?h(?:(?:ou)?rs?)?\b", re.I)),
    ("hourly", re.compile(r"\b(hourly|per hour)\b", re.I)),
    ("session", re.compile(r"\bsession\b", re.I)),
]
USED_KEYS = ("usedpercent", "percentused", "usedpct", "usagepercent", "used_percent", "percent_used", "usedfraction", "used_fraction")
REM_KEYS = ("remainingpercent", "percentremaining", "remainingpct", "percentleft", "remaining_percent", "percent_remaining", "remainingfraction", "remaining_fraction")
LABEL_KEYS = ("name", "label", "window", "period", "bucket", "title", "type", "kind", "scope", "id", "model", "quota")


def command(cli: str, adapter, fmt: str = "json") -> list[str]:
    b = adapter.bin()
    if cli == "antigravity":
        return [b, "-p", "/usage"] + (["--output-format", "json"] if fmt == "json" else [])
    return [b, "-p", "/usage", "--allow-all-tools", "--silent"]  # copilot: -p requires allow-all-tools; /usage never reaches the model


def normalise_window(label: str | None, text: str = "") -> str:
    blob = f"{label or ''} {text}"
    for name, rx in WINDOW_WORDS:
        if rx.search(blob):
            return name
    return (label or "limit").strip().lower()[:24] or "limit"


def _ts(v):
    """(epoch seconds | None, display text) from an epoch number or ISO string; otherwise text only."""
    if isinstance(v, (int, float)) and v > 1e9:
        v = v / 1000 if v > 1e12 else v
        return float(v), time.strftime("%a %d %b %H:%M", time.localtime(v))
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            t = d.timestamp()
            return t, time.strftime("%a %d %b %H:%M", time.localtime(t))
        except ValueError:
            return None, v.strip()[:60]
    return None, None


WINDOW_NAMES = {"5h": "5-hour", "5-hour": "5-hour", "five_hour": "5-hour", "weekly": "weekly", "daily": "daily", "monthly": "monthly", "hourly": "hourly"}


def _from_groups(doc, out: list) -> None:
    """Antigravity's real `/usage` JSON (verified 2026-11-03): command.data.groups[].buckets[] with window, remaining_fraction and
    an ISO reset_time in UTC, one group per model family (Gemini vs Claude/GPT)."""
    def find(o):
        if isinstance(o, dict):
            g = o.get("groups")
            if isinstance(g, list) and g and isinstance(g[0], dict) and isinstance(g[0].get("buckets"), list):
                yield g
            for v in o.values():
                yield from find(v)
        elif isinstance(o, list):
            for v in o:
                yield from find(v)
    for groups in find(doc):
        for g in groups:
            for b in g.get("buckets") or []:
                rf = _num(b.get("remaining_fraction"))
                if rf is None:
                    continue
                rem = rf * 100.0 if 0.0 <= rf <= 1.0 else rf
                w = str(b.get("window") or "").lower()
                win = WINDOW_NAMES.get(w) or normalise_window(b.get("name"), "")
                rts, rtxt = _ts(b.get("reset_time"))
                out.append({"group": g.get("name"), "window": win, "used_pct": max(0.0, 100.0 - rem), "remaining_pct": rem,
                            "resets_text": rtxt, "resets_ts": rts, "detail": (b.get("description") or "")[:200]})


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _walk(o, label, out):
    if isinstance(o, dict):
        low = {k.lower(): v for k, v in o.items()}
        used = next((low[k] for k in USED_KEYS if k in low), None)
        rem = next((low[k] for k in REM_KEYS if k in low), None)
        u_key = next((k for k in USED_KEYS if k in low), "")
        r_key = next((k for k in REM_KEYS if k in low), "")
        used_pct = _num(used) * (100 if "fraction" in u_key and _num(used) is not None and _num(used) <= 1 else 1) if _num(used) is not None else None
        rem_pct = _num(rem) * (100 if "fraction" in r_key and _num(rem) is not None and _num(rem) <= 1 else 1) if _num(rem) is not None else None
        n_used, n_lim = _num(low.get("used")), _num(low.get("limit") if "limit" in low else low.get("quota", low.get("allowance")))
        if used_pct is None and rem_pct is None and n_used is not None and n_lim:
            used_pct = 100.0 * n_used / n_lim
        if rem_pct is None and used_pct is not None:
            rem_pct = max(0.0, 100.0 - used_pct)
        if used_pct is None and rem_pct is not None:
            used_pct = max(0.0, 100.0 - rem_pct)
        if used_pct is not None or rem_pct is not None:
            lab = next((str(low[k]) for k in LABEL_KEYS if isinstance(low.get(k), (str, int)) and str(low[k]).strip()), label)
            reset_v = next((v for k, v in o.items() if re.search(r"reset|renew|expire", k, re.I) and v not in (None, "")), None)
            rts, rtxt = _ts(reset_v)
            out.append({"window": normalise_window(lab, json.dumps({k: v for k, v in o.items() if isinstance(v, str)})[:200]), "used_pct": used_pct, "remaining_pct": rem_pct,
                        "resets_text": rtxt, "resets_ts": rts, "detail": json.dumps(o)[:300]})
            return
        for k, v in o.items():
            _walk(v, k, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, label, out)


PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
RESET = re.compile(r"reset[s]?\s*:?\s*((?:at|in|on)\s+[^|;()\[\]•·]+|[^|;()\[\]•·]+)", re.I)  # keeps "in 5h" / "at 09:00" so the meaning survives


def _parse_text(raw: str) -> list[dict]:
    out = []
    for line in raw.splitlines():
        line = line.strip(" \t│|•·-─")
        if not line or not PCT.search(line):
            continue
        low = line.lower()
        pct = float(PCT.search(line).group(1))
        used = remaining = None
        if re.search(r"\b(left|remaining|available)\b", low):
            remaining, used = pct, max(0.0, 100.0 - pct)
        elif re.search(r"\b(used|consumed|usage)\b", low):
            used, remaining = pct, max(0.0, 100.0 - pct)
        m = RESET.search(line)
        rtxt = m.group(1).strip(" .,") if m else None
        out.append({"window": normalise_window(None, line), "used_pct": used, "remaining_pct": remaining, "resets_text": rtxt, "resets_ts": None, "detail": line[:200]})
    return out


def parse(raw: str) -> list[dict]:
    """Windows found in the CLI's output (JSON first, then text). Possibly empty: the caller then shows the raw text."""
    raw = raw or ""
    out: list[dict] = []
    docs = []
    try:
        docs.append(json.loads(raw))
    except ValueError:
        for ln in raw.splitlines():
            ln = ln.strip()
            if ln.startswith(("{", "[")):
                try:
                    docs.append(json.loads(ln))
                except ValueError:
                    pass
    for d in docs:
        _from_groups(d, out)
    if out:
        return out  # structured form found: the same numbers also appear in the human text, do not parse them twice
    for d in docs:
        _walk(d, None, out)
        # JSON events often carry the human text in a string field; parse that too if no structure matched
        if not out:
            for s in _strings(d):
                out += _parse_text(s)
    if not out:
        out = _parse_text(raw)
    seen, dedup = set(), []
    for w in out:
        k = (w.get("group"), w["window"], w["used_pct"], w["remaining_pct"], w["resets_text"])
        if k not in seen:
            seen.add(k); dedup.append(w)
    return dedup


def _strings(o):
    if isinstance(o, str):
        if len(o) > 6:
            yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def run_cli(db: DB, cli: str, timeout: float = 60) -> tuple[bool, str, str | None, str]:
    """Run the CLI's own /usage in the disposable sandbox worktree. Returns (ok, raw, error, command string)."""
    from . import brain
    ad = adapters.ADAPTERS[cli](db.paths)
    if not ad.available():
        return False, "", f"{cli} is not installed", ""
    cwd = brain.sandbox(db, f"limits-{cli}")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "TMPDIR", "SHELL", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")
           or k.startswith(("COPILOT_", "GITHUB_TOKEN", "GH_TOKEN", "GEMINI_", "GOOGLE_", "MESH_BIN_", "FAKE_"))}
    env["MESH_ROOT"] = str(db.paths.root)
    env["MESH_NOHOOK"] = "1"  # /usage is a report, not an agent session: keep it out of the sessions list
    last = ""
    for fmt in (("json", "text") if cli == "antigravity" else ("text",)):
        cmd = command(cli, ad, fmt)
        try:
            p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, "", f"`{' '.join(cmd[1:])}` did not finish within {int(timeout)}s", " ".join(cmd)
        last = " ".join(cmd)
        out = (p.stdout or "").strip() or (p.stderr or "").strip()
        if out and p.returncode in (0, 2):  # agy exits 2 for a CLI-handled slash command
            return True, out, None, last
    return False, "", "the CLI printed nothing for /usage", last


def refresh(db: DB, cli: str, timeout: float = 60) -> dict:
    ok, raw, err, cmd = run_cli(db, cli, timeout)
    raw = redact(raw)[:12000]
    wins = parse(raw) if ok else []
    t = now()
    with db.tx() as c:
        c.execute("DELETE FROM limits WHERE cli=?", (cli,))
        for w in wins:
            c.execute("INSERT INTO limits(ts,cli,window,used_pct,remaining_pct,resets_text,resets_ts,detail,source,grp) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (t, cli, w["window"], w["used_pct"], w["remaining_pct"], w["resets_text"], w["resets_ts"], w["detail"], cmd, w.get("group")))
        c.execute("INSERT INTO limit_raw(cli,ts,ok,raw,error,command) VALUES(?,?,?,?,?,?) ON CONFLICT(cli) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, raw=excluded.raw, error=excluded.error, command=excluded.command",
                  (cli, t, 1 if ok else 0, raw, err, cmd))
        db.event("limits", "limits.refreshed", None, {"cli": cli, "ok": ok, "windows": len(wins)}, conn=c)
    return {"cli": cli, "ok": ok, "error": err, "windows": wins, "raw": raw, "command": cmd}


def rel(ts: float | None) -> str | None:
    """'4d 1h' / '4h 34m' / '12m' until a reset the CLI reported (computed from its own timestamp)."""
    if not ts:
        return None
    d = int(ts - now())
    if d <= 0:
        return "now"
    days, rem = divmod(d, 86400)
    hrs, rem = divmod(rem, 3600)
    mins = rem // 60
    return f"{days}d {hrs}h" if days else f"{hrs}h {mins}m" if hrs else f"{mins}m"


def short_group(name: str | None) -> str:
    if not name:
        return ""
    n = re.sub(r"\bmodels?\b", "", name, flags=re.I).replace(" and ", "/").strip()
    return n.replace("Claude/GPT", "Claude/GPT")[:14]


def label(w: dict) -> str:
    """One readable line for a window, never inventing a field the CLI did not report."""
    parts = ([short_group(w.get("group"))] if w.get("group") else []) + [w["window"]]
    if w.get("remaining_pct") is not None:
        parts.append(f"{w['remaining_pct']:.0f}% left")
    elif w.get("used_pct") is not None:
        parts.append(f"{w['used_pct']:.0f}% used")
    else:
        parts.append(w.get("detail") or "reported")
    r = rel(w.get("resets_ts"))
    if r:
        parts.append(f"resets in {r}" + (f" ({w['resets_text']})" if w.get("resets_text") else ""))
    elif w.get("resets_text"):
        parts.append(f"resets {w['resets_text']}")
    return " · ".join(parts)

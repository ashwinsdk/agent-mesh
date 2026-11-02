"""Replay of REAL captured CLI output (claude 2.1.316, agy, copilot 1.0.80; captured 2026-11-02 with `mesh probe`, sanitized).
These guard the parsers against the vendors' actual formats, not against what the docs say."""
import json
import unittest
from pathlib import Path

from helpers import MeshCase
from mesh import streams
from mesh.runner import Acc

FX = Path(__file__).resolve().parent / "fixtures" / "streams"


def events(cli, name=None):
    out = []
    for line in (FX / (name or f"{cli}.jsonl")).read_text().splitlines():
        out += streams.parse_line(cli, line)
    return out


def replay(cli):
    acc = Acc(); res = None; sess = None
    for e in events(cli):
        sess = sess or e.get("session")
        if e["kind"] == "usage":
            acc.add(e)
        elif e["kind"] == "result":
            res = e; acc.final(e)
    return acc, res, sess, events(cli)


class TestRealStreams(unittest.TestCase):
    def test_claude_real_stream(self):
        acc, res, sess, ev = replay("claude")
        self.assertEqual(sess, "4a860090-38bd-4f3c-9906-d7e7f0b3a8db")
        self.assertTrue(res["ok"])
        self.assertEqual(res["usage"], {"input": 4, "output": 133, "cache_read": 42799, "cache_create": 16880})
        self.assertAlmostEqual(res["cost"], 0.0478179)
        self.assertEqual(res["context_size"], 1_000_000)  # modelUsage.contextWindow
        self.assertEqual(acc.totals(), (4, 133, 42799, 16880))
        self.assertEqual(acc.cost, res["cost"])
        self.assertIn("tool", {e["kind"] for e in ev})
        self.assertNotIn("other", {e["kind"] for e in ev})  # nothing unparsed

    def test_antigravity_real_stream_nested_events_and_delta_usage(self):
        acc, res, sess, ev = replay("antigravity")
        self.assertEqual(sess, "90aca43f-5ef0-41a9-84d3-4594c47f79c7")
        self.assertTrue(res["ok"])  # regression: nested result was read as failure -> every good run looked like a crash
        deltas = [e for e in ev if e["kind"] == "usage"]
        self.assertTrue(all(d["scope"] == "delta" for d in deltas))
        self.assertEqual((sum(d["input"] for d in deltas), sum(d["output"] for d in deltas)), (14259, 399))  # per-step usage sums to the result total
        self.assertEqual(res["usage"]["input"], 14259); self.assertEqual(res["usage"]["output"], 399)
        self.assertEqual(acc.totals()[:2], (14259, 399))  # result replaces (does not add to) the running sum
        self.assertIsNone(res["cost"])  # Antigravity exposes no cost
        tools = [e for e in ev if e["kind"] == "tool"]
        self.assertEqual(len(tools), 1)  # ACTIVE only; the DONE echo is not double-counted
        self.assertEqual(tools[0]["name"], "write_file")

    def test_copilot_real_stream_and_otel(self):
        acc, res, sess, ev = replay("copilot")
        self.assertEqual(sess, "ade49e69-8890-4a42-bbd6-ef682d9601de")  # only present on the result event
        self.assertTrue(res["ok"])
        kinds = [e["kind"] for e in ev]
        self.assertEqual(kinds.count("tool"), 1)  # regression: 31 streaming deltas were counted as tool calls
        self.assertAlmostEqual(acc.aiu, 0.3135996)  # session.usage_checkpoint.totalNanoAiu / 1e9
        otel = streams.parse_otel_file((FX / "copilot_otel.jsonl").read_text())
        # regression: invoke_agent repeats its chat children; summing both doubled the tokens (66006 vs the true 33003)
        self.assertEqual((otel["input"], otel["output"], otel["cache_read"]), (33003, 72, 17792))
        self.assertEqual(otel["source"], "invoke_agent"); self.assertEqual(otel["session"], "ade49e69-8890-4a42-bbd6-ef682d9601de")
        self.assertAlmostEqual(otel["nano_aiu"] / 1e9, acc.aiu)  # two independent sources agree

    def test_otel_without_invoke_agent_sums_chat_spans_only_once(self):
        chat = [json.dumps({"type": "span", "attributes": {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 2}})] * 2
        self.assertEqual(streams.parse_otel_file("\n".join(chat))["input"], 20)
        self.assertIsNone(streams.parse_otel_file('{"type":"metric","name":"x"}\nnot json\n'))  # nothing found -> unavailable, not zero

    def test_failures_are_classified_not_swallowed(self):
        lim = streams.parse_line("copilot", json.dumps({"type": "session.error", "data": {"message": "Rate limit exceeded (429)"}}))
        self.assertEqual(lim[0]["kind"], "limit")
        bad = streams.parse_line("copilot", json.dumps({"type": "result", "sessionId": "s", "exitCode": 1}))
        self.assertFalse(bad[0]["ok"])
        ag = streams.parse_line("antigravity", json.dumps({"event": "result", "result": {"conversation_id": "c", "status": "ERROR", "error": "quota exhausted"}}))
        self.assertEqual([e["kind"] for e in ag], ["result", "limit"])
        self.assertTrue(any(e["kind"] == "limit" for e in streams.parse_line("claude", json.dumps(
            {"type": "system", "subtype": "api_retry", "error": "rate_limit", "error_status": 429, "attempt": 1}))))


if __name__ == "__main__":
    unittest.main()

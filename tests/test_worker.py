"""Offline tests for the Subscription Usage worker (stdlib unittest, no network)."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import worker  # noqa: E402

SOON = (datetime.now(timezone.utc) + timedelta(days=3, hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")

OPENUSAGE = [
    {"displayName": "Codex", "providerId": "codex", "plan": "Business Premium", "lines": [
        {"type": "progress", "label": "Weekly", "used": 49, "limit": 100, "resetsAt": SOON},
        {"type": "text", "label": "Today", "value": "$17.54 · 48.4M tokens"},
        {"type": "text", "label": "Last 30 Days", "value": "$737.06 · 644.4M tokens"},
    ]},
    {"displayName": "Grok", "providerId": "grok", "plan": "SuperGrok", "lines": [
        {"type": "progress", "label": "Weekly limit", "used": 10, "limit": 100, "resetsAt": SOON},
        {"type": "chart", "label": "Usage Trend", "points": [{"value": 0}, {"value": 5}, {"value": 2}]},
    ]},
]

CODEXBAR = {
    "codex": [{"provider": "codex", "account": "someone@example.com", "usage": {
        "secondary": {"usedPercent": 49, "windowMinutes": 10080, "resetsAt": SOON}},
        "pace": {"secondary": {"willLastToReset": False, "etaSeconds": 3600 * 80}}}],
    "claude": [{"provider": "claude", "usage": {
        "primary": {"usedPercent": 18, "windowMinutes": 300, "resetsAt": SOON},
        "secondary": {"usedPercent": 95, "windowMinutes": 10080, "resetsAt": SOON},
        "extraRateWindows": [{"id": "x", "title": "Fable only",
                              "window": {"usedPercent": 0, "windowMinutes": 10080, "resetsAt": SOON}}]}}],
    "grok": [{"provider": "grok", "usage": {"primary": {"usedPercent": 10, "resetsAt": SOON}}}],
}


class Formatting(unittest.TestCase):
    def test_window_labels(self) -> None:
        self.assertEqual(worker.window_label(300), "5시간 세션")
        self.assertEqual(worker.window_label(10080), "주간")
        self.assertEqual(worker.window_label(None), "한도")

    def test_human_span(self) -> None:
        self.assertEqual(worker.human_span(3 * 86400 + 4 * 3600), "3일 4시간")
        self.assertEqual(worker.human_span(25 * 60), "25분")

    def test_mask(self) -> None:
        self.assertEqual(worker.mask("someone@example.com", on=True), "som…@example.com")
        self.assertEqual(worker.mask("someone@example.com", on=False), "someone@example.com")

    def test_reset_text_mentions_remaining(self) -> None:
        self.assertIn("남음", worker.reset_text(SOON))


class Model(unittest.TestCase):
    def setUp(self) -> None:
        self.model = worker.build_model(OPENUSAGE, CODEXBAR, mask_on=True)
        self.by_id = {m["id"]: m for m in self.model}

    def test_codexbar_overrides_windows_and_keeps_plan(self) -> None:
        codex = self.by_id["codex"]
        self.assertEqual(codex["plan"], "Business Premium")
        self.assertEqual(codex["accounts"][0]["label"], "som…@example.com")
        self.assertEqual(codex["accounts"][0]["pace"]["will_last"], False)

    def test_extra_windows_and_borrowed_label(self) -> None:
        labels = [w["label"] for w in self.by_id["claude"]["accounts"][0]["windows"]]
        self.assertEqual(labels, ["5시간 세션", "주간", "주간 · Fable 전용"])
        self.assertEqual(self.by_id["grok"]["accounts"][0]["windows"][0]["label"], "주간")

    def test_sorted_by_highest_usage(self) -> None:
        self.assertEqual([m["id"] for m in self.model], ["claude", "codex", "grok"])

    def test_risk_callout_lists_pace_and_danger(self) -> None:
        callout = worker.risk_callout(self.model, danger=90)
        self.assertIsNotNone(callout)
        self.assertIn("Codex", callout["detail"])
        self.assertIn("Claude 주간 95%", callout["detail"])

    def test_section_blocks(self) -> None:
        section = worker.provider_section(self.by_id["codex"], warn=75, danger=90)
        kinds = [c["kind"] for c in section["children"]]
        self.assertIn("bar", kinds)
        self.assertIn("columns", kinds)
        self.assertTrue(any(c.get("tone") == "warn" for c in section["children"] if c["kind"] == "note"))
        self.assertLess(len(json.dumps(section, ensure_ascii=False).encode()), 64 * 1024)


class Contract(unittest.TestCase):
    def test_unknown_method_returns_error_and_exits_on_eof(self) -> None:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, str(ROOT / "worker.py")],
            input=json.dumps({"jsonrpc": "2.0", "id": 99, "method": "plugin.x.nope", "params": {}}) + "\n",
            capture_output=True, text=True, timeout=20, check=False,
        )
        replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertIn({"jsonrpc": "2.0", "id": 99, "error": {"code": -32601, "message": "unknown method plugin.x.nope"}},
                      replies)


class StatusChips(unittest.TestCase):
    def setUp(self) -> None:
        self.model = worker.build_model(OPENUSAGE, CODEXBAR, mask_on=True)
        self.calls: list[tuple[str, dict]] = []
        self._orig = worker.call
        worker.call = lambda method, params: self.calls.append((method, params)) or {"ok": True}

    def tearDown(self) -> None:
        worker.call = self._orig

    def test_one_chip_per_service_in_fixed_order(self) -> None:
        worker.push_status_chips(self.model, warn=75, danger=90)
        sets = [p for m, p in self.calls if m == "ui.state.set"]
        self.assertEqual(sorted(p["id"] for p in sets), ["a-codex", "b-claude", "c-grok"])
        texts = {p["id"]: p["payload"]["text"] for p in sets}
        self.assertEqual(texts["a-codex"], "Codex 49%")
        removed = {p["id"] for m, p in self.calls if m == "ui.state.remove"}
        self.assertEqual(removed, {"d-antigravity", "e-opencode", "z-none"})

    def test_pace_risk_turns_green_chip_amber(self) -> None:
        codex = next(m for m in self.model if m["id"] == "codex")
        self.assertEqual(worker.chip_tone(codex, warn=75, danger=90), "warn")
        self.assertIn("소진", worker.chip_tooltip(codex))

    def test_chip_text_fits_without_truncation(self) -> None:
        worker.push_status_chips(self.model, warn=75, danger=90)
        for m, p in self.calls:
            if m == "ui.state.set":
                self.assertLessEqual(len(p["payload"]["text"]), 16)

    def test_no_data_chip(self) -> None:
        worker.push_status_chips([], warn=75, danger=90)
        self.assertEqual([p["id"] for m, p in self.calls if m == "ui.state.set"], ["z-none"])


if __name__ == "__main__":
    unittest.main()

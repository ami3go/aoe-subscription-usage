"""Offline localization regression tests (no credentials or live collectors)."""
import ast
import json
from pathlib import Path
import re
import sys
import unittest
from unittest import mock
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 can run the worker and other tests.
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import i18n
import worker


class CatalogTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("ko")

    def test_default_and_unknown_language_keep_korean(self):
        i18n.set_language("ko")
        self.assertEqual(i18n.t("구독 사용량"), "구독 사용량")
        for invalid in ("ja", "", None, {}):
            i18n.set_language(invalid)
            self.assertEqual(i18n.get_language(), "ko")

    def test_switch_both_ways_without_changing_user_values(self):
        i18n.set_language("en")
        self.assertEqual(i18n.t("구독 사용량"), "Subscription usage")
        self.assertEqual(i18n.t("지금 새로고침"), "Refresh now")
        self.assertEqual(i18n.t("unrecognized source {name}"), "unrecognized source {name}")
        source = "실패: {p0}" if "실패: {p0}" in i18n.MESSAGES else "OpenUsage 연결 실패: {p0}"
        self.assertEqual(i18n.t(source, p0="<script>{p1}</script>"),
                         i18n.MESSAGES[source].replace("{p0}", "<script>{p1}</script>"))
        i18n.set_language("ko")
        self.assertEqual(i18n.t("구독 사용량"), "구독 사용량")

    def test_catalog_covers_worker_messages_and_keeps_format_fields(self):
        from string import Formatter
        tree = ast.parse((ROOT / "worker.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr":
                if node.args and isinstance(node.args[0], ast.Constant):
                    self.assertIn(node.args[0].value, i18n.MESSAGES)
        for source, english in i18n.MESSAGES.items():
            fields = lambda s: sorted((name, spec, conv) for _, name, spec, conv in Formatter().parse(s) if name is not None)
            self.assertEqual(fields(source), fields(english), source)
            self.assertNotRegex(english, r"[가-힣]")

    def test_manifest_has_discoverable_language_setting(self):
        if tomllib is None:
            self.skipTest("Manifest parsing requires Python 3.11+")
        manifest = tomllib.loads((ROOT / "aoe-plugin.toml").read_text())
        setting = next(s for s in manifest["settings"] if s["key"] == "language")
        self.assertEqual(setting["type"], "select")
        self.assertEqual(setting["options"], ["ko", "en"])
        self.assertEqual(setting["default"], "ko")
        self.assertIn("Language", setting["label"])


class EnglishUsageTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("ko")

    def test_formatting_and_real_provider_payload_in_english(self):
        from test_worker import OPENUSAGE, CODEXBAR, SOON
        i18n.set_language("en")
        self.assertEqual(worker.window_label(300), "5-hour session")
        self.assertEqual(worker.human_span(3 * 86400 + 4 * 3600), "3d 4h")
        self.assertIn("remaining", worker.reset_text(SOON))
        self.assertEqual(i18n.weekday(0), "Mon")
        model = worker.build_model(OPENUSAGE, CODEXBAR, mask_on=True)
        claude = next(m for m in model if m["id"] == "claude")
        self.assertEqual([w["label"] for w in claude["accounts"][0]["windows"]],
                         ["5-hour session", "Weekly", "Weekly · Fable only"])
        section = worker.provider_section(claude, 75, 90)
        self.assertNotRegex(json.dumps(section, ensure_ascii=False), r"[가-힣]")
        self.assertIn("Weekly", worker.chip_tooltip(claude))
        self.assertIn("need attention", worker.risk_callout(model, 90)["title"])
        i18n.set_language("ko")
        self.assertEqual(worker.window_label(300), "5시간 세션")

    def test_refresh_reads_language_and_translates_empty_and_error_states(self):
        calls = []
        settings = {"language": "en", "session_badges": False}
        with mock.patch.object(worker, "setting", side_effect=lambda key, default: settings.get(key, default)), \
             mock.patch.object(worker, "fetch_openusage", return_value=([], "offline")), \
             mock.patch.object(worker, "call", side_effect=lambda method, params: calls.append((method, params)) or {}), \
             mock.patch.dict(worker._cb_cache, {"data": {}, "errors": {"codex": "codexbar 없음"}, "at": None}):
            worker.refresh()
            payload = next(p["payload"] for m, p in calls if p.get("slot") == "home-pane")
            self.assertEqual(payload["title"], "Subscription usage")
            self.assertNotRegex(json.dumps(payload, ensure_ascii=False), r"[가-힣]")
            calls.clear()
            settings["language"] = "ko"
            worker.refresh()
            payload = next(p["payload"] for m, p in calls if p.get("slot") == "home-pane")
            self.assertEqual(payload["title"], "구독 사용량")

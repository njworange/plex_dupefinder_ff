from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StaticSecurityTests(unittest.TestCase):
    def test_templates_and_static_assets_do_not_reference_plex_token(self):
        files = list((ROOT / "templates").glob("*.html")) + list((ROOT / "static").glob("*"))
        combined = "\n".join(path.read_text(encoding="utf-8") for path in files if path.is_file())
        self.assertNotIn("base_token", combined)
        self.assertNotRegex(combined, r"X-Plex-Token=[A-Za-z0-9._~-]{12,}")

    def test_delete_handshake_contains_csrf_nonce_and_confirmation(self):
        module = (ROOT / "mod_scan.py").read_text(encoding="utf-8")
        self.assertIn('sub == "delete_preview"', module)
        self.assertIn('sub == "delete_media"', module)
        self.assertIn("compare_digest", module)
        self.assertIn("csrf_token", module)
        self.assertIn("expires_at", module)
        service = (ROOT / "delete_service.py").read_text(encoding="utf-8")
        self.assertIn('"DELETE %s"', service)
        self.assertIn("validate_fresh_snapshot", service)
        self.assertNotIn("os.remove", service)
        self.assertNotIn("shutil", service)

    def test_scan_start_and_cancel_require_post_and_csrf(self):
        module = (ROOT / "mod_scan.py").read_text(encoding="utf-8")
        template = (ROOT / "templates" / "plex_dupefinder_ff_scan_list.html").read_text(
            encoding="utf-8"
        )
        for action in ("start", "cancel"):
            branch = module.split('if sub == "%s":' % action, 1)[1].split("if sub ==", 1)[0]
            self.assertIn('req.method != "POST"', branch)
            self.assertIn("self._csrf(req)", branch)
        self.assertIn("var csrfToken", template)
        self.assertGreaterEqual(template.count("csrf_token: csrfToken"), 2)

    def test_paginated_templates_follow_server_response_contract(self):
        scan = (ROOT / "templates" / "plex_dupefinder_ff_scan_results.html").read_text(
            encoding="utf-8"
        )
        history = (ROOT / "templates" / "plex_dupefinder_ff_history_list.html").read_text(
            encoding="utf-8"
        )
        for template in (scan, history):
            self.assertIn("result.items || []", template)
            self.assertIn("result.pages", template)
            self.assertIn("page_size", template)
        self.assertNotIn("allGroups = ret.data", scan)
        self.assertNotIn("actionRows = ret.data", history)

    def test_jinja_delimiters_are_balanced(self):
        for path in (ROOT / "templates").glob("*.html"):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("{{"), text.count("}}"), path.name)
            self.assertEqual(text.count("{%"), text.count("%}"), path.name)

    def test_no_unauthenticated_delete_api_is_declared(self):
        for path in (ROOT / "mod_scan.py", ROOT / "delete_service.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"def\s+process_(?:api|normal)\s*\(")


if __name__ == "__main__":
    unittest.main()

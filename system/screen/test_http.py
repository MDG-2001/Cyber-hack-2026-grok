"""Request guards for the review server. They do not change a screening decision."""

from __future__ import annotations

import unittest

from serve import bind_host, own_origin, parse_json_object, serve_args, state_change_error


class GuardTests(unittest.TestCase):
    def test_csrf_blocks_a_foreign_origin_and_a_missing_token(self):
        self.assertIsNotNone(state_change_error(
            "/api/analyst", "text/plain", "https://evil.example", None, None, "token", 8766,
        ))
        self.assertIsNotNone(state_change_error(
            "/api/analyst", "application/json", "https://evil.example", None, "token", "token", 8766,
        ))
        self.assertIsNotNone(state_change_error(
            "/api/investigate", "application/json", None, "http://evil.example/x", "token", "token", 8766,
        ))
        self.assertIsNotNone(state_change_error(
            "/api/analyst", "application/json", "http://127.0.0.1:8766", None, "nope", "token", 8766,
        ))
        self.assertIsNone(state_change_error(
            "/api/analyst", "application/json", "http://127.0.0.1:8766", "http://localhost:8766/case", "token", "token", 8766,
        ))
        self.assertIsNone(state_change_error(
            "/api/try", "text/plain", "https://evil.example", None, None, "token", 8766,
        ))

    def test_origin_allows_only_this_loopback_port(self):
        self.assertTrue(own_origin("", 8766))
        self.assertTrue(own_origin("http://127.0.0.1:8766", 8766))
        self.assertTrue(own_origin("http://localhost:8766/path", 8766))
        self.assertFalse(own_origin("http://127.0.0.1:9", 8766))
        self.assertFalse(own_origin("https://evil.example", 8766))

    def test_json_body_rules(self):
        status, error, payload = parse_json_object(b'{"hit_id": "H-1"}')
        self.assertIsNone(status)
        self.assertEqual(payload["hit_id"], "H-1")
        for raw in (b"[]", b'"x"', b"1"):
            status, error, payload = parse_json_object(raw)
            self.assertEqual(status, 400, raw)
            self.assertIn("object", error["error"])
        status, error, payload = parse_json_object(b"{broken")
        self.assertEqual(status, 400)
        self.assertIn("JSON", error["error"])

    def test_bind_defaults_and_warns(self):
        host, warning = bind_host("127.0.0.1")
        self.assertEqual(host, "127.0.0.1")
        self.assertIsNone(warning)
        host, warning = bind_host("0.0.0.0")
        self.assertIn("warning", warning)
        parsed = serve_args(["serve.py", "--host", "0.0.0.0", "data", "out.jsonl"], {})
        self.assertEqual(parsed[0], "0.0.0.0")
        parsed = serve_args(["serve.py", "data", "out.jsonl"], {"HOST": "localhost"})
        self.assertEqual(parsed[0], "localhost")
        self.assertIsNone(serve_args(["serve.py"], {}))


if __name__ == "__main__":
    unittest.main()

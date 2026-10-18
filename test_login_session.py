"""Offline tests for login-token integrity, lifetime, and credential rotation."""
import base64
import json
import unittest

import login_session as login


class LoginTokenTests(unittest.TestCase):
    def setUp(self):
        self.token = login.issue_token("sample-user", "sample-password", now=1000)

    def verify(self, token=None, **kwargs):
        return login.verify_token(self.token if token is None else token,
                                  "sample-user", "sample-password", **kwargs)

    def test_valid_login_and_fixed_24_hour_expiry(self):
        self.assertEqual(self.verify(now=1001)["sub"], "sample-user")
        self.assertIsNotNone(self.verify(now=1000 + login.LOGIN_SECONDS - 1))
        self.assertIsNone(self.verify(now=1000 + login.LOGIN_SECONDS))
        self.assertIsNone(self.verify(now=999))

    def test_reading_token_does_not_extend_expiry(self):
        for current in (1001, 5000, 80000):
            self.assertEqual(self.verify(now=current)["exp"], 1000 + login.LOGIN_SECONDS)

    def test_tampering_is_rejected(self):
        body, signature = self.token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        payload["exp"] += login.LOGIN_SECONDS
        altered = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        self.assertIsNone(self.verify(altered + "." + signature, now=1001))

    def test_password_and_user_changes_invalidate_token(self):
        self.assertIsNone(login.verify_token(self.token, "sample-user", "new-password", now=1001))
        self.assertIsNone(login.verify_token(self.token, "other-user", "sample-password", now=1001))

    def test_dedicated_secret_rotation(self):
        token = login.issue_token("sample-user", "sample-password", secret="server-secret", now=1000)
        self.assertIsNotNone(self.verify(token, secret="server-secret", now=1001))
        self.assertIsNone(self.verify(token, secret="rotated-secret", now=1001))

    def test_demo_access_tracks_current_demo_keys(self):
        token = login.issue_token("sample-user", "sample-password", demo_key="demo-link", now=1000)
        self.assertEqual(self.verify(token, demo_keys=["demo-link"], now=1001)["mode"], "demo")
        self.assertIsNone(self.verify(token, demo_keys=[], now=1001))
        self.assertIsNone(self.verify(token, demo_keys=["other-link"], now=1001))
        self.assertNotIn("demo-link", base64.urlsafe_b64decode(token.split(".")[0] + "==").decode())

    def test_payload_contains_no_password(self):
        payload = base64.urlsafe_b64decode(self.token.split(".")[0] + "==").decode()
        self.assertNotIn("sample-password", payload)

    def test_malformed_values_fail_closed(self):
        for token in (None, "", "garbage", "a.b.c", "a." + "x" * 43, "x" * 5000, "☁.abc"):
            self.assertIsNone(login.verify_token(token, "sample-user", "sample-password", now=1001))


if __name__ == "__main__":
    unittest.main()

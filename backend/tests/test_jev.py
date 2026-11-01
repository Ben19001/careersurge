"""The Jev client (`app.services.jev`).

The HTTP call and the key lookup are rebound per test; nothing reaches the network or the keychain.
"""
import os
import tempfile
import unittest
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports

from app.services import jev  # noqa: E402
from app.store import db  # noqa: E402


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _answer(choice, confidence=0.9):
    return {"type": "choice", "choice": choice, "probabilities": {choice: 1.0}, "confidence": confidence}


class ChooseTests(unittest.TestCase):
    def setUp(self):
        # choose() records a refused key in `settings`, so it needs a database. A throwaway one, or a
        # 401 test would write the flag into the real data dir and Settings would call a good key refused.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CAREERSURGE_DATA_DIR": os.path.join(tmp.name, "data")})
        env.start()
        self.addCleanup(env.stop)
        db.migrate()
        key = mock.patch.object(jev.secrets, "get_secret", return_value="test-key")
        key.start()
        self.addCleanup(key.stop)

    def post(self, response=None, error=None):
        patcher = mock.patch.object(jev.httpx, "post", create=True, return_value=response, side_effect=error)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def test_request_shape_and_choice_is_the_option_string(self):
        mocked = self.post(_Response(200, {"answers": {"email": _answer("b@x.com"), "name": _answer("None of these", 0.4)}}))

        result = jev.choose("resume text", {
            "email": ("Which is the email?", ["a@x.com", "b@x.com"]),
            "name": ("Which is the name?", ["Jane Doe"]),
        })

        self.assertEqual(result, {"email": ("b@x.com", 0.9), "name": (None, 0.4)})
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer test-key"})
        self.assertEqual(kwargs["timeout"], 10)
        body = kwargs["json"]
        self.assertEqual(body["model"], "jev-1.13.0")
        self.assertEqual(body["state"], "resume text")
        self.assertEqual(body["questions"]["email"]["type"], "choice")
        self.assertEqual(body["questions"]["email"]["criteria"], {
            "a@x.com": None, "b@x.com": None, "None of these": None})
        self.assertEqual(body["questions"]["name"]["criteria"], {"Jane Doe": None, "None of these": None})

    def test_no_key_raises_without_a_request(self):
        mocked = self.post(_Response(200, {}))
        with mock.patch.object(jev.secrets, "get_secret", return_value=None):
            with self.assertRaises(jev.JevUnavailable) as caught:
                jev.choose("t", {"q": ("?", ["a"])})
        self.assertEqual(caught.exception.reason, "no_key")
        mocked.assert_not_called()

    def test_status_codes_map_to_reasons(self):
        for status, reason in ((401, "no_key"), (403, "no_key"), (429, "rate_limited"),
                               (500, "server"), (529, "server"), (422, "bad_response")):
            with self.subTest(status=status):
                self.post(_Response(status, {}))
                with self.assertRaises(jev.JevUnavailable) as caught:
                    jev.choose("t", {"q": ("?", ["a"])})
                self.assertEqual(caught.exception.reason, reason)

    def test_transport_failure_is_network(self):
        self.post(error=jev.httpx.TransportError("boom"))
        with self.assertRaises(jev.JevUnavailable) as caught:
            jev.choose("t", {"q": ("?", ["a"])})
        self.assertEqual(caught.exception.reason, "network")

    def test_unreadable_answers_are_bad_response(self):
        for payload in ({}, {"answers": {}}, {"answers": {"q": {"choice": "not an option", "confidence": 0.5}}},
                        {"answers": {"q": {"choice": "a"}}}):
            with self.subTest(payload=payload):
                self.post(_Response(200, payload))
                with self.assertRaises(jev.JevUnavailable) as caught:
                    jev.choose("t", {"q": ("?", ["a"])})
                self.assertEqual(caught.exception.reason, "bad_response")

    def test_a_confidence_that_is_not_a_finite_number_is_bad_response(self):
        # NaN fails `confidence < floor`, so it would have counted as confident.
        for confidence in ("nan", float("nan"), "inf", float("-inf")):
            with self.subTest(confidence=confidence):
                self.post(_Response(200, {"answers": {"q": _answer("a", confidence)}}))
                with self.assertRaises(jev.JevUnavailable) as caught:
                    jev.choose("t", {"q": ("?", ["a"])})
                self.assertEqual(caught.exception.reason, "bad_response")

    def test_more_options_than_the_limit_is_a_caller_bug(self):
        mocked = self.post(_Response(200, {}))
        options = [f"line {n}" for n in range(jev.MAX_OPTIONS + 1)]
        with self.assertRaises(ValueError):
            jev.choose("t", {"q": ("?", options)})
        mocked.assert_not_called()
        self.post(_Response(200, {"answers": {"q": _answer("line 0")}}))
        self.assertEqual(jev.choose("t", {"q": ("?", options[:jev.MAX_OPTIONS])}), {"q": ("line 0", 0.9)})

    def test_duplicate_options_or_the_none_text_are_a_caller_bug(self):
        mocked = self.post(_Response(200, {}))
        for options in (["a", "b", "a"], ["a", "None of these"]):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    jev.choose("t", {"q": ("?", options)})
        mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()

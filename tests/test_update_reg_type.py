import io
import json
import unittest
import urllib.error
from contextlib import redirect_stderr
from unittest import mock

from scripts import update_reg_type


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


class RequestJsonTests(unittest.TestCase):
    @mock.patch.object(update_reg_type.time, "sleep")
    @mock.patch.object(update_reg_type.urllib.request, "urlopen")
    def test_retries_timeout_then_returns_json(self, urlopen, sleep):
        urlopen.side_effect = [
            urllib.error.URLError("timed out"),
            FakeResponse({"data": [1]}),
        ]
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            result = update_reg_type.request_json(
                "https://example.invalid/data", source="ATIS"
            )

        self.assertEqual(result, {"data": [1]})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)
        self.assertIn("ATIS request attempt 1/3 failed", stderr.getvalue())

    @mock.patch.object(update_reg_type.time, "sleep")
    @mock.patch.object(update_reg_type.urllib.request, "urlopen")
    def test_reports_source_after_all_attempts_timeout(self, urlopen, sleep):
        urlopen.side_effect = urllib.error.URLError("timed out")

        with self.assertRaisesRegex(
            RuntimeError,
            "ATIS request failed after 3 attempts: timed out",
        ):
            update_reg_type.request_json(
                "https://example.invalid/data", source="ATIS"
            )

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(4)])

    @mock.patch.object(update_reg_type.urllib.request, "urlopen")
    def test_does_not_retry_or_leak_url_for_auth_error(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            "https://example.invalid/data?serviceKey=secret",
            401,
            "Unauthorized",
            {},
            None,
        )

        with self.assertRaises(RuntimeError) as raised:
            update_reg_type.request_json(
                "https://example.invalid/data?serviceKey=secret",
                source="ODCloud page 1",
            )

        message = str(raised.exception)
        self.assertEqual(
            message,
            "ODCloud page 1 request failed: HTTP 401 Unauthorized",
        )
        self.assertNotIn("secret", message)
        urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()

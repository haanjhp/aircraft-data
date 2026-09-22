import io
import json
import unittest
import urllib.error
from contextlib import redirect_stderr
from unittest import mock

from scripts import update_reg_type


class FakeResponse:
    status = 201

    def __init__(self, payload, error=None):
        self.body = json.dumps(payload).encode("utf-8")
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        if self.error:
            raise self.error
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
            "ATIS request failed after 3 attempts during connect/headers: timed out",
        ):
            update_reg_type.request_json(
                "https://example.invalid/data", source="ATIS"
            )

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(4)])

    @mock.patch.object(update_reg_type.time, "sleep")
    @mock.patch.object(update_reg_type.urllib.request, "urlopen")
    def test_reports_body_read_timeout_and_custom_retry_delays(self, urlopen, sleep):
        urlopen.side_effect = [
            FakeResponse({}, error=TimeoutError("timed out")),
            FakeResponse({"data": []}),
        ]
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            result = update_reg_type.request_json(
                "https://example.invalid/data",
                source="ATIS",
                attempts=2,
                retry_delays=(30,),
            )

        self.assertEqual(result, {"data": []})
        sleep.assert_called_once_with(30)
        self.assertIn("failed during read body", stderr.getvalue())

    def test_rejects_mismatched_retry_delays(self):
        with self.assertRaisesRegex(ValueError, "retry_delays"):
            update_reg_type.request_json(
                "https://example.invalid/data",
                source="ATIS",
                attempts=3,
                retry_delays=(30,),
            )

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


class AtisDataTests(unittest.TestCase):
    @mock.patch.object(update_reg_type, "request_json")
    def test_skips_one_missing_registration(self, request_json):
        request_json.return_value = {
            "data": [
                {"REG_SNO": "HL1234"},
                {"REG_SNO": "", "SNO": "source-row"},
                {"REG_SNO": "HL5678"},
            ]
        }
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            rows = update_reg_type.fetch_atis()

        self.assertEqual([row["REG_SNO"] for row in rows], ["HL1234", "HL5678"])
        self.assertIn("skipped 1 rows without a registration", stderr.getvalue())
        self.assertEqual(request_json.call_args.kwargs["attempts"], 4)
        self.assertEqual(
            request_json.call_args.kwargs["retry_delays"], (30, 90, 180)
        )

    @mock.patch.object(update_reg_type, "request_json")
    def test_rejects_many_missing_registrations(self, request_json):
        request_json.return_value = {
            "data": [{"REG_SNO": "HL1234"}] + [{"REG_SNO": ""}] * 4
        }

        with self.assertRaisesRegex(RuntimeError, "4 rows without a registration"):
            update_reg_type.fetch_atis()

    def test_existing_row_count_and_duplicate_guards_remain(self):
        with self.assertRaisesRegex(RuntimeError, "only 1 ATIS rows"):
            update_reg_type.validate([{"등록기호": "HL1234"}])
        with self.assertRaisesRegex(RuntimeError, "duplicate registrations"):
            update_reg_type.validate([{"등록기호": "HL1234"}] * 800)


if __name__ == "__main__":
    unittest.main()

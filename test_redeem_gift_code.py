import csv
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import redeem_gift_code as redeemer


SUCCESS = {"code": 0, "data": [], "msg": "SUCCESS", "err_code": 20000}
CLAIMED = {"code": 1, "data": [], "msg": "RECEIVED.", "err_code": 40008}
INVALID = {"code": 1, "data": [], "msg": "CDK NOT FOUND.", "err_code": 40014}
LIMITED = {"code": 1, "data": [], "msg": "TOO FREQUENT.", "err_code": 40019}
PLAYERS = [redeemer.Player(str(i), "2018", f"Player {i}") for i in range(1, 4)]


def http_response(body, status=200):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = status
    response.read.return_value = (body if isinstance(body, str) else json.dumps(body)).encode()
    return response


class RedeemerTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"WEBEX_WEBHOOK_URL": "https://example.invalid/webex-test"})
        environment.start()
        self.addCleanup(environment.stop)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.results = Path(self.directory.name) / "results.csv"
        # Every test blocks network by default, including Webex.
        self.transport = patch.object(redeemer, "urlopen", side_effect=AssertionError("Unexpected network call"))
        self.urlopen = self.transport.start()
        self.addCleanup(self.transport.stop)

    def read_results(self):
        with self.results.open(newline="", encoding="utf-8") as source:
            return list(csv.DictReader(source))

    def run_batch(self):
        with redirect_stdout(io.StringIO()), patch.object(redeemer.time, "sleep"):
            self.assertEqual(redeemer.submit_players(PLAYERS, "TESTCODE", self.results), 0)
        return self.read_results()

    def test_signature_matches_independently_generated_fixture(self):
        with patch.object(redeemer.time, "time", return_value=1700000000.9):
            payload = redeemer.signed_payload(redeemer.Player("123456789", "2018"), "TESTCODE")
        self.assertEqual(payload["time"], 1700000000)
        self.assertEqual(payload["sign"], "1787b23cd5b5bf1a398166b7b28907bd")

    def test_live_responses_and_unknowns_are_classified_without_success_text_guessing(self):
        cases = [
            (SUCCESS, "SUCCESS"), (CLAIMED, "ALREADY_CLAIMED"), (INVALID, "INVALID_CODE"),
            (LIMITED, "RATE_LIMITED"),
            ({"code": 0, "msg": "SUCCESS"}, "REVIEW"),
            ({"code": 1, "err_code": 20000, "msg": "SUCCESS"}, "REVIEW"),
            ({"msg": "RECEIVED."}, "REVIEW"),
            ({"err_code": 98765, "msg": "unknown"}, "REVIEW"),
            ({"err_code": 40103, "msg": "CAPTCHA ERROR"}, "VERIFICATION_REQUIRED"),
            ({"err_code": 40006}, "REJECTED"), ([], "REVIEW"),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(redeemer.classify_outcome(body), expected)

    def test_api_request_uses_signed_form_and_preserves_raw_response(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = http_response(CLAIMED)
        result = redeemer.redeem_player(PLAYERS[0], "MiXeD_CODE")
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, redeemer.GIFT_CODE_API_URL)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        form = parse_qs(request.data.decode())
        self.assertEqual(form["cdk"], ["MiXeD_CODE"])
        self.assertEqual(form["fid"], ["1"])
        self.assertEqual(form["kid"], ["2018"])
        self.assertIn("sign", form)
        self.assertEqual(json.loads(result.response), CLAIMED)
        self.assertEqual(result.status, "ALREADY_CLAIMED")

    def test_completed_batch_writes_csv_and_sends_webex_summary(self):
        self.urlopen.side_effect = [
            http_response(SUCCESS), http_response(CLAIMED), http_response(SUCCESS), http_response({}),
        ]
        rows = self.run_batch()
        self.assertEqual([row["status"] for row in rows], ["SUCCESS", "ALREADY_CLAIMED", "SUCCESS"])
        self.assertEqual(rows[1]["err_code"], "40008")
        self.assertEqual(rows[1]["message"], "RECEIVED.")
        self.assertEqual(rows[0]["api_code"], "0")
        self.assertEqual(rows[0]["http_status"], "200")
        self.assertEqual(json.loads(rows[0]["response"]), SUCCESS)
        self.assertEqual([row["player_id"] for row in rows], ["1", "2", "3"])
        webhook = self.urlopen.call_args.args[0]
        summary = json.loads(webhook.data)["markdown"]
        self.assertIn("2 of 3 players succeeded", summary)
        self.assertIn("Already claimed: 1", summary)
        self.assertNotIn("TESTCODE", summary)

    def test_invalid_unknown_and_verification_responses_stop_batch_and_notify(self):
        for body, status in [
            (INVALID, "INVALID_CODE"), ({"msg": "unexpected"}, "REVIEW"),
            ({"err_code": 40103}, "VERIFICATION_REQUIRED"), ("<html>error</html>", "REVIEW"),
        ]:
            with self.subTest(status=status, body=body):
                self.results.unlink(missing_ok=True)
                self.urlopen.side_effect = [http_response(body), http_response({})]
                rows = self.run_batch()
                self.assertEqual([row["status"] for row in rows], [status, "NOT_ATTEMPTED", "NOT_ATTEMPTED"])
                self.assertIn("Not submitted: 2", json.loads(self.urlopen.call_args.args[0].data)["markdown"])

    def test_timeout_is_not_retried_and_notification_is_sent(self):
        self.urlopen.side_effect = [TimeoutError("timed out"), http_response({})]
        rows = self.run_batch()
        self.assertEqual([row["status"] for row in rows], ["UNCERTAIN", "NOT_ATTEMPTED", "NOT_ATTEMPTED"])
        self.assertEqual(self.urlopen.call_count, 2)

    def test_http_429_gets_one_retry_with_fresh_signature_then_stops(self):
        error = HTTPError(redeemer.GIFT_CODE_API_URL, 429, "Limited", {}, io.BytesIO(b'{"msg":"limited"}'))
        self.urlopen.side_effect = [error, http_response(LIMITED), http_response({})]
        with patch.object(redeemer.time, "time", side_effect=[1700000000, 1700000001]):
            rows = self.run_batch()
        self.assertEqual([row["status"] for row in rows], ["RATE_LIMITED", "NOT_ATTEMPTED", "NOT_ATTEMPTED"])
        first, second = [parse_qs(call.args[0].data.decode()) for call in self.urlopen.call_args_list[:2]]
        self.assertNotEqual(first["time"], second["time"])
        self.assertNotEqual(first["sign"], second["sign"])

    def test_recovered_rate_limit_continues_batch(self):
        self.urlopen.side_effect = [
            http_response(LIMITED), http_response(SUCCESS), http_response(CLAIMED),
            http_response(SUCCESS), http_response({}),
        ]
        rows = self.run_batch()
        self.assertEqual([row["status"] for row in rows], ["SUCCESS", "ALREADY_CLAIMED", "SUCCESS"])

    def test_server_error_body_is_saved_without_retry(self):
        error = HTTPError(redeemer.GIFT_CODE_API_URL, 503, "Unavailable", {}, io.BytesIO(b"maintenance"))
        self.urlopen.side_effect = [error, http_response({})]
        rows = self.run_batch()
        self.assertEqual(rows[0]["status"], "UNCERTAIN")
        self.assertEqual(rows[0]["http_status"], "503")
        self.assertEqual(rows[0]["response"], "maintenance")
        self.assertEqual(self.urlopen.call_count, 2)

    def test_interrupt_during_request_saves_all_rows_and_notifies(self):
        self.urlopen.side_effect = [KeyboardInterrupt(), http_response({})]
        rows = self.run_batch()
        self.assertEqual([row["status"] for row in rows], ["UNCERTAIN", "NOT_ATTEMPTED", "NOT_ATTEMPTED"])

    def test_webex_failure_keeps_csv(self):
        self.urlopen.side_effect = [
            http_response(CLAIMED), http_response(CLAIMED), http_response(CLAIMED), URLError("offline"),
        ]
        with redirect_stderr(io.StringIO()) as errors:
            rows = self.run_batch()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["status"] == "ALREADY_CLAIMED" for row in rows))
        self.assertIn("Webex notification failed", errors.getvalue())

    def test_unconfigured_webex_skips_network(self):
        with patch.dict(os.environ, {"WEBEX_WEBHOOK_URL": ""}), redirect_stdout(io.StringIO()) as output:
            redeemer.send_webex_summary(self.results)
        self.urlopen.assert_not_called()
        self.assertIn("Webex notification skipped", output.getvalue())

    def test_existing_csv_is_not_overwritten_or_submitted(self):
        self.results.write_text("keep me")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(redeemer.submit_players(PLAYERS, "TESTCODE", self.results), 2)
        self.assertEqual(self.results.read_text(), "keep me")
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

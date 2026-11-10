import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import kingshot_gift_code_monitor as monitor


PAGE = """
<h2>Active Gift Codes</h2>
<div>Active</div><div>EXPLORETHEWORLD</div><div>Expires: 10/02/2026</div>
<div>Copy Code Sign In to Redeem Share Link</div>
<div>Kingshot888</div><div>VIP777</div>
<h2>Expired codes (128)</h2><div>OLD-CODE-123</div>
"""


class GiftCodeMonitorTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"WEBEX_WEBHOOK_URL": "https://example.invalid/webex-test"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_webex_defaults_to_environment(self):
        self.assertEqual(monitor.build_parser().parse_args([]).webex_webhook,
                         "https://example.invalid/webex-test")

    def test_unconfigured_webex_does_not_block_monitor(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps(["OLD12345"]))
            with patch.dict(os.environ, {"WEBEX_WEBHOOK_URL": ""}), patch.object(monitor, "fetch_page", return_value=PAGE), patch.object(monitor, "send_webex_update") as notify:
                args = monitor.build_parser().parse_args(["--state-file", str(state)])
                self.assertEqual(monitor.run(args), 0)
            notify.assert_not_called()
            self.assertEqual(json.loads(state.read_text()), ["EXPLORETHEWORLD", "KINGSHOT888", "VIP777"])

    def test_extracts_only_active_codes_and_normalizes_case(self):
        self.assertEqual(monitor.extract_active_codes(PAGE), ["EXPLORETHEWORLD", "KINGSHOT888", "VIP777"])


    def test_missing_boundaries_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "expired-codes boundary"):
            monitor.extract_active_codes("<h2>Active Gift Codes</h2><div>ABC12345</div>")


    def test_state_is_written_as_sorted_json(self):
        with self.subTest("state"):
            from tempfile import TemporaryDirectory
            with TemporaryDirectory() as directory:
                state = Path(directory) / "state.json"
                monitor.save_state(state, ["VIP777", "abc12345", "VIP777"])
                self.assertEqual(json.loads(state.read_text()), ["ABC12345", "VIP777"])


    def test_claim_script_is_called_once_per_new_code(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            state = tmp_path / "state.json"
            claim = tmp_path / "claim.py"
            claim.write_text("# placeholder\n")
            (tmp_path / "players.csv").write_text("player_id,kingdom_id\n123,456\n")
            state.write_text(json.dumps(["OLD12345"]))

            calls = []

            def fake_claim(script, players_file, code, python):
                calls.append((script, players_file, code, python))
                return monitor.ClaimResult(code, 0)

            with patch.object(monitor, "fetch_page", return_value=PAGE), patch.object(monitor, "send_webex_update"), patch.object(monitor, "invoke_claim_script", side_effect=fake_claim):
                args = monitor.build_parser().parse_args(["--claim-script", str(claim), "--state-file", str(state)])
                self.assertEqual(monitor.run(args), 0)
            self.assertEqual([code for _, _, code, _ in calls], ["EXPLORETHEWORLD", "KINGSHOT888", "VIP777"])
            self.assertTrue(all(players == tmp_path / "players.csv" for _, players, _, _ in calls))
            self.assertEqual(json.loads(state.read_text()), ["EXPLORETHEWORLD", "KINGSHOT888", "VIP777"])


    def test_nonzero_claim_status_is_reported_but_code_is_persisted(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            state = tmp_path / "state.json"
            claim = tmp_path / "claim.py"
            claim.write_text("# placeholder\n")
            (tmp_path / "players.csv").write_text("player_id,kingdom_id\n123,456\n")
            state.write_text(json.dumps(["OLD12345"]))
            with patch.object(monitor, "fetch_page", return_value=PAGE), patch.object(monitor, "send_webex_update"), patch.object(monitor, "invoke_claim_script", return_value=monitor.ClaimResult("VIP777", 1)):
                args = monitor.build_parser().parse_args(["--claim-script", str(claim), "--state-file", str(state)])
                self.assertEqual(monitor.run(args), 1)
            self.assertEqual(json.loads(state.read_text()), ["EXPLORETHEWORLD", "KINGSHOT888", "VIP777"])

    def test_new_codes_are_not_recorded_when_webex_fails(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            state = tmp_path / "state.json"
            state.write_text(json.dumps(["OLD12345"]))
            with patch.object(monitor, "fetch_page", return_value=PAGE), patch.object(monitor, "send_webex_update", side_effect=RuntimeError("webex down")):
                args = monitor.build_parser().parse_args(["--state-file", str(state)])
                with self.assertRaisesRegex(RuntimeError, "webex down"):
                    monitor.run(args)
            self.assertEqual(json.loads(state.read_text()), ["OLD12345"])


if __name__ == "__main__":
    unittest.main()

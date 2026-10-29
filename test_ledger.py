import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SLEEPER_LEAGUE_ID", "test")
os.environ.setdefault("DISCORD_WEBHOOK_WAIVERS", "https://example.invalid/waivers")
os.environ.setdefault("DISCORD_WEBHOOK_TRADES", "https://example.invalid/trades")

import ledger


def tx(identity, timestamp, kind="trade", status="complete"):
    return {
        "transaction_id": identity,
        "status_updated": timestamp,
        "type": kind,
        "status": status,
        "adds": {"player": 1},
        "roster_ids": [1, 2],
    }


class LedgerTests(unittest.TestCase):
    def test_rounds_follow_nfl_week_and_recover_history(self):
        with patch.object(ledger, "_get", return_value={"leg": 8}):
            self.assertEqual(ledger.automatic_transaction_rounds(), list(range(10)))

    def test_selection_deduplicates_and_respects_cursor(self):
        state = {"last_seen_ms": 100, "seen_at_last_ms": ["old"]}
        result = ledger.new_final_transactions(
            [tx("old", 100), tx("new", 100), tx("next", 110),
             tx("next", 110), tx("pending", 120, status="pending")], state
        )
        self.assertEqual([t["transaction_id"] for t in result], ["new", "next"])

    def test_failure_does_not_acknowledge_failed_receipt(self):
        state = {"last_seen_ms": 100}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(ledger, "STATE_FILE", str(Path(directory) / "state.json")):
                with patch.object(ledger, "post", side_effect=RuntimeError("Discord error")):
                    with self.assertRaisesRegex(RuntimeError, "Discord error"):
                        ledger.publish_transactions(
                            [tx("new", 110)], state, {1: "A", 2: "B"},
                            {"player": "Player"}, {}, limit=8
                        )
                self.assertEqual(state["last_seen_ms"], 100)
                self.assertFalse((Path(directory) / "state.json").exists())

    def test_success_acknowledged_and_batch_bounded(self):
        state = {"last_seen_ms": 100}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(ledger, "STATE_FILE", str(Path(directory) / "state.json")):
                with patch.object(ledger, "post") as post:
                    result = ledger.publish_transactions(
                        [tx("a", 110), tx("b", 120)], state,
                        {1: "A", 2: "B"}, {"player": "Player"}, {}, limit=1
                    )
                self.assertEqual(result, 1)
                self.assertEqual(state["last_seen_ms"], 110)
                self.assertEqual(post.call_count, 1)
                saved = ledger.load_state()
                self.assertEqual(saved["last_seen_ms"], 110)


if __name__ == "__main__":
    unittest.main()

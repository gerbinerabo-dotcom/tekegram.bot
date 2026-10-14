import tempfile
import unittest
import sqlite3
import gc
from pathlib import Path
from unittest.mock import patch

import database
import mafia_db


class PurchaseStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.path_patch = patch.object(database, "DATABASE_PATH", self.db_path)
        self.path_patch.start()
        database.init_db()

    def tearDown(self):
        self.path_patch.stop()
        gc.collect()
        self.temp_dir.cleanup()

    def create_intent(self, payload="mafia_plus_role_stats:42:nonce"):
        return database.create_purchase_intent(
            telegram_id=42,
            product_id="mafia_plus_role_stats",
            amount_stars=100,
            currency="XTR",
            invoice_payload=payload,
        )

    def record_payment(self, *, payload="mafia_plus_role_stats:42:nonce", **overrides):
        values = {
            "telegram_id": 42,
            "product_id": "mafia_plus_role_stats",
            "amount_stars": 100,
            "currency": "XTR",
            "invoice_payload": payload,
            "charge_id": "charge-1",
        }
        values.update(overrides)
        return database.record_successful_star_payment(**values)

    def test_schema_initialization_is_repeatable(self):
        database.init_db()
        database.init_db()
        purchase = self.create_intent()
        self.assertEqual(purchase, 1)
        self.assertFalse(database.has_product_access(42, "mafia_plus_role_stats"))
        pending = database.get_purchase_by_payload("mafia_plus_role_stats:42:nonce")
        self.assertIsNone(pending["purchased_at"])

    def test_access_only_after_matching_success_and_duplicate_is_idempotent(self):
        self.create_intent()
        self.assertEqual(self.record_payment(), "paid")
        self.assertTrue(database.has_product_access(42, "mafia_plus_role_stats"))
        self.assertIsNotNone(database.get_purchase_by_payload("mafia_plus_role_stats:42:nonce")["purchased_at"])
        self.assertEqual(self.record_payment(), "duplicate")
        self.assertEqual(len(database.get_user_purchases(42)), 1)

    def test_mismatched_payment_is_rejected_and_grants_no_access(self):
        self.create_intent()
        self.assertEqual(self.record_payment(amount_stars=99), "mismatch")
        self.assertFalse(database.has_product_access(42, "mafia_plus_role_stats"))
        purchase = database.get_purchase_by_payload("mafia_plus_role_stats:42:nonce")
        self.assertEqual(purchase["status"], "rejected")
        self.assertEqual(purchase["telegram_payment_charge_id"], "charge-1")

    def test_charge_cannot_be_applied_to_another_invoice(self):
        self.create_intent()
        self.create_intent("mafia_plus_role_stats:42:second")
        self.assertEqual(self.record_payment(), "paid")
        self.assertEqual(
            self.record_payment(payload="mafia_plus_role_stats:42:second"),
            "duplicate_charge",
        )


class MafiaRoleStatisticsMigrationTests(unittest.TestCase):
    def test_legacy_statistics_are_preserved_and_new_role_win_counts_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mafia.db"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """CREATE TABLE mafia_stats (
                        user_id INTEGER PRIMARY KEY, games INTEGER NOT NULL DEFAULT 0,
                        wins INTEGER NOT NULL DEFAULT 0, mafia_roles INTEGER NOT NULL DEFAULT 0,
                        don_roles INTEGER NOT NULL DEFAULT 0, doctor_roles INTEGER NOT NULL DEFAULT 0,
                        detective_roles INTEGER NOT NULL DEFAULT 0, sheriff_roles INTEGER NOT NULL DEFAULT 0,
                        maniac_roles INTEGER NOT NULL DEFAULT 0, civilian_roles INTEGER NOT NULL DEFAULT 0
                    )"""
                )
                connection.execute(
                    "INSERT INTO mafia_stats(user_id, games, wins, mafia_roles) VALUES (77, 5, 2, 3)"
                )
            connection.close()
            with patch.object(mafia_db, "MAFIA_DATABASE_PATH", path):
                mafia_db.init_db()
                mafia_db.init_db()
                before = mafia_db.get_player_statistics(77)
                self.assertEqual(before["games"], 5)
                self.assertEqual(before["mafia_roles"], 3)
                self.assertEqual(before["mafia_role_games"], 0)

                room_id = mafia_db.create_room(chat_id=-100123, host_user_id=77)
                mafia_db.add_player(room_id, 77, "Mafia")
                mafia_db.add_player(room_id, 88, "Civilian")
                mafia_db.set_player_role(room_id, 77, "mafia")
                mafia_db.set_player_role(room_id, 88, "civilian")
                self.assertTrue(mafia_db.finish_room(room_id, "mafia"))
                after = mafia_db.get_player_statistics(77)
                self.assertEqual(after["games"], 6)
                self.assertEqual(after["mafia_role_games"], 1)
                self.assertEqual(after["mafia_role_wins"], 1)
            gc.collect()


if __name__ == "__main__":
    unittest.main()

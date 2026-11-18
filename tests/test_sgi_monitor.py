import io
import json
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, Mock, patch

import marketplace_leads
import marketplace_listings
from sgi_monitor import SYNC_PATH, build_snapshot, send_snapshot, SgiError
import sgi_monitor as monitor


class MonitorTest(unittest.TestCase):
    def test_snapshot_uses_only_commercial_lead_fields(self):
        today = date.today().isoformat()
        store = MagicMock()
        store.connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (2,)
        store.worker_state.return_value = {"status": "idle"}
        store.summary.return_value = {"counts": {}}
        store.list_queue.return_value = []
        store.list_alerts.return_value = [
            {"id": "alert", "severity": "warning", "code": "TEST",
             "message": "internal detail", "account": "shop", "created_at": today}
        ]
        report = {
            "accounts": [{"last_14": 2}],
            "awaiting_reply": 2,
            "products": [{"family_key": "demo_item", "clicks": 10, "leads": 2,
                          "rate": 20, "focus": True}],
            "unanswered": [
                {"family_key": "demo_item", "account": "shop", "last_activity": today, "started_on": today,
                 "title": "raw title", "thread": "thread-id", "buyer": "buyer name"},
                {"family_key": "demo_item", "account": "shop",
                 "last_activity": (date.today() - timedelta(days=5)).isoformat(), "started_on": ""},
            ],
        }
        listing = {"key": "shop::123", "account": "shop", "title": "Demo",
                   "family_key": "demo_item", "status": "in_stock", "listed_on": today,
                   "clicks_14d": 10, "recommendation": "keep"}
        with (
            patch.object(marketplace_leads, "report", return_value=report),
            patch.object(marketplace_leads, "listing_leads", return_value=({"shop"}, {})),
            patch.object(marketplace_listings, "list_listings", return_value=[listing]),
            patch.object(marketplace_listings, "pending_actions", return_value=[]),
            patch("sgi_monitor.health_snapshot", return_value={}),
            patch("sgi_monitor.load_safety", return_value={"level": "conservative"}),
        ):
            snapshot = build_snapshot(store, {"accounts": [], "enabled": False, "mode": "supervised"})
        self.assertEqual(snapshot["worker"]["safetyLevel"], "conservative")
        self.assertEqual(snapshot["listings"]["items"][0]["leads14d"], 0)
        self.assertEqual(snapshot["leads"]["products"][0]["activeListings"], 1)
        self.assertEqual(snapshot["leads"]["unanswered"][0]["title"], "demo item")
        self.assertEqual(snapshot["leads"]["awaitingReply"], 2)
        self.assertEqual(snapshot["leads"]["unansweredRecent"], 1)
        self.assertEqual(len(snapshot["leads"]["unanswered"]), 2)
        for value in ("raw title", "thread-id", "buyer name", "internal detail"):
            self.assertNotIn(value, json.dumps(snapshot))

    def test_sends_monitor_only_without_requesting_commands(self):
        key = "x" * 43
        opener = Mock(return_value=io.BytesIO(b'{"data":{"pendingCommands":[]}}'))
        send_snapshot("https://sgi.example.com", key, "demo-bot", "Demo bot", {}, open_request=opener)
        args, kwargs = opener.call_args
        request = args[0]
        self.assertEqual(request.full_url, "https://sgi.example.com" + SYNC_PATH)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + key)
        self.assertNotIn("commandProtocolVersion", json.loads(request.data))
        self.assertEqual(kwargs["timeout"], 5)

    def test_rejects_unexpected_commands(self):
        opener = Mock(return_value=io.BytesIO(b'{"data":{"pendingCommands":[{}]}}'))
        with self.assertRaisesRegex(SgiError, "sin órdenes"):
            send_snapshot("https://sgi.example.com", "x" * 43, "demo-bot", "Demo bot", {}, open_request=opener)


def test_real_store_counts_current_unanswered_and_pending_replies(store):
    marketplace_leads.ensure_schema(store)
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=15)).isoformat()
    with store.connect() as connection:
        for thread, activity, awaiting, never in [
            ("new", today, 1, 1),
            ("answered", today, 0, 0),
            ("ongoing", today, 1, 0),
            ("old", old, 1, 1),
        ]:
            connection.execute(
                """INSERT INTO leads
                   (account, thread, title, family_key, started_on, last_activity,
                    awaiting_reply, never_answered, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("shop", thread, "Private conversation title", "demo_item", activity,
                 activity, awaiting, never, activity, activity),
            )
    snapshot = build_snapshot(store, {"accounts": [], "enabled": False, "mode": "supervised"})
    assert snapshot["leads"]["total14d"] == 3
    assert snapshot["leads"]["awaitingReply"] == 2
    assert snapshot["leads"]["unansweredRecent"] == 1
    assert snapshot["leads"]["unanswered"][0]["startedOn"] == ""
    assert "Private conversation title" not in json.dumps(snapshot)


def test_snapshot_marks_twins_before_truncating_listings(store, monkeypatch):
    def listing(i, title):
        return {
            "key": str(i), "account": "test", "title": title, "family_key": "demo",
            "listed_on": date.today().isoformat(), "status": "in_stock",
            "clicks_14d": 0, "recommendation": "keep",
        }

    rows = [listing(i, f"Producto {i}") for i in range(50)]
    rows.append(listing(50, "Producto 0"))
    monkeypatch.setattr(marketplace_listings, "list_listings", lambda store: rows)
    snapshot = build_snapshot(store, {"accounts": [], "enabled": False, "mode": "supervised"})
    items = snapshot["listings"]["items"]
    assert len(items) == 50
    assert [item["key"] for item in items if item["isExactTwin"]] == ["0"]
    assert items[1]["isExactTwin"] is False


def test_monitor_uses_its_own_secret_name_and_file():
    with patch.object(monitor, "save_secret") as save:
        monitor.save_monitor_key("x" * 43)
    assert save.call_args.args[0] == "sgi_monitor_key"
    assert save.call_args.args[1] == monitor.MONITOR_SECRET_PATH
    assert monitor.MONITOR_SECRET_PATH.name == ".sgi_monitor_key.bin"


def test_disabled_monitor_does_not_read_secret_or_build_snapshot():
    with (
        patch.object(monitor, "load_settings", return_value={"sgi": {}}),
        patch.object(monitor, "load_secret") as secret,
        patch.object(monitor, "build_snapshot") as snapshot,
    ):
        assert monitor.send_configured_snapshot(Mock(), {}) is False
    secret.assert_not_called()
    snapshot.assert_not_called()


def test_enabled_monitor_uses_platform_secret_store_without_catalog_key(monkeypatch):
    monkeypatch.delenv(monitor.MONITOR_KEY_ENV, raising=False)
    settings = {"sgi": {"base_url": "https://sgi.example.com", "monitor": {
        "enabled": True, "instance_key": "demo-bot", "instance_name": "Demo bot",
    }}}
    with (
        patch.object(monitor, "load_settings", return_value=settings),
        patch.object(monitor, "load_secret", return_value="x" * 43) as secret,
        patch.object(monitor, "build_snapshot", return_value={}),
        patch.object(monitor, "send_snapshot") as send,
    ):
        assert monitor.send_configured_snapshot(Mock(), {}) is True
    secret.assert_called_once_with("sgi_monitor_key", monitor.MONITOR_SECRET_PATH)
    assert send.call_args.args[:4] == ("https://sgi.example.com", "x" * 43, "demo-bot", "Demo bot")

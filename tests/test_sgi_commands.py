"""Remote command delivery uses temporary databases and mocked network effects."""
from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest

import marketplace_listing_worker as listing_worker
import marketplace_listings as listings
import marketplace_safety as safety
import marketplace_scheduler as scheduler
import marketplace_scheduler_worker as worker
import sgi_commands as commands
import sgi_monitor as monitor
from conftest import enqueue_job
from marketplace_storage import MarketplaceStore
from sgi_command_journal import CommandJournal


def order(payload, **overrides):
    return {"id": str(uuid4()), "payload": payload,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), **overrides}


@pytest.fixture()
def journal(store):
    return CommandJournal(store, "demo-bot")


@pytest.fixture()
def local_config(store, monkeypatch, tmp_path):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"accounts": {"shop": {}}}), encoding="utf-8")
    monkeypatch.setattr(scheduler, "ACCOUNTS_PATH", accounts)
    monkeypatch.setattr(listing_worker, "_accounts", lambda: {"shop": {}})
    monkeypatch.setattr(worker, "PUBLISH_INTENTS_DIR", tmp_path / "intents")
    scheduler.save_config(store, {"accounts": ["shop"], "account": "shop", "enabled": False})


@pytest.mark.parametrize("kind", [
    "SET_BOT_ENABLED", "GENERATE_WEEK", "APPROVE_QUEUE_ITEMS", "QUEUE_ITEM_ACTION",
    "LISTING_ACTION", "CANCEL_LISTING_ACTION", "REQUEST_SCAN", "SYNC_SGI_CATALOG",
    "PAUSE_ACCOUNT", "RESUME_ACCOUNT", "SET_SAFETY_LEVEL", "RESOLVE_ALERT", "RESOLVE_ALL_ALERTS",
])
def test_every_sgi_command_has_one_durable_execution(store, journal, monkeypatch, kind):
    handler = Mock(return_value="Confirmado")
    monkeypatch.setitem(commands.HANDLERS, kind, handler)
    incoming = order({"type": kind})
    commands.execute_command(store, journal, incoming)
    commands.execute_command(store, CommandJournal(MarketplaceStore(store.path), "demo-bot"), incoming)
    handler.assert_called_once_with(store, incoming["payload"])
    assert journal.pending_results() == [{"id": incoming["id"], "status": "COMPLETED", "detail": "Confirmado"}]


def test_simultaneous_receipts_claim_the_id_once(store):
    command_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: CommandJournal(store, "demo-bot").begin(command_id, {"type": "TEST"}), range(2)))
    assert sorted(claims) == [False, True]


def test_same_id_with_changed_payload_is_rejected(store, journal, monkeypatch):
    handler = Mock(return_value="Confirmado")
    monkeypatch.setitem(commands.HANDLERS, "SET_BOT_ENABLED", handler)
    incoming = order({"type": "SET_BOT_ENABLED", "enabled": False})
    commands.execute_command(store, journal, incoming)
    with pytest.raises(ValueError, match="otro contenido"):
        commands.execute_command(store, journal, {**incoming, "payload": {"type": "SET_BOT_ENABLED", "enabled": True}})
    assert handler.call_count == 1


@pytest.mark.parametrize("expires", ["2020-01-01T00:00:00Z", "2020-01-01T00:00:00", "invalid"])
def test_expired_or_invalid_expiry_never_executes(store, journal, monkeypatch, expires):
    handler = Mock()
    monkeypatch.setitem(commands.HANDLERS, "SET_BOT_ENABLED", handler)
    commands.execute_command(store, journal, order({"type": "SET_BOT_ENABLED", "enabled": True}, expiresAt=expires))
    handler.assert_not_called()
    assert journal.pending_results()[0]["status"] == "FAILED"


def test_interruption_after_effect_requires_review_and_never_repeats(store, journal, monkeypatch):
    effect = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setitem(commands.HANDLERS, "SET_BOT_ENABLED", effect)
    incoming = order({"type": "SET_BOT_ENABLED", "enabled": False})
    with pytest.raises(KeyboardInterrupt):
        commands.execute_command(store, journal, incoming)
    restored = CommandJournal(MarketplaceStore(store.path), "demo-bot")
    assert restored.recover_interrupted() == 1
    commands.execute_command(store, restored, incoming)
    assert effect.call_count == 1
    result = restored.pending_results()[0]
    assert result["status"] == "FAILED"
    assert "Verifica el estado local" in result["detail"]


def test_unknown_type_and_private_exception_are_not_exposed(store, journal, monkeypatch):
    commands.execute_command(store, journal, order({"type": "RUN_ARBITRARY_CODE"}))
    monkeypatch.setitem(commands.HANDLERS, "SYNC_SGI_CATALOG", Mock(side_effect=RuntimeError("private-token-value")))
    commands.execute_command(store, journal, order({"type": "SYNC_SGI_CATALOG"}))
    assert all(result["status"] == "FAILED" for result in journal.pending_results())
    assert "private-token-value" not in json.dumps(journal.pending_results())


def test_journal_scope_with_underscore_is_literal(store):
    first = CommandJournal(store, "bot_one")
    second = CommandJournal(store, "botXone")
    command_id = str(uuid4())
    second.begin(command_id, {"type": "TEST"})
    second.finish(command_id, "COMPLETED", "Confirmado")
    assert first.pending_results() == []
    assert first.recover_interrupted() == 0


def test_results_survive_lost_ack_and_restart_without_another_effect(store, journal, monkeypatch):
    settings = {"sgi": {"base_url": "https://sgi.example.com", "monitor": {
        "enabled": True, "control_enabled": True, "instance_key": "demo-bot",
    }}}
    monkeypatch.setattr(monitor, "load_settings", lambda: settings)
    monkeypatch.setattr(monitor, "load_secret", lambda *args: "x" * 43)
    monkeypatch.setattr(monitor, "build_snapshot", lambda *args: {})
    incoming = order({"type": "SET_BOT_ENABLED", "enabled": False})
    handler = Mock(return_value="Confirmado")
    monkeypatch.setitem(commands.HANDLERS, "SET_BOT_ENABLED", handler)
    deliveries = []

    def exchange(*args, **kwargs):
        deliveries.append(kwargs["command_results"])
        if len(deliveries) == 1:
            return [incoming]
        if len(deliveries) == 2:
            raise monitor.SgiError("MONITOR_UNAVAILABLE", "Respuesta perdida")
        return []

    monkeypatch.setattr(monitor, "send_snapshot", exchange)
    monitor.send_configured_snapshot(store, {})
    with pytest.raises(monitor.SgiError):
        monitor.send_configured_snapshot(store, {})
    assert len(journal.pending_results()) == 1
    monitor.send_configured_snapshot(MarketplaceStore(store.path), {})
    assert deliveries[1] == deliveries[2]
    assert journal.pending_results() == []
    assert handler.call_count == 1


def test_command_protocol_and_results_are_sent_only_when_enabled():
    command = order({"type": "SYNC_SGI_CATALOG"})
    response = Mock(return_value=io.BytesIO(json.dumps({"data": {"pendingCommands": [command]}}).encode()))
    results = [{"id": str(uuid4()), "status": "COMPLETED", "detail": "Confirmado"}]
    received = monitor.send_snapshot("https://sgi.example.com", "x" * 43, "demo-bot", "Demo", {},
                                     control_enabled=True, command_results=results, open_request=response)
    body = json.loads(response.call_args.args[0].data)
    assert body["commandProtocolVersion"] == 1
    assert body["commandResults"] == results
    assert received == [command]


def test_more_than_one_batch_of_results_is_not_lost(journal):
    for _ in range(30):
        command_id = str(uuid4())
        journal.begin(command_id, {"type": "TEST"})
        journal.finish(command_id, "COMPLETED", "Confirmado")
    first = journal.pending_results()
    assert len(first) == 25
    journal.acknowledge([item["id"] for item in first])
    assert len(journal.pending_results()) == 5


def test_enable_pause_resume_and_safety_use_existing_settings(store, journal, local_config):
    for payload in [
        {"type": "SET_BOT_ENABLED", "enabled": True},
        {"type": "PAUSE_ACCOUNT", "account": "shop", "hours": 2},
        {"type": "SET_SAFETY_LEVEL", "level": "conservative"},
    ]:
        commands.execute_command(store, journal, order(payload))
    assert scheduler.load_config(store)["enabled"] is True
    assert "shop" in safety.active_pauses(store)
    assert safety.load_safety(store)["level"] == "conservative"
    assert safety.load_safety(store)["max_per_account_day"] == safety.LEVELS["conservative"]["max_per_account_day"]
    commands.execute_command(store, journal, order({"type": "RESUME_ACCOUNT", "account": "shop"}))
    assert "shop" not in safety.active_pauses(store)
    assert all(result["status"] == "COMPLETED" for result in journal.pending_results())


def test_empty_approval_cannot_approve_the_entire_calendar(store, journal):
    queue_id = enqueue_job(store, status="planned", approved=False)
    commands.execute_command(store, journal, order({"type": "APPROVE_QUEUE_ITEMS", "ids": []}))
    assert store.get_queue_item(queue_id)["approved"] is False
    commands.execute_command(store, journal, order({"type": "APPROVE_QUEUE_ITEMS", "ids": [queue_id]}))
    assert store.get_queue_item(queue_id)["approved"] is True


def test_queue_action_cannot_change_an_item_already_running(store, journal):
    queue_id = enqueue_job(store, status="running")
    commands.execute_command(store, journal, order({"type": "QUEUE_ITEM_ACTION", "id": queue_id, "action": "cancel"}))
    assert store.get_queue_item(queue_id)["status"] == "running"
    assert journal.pending_results()[0]["status"] == "FAILED"


def test_listing_command_enqueues_one_existing_local_action(store, journal):
    listings.save_scan(store, "shop", [{"title": "Demo product", "listed_on": date.today().isoformat(),
                                       "status": "in_stock", "clicks_14d": 1}])
    listing = listings.list_listings(store)[0]
    incoming = order({"type": "LISTING_ACTION", "key": listing["key"], "action": "renew"})
    commands.execute_command(store, journal, incoming)
    commands.execute_command(store, journal, incoming)
    pending = listings.pending_actions(store)
    assert len(pending) == 1
    commands.execute_command(store, journal, order({"type": "CANCEL_LISTING_ACTION", "id": pending[0]["id"]}))
    assert listings.pending_actions(store) == []


def test_scan_and_alert_resolution_reuse_the_worker_state(store, journal, local_config):
    alert_id = store.create_alert("warning", "TEST", "Demo")
    commands.execute_command(store, journal, order({"type": "REQUEST_SCAN", "account": "shop"}))
    assert store.worker_state(listing_worker.scan_state_key("shop"))["status"] == "requested"
    commands.execute_command(store, journal, order({"type": "RESOLVE_ALERT", "id": alert_id}))
    assert store.list_alerts() == []


def test_sync_uses_the_existing_catalog_refresh(store, journal, monkeypatch):
    refresh = Mock(return_value={})
    monkeypatch.setattr(worker, "refresh_from_sgi", refresh)
    commands.execute_command(store, journal, order({"type": "SYNC_SGI_CATALOG"}))
    refresh.assert_called_once_with(store)
    assert journal.pending_results()[0]["status"] == "COMPLETED"


def test_generation_calls_existing_cancellation_then_generation(store, journal, monkeypatch):
    called = []
    monkeypatch.setattr(scheduler, "cancel_future", lambda value: called.append("cancel"))
    monkeypatch.setattr(scheduler, "generate_week", lambda value, config: called.append("generate"))
    commands.execute_command(store, journal, order({"type": "GENERATE_WEEK", "replace": True}))
    assert called == ["cancel", "generate"]
    assert journal.pending_results()[0]["status"] == "COMPLETED"


@pytest.mark.parametrize("payload", [
    {"type": "SET_BOT_ENABLED", "enabled": "false"},
    {"type": "PAUSE_ACCOUNT", "account": "shop", "hours": True},
    {"type": "PAUSE_ACCOUNT", "account": "unknown", "hours": 2},
    {"type": "SET_SAFETY_LEVEL", "level": "unsafe"},
    {"type": "REQUEST_SCAN", "account": False},
])
def test_invalid_control_input_does_not_change_local_settings(store, journal, local_config, payload):
    before = scheduler.load_config(store)
    safety_before = safety.load_safety(store)
    commands.execute_command(store, journal, order(payload))
    assert scheduler.load_config(store) == before
    assert safety.load_safety(store) == safety_before
    assert safety.active_pauses(store) == {}
    assert journal.pending_results()[0]["status"] == "FAILED"


def test_account_names_with_spaces_and_accents_can_be_controlled(store, journal, monkeypatch, tmp_path):
    names = ["Dylan Steven", "Sebastián"]
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"accounts": {name: {} for name in names}}), encoding="utf-8")
    monkeypatch.setattr(scheduler, "ACCOUNTS_PATH", accounts)
    monkeypatch.setattr(listing_worker, "_accounts", lambda: {name: {} for name in names})
    scheduler.save_config(store, {"accounts": names, "account": names[0], "enabled": False})
    for name in names:
        commands.execute_command(store, journal, order({"type": "PAUSE_ACCOUNT", "account": name, "hours": 2}))
        assert name in safety.active_pauses(store)
        commands.execute_command(store, journal, order({"type": "REQUEST_SCAN", "account": name}))
        assert store.worker_state(listing_worker.scan_state_key(name))["status"] == "requested"
        commands.execute_command(store, journal, order({"type": "RESUME_ACCOUNT", "account": name}))
        assert name not in safety.active_pauses(store)
    assert all(result["status"] == "COMPLETED" for result in journal.pending_results())


@pytest.mark.parametrize("name", ["Dylan\nSteven", "Dylan\tSteven", " ", "../shop", "x" * 65])
def test_malformed_account_names_are_rejected(store, journal, local_config, name):
    commands.execute_command(store, journal, order({"type": "PAUSE_ACCOUNT", "account": name, "hours": 2}))
    assert safety.active_pauses(store) == {}
    assert journal.pending_results()[0]["status"] == "FAILED"


def test_a_malformed_order_is_closed_and_does_not_block_the_next_ones(store, journal, monkeypatch):
    settings = {"sgi": {"base_url": "https://sgi.example.com", "monitor": {
        "enabled": True, "control_enabled": True, "instance_key": "demo-bot",
    }}}
    monkeypatch.setattr(monitor, "load_settings", lambda: settings)
    monkeypatch.setattr(monitor, "load_secret", lambda *args: "x" * 43)
    monkeypatch.setattr(monitor, "build_snapshot", lambda *args: {})
    handler = Mock(return_value="Confirmado")
    monkeypatch.setitem(commands.HANDLERS, "SET_BOT_ENABLED", handler)
    done = order({"type": "SET_BOT_ENABLED", "enabled": False})
    commands.execute_command(store, journal, done)
    journal.acknowledge([done["id"]])
    malformed = order("no es un objeto")
    reused = {**done, "payload": {"type": "SET_BOT_ENABLED", "enabled": True}}
    valid = order({"type": "SET_BOT_ENABLED", "enabled": True})
    deliveries = []

    def exchange(*args, **kwargs):
        deliveries.append(kwargs["command_results"])
        return [malformed, reused, valid] if len(deliveries) == 1 else []

    monkeypatch.setattr(monitor, "send_snapshot", exchange)
    monitor.send_configured_snapshot(store, {})
    assert handler.call_count == 2  # la orden original y la válida; la reutilizada no corre
    monitor.send_configured_snapshot(store, {})
    sent = {result["id"]: result for result in deliveries[1]}
    assert sent[malformed["id"]]["status"] == "FAILED"
    assert sent[done["id"]] == {"id": done["id"], "status": "COMPLETED", "detail": "Confirmado"}
    assert sent[valid["id"]]["status"] == "COMPLETED"


def test_rejecting_an_unknown_or_invalid_id_records_nothing(store, journal):
    assert journal.reject(None, "x") is False
    assert journal.reject("no-es-uuid", "x") is False
    assert journal.pending_results() == []


def test_manual_retry_clears_only_its_own_publishing_intent(store, journal, local_config):
    queue_id = enqueue_job(store, status="blocked")
    worker.PUBLISH_INTENTS_DIR.mkdir()
    path = worker.intent_path_for(queue_id)
    path.write_text("{}", encoding="utf-8")
    commands.execute_command(store, journal, order({"type": "QUEUE_ITEM_ACTION", "id": queue_id, "action": "retry"}))
    assert not path.exists()
    assert store.get_queue_item(queue_id)["status"] == "queued"

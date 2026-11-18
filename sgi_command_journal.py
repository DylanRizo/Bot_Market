"""Durable receipts and results for the SGI command protocol."""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from marketplace_storage import MarketplaceStore, json_text, now_iso


class CommandJournal:
    def __init__(self, store: MarketplaceStore, instance_key: str):
        self.store = store
        self.prefix = f"sgi.command.{instance_key}."

    def begin(self, command_id: str, payload: dict[str, Any]) -> bool:
        """Claim an ID before applying its effect. A repeated ID never runs twice."""
        key = self.prefix + command_id
        digest = hashlib.sha256(json_text(payload).encode("utf-8")).hexdigest()
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            if row:
                receipt = json.loads(row["value_json"])
                if receipt["payload_hash"] != digest:
                    raise ValueError("El SGI reutilizó una orden con otro contenido.")
                if receipt["status"] != "RUNNING":
                    receipt["acknowledged"] = False
                    connection.execute("UPDATE settings SET value_json=?, updated_at=? WHERE key=?",
                                       (json_text(receipt), now_iso(), key))
                return False
            receipt = {"id": command_id, "payload_hash": digest, "status": "RUNNING",
                       "type": payload.get("type"), "detail": "", "acknowledged": False}
            connection.execute("INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?)",
                               (key, json_text(receipt), now_iso()))
        return True

    def finish(self, command_id: str, status: str, detail: str) -> None:
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("Estado de resultado no válido.")
        key = self.prefix + command_id
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            if not row:
                raise ValueError("La orden no tiene recibo local.")
            receipt = json.loads(row["value_json"])
            receipt.update(status=status, detail=detail[:500], acknowledged=False)
            connection.execute("UPDATE settings SET value_json=?, updated_at=? WHERE key=?",
                               (json_text(receipt), now_iso(), key))

    def reject(self, command_id: Any, detail: str) -> bool:
        """Cierra una orden recibida que no se pudo empezar, para que no trabe la cola.

        Si el ID ya tiene recibo, no lo pisa: vuelve a enviar el resultado guardado.
        Devuelve True solo cuando registra un fallo nuevo.
        """
        if not isinstance(command_id, str):
            return False
        try:
            if str(UUID(command_id)) != command_id:
                return False
        except ValueError:
            return False
        key = self.prefix + command_id
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            if row:
                receipt = json.loads(row["value_json"])
                if receipt["status"] in {"COMPLETED", "FAILED"} and receipt["acknowledged"]:
                    receipt["acknowledged"] = False
                    connection.execute("UPDATE settings SET value_json=?, updated_at=? WHERE key=?",
                                       (json_text(receipt), now_iso(), key))
                return False
            receipt = {"id": command_id, "payload_hash": "", "status": "FAILED",
                       "type": None, "detail": detail[:500], "acknowledged": False}
            connection.execute("INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?)",
                               (key, json_text(receipt), now_iso()))
        return True

    def pending_results(self) -> list[dict[str, str]]:
        with self.store.connect() as connection:
            rows = connection.execute("SELECT value_json FROM settings WHERE substr(key,1,?)=? ORDER BY updated_at,key",
                                      (len(self.prefix), self.prefix)).fetchall()
        results = []
        for row in rows:
            receipt = json.loads(row["value_json"])
            if receipt["status"] in {"COMPLETED", "FAILED"} and not receipt["acknowledged"]:
                results.append({"id": receipt["id"], "status": receipt["status"], "detail": receipt["detail"]})
        return results[:25]

    def acknowledge(self, command_ids: list[str]) -> None:
        with self.store.transaction(immediate=True) as connection:
            for command_id in command_ids:
                key = self.prefix + command_id
                row = connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
                if not row:
                    continue
                receipt = json.loads(row["value_json"])
                if receipt["status"] == "RUNNING":
                    continue
                receipt["acknowledged"] = True
                connection.execute("UPDATE settings SET value_json=?, updated_at=? WHERE key=?",
                                   (json_text(receipt), now_iso(), key))

    def recover_interrupted(self) -> int:
        """Called once after acquiring the worker's exclusive process lock."""
        changed = 0
        with self.store.transaction(immediate=True) as connection:
            rows = connection.execute("SELECT key,value_json FROM settings WHERE substr(key,1,?)=?",
                                      (len(self.prefix), self.prefix)).fetchall()
            for row in rows:
                receipt = json.loads(row["value_json"])
                if receipt["status"] != "RUNNING":
                    continue
                receipt.update(status="FAILED", acknowledged=False,
                               detail="Ejecución interrumpida. Verifica el estado local antes de solicitar otra vez esta acción.")
                connection.execute("UPDATE settings SET value_json=?, updated_at=? WHERE key=?",
                                   (json_text(receipt), now_iso(), row["key"]))
                changed += 1
        return changed

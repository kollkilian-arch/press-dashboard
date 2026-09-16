"""Resumable assistant indexing shared by all web workers.

The scheduler checks for changed articles automatically. HTTP handlers only
request a pass or read progress; they never call the embedding provider.
"""
import json
import logging
import time
import uuid

import ai
import database as db


STATE_KEY = "assistant_index_state"
REQUEST_KEY = "assistant_index_request"
logger = logging.getLogger(__name__)


def _read_state():
    try:
        state = json.loads(db.get_setting(STATE_KEY) or "{}")
        return state if isinstance(state, dict) else {}
    except (ValueError, TypeError):
        return {}


def get_status():
    state = _read_state()
    requested = db.get_setting(REQUEST_KEY)
    if requested and requested != state.get("request_id"):
        return {**state, "status": "queued", "message": "Suchindex-Aktualisierung ist vorgemerkt."}
    return {"status": "idle", "message": "Suchindex wird automatisch aktuell gehalten.", **state}


def request_refresh():
    # Durable even if the worker serving this request immediately restarts.
    db.set_setting(REQUEST_KEY, uuid.uuid4().hex)
    return get_status()


def run_batch():
    with db.assistant_index_lock() as acquired:
        if not acquired:
            return
        state = _read_state()
        requested = db.get_setting(REQUEST_KEY)
        now = time.time()
        new_request = bool(requested and requested != state.get("request_id"))
        # Each worker has a scheduler, but the shared lock and timestamp keep
        # the effective cadence independent of the number of workers.
        if not new_request:
            cooldown = 5 if state.get("has_more") else 60
            if now - state.get("updated_at", 0) < cooldown:
                return
        if not state.get("has_more"):
            state = {
                "last_article_id": 0, "checked_articles": 0,
                "refreshed_articles": 0, "embedded_chunks": 0, "failed_articles": 0,
            }
        state.update({
            "request_id": requested, "status": "running", "has_more": True,
            "updated_at": now, "message": "Suchindex wird im Hintergrund aktualisiert.",
        })
        db.set_setting(STATE_KEY, json.dumps(state))
        try:
            state["total_articles"] = db.count_pinned_assistant_articles()
            result = ai.refresh_pinned_article_chunks(after_article_id=state["last_article_id"])
            for key in ("checked_articles", "refreshed_articles", "embedded_chunks", "failed_articles"):
                state[key] += result[key]
            state.update({
                "last_article_id": result["last_article_id"],
                "has_more": result["has_more"],
                "embedding_model": result["embedding_model"],
            })
            if not result["has_more"]:
                state["status"] = "error" if state["failed_articles"] else "idle"
                state["message"] = (
                    "Einige Artikel konnten nicht indexiert werden. Ein neuer Versuch erfolgt automatisch."
                    if state["failed_articles"] else "Suchindex-Aktualisierung abgeschlossen."
                )
        except Exception:
            logger.exception("Assistant index batch failed; the next tick will resume it")
            state["status"] = "error"
            state["message"] = "Indexaktualisierung unterbrochen. Sie wird automatisch fortgesetzt."
        state["updated_at"] = time.time()
        db.set_setting(STATE_KEY, json.dumps(state))

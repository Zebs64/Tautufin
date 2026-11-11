"""Webhook du plugin Jellyfin Webhook (authentifié par jeton)."""

import asyncio
import logging
import secrets

from fastapi import FastAPI, HTTPException, Request

logger = logging.getLogger(__name__)


def register(app: FastAPI, context) -> None:
    config = context.config
    monitor = context.monitor

    # ------------------------------------------------------------------

    @app.post("/webhook")
    async def webhook(request: Request):
        """Événements du plugin Jellyfin Webhook. Exige le jeton configuré
        (en-tête X-Tautufin-Token ou paramètre ?token=) ; sans jeton
        configuré, l'endpoint n'existe pas. L'authentification précède la
        lecture du corps."""
        expected = config.webhook_token
        supplied = (
            request.headers.get("X-Tautufin-Token") or request.query_params.get("token") or ""
        )
        if not expected or not secrets.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(status_code=404)
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="JSON attendu") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Objet JSON attendu")
        try:
            await asyncio.to_thread(monitor.handle_webhook, payload)
        except Exception:
            logger.exception("Erreur de traitement du webhook")
        return {"ok": True}

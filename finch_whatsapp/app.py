"""
Finch WhatsApp bridge v0.3 — Meta webhook receiver that calls the local Finch API.

WhatsApp message -> Finch WhatsApp service -> Finch local API -> WhatsApp reply.
No grocery logic lives here; cart guardrails remain in finch/api.py.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response

from finch.env_util import load_env
from finch_whatsapp import __version__, config, handler
from finch_whatsapp.webhook import extract_text_messages

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    load_env(force=True)
    config.validate_startup()
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="Finch WhatsApp Bridge",
        version=__version__,
        lifespan=_lifespan,
    )

    @application.get("/webhook")
    def verify_webhook(
        hub_mode: str | None = Query(default=None, alias="hub.mode"),
        hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    ) -> Response:
        expected = config.verify_token()
        if hub_mode != "subscribe":
            raise HTTPException(status_code=403, detail="Invalid hub.mode")
        if not hub_verify_token or hub_verify_token != expected:
            logger.warning("Webhook verification failed")
            raise HTTPException(status_code=403, detail="Verification failed")
        if not hub_challenge:
            raise HTTPException(status_code=400, detail="Missing hub.challenge")
        logger.info("Webhook verification succeeded")
        return Response(content=hub_challenge, media_type="text/plain")

    @application.post("/webhook")
    async def receive_webhook(request: Request) -> dict[str, str]:
        try:
            payload: dict[str, Any] = await request.json()
        except Exception:
            logger.warning("Webhook POST received invalid JSON")
            return {"status": "ignored"}

        messages = extract_text_messages(payload)
        if not messages:
            return {"status": "ok"}

        for message in messages:
            if not config.is_allowed_sender(message.sender):
                logger.warning("Rejected message from non-whitelisted sender")
                continue
            logger.info("Processing WhatsApp command for allowed sender")
            handler.process_inbound(message)

        return {"status": "ok"}

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return application


app = create_app()


def main() -> None:
    import uvicorn

    host = config.bind_host()
    port = config.bind_port()
    uvicorn.run("finch_whatsapp.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()

"""
Tests for Finch WhatsApp bridge v0.3.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finch.env_util import reset_env_load_state


@pytest.fixture(autouse=True)
def _whatsapp_test_env_isolation(monkeypatch):
    monkeypatch.setenv("FINCH_SKIP_DOTENV", "1")
    reset_env_load_state()
    yield
    reset_env_load_state()


@pytest.fixture
def whatsapp_env(monkeypatch):
    monkeypatch.setenv("FINCH_WHATSAPP_TEST_MODE", "1")
    monkeypatch.setenv("FINCH_WHATSAPP_VERIFY_TOKEN", "test-verify-token")
    monkeypatch.setenv("FINCH_WHATSAPP_ACCESS_TOKEN", "test-wa-access-token")
    monkeypatch.setenv("FINCH_WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    monkeypatch.setenv("FINCH_WHATSAPP_ALLOWED_NUMBERS", "15551234567,15557654321")
    monkeypatch.setenv("FINCH_API_KEY", "test-fin-api-key")
    monkeypatch.setenv("FINCH_API_BASE_URL", "http://127.0.0.1:8091")


@pytest.fixture
def whatsapp_client(whatsapp_env):
    from finch_whatsapp.app import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client


WHATSAPP_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "messages": [
                            {
                                "from": "15551234567",
                                "id": "wamid.test",
                                "type": "text",
                                "text": {"body": "preview eggs, milk"},
                            }
                        ]
                    }
                }
            ]
        }
    ],
}


class TestWhatsAppStartup:
    def test_refuses_start_without_verify_token(self, monkeypatch):
        monkeypatch.setenv("FINCH_SKIP_DOTENV", "1")
        monkeypatch.delenv("FINCH_WHATSAPP_TEST_MODE", raising=False)
        monkeypatch.delenv("FINCH_WHATSAPP_VERIFY_TOKEN", raising=False)
        reset_env_load_state()

        from finch_whatsapp.app import create_app

        app = create_app()
        with pytest.raises(RuntimeError, match="FINCH_WHATSAPP_VERIFY_TOKEN"):
            with TestClient(app):
                pass


class TestWebhookVerification:
    def test_verification_success(self, whatsapp_client):
        response = whatsapp_client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test-verify-token",
                "hub.challenge": "challenge-12345",
            },
        )
        assert response.status_code == 200
        assert response.text == "challenge-12345"

    def test_verification_failure_wrong_token(self, whatsapp_client):
        response = whatsapp_client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token",
                "hub.challenge": "challenge-12345",
            },
        )
        assert response.status_code == 403

    def test_verification_failure_wrong_mode(self, whatsapp_client):
        response = whatsapp_client.get(
            "/webhook",
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": "test-verify-token",
                "hub.challenge": "challenge-12345",
            },
        )
        assert response.status_code == 403


class TestWebhookInbound:
    def test_rejects_non_whitelisted_sender(self, whatsapp_client):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "19998887777",
                                        "type": "text",
                                        "text": {"body": "preview eggs"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        }
        with patch("finch_whatsapp.handler.finch_client.preview") as mock_preview:
            with patch("finch_whatsapp.handler.whatsapp_client.send_text_message") as mock_send:
                response = whatsapp_client.post("/webhook", json=payload)
        assert response.status_code == 200
        mock_preview.assert_not_called()
        mock_send.assert_not_called()

    @patch("finch_whatsapp.handler.whatsapp_client.send_text_message")
    @patch("finch_whatsapp.handler.finch_client.preview")
    def test_preview_calls_finch_api_and_replies(
        self, mock_preview, mock_send, whatsapp_client
    ):
        mock_preview.return_value = {
            "lines": [
                {
                    "requested_item": "eggs",
                    "normalized_name": "eggs",
                    "matched_alias": "Kroger Eggs",
                    "status": "exact_default",
                },
                {
                    "requested_item": "milk",
                    "normalized_name": "milk",
                    "status": "missing",
                },
            ]
        }

        response = whatsapp_client.post("/webhook", json=WHATSAPP_PAYLOAD)

        assert response.status_code == 200
        mock_preview.assert_called_once_with("eggs, milk")
        mock_send.assert_called_once()
        to, body = mock_send.call_args[0]
        assert to == "15551234567"
        assert "eggs" in body
        assert "milk" in body
        assert "Missing" in body
        assert "test-fin-api-key" not in body
        assert "test-wa-access-token" not in body

    @patch("finch_whatsapp.handler.whatsapp_client.send_text_message")
    @patch("finch_whatsapp.handler.finch_client.cart_add")
    def test_add_blocked_when_live_cart_off(self, mock_add, mock_send, whatsapp_client):
        from finch_whatsapp.finch_client import FinchApiError

        mock_add.side_effect = FinchApiError(403, "FINCH_LIVE_CART is not enabled")

        payload = dict(WHATSAPP_PAYLOAD)
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "add eggs"

        response = whatsapp_client.post("/webhook", json=payload)

        assert response.status_code == 200
        mock_add.assert_called_once_with("eggs")
        body = mock_send.call_args[0][1]
        assert "Cart writes are currently disabled" in body

    @patch("finch_whatsapp.handler.whatsapp_client.send_text_message")
    @patch("finch_whatsapp.handler.finch_client.cart_history")
    def test_history_calls_finch_api(self, mock_history, mock_send, whatsapp_client):
        mock_history.return_value = {
            "entries": [
                {
                    "requested_text": "eggs",
                    "action": "cart_add",
                    "result": "ok (ok)",
                }
            ]
        }

        payload = dict(WHATSAPP_PAYLOAD)
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "history"

        response = whatsapp_client.post("/webhook", json=payload)

        assert response.status_code == 200
        mock_history.assert_called_once_with(limit=10)
        body = mock_send.call_args[0][1]
        assert "eggs" in body
        assert "cart_add" in body

    @patch("finch_whatsapp.handler.whatsapp_client.send_text_message")
    def test_unknown_message_gets_help_hint(self, mock_send, whatsapp_client):
        payload = dict(WHATSAPP_PAYLOAD)
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "hello there"

        response = whatsapp_client.post("/webhook", json=payload)

        assert response.status_code == 200
        body = mock_send.call_args[0][1]
        assert "Unknown command" in body


class TestCommandParsing:
    def test_parse_preview(self):
        from finch_whatsapp.commands import PreviewCommand, parse_command

        cmd = parse_command("preview eggs, milk")
        assert isinstance(cmd, PreviewCommand)
        assert cmd.text == "eggs, milk"

    def test_parse_add(self):
        from finch_whatsapp.commands import AddCommand, parse_command

        cmd = parse_command("add eggs")
        assert isinstance(cmd, AddCommand)
        assert cmd.item == "eggs"

    def test_parse_add_list(self):
        from finch_whatsapp.commands import AddListCommand, parse_command

        cmd = parse_command("add-list eggs, milk")
        assert isinstance(cmd, AddListCommand)
        assert cmd.text == "eggs, milk"

    def test_random_message_not_parsed(self):
        from finch_whatsapp.commands import parse_command

        assert parse_command("buy some eggs") is None


class TestFinchClient:
    def test_finch_client_sends_api_key_header(self, whatsapp_env, monkeypatch):
        from finch_whatsapp import finch_client

        captured: dict = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"lines": []}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def request(self, method, url, headers=None, **kwargs):
                captured["method"] = method
                captured["url"] = url
                captured["headers"] = headers
                captured["kwargs"] = kwargs
                return FakeResponse()

        monkeypatch.setattr(finch_client.httpx, "Client", FakeClient)
        finch_client.preview("eggs")

        assert captured["method"] == "POST"
        assert captured["url"] == "http://127.0.0.1:8091/finch/preview"
        assert captured["headers"]["X-Finch-Key"] == "test-fin-api-key"
        assert captured["kwargs"]["json"] == {"text": "eggs"}


class TestWhatsAppClient:
    def test_send_does_not_log_secrets(self, whatsapp_env, monkeypatch, caplog):
        from finch_whatsapp import whatsapp_client

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"messages": [{"id": "wamid.out"}]}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, headers=None, json=None):
                assert headers["Authorization"] == "Bearer test-wa-access-token"
                assert "test-wa-access-token" not in str(json)
                return FakeResponse()

        monkeypatch.setattr(whatsapp_client.httpx, "Client", FakeClient)

        with caplog.at_level("INFO"):
            whatsapp_client.send_text_message("15551234567", "Preview ok")

        log_text = caplog.text
        assert "test-wa-access-token" not in log_text
        assert "test-fin-api-key" not in log_text

    def test_send_uses_graph_api(self, whatsapp_env, monkeypatch):
        from finch_whatsapp import whatsapp_client

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"messages": [{"id": "wamid.out"}]}
        mock_client.post.return_value = mock_response
        mock_client.__enter__.return_value = mock_client

        monkeypatch.setattr(whatsapp_client.httpx, "Client", lambda *a, **k: mock_client)

        whatsapp_client.send_text_message("15551234567", "hello")

        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://graph.facebook.com/v21.0/1234567890/messages"
        payload = call_args[1]["json"]
        assert payload["to"] == "15551234567"
        assert payload["text"]["body"] == "hello"

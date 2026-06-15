"""
Tests for Finch Telegram bridge v0.1.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finch.env_util import reset_env_load_state


@pytest.fixture(autouse=True)
def _telegram_test_env_isolation(monkeypatch):
    monkeypatch.setenv("FINCH_SKIP_DOTENV", "1")
    reset_env_load_state()
    yield
    reset_env_load_state()


@pytest.fixture
def telegram_env(monkeypatch):
    monkeypatch.setenv("FINCH_TELEGRAM_TEST_MODE", "1")
    monkeypatch.setenv("FINCH_TELEGRAM_BOT_TOKEN", "test-telegram-bot-token")
    monkeypatch.setenv("FINCH_TELEGRAM_ALLOWED_USER_IDS", "111222333,444555666")
    monkeypatch.setenv("FINCH_API_KEY", "test-fin-api-key")
    monkeypatch.setenv("FINCH_API_BASE_URL", "http://127.0.0.1:8091")


SAMPLE_UPDATE = {
    "update_id": 42,
    "message": {
        "message_id": 7,
        "from": {"id": 111222333, "is_bot": False, "first_name": "Vincent"},
        "chat": {"id": 111222333, "type": "private"},
        "text": "preview eggs, milk",
    },
}


class TestTelegramStartup:
    def test_refuses_start_without_bot_token(self, monkeypatch):
        monkeypatch.setenv("FINCH_SKIP_DOTENV", "1")
        monkeypatch.delenv("FINCH_TELEGRAM_TEST_MODE", raising=False)
        monkeypatch.delenv("FINCH_TELEGRAM_BOT_TOKEN", raising=False)
        reset_env_load_state()

        from finch_telegram import config

        with pytest.raises(RuntimeError, match="FINCH_TELEGRAM_BOT_TOKEN"):
            config.validate_startup()


class TestCommandParsing:
    def test_parse_start(self):
        from finch_telegram.commands import StartCommand, parse_command

        assert isinstance(parse_command("/start"), StartCommand)
        assert isinstance(parse_command("/start@FinchBot"), StartCommand)

    def test_parse_preview(self):
        from finch_telegram.commands import PreviewCommand, parse_command

        cmd = parse_command("preview eggs, milk")
        assert isinstance(cmd, PreviewCommand)
        assert cmd.text == "eggs, milk"

    def test_parse_add(self):
        from finch_telegram.commands import AddCommand, parse_command

        cmd = parse_command("add eggs")
        assert isinstance(cmd, AddCommand)
        assert cmd.item == "eggs"

    def test_parse_quantity_shorthand(self):
        from finch_telegram.commands import AddCommand, parse_command

        cmd = parse_command("2 eggs")
        assert isinstance(cmd, AddCommand)
        assert cmd.item == "2 eggs"

    def test_parse_add_list(self):
        from finch_telegram.commands import AddListCommand, parse_command

        cmd = parse_command("add-list eggs, milk")
        assert isinstance(cmd, AddListCommand)
        assert cmd.text == "eggs, milk"

    def test_bare_preview_not_parsed(self):
        from finch_telegram.commands import parse_command

        assert parse_command("preview") is None

    def test_parse_cart_commands(self):
        from finch_telegram.commands import CartCommand, parse_command

        for text in ("cart", "show cart", "current cart"):
            cmd = parse_command(text)
            assert isinstance(cmd, CartCommand), text

    def test_format_cart_response_supported(self):
        from finch_telegram.commands import format_cart_response

        body = format_cart_response(
            {
                "supported": True,
                "items": [
                    {
                        "name": "Kroger Large Eggs",
                        "quantity": 2,
                        "price": "$2.99",
                        "line_total": "$5.98",
                    }
                ],
                "subtotal": "$5.98",
            }
        )
        assert "Current Kroger cart" in body
        assert "Kroger Large Eggs" in body
        assert "Subtotal: $5.98" in body

    def test_format_cart_response_not_supported(self):
        from finch_telegram.commands import format_cart_response

        body = format_cart_response(
            {
                "supported": False,
                "message": "Kroger cart read is not available with Public API access.",
            }
        )
        assert "not available yet" in body
        assert "Public API" in body


class TestHandler:
    @patch("finch_telegram.handler.telegram_client.send_text_message")
    @patch("finch_telegram.handler.finch_client.preview")
    def test_preview_calls_finch_api_and_replies(self, mock_preview, mock_send, telegram_env):
        from finch_telegram.handler import process_inbound
        from finch_telegram.telegram_client import InboundTextMessage

        mock_preview.return_value = {
            "lines": [
                {
                    "requested_item": "eggs",
                    "normalized_name": "eggs",
                    "matched_alias": "Kroger Eggs",
                    "status": "exact_default",
                }
            ]
        }
        message = InboundTextMessage(
            chat_id="111222333",
            user_id="111222333",
            text="preview eggs, milk",
            update_id=42,
        )

        process_inbound(message)

        mock_preview.assert_called_once_with("eggs, milk")
        mock_send.assert_called_once()
        chat_id, body = mock_send.call_args[0]
        assert chat_id == "111222333"
        assert "eggs" in body
        assert "test-fin-api-key" not in body
        assert "test-telegram-bot-token" not in body

    @patch("finch_telegram.handler.telegram_client.send_text_message")
    @patch("finch_telegram.handler.finch_client.cart_add")
    def test_quantity_shorthand_add(self, mock_add, mock_send, telegram_env):
        from finch_telegram.handler import process_inbound
        from finch_telegram.telegram_client import InboundTextMessage

        mock_add.return_value = {
            "attempt": {
                "requested_item": "2 eggs",
                "normalized_name": "eggs",
                "alias_name": "Kroger Eggs",
            }
        }
        message = InboundTextMessage(
            chat_id="111222333",
            user_id="111222333",
            text="2 eggs",
            update_id=43,
        )

        process_inbound(message)

        mock_add.assert_called_once_with("2 eggs")

    @patch("finch_telegram.handler.telegram_client.send_text_message")
    @patch("finch_telegram.handler.finch_client.cart_current")
    def test_cart_calls_finch_api_and_replies(self, mock_cart, mock_send, telegram_env):
        from finch_telegram.handler import process_inbound
        from finch_telegram.telegram_client import InboundTextMessage

        mock_cart.return_value = {
            "supported": True,
            "items": [
                {
                    "name": "Kroger Large Eggs",
                    "quantity": 2,
                    "price": "$2.99",
                    "line_total": "$5.98",
                }
            ],
            "subtotal": "$5.98",
        }
        message = InboundTextMessage(
            chat_id="111222333",
            user_id="111222333",
            text="cart",
            update_id=46,
        )

        process_inbound(message)

        mock_cart.assert_called_once()
        mock_send.assert_called_once()
        body = mock_send.call_args[0][1]
        assert "Kroger Large Eggs" in body
        assert "Subtotal" in body

    @patch("finch_telegram.handler.telegram_client.send_text_message")
    def test_rejects_non_whitelisted_user(self, mock_send, telegram_env, caplog):
        from finch_telegram.handler import process_inbound
        from finch_telegram.telegram_client import InboundTextMessage

        message = InboundTextMessage(
            chat_id="999888777",
            user_id="999888777",
            text="help",
            update_id=44,
        )

        with caplog.at_level("WARNING"):
            process_inbound(message)

        mock_send.assert_not_called()
        assert "non-whitelisted user_id=999888777" in caplog.text

    @patch("finch_telegram.handler.telegram_client.send_text_message")
    def test_logs_user_id_when_whitelist_empty(self, mock_send, monkeypatch, caplog):
        monkeypatch.setenv("FINCH_TELEGRAM_TEST_MODE", "1")
        monkeypatch.setenv("FINCH_TELEGRAM_BOT_TOKEN", "test-telegram-bot-token")
        monkeypatch.setenv("FINCH_TELEGRAM_ALLOWED_USER_IDS", "")
        monkeypatch.setenv("FINCH_API_KEY", "test-fin-api-key")

        from finch_telegram.handler import process_inbound
        from finch_telegram.telegram_client import InboundTextMessage

        message = InboundTextMessage(
            chat_id="555444333",
            user_id="555444333",
            text="/start",
            update_id=45,
        )

        with caplog.at_level("INFO"):
            process_inbound(message)

        mock_send.assert_called_once()
        assert "user_id=555444333" in caplog.text
        assert "test-telegram-bot-token" not in caplog.text


class TestTelegramClient:
    def test_extract_text_messages(self):
        from finch_telegram.telegram_client import extract_text_messages

        messages = extract_text_messages([SAMPLE_UPDATE])
        assert len(messages) == 1
        assert messages[0].chat_id == "111222333"
        assert messages[0].user_id == "111222333"
        assert messages[0].text == "preview eggs, milk"

    def test_send_does_not_log_secrets(self, telegram_env, monkeypatch, caplog):
        from finch_telegram import telegram_client

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"ok": True, "result": {"message_id": 1}}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def request(self, method, url, params=None, json=None):
                assert "test-telegram-bot-token" in url
                assert "test-telegram-bot-token" not in str(json)
                return FakeResponse()

        monkeypatch.setattr(telegram_client.httpx, "Client", FakeClient)

        with caplog.at_level("INFO"):
            telegram_client.send_text_message("111222333", "Preview ok")

        assert "test-telegram-bot-token" not in caplog.text


class TestFinchClient:
    def test_finch_client_sends_api_key_header(self, telegram_env, monkeypatch):
        from finch_telegram import finch_client

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


class TestPollingLoop:
    @patch("finch_telegram.handler.process_inbound")
    @patch("finch_telegram.telegram_client.get_updates")
    def test_run_polling_processes_updates_once(self, mock_get_updates, mock_process, telegram_env):
        from finch_telegram.__main__ import run_polling

        mock_get_updates.return_value = [SAMPLE_UPDATE]

        run_polling(once=True)

        mock_get_updates.assert_called_once()
        mock_process.assert_called_once()
        message = mock_process.call_args[0][0]
        assert message.user_id == "111222333"

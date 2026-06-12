"""Guided Kroger OAuth — browser login and secure token save."""

from __future__ import annotations

import argparse
import secrets
import sys

from finch.env_util import load_env
from finch.kroger_client import KrogerAuthError, load_kroger_client_from_env
from finch.token_store import FINCH_TOKENS_PATH, has_saved_tokens, save_tokens_from_response


def run_auth_flow(*, input_fn=input) -> int:
    load_env()
    try:
        client = load_kroger_client_from_env()
    except KrogerAuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not client.oauth.redirect_uri:
        print(
            "Error: FINCH_KROGER_REDIRECT_URI must be set in .env before OAuth.\n"
            "Example: FINCH_KROGER_REDIRECT_URI=http://localhost:8765/callback",
            file=sys.stderr,
        )
        return 1

    state = secrets.token_urlsafe(16)
    authorize_url = client.build_authorize_url(state=state)

    print("Finch Kroger OAuth")
    print("=" * 40)
    print()
    print("1. Open this URL in your browser and sign in to Kroger:")
    print()
    print(authorize_url)
    print()
    print("2. After approving, Kroger redirects to your callback URL.")
    print("   Copy the authorization code from the redirect (the 'code=' query param).")
    print()
    if has_saved_tokens():
        print("Note: existing saved tokens will be replaced.")
        print()

    code = input_fn("Authorization code: ").strip()
    if not code:
        print("Error: no authorization code provided.", file=sys.stderr)
        return 1

    try:
        token_response = client.exchange_authorization_code_full(code)
        save_tokens_from_response(token_response)
    except (KrogerAuthError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    refresh_saved = bool(token_response.get("refresh_token"))
    print()
    print(f"Tokens saved to {FINCH_TOKENS_PATH} (file mode 600)")
    print(f"  access token: saved")
    print(f"  refresh token: {'saved' if refresh_saved else 'not returned — re-auth may be needed later'}")
    print()
    print("Next steps:")
    print("  1. Set FINCH_LIVE_CART=true in .env")
    print("  2. python -m finch.cart test")
    print("  3. python -m finch.cart add eggs")
    print("  4. Review your cart in the Kroger app (Finch never checks out)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Complete Kroger OAuth and save user tokens outside git.",
    )
    parser.parse_args(argv)
    return run_auth_flow()


if __name__ == "__main__":
    raise SystemExit(main())

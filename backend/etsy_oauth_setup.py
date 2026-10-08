"""
Etsy OAuth 2.0 PKCE Setup — One-Time Token Generator
=====================================================
Run this to authorize this app against your Etsy shop (first time, or to
recover after the refresh token was invalidated). The new access_token +
refresh_token are saved to the shared Mongo record settings {"id": "etsy_auth"},
which the deployed backend and the local scripts all read - nothing to paste.

Requires ETSY_API_KEY, ETSY_SHOP_ID, MONGO_URL and DB_NAME in backend/.env.

Usage:
    python etsy_oauth_setup.py
"""
import base64
import hashlib
import http.server
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pymongo import MongoClient

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

ETSY_API_KEY = os.environ["ETSY_API_KEY"]
REDIRECT_URI = "http://localhost:3003/callback"
SCOPES = "listings_r listings_w"

AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"

_result: dict = {}
_done_event = threading.Event()


def generate_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        _result["code"] = params.get("code", [None])[0]
        _result["state"] = params.get("state", [None])[0]
        _result["error"] = params.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if _result["error"]:
            body = f"<html><body><h1>Authorization failed</h1><p>{_result['error']}</p></body></html>"
        else:
            body = "<html><body><h1>Authorized</h1><p>You can close this tab and return to the terminal.</p></body></html>"
        self.wfile.write(body.encode("utf-8"))

        _done_event.set()

    def log_message(self, format, *args):
        pass  # silence default request logging


def wait_for_callback(timeout: int = 300) -> dict:
    server = http.server.HTTPServer(("localhost", 3003), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    got_it = _done_event.wait(timeout=timeout)
    server.shutdown()
    server.server_close()
    return _result if got_it else {}


def exchange_code_for_tokens(code: str, code_verifier: str) -> dict:
    response = httpx.post(
        TOKEN_URL,
        json={
            "grant_type": "authorization_code",
            "client_id": ETSY_API_KEY,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": code_verifier,
        },
    )
    response.raise_for_status()
    return response.json()


def main():
    state = secrets.token_urlsafe(16)
    code_verifier, code_challenge = generate_pkce_pair()

    auth_params = {
        "response_type": "code",
        "client_id": ETSY_API_KEY,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(auth_params)}"

    print("=" * 70)
    print("ETSY OAUTH SETUP")
    print("=" * 70)
    print("\n1. Open this URL in your browser and approve the app:\n")
    print(auth_url)
    print("\n2. Waiting for callback on http://localhost:3003/callback ...\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    result = wait_for_callback()

    if not result:
        print("\nERROR - timed out waiting for the OAuth callback.")
        return
    if result.get("error"):
        print(f"\nERROR - authorization failed: {result['error']}")
        return
    if not result.get("code"):
        print("\nERROR - no authorization code received.")
        return
    if result.get("state") != state:
        print("\nERROR - state mismatch (possible CSRF). Aborting.")
        return

    print("Authorization code received. Exchanging for tokens...\n")

    try:
        tokens = exchange_code_for_tokens(result["code"], code_verifier)
    except httpx.HTTPStatusError as e:
        print(f"ERROR exchanging code for tokens: {e.response.status_code} {e.response.text}")
        return

    expires_in = int(tokens.get("expires_in") or 3600)
    db = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    db.settings.update_one(
        {"id": "etsy_auth"},
        {"$set": {
            "id": "etsy_auth",
            "client_id": ETSY_API_KEY,
            "shop_id": os.environ.get("ETSY_SHOP_ID", "").strip(),
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": time.time() + expires_in,
            "source": "oauth_setup",
        }},
        upsert=True,
    )

    print("=" * 70)
    print('SUCCESS - tokens saved to Mongo settings {"id": "etsy_auth"} (values not shown).')
    print("=" * 70)
    print(f"(access_token expires in {expires_in} seconds; it auto-refreshes from Mongo after that)")


if __name__ == "__main__":
    main()

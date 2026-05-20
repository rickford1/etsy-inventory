"""
Etsy OAuth2 + PKCE authentication.
Run directly to authorize: python auth.py
Tokens are saved to tokens.json and auto-refreshed.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

ETSY_API_KEY = os.environ["ETSY_API_KEY"]
REDIRECT_URI = os.getenv("ETSY_REDIRECT_URI", "http://localhost:3003/callback")
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "tokens.json")

AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
SCOPES = "listings_r listings_w transactions_r"


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _save_tokens(tokens: dict):
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)


def load_tokens() -> dict | None:
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f)


def refresh_access_token(refresh_token: str) -> dict:
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": ETSY_API_KEY,
        "refresh_token": refresh_token,
    })
    resp.raise_for_status()
    tokens = resp.json()
    _save_tokens(tokens)
    return tokens


def get_valid_token() -> str:
    """Return a valid access token, refreshing if needed."""
    tokens = load_tokens()
    if not tokens:
        raise RuntimeError("Not authenticated. Run: python auth.py")

    # Try a refresh if we have a refresh_token (Etsy tokens expire in 1 hour)
    if "refresh_token" in tokens:
        try:
            tokens = refresh_access_token(tokens["refresh_token"])
        except requests.HTTPError:
            raise RuntimeError("Token refresh failed. Run: python auth.py")

    return tokens["access_token"]


def authorize():
    """Run the full OAuth2 PKCE flow and save tokens."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_params = {
        "response_type": "code",
        "client_id": ETSY_API_KEY,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTH_URL}?{urlencode(auth_params)}"

    auth_code = None
    received_state = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            nonlocal auth_code, received_state
            params = parse_qs(urlparse(self.path).query)
            auth_code = params.get("code", [None])[0]
            received_state = params.get("state", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Authorized! You can close this tab.</h2>")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    port = int(urlparse(REDIRECT_URI).port or 3003)
    server = HTTPServer(("localhost", port), Handler)

    print(f"Opening browser for Etsy authorization...")
    webbrowser.open(auth_url)
    server.serve_forever()

    if received_state != state:
        raise RuntimeError("State mismatch — possible CSRF")
    if not auth_code:
        raise RuntimeError("No auth code received")

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": ETSY_API_KEY,
        "redirect_uri": REDIRECT_URI,
        "code": auth_code,
        "code_verifier": verifier,
    })
    resp.raise_for_status()
    tokens = resp.json()
    _save_tokens(tokens)
    print("Authorized! Tokens saved to tokens.json")
    return tokens


if __name__ == "__main__":
    authorize()

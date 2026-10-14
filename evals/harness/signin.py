"""Sign in to the portal the way a person does.

Nothing here shortcuts the authorization server. The harness registers itself as
a client (RFC 7591), builds a PKCE challenge, walks /oauth/authorize, types the
mobile number, waits for the OTP to arrive on the simulated phone, types it,
consents, and exchanges the code for a token. If the portal's sign-in is broken,
the evals fail -- which is the point: the deployment the graders will test is the
one the evals test, and the graders sign in by hand.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import re
import secrets
import threading
import time

import requests


class SignInError(RuntimeError):
    pass


class _Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                   # noqa: N802 - stdlib name
        self.server.captured = self.path
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Signed in. You can close this window.")

    def log_message(self, *args):
        pass


class _CallbackServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    captured = None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _query(url: str) -> dict:
    from urllib.parse import urlparse, parse_qs
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class Portal:
    """Just enough portal to sign somebody in and read their phone."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.http = requests.Session()

    # ------------------------------------------------------------------
    # the simulated phone
    # ------------------------------------------------------------------
    def wait_for_sms(self, mobile: str, after: float = 0.0, wait: float = 25.0) -> str:
        """The next message that actually reaches this number.

        The gateway holds a message for one to four seconds before it counts as
        delivered, and the text is null until then, so a person reading their
        phone is a poll.
        """
        deadline = time.time() + wait
        while time.time() < deadline:
            resp = self.http.get(self.base + "/__gateway/api/messages",
                                 params={"to": mobile}, timeout=self.timeout)
            resp.raise_for_status()
            for message in reversed(resp.json().get("messages", [])):
                if message.get("delivered") and message.get("text") and \
                        float(message.get("sent_at", 0)) >= after:
                    return message["text"]
            time.sleep(1.0)
        raise SignInError("no SMS reached %s within %.0fs" % (mobile, wait))

    # ------------------------------------------------------------------
    # the authorization walk
    # ------------------------------------------------------------------
    def _register(self, redirect_uri: str, name: str) -> str:
        resp = self.http.post(self.base + "/oauth/register", timeout=self.timeout, json={
            "client_name": name,
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
        })
        if resp.status_code != 201:
            raise SignInError("client registration failed: %s %s"
                              % (resp.status_code, resp.text[:200]))
        return resp.json()["client_id"]

    def sign_in(self, *, scope: str, resource: str, client_name: str,
                mobile: str | None = None, username: str | None = None,
                password: str | None = None) -> dict:
        """Returns {access_token, refresh_token, subject, expires_in}."""
        server = _CallbackServer(("127.0.0.1", 0), _Callback)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        redirect_uri = "http://127.0.0.1:%d/callback" % port

        try:
            client_id = self._register(redirect_uri, client_name)
            verifier = secrets.token_urlsafe(48)
            challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
            state = secrets.token_urlsafe(12)

            # /oauth/authorize redirects to the sign-in page for this request.
            login = self.http.get(self.base + "/oauth/authorize", timeout=self.timeout, params={
                "client_id": client_id, "redirect_uri": redirect_uri,
                "response_type": "code", "scope": scope, "resource": resource,
                "code_challenge": challenge, "code_challenge_method": "S256",
                "state": state,
            })
            if "/oauth/login" not in login.url:
                raise SignInError("expected the sign-in page, landed on %s (%s)"
                                  % (login.url, login.status_code))
            rid = _query(login.url).get("rid", "")

            if scope == "citizen":
                self._sign_in_citizen(rid, mobile)
            else:
                self._sign_in_officer(rid, username, password)

            # Consent, and the code comes back to our little local server.
            consent = self.http.post(login.url.replace("/oauth/login", "/oauth/consent"),
                                     timeout=self.timeout, data={"decision": "allow"})
            if server.captured is None:
                raise SignInError("consent did not come back to the redirect URI "
                                  "(landed on %s)" % consent.url)
            back = _query("http://127.0.0.1" + server.captured)
            if back.get("state") != state:
                raise SignInError("state did not survive the round trip")
            if back.get("iss") != self.base:
                raise SignInError("iss on the authorization response was %r, expected %r"
                                  % (back.get("iss"), self.base))
            if "code" not in back:
                raise SignInError("no code in the authorization response: %s" % back)

            token = self.http.post(self.base + "/oauth/token", timeout=self.timeout, data={
                "grant_type": "authorization_code", "code": back["code"],
                "redirect_uri": redirect_uri, "client_id": client_id,
                "code_verifier": verifier, "resource": resource,
            })
            if token.status_code != 200:
                raise SignInError("token exchange failed: %s %s"
                                  % (token.status_code, token.text[:200]))
            out = token.json()
            out["client_id"] = client_id
            out["subject"] = ("citizen:" + mobile) if scope == "citizen" else ("officer:" + username)
            return out
        finally:
            server.shutdown()
            server.server_close()

    def _sign_in_citizen(self, rid: str, mobile: str) -> None:
        if not mobile:
            raise SignInError("a citizen sign-in needs a mobile number")
        url = self.base + "/oauth/login"
        sent_after = time.time()
        first = self.http.post(url, timeout=self.timeout, params={"rid": rid},
                               data={"mobile": mobile})
        if first.status_code != 200:
            raise SignInError("the portal refused the mobile number: %s" % first.status_code)
        text = self.wait_for_sms(mobile, after=sent_after)
        found = re.search(r"OTP is (\d{6})", text)
        if not found:
            raise SignInError("no OTP in the message the gateway delivered: %r" % text)
        second = self.http.post(url, timeout=self.timeout, params={"rid": rid},
                                data={"otp": found.group(1)})
        if "/oauth/consent" not in second.url:
            raise SignInError("the OTP was not accepted (landed on %s)" % second.url)

    def _sign_in_officer(self, rid: str, username: str, password: str) -> None:
        if not username or not password:
            raise SignInError("an officer sign-in needs a username and password")
        resp = self.http.post(self.base + "/oauth/login", timeout=self.timeout,
                              params={"rid": rid},
                              data={"username": username, "password": password})
        if "/oauth/consent" not in resp.url:
            raise SignInError("the departmental login was refused (landed on %s)" % resp.url)

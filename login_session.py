"""Signed, fixed-duration login cookies for browser refreshes (Streamlit 1.57)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

import streamlit as st
import streamlit.components.v2 as components

COOKIE_NAME = "ai_race_login_v1"
LOGIN_SECONDS = 24 * 60 * 60


def _signing_key(user: str, password: str, secret: str = "") -> bytes:
    # Changing either credential or the optional dedicated secret invalidates cookies.
    material = json.dumps(["ai-race-login-v1", user, password, secret], ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).digest()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def issue_token(user: str, password: str, *, secret: str = "", demo_key: str = "",
                now: int | None = None) -> str:
    issued = int(time.time()) if now is None else int(now)
    payload = {"v": 1, "sub": user, "iat": issued, "exp": issued + LOGIN_SECONDS,
               "jti": secrets.token_hex(16), "mode": "demo" if demo_key else "password"}
    if demo_key:
        payload["demo"] = hashlib.sha256(demo_key.encode("utf-8")).hexdigest()
    body = _encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = hmac.new(_signing_key(user, password, secret), body.encode("ascii"), hashlib.sha256).digest()
    return body + "." + _encode(signature)


def verify_token(token: str, user: str, password: str, *, secret: str = "",
                 demo_keys=(), now: int | None = None) -> dict | None:
    if not isinstance(token, str) or not token or len(token) > 4096:
        return None
    try:
        body, signature = token.split(".")
        expected = _encode(hmac.new(_signing_key(user, password, secret), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        current = int(time.time()) if now is None else int(now)
        if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("sub") != user:
            return None
        issued, expires = payload.get("iat"), payload.get("exp")
        if type(issued) is not int or type(expires) is not int:
            return None
        if not (issued <= current < expires) or expires - issued != LOGIN_SECONDS:
            return None
        if payload.get("mode") == "demo":
            allowed = [hashlib.sha256(key.encode("utf-8")).hexdigest() for key in demo_keys]
            if payload.get("demo") not in allowed:
                return None
        elif payload.get("mode") != "password":
            return None
        return payload
    except (ValueError, TypeError, UnicodeError):
        return None


_COOKIE_JS = """
export default function(component) {
    const {data, parentElement, setStateValue} = component;
    if (parentElement.loginRequest === data.id) return;
    parentElement.loginRequest = data.id;
    let value = '', ok = true;
    try {
        const read = () => {
            const item = document.cookie.split(';').map(s => s.trim())
                .find(s => s.startsWith(data.name + '='));
            return item ? decodeURIComponent(item.slice(data.name.length + 1)) : '';
        };
        const attributes = '; Path=' + data.path + '; SameSite=Lax' +
            (location.protocol === 'https:' ? '; Secure' : '');
        if (data.action === 'write') {
            document.cookie = data.name + '=' + encodeURIComponent(data.token) +
                '; Max-Age=' + data.max_age + attributes;
            ok = read() === data.token;
        } else if (data.action === 'delete') {
            document.cookie = data.name + '=; Max-Age=0' + attributes;
            ok = read() === '';
        }
        value = read();
    } catch (_) { ok = false; }
    setStateValue('sync', {id: data.id, value, ok});
}
"""

def _request(action: str, token: str = "") -> None:
    st.session_state["_login_cookie_request"] = {
        "id": secrets.token_hex(12), "action": action, "token": token,
    }


def _accept(token: str, payload: dict) -> None:
    st.session_state["authenticated"] = True
    st.session_state["demo_mode"] = payload["mode"] == "demo"
    st.session_state["_login_token"] = token


def start_login(user: str, password: str, *, secret: str = "", demo_key: str = "") -> None:
    token = issue_token(user, password, secret=secret, demo_key=demo_key)
    _accept(token, {"mode": "demo" if demo_key else "password"})
    st.session_state["_login_cookie_checked"] = True
    _request("write", token)


def logout() -> None:
    st.session_state["authenticated"] = False
    st.session_state["demo_mode"] = False
    st.session_state.pop("_login_token", None)
    st.session_state["_login_cookie_checked"] = True
    _request("delete")
    # A demo URL must not immediately authenticate again after logout.
    st.query_params.pop("key", None)


def restore_login(user: str, password: str, *, secret: str = "", demo_keys=()) -> bool:
    state = st.session_state
    token = state.get("_login_token")
    if token and not verify_token(token, user, password, secret=secret, demo_keys=demo_keys):
        logout()
    if "_login_cookie_request" not in state:
        _request("read")
    request = state["_login_cookie_request"]
    base_path = str(st.get_option("server.baseUrlPath") or "").strip("/")
    path = "/" + base_path + ("/" if base_path else "")
    # JS has access to the app document in v2; no iframe or new dependency is needed.
    # Register against the current runtime (AppTest can create several runtimes
    # in one process). Re-registering an identical definition is idempotent.
    cookie_bridge = components.component("ai_race_login_cookie", js=_COOKIE_JS)
    result = cookie_bridge(
        key="login_cookie_bridge", height=0,
        data={**request, "name": COOKIE_NAME, "path": path, "max_age": LOGIN_SECONDS},
        on_sync_change=lambda: None,
    )
    response = result.sync
    matched = isinstance(response, dict) and response.get("id") == request["id"]
    if matched and not response.get("ok") and request["action"] == "write":
        st.warning("浏览器未能保存登录状态，请允许此网站使用 Cookie，以便刷新后保持登录。")
    if state.get("authenticated", False):
        return True
    # Write/delete acknowledgments must never restore stale browser state.
    if request["action"] != "read":
        return False
    if not state.get("_login_cookie_checked"):
        cookie = st.context.cookies.get(COOKIE_NAME, "")
        if not cookie and matched:
            cookie = response.get("value", "")
        if cookie or matched:
            state["_login_cookie_checked"] = True
            payload = verify_token(cookie, user, password, secret=secret, demo_keys=demo_keys)
            if payload:
                _accept(cookie, payload)
                return True
            if cookie:
                _request("delete")
                st.rerun()
    return False

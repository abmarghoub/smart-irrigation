"""
Authentification Supabase (JWT) + creation utilisateur (service role).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

import jwt


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def auth_enabled() -> bool:
    if _env("AUTH_DISABLED", "").lower() in ("1", "true", "yes"):
        return False
    return bool(_env("SUPABASE_URL") and (_env("SUPABASE_JWT_SECRET") or _env("SUPABASE_ANON_KEY")))


def supabase_url() -> str:
    return _env("SUPABASE_URL").rstrip("/")


def verify_access_token(token: str) -> dict[str, Any] | None:
    """Retourne {id, email, ...} ou None."""
    if not token:
        return None
    secret = _env("SUPABASE_JWT_SECRET")
    if secret:
        try:
            payload = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience="authenticated",
                options={"verify_aud": True},
            )
            sub = payload.get("sub")
            email = payload.get("email") or ""
            if sub:
                return {"id": sub, "email": email, "role": payload.get("role", "")}
        except jwt.PyJWTError:
            return None
    # Fallback : API Supabase /auth/v1/user
    url = supabase_url()
    anon = _env("SUPABASE_ANON_KEY")
    if not url or not anon:
        return None
    req = urllib.request.Request(
        f"{url}/auth/v1/user",
        headers={
            "Authorization": f"Bearer {token}",
            "apikey": anon,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("id"):
                return {
                    "id": data["id"],
                    "email": data.get("email") or "",
                    "role": "",
                }
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None
    return None


def register_supabase_user(email: str, password: str) -> dict[str, Any]:
    """Cree un utilisateur via service role (inscription device)."""
    url = supabase_url()
    service = _env("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not service:
        raise RuntimeError("SUPABASE_URL et SUPABASE_SERVICE_ROLE_KEY requis pour l'inscription.")
    body = json.dumps({"email": email, "password": password, "email_confirm": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/auth/v1/admin/users",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {service}",
            "apikey": service,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(err_body)
        except json.JSONDecodeError:
            detail = {"message": err_body}
        msg = detail.get("msg") or detail.get("message") or str(detail)
        if "already" in msg.lower() or e.code == 422:
            # Recuperer id via liste admin (email)
            return _lookup_user_by_email(email) or {"error": msg}
        raise RuntimeError(msg) from e


def _lookup_user_by_email(email: str) -> dict[str, Any] | None:
    url = supabase_url()
    service = _env("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not service:
        return None
    req = urllib.request.Request(
        f"{url}/auth/v1/admin/users",
        headers={
            "Authorization": f"Bearer {service}",
            "apikey": service,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            users = data.get("users") if isinstance(data, dict) else data
            if not isinstance(users, list):
                return None
            email_l = email.lower().strip()
            for u in users:
                if (u.get("email") or "").lower() == email_l:
                    return u
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return None

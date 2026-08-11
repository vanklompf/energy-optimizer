from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from energy_optimizer.web import auth


OIDC_ENV = {
    "OIDC_ENABLED": "true",
    "OIDC_CLIENT_ID": "pv",
    "OIDC_CLIENT_SECRET": "client-secret",
    "OIDC_ISSUER_URL": "https://idp.example/application/o/pv/",
    "APP_URL": "https://pv.example",
    "SESSION_SECRET": "a" * 32,
}


def test_oidc_requires_persistent_session_secret() -> None:
    env = {**OIDC_ENV}
    env.pop("SESSION_SECRET")
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(RuntimeError, match="SESSION_SECRET is required"):
            auth.session_secret()


def test_oidc_rejects_short_session_secret() -> None:
    with patch.dict(os.environ, {**OIDC_ENV, "SESSION_SECRET": "guessable"}, clear=True):
        with pytest.raises(RuntimeError, match="at least 32 bytes"):
            auth.session_secret()


def test_current_user_requires_nonempty_subject() -> None:
    for user in ({}, {"sub": None}, {"sub": "  "}):
        request = SimpleNamespace(session={auth.SESSION_USER_KEY: user})
        assert auth.current_user(request) is None

    user = {"sub": "user-123"}
    request = SimpleNamespace(session={auth.SESSION_USER_KEY: user})
    assert auth.current_user(request) == user


def test_oidc_requires_https_app_url() -> None:
    with patch.dict(
        os.environ, {**OIDC_ENV, "APP_URL": "http://pv.example"}, clear=True
    ):
        with pytest.raises(RuntimeError, match="canonical HTTPS URL"):
            auth.build_oauth()


def test_openid_scope_cannot_be_removed() -> None:
    registry = SimpleNamespace()
    registry.register = lambda **kwargs: setattr(registry, "registration", kwargs)
    with (
        patch.dict(os.environ, {**OIDC_ENV, "OIDC_SCOPES": "email profile"}, clear=True),
        patch.object(auth, "OAuth", return_value=registry),
    ):
        assert auth.build_oauth() is registry

    scopes = registry.registration["client_kwargs"]["scope"].split()
    assert "openid" in scopes


@pytest.mark.asyncio
async def test_callback_requires_id_token() -> None:
    class Client:
        async def authorize_access_token(self, request):
            return {"userinfo": {"sub": "user-123"}}

    request = SimpleNamespace(session={"oidc_next": "/"})
    response = await auth.callback(request, SimpleNamespace(oidc=Client()))
    assert response.status_code == 400
    assert auth.SESSION_USER_KEY not in request.session


@pytest.mark.asyncio
async def test_callback_requires_validated_subject() -> None:
    class Client:
        async def authorize_access_token(self, request):
            return {"id_token": "encoded-token", "userinfo": {}}

    request = SimpleNamespace(session={"oidc_next": "/"})
    response = await auth.callback(request, SimpleNamespace(oidc=Client()))
    assert response.status_code == 400
    assert auth.SESSION_USER_KEY not in request.session


@pytest.mark.asyncio
async def test_callback_stores_validated_identity() -> None:
    class Client:
        async def authorize_access_token(self, request):
            return {
                "id_token": "encoded-token",
                "userinfo": {"sub": "user-123", "email": "user@example.com"},
            }

    request = SimpleNamespace(session={"oidc_next": "/"})
    response = await auth.callback(request, SimpleNamespace(oidc=Client()))
    assert response.status_code == 302
    assert request.session[auth.SESSION_USER_KEY]["sub"] == "user-123"


def test_healthz_is_public() -> None:
    assert auth.is_public_path("/healthz")
    assert auth.is_public_path("/auth/login")
    assert not auth.is_public_path("/api/status")

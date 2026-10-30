import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.drive import DriveClient
from app.errors import DriveError
from app.oauth import OAuthService
from app.token_store import TokenStore

BASE_ENV = {
    "BOT_TOKEN": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "GOOGLE_CLIENT_ID": "client-id.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET": "client-secret",
    "WEBHOOK_BASE_URL": "https://bot.example.com",
    "WEBHOOK_SECRET": "secret",
    "ALLOWED_USER_IDS": "42",
}


def settings(**overrides):
    return Settings.from_env({**BASE_ENV, **overrides})


def drive(settings_obj, handler=None, store=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler or (lambda r: httpx.Response(200))))
    return DriveClient(settings_obj, http, store), http


def test_settings_no_exige_google_refresh_token():
    s = settings(GOOGLE_REFRESH_TOKEN="")
    assert s.google_refresh_token is None
    assert s.google_redirect_uri == "https://bot.example.com/oauth/callback"


def test_settings_exige_bot_token():
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        Settings.from_env({})


def test_settings_valida_chat_de_guardado():
    with pytest.raises(RuntimeError, match="OAUTH_STORE_CHAT_ID"):
        settings(OAUTH_STORE_CHAT_ID="no-es-un-numero")


def test_token_store_archivo_y_cifrado(tmp_path):
    a = TokenStore(tmp_path / "token.json", bot_token="bot", client_id="id", client_secret="secret")
    assert a.load() is None
    a.save("refresh-123")
    assert a.load() == "refresh-123"
    assert (tmp_path / "token.json").stat().st_mode & 0o777 == 0o600

    text = a.encode_for_chat("refresh-123")
    assert a.decode_from_chat(text) == "refresh-123"
    assert a.decode_from_chat("otra cosa") is None

    b = TokenStore(tmp_path / "token.json", bot_token="otro", client_id="id", client_secret="secret")
    assert b.decode_from_chat(text) is None

    a.clear()
    assert a.load() is None


def test_state_firmado_y_caducidad(monkeypatch):
    s = settings()
    d, http = drive(s)
    service = OAuthService(s, http, d)
    state = service.make_state(42)
    assert service.verify_state(state)["uid"] == 42
    assert service.verify_state(state + "x") is None
    assert "state=" in service.start_url(42)

    import app.oauth as oauth

    real_time = oauth.time.time
    monkeypatch.setattr(oauth.time, "time", lambda: real_time() + 10_000)
    assert service.verify_state(state) is None


def test_authorization_url_incluye_offline_y_scope():
    s = settings()
    d, http = drive(s)
    service = OAuthService(s, http, d)
    url = service.authorization_url(service.make_state(42))
    assert "accounts.google.com/o/oauth2/v2/auth" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "drive.file" in url
    assert "redirect_uri=https%3A%2F%2Fbot.example.com%2Foauth%2Fcallback" in url


def test_exchange_guarda_el_refresh_token(tmp_path):
    s = settings()
    store = TokenStore(tmp_path / "token.json", bot_token=s.bot_token)
    seen = {}

    def handler(req):
        seen.update(dict(httpx.QueryParams(req.content.decode())))
        return httpx.Response(200, json={"refresh_token": "nuevo-token", "access_token": "a"})

    d, http = drive(s, handler, store)
    service = OAuthService(s, http, d)
    result = asyncio.run(service.exchange("code-1", service.make_state(42)))
    assert result.refresh_token == "nuevo-token"
    assert result.user_id == 42
    assert seen["grant_type"] == "authorization_code"
    assert seen["redirect_uri"] == "https://bot.example.com/oauth/callback"
    assert store.load() == "nuevo-token"
    assert d.has_auth


def test_exchange_rechaza_state_invalido_y_errores_de_google(tmp_path):
    s = settings()
    d, http = drive(s, lambda r: httpx.Response(400, text="invalid_grant"))
    service = OAuthService(s, http, d)
    with pytest.raises(DriveError, match="caducó"):
        asyncio.run(service.exchange("code", "state-falso"))
    with pytest.raises(DriveError, match="rechazó"):
        asyncio.run(service.exchange("code", service.make_state(42)))


def test_drive_usa_el_token_guardado_y_lo_borra_si_google_lo_revoca(tmp_path):
    s = settings(GOOGLE_REFRESH_TOKEN="token-del-entorno")
    store = TokenStore(tmp_path / "token.json", bot_token=s.bot_token)
    store.save("token-del-navegador")
    sent = {}

    def handler(req):
        sent.update(dict(httpx.QueryParams(req.content.decode())))
        return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})

    d, http = drive(s, handler, store)
    assert asyncio.run(d._access_token()) == "access"
    assert sent["refresh_token"] == "token-del-navegador"

    def revoked(req):
        return httpx.Response(400, text="invalid_grant")

    d2, http2 = drive(s, revoked, store)
    with pytest.raises(DriveError, match="/conectar"):
        asyncio.run(d2._access_token())
    assert not d2.has_auth
    assert store.load() is None


def test_drive_sin_token_pide_conectar(tmp_path):
    s = settings()
    d, _ = drive(s, store=TokenStore(tmp_path / "token.json", bot_token=s.bot_token))
    with pytest.raises(DriveError, match="/conectar"):
        asyncio.run(d._access_token())

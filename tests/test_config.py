"""Configuración: validación de variables y normalización del secret del webhook."""
import re

from app.config import Settings, normalize_webhook_secret

_ENV_OK = {
    "BOT_TOKEN": "123456:ABCdefGHIjklMNOpqrSTUvwxYZ123456",
    "TELEGRAM_API_ID": "123",
    "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
    "TELEGRAM_SESSION": "1" * 60,
}


def test_secret_valido_de_se_igual():
    secret = "abc_DEF-123"
    assert normalize_webhook_secret(secret) == secret


def test_secret_con_caracteres_ilegales_se_deriva_por_hash():
    # Así genera Render: valor aleatorio con símbolos que Telegram rechaza
    raw = "abc+DEF/xyz=?.987"
    derived = normalize_webhook_secret(raw)
    assert re.fullmatch(r"[0-9a-f]{64}", derived)
    assert derived == normalize_webhook_secret(raw)  # determinista
    assert derived != raw


def test_secret_demasiado_largo_con_caracteres_ilegales_tambien_se_deriva():
    raw = "x" * 257  # Telegram admite máximo 256
    assert re.fullmatch(r"[0-9a-f]{64}", normalize_webhook_secret(raw))


def test_from_env_normaliza_el_secret():
    env = {**_ENV_OK, "WEBHOOK_SECRET": "valor+ilegal=para/telegram"}
    s = Settings.from_env(env)
    assert s.webhook_secret is not None
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,256}", s.webhook_secret)


def test_from_env_sin_secret_sigue_en_none():
    s = Settings.from_env(dict(_ENV_OK))
    assert s.webhook_secret is None
    assert s.webhook_base_url is None

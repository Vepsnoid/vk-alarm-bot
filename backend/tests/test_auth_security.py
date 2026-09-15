# -*- coding: utf-8 -*-
"""Регрессия по безопасности: JWT после смены пароля, пароли, маскировка секретов.

* ``token_version_ok`` отзывает токены, выпущенные до смены пароля (иначе старый
  JWT живёт все 24 часа);
* ``verify_password`` работает с bcrypt-хешами и с plaintext только для
  ``ADMIN_PASSWORD`` из .env (такие записи перехешируются на старте);
* пароли длиннее лимита bcrypt (72 байта) больше не усекаются молча;
* секреты в настройках отдаются только замаской, а фильтр логирования вырезает
  ``access_token``/``Bearer`` из любых записей логов.

Запуск из каталога backend:  python tests/test_auth_security.py
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.redaction import (  # noqa: E402
    RedactingFilter,
    is_masked_secret,
    mask_secret,
    redact_sensitive_data,
)
from app.core.security import (  # noqa: E402
    create_access_token,
    decode_token,
    get_password_hash,
    is_env_fallback_token,
    is_password_hash,
    password_byte_error,
    prune_ttl_cache,
    secret_fingerprint,
    token_version_ok,
    verify_password,
)


def test_password_hashes_and_legacy_plaintext():
    """bcrypt-хеш проверяется, plaintext остаётся только для .env-пароля."""
    hashed = get_password_hash("Секрет-123")
    assert is_password_hash(hashed), hashed[:7]
    assert verify_password("Секрет-123", hashed) is True
    assert verify_password("Секрет-124", hashed) is False

    # Значение без bcrypt: сравнение с ADMIN_PASSWORD из .env / старыми записями.
    assert is_password_hash("простой") is False
    assert verify_password("простой", "простой") is True
    assert verify_password("другой", "простой") is False
    assert verify_password("кириллица", "кириллица") is True

    assert verify_password("что угодно", "") is False


def test_long_passwords_are_not_truncated_silently():
    """Больше 72 байт — явная ошибка, а не молчаливое усечение."""
    assert password_byte_error("a" * 72) is None
    assert password_byte_error("a" * 73) is not None
    assert password_byte_error("я" * 36) is None          # 36 символов = 72 байта
    assert password_byte_error("я" * 37) is not None      # 74 байта
    assert password_byte_error("") is None
    # Хеширование всё равно не падает на длинном пароле (bcrypt 5.x бросает
    # ValueError без ручного усечения).
    long_password = "a" * 200
    hashed = get_password_hash(long_password)
    assert verify_password(long_password, hashed) is True


def test_token_version_revokes_old_tokens():
    """Смена пароля (token_version) делает ранее выпущенный JWT недействительным."""
    token = create_access_token({"sub": "admin", "tv": 1})
    assert token_version_ok(decode_token(token), 1) is True
    # пароль сменили → версия в БД другая
    assert token_version_ok(decode_token(token), 2) is False
    # токен, выпущенный до появления версии, не принимается
    assert token_version_ok({"sub": "admin"}, 1) is False
    assert token_version_ok({"sub": "admin", "tv": "мусор"}, 1) is False
    assert token_version_ok({"sub": "admin", "tv": 2}, 2) is True
    # у старой записи users без значения версии считаем её равной 1
    assert token_version_ok({"sub": "admin", "tv": 1}, None) is True


def test_secrets_are_masked():
    """Настройки отдают маску, а не сам секрет."""
    assert mask_secret("1234567890") == "••••7890"
    assert mask_secret("short") == "••••"
    assert mask_secret("") is None
    assert mask_secret(None) is None
    assert is_masked_secret("••••7890") is True
    assert is_masked_secret("") is False
    assert is_masked_secret(None) is False
    assert is_masked_secret("1234567890") is False


def test_log_records_are_redacted():
    """Фильтр логирования вырезает токены из сообщения и аргументов записи."""
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1,
        "HTTP Request: GET https://api.vk.com/method/wall.get?access_token=SECRET123&v=5.199",
        None, None,
    )
    assert RedactingFilter().filter(record) is True
    assert "SECRET123" not in record.getMessage(), record.getMessage()

    record = logging.LogRecord("app.x", logging.WARNING, __file__, 1, "url=%s", ("https://a/b?access_token=SECRET123",), None)
    RedactingFilter().filter(record)
    assert "SECRET123" not in record.getMessage(), record.getMessage()

    record = logging.LogRecord("app.x", logging.ERROR, __file__, 1, "headers=%s", ({"Authorization": "Bearer SECRET123"},), None)
    RedactingFilter().filter(record)
    assert "SECRET123" not in record.getMessage(), record.getMessage()

    assert "[REDACTED]" in redact_sensitive_data("access_token=SECRET123")


def test_env_fallback_token_is_recognised():
    """Recovery-ветка принимает только токены, выпущенные ей самой (tv = 0)."""
    assert is_env_fallback_token({"sub": "admin", "tv": 0}) is True
    # Токен без claim (до появления версий) не должен оживать в recovery-ветке.
    assert is_env_fallback_token({"sub": "admin"}) is False
    assert is_env_fallback_token({"sub": "admin", "tv": 1}) is False
    assert is_env_fallback_token({"sub": "admin", "tv": "мусор"}) is False


def test_cache_keys_do_not_keep_secrets():
    """Кэши токенов не должны хранить сам токен ни в ключе, ни в значении."""
    token = "vk1.a.SUPERSECRET-TOKEN"
    fingerprint = secret_fingerprint(token)
    assert token not in fingerprint
    assert len(fingerprint) == 32, len(fingerprint)
    assert secret_fingerprint(token) == fingerprint
    assert secret_fingerprint("другой-токен") != fingerprint
    assert secret_fingerprint("") != fingerprint


def test_ttl_cache_is_bounded():
    """TTL сам по себе записи не удаляет — кэш подрезается при записи."""
    cache = {}
    for index in range(20):
        cache[secret_fingerprint(f"token-{index}")] = (time.monotonic() - index, index)
        prune_ttl_cache(cache, ttl=60.0, max_entries=4)
    assert len(cache) <= 4, len(cache)
    # Остаются самые свежие записи.
    assert secret_fingerprint("token-0") in cache, sorted(cache)

    # Просроченные записи уходят, даже если порог не превышен.
    cache = {
        secret_fingerprint("old"): (time.monotonic() - 120, 1),
        secret_fingerprint("new"): (time.monotonic(), 2),
    }
    prune_ttl_cache(cache, ttl=60.0, max_entries=8)
    assert len(cache) == 2, len(cache)          # порог не превышен — не трогаем
    for index in range(10):
        cache[secret_fingerprint(f"t{index}")] = (time.monotonic(), index)
    prune_ttl_cache(cache, ttl=60.0, max_entries=8)
    assert secret_fingerprint("old") not in cache
    assert len(cache) <= 8, len(cache)


_TESTS = [
    ("пароли: bcrypt и legacy plaintext", test_password_hashes_and_legacy_plaintext),
    ("длинные пароли: явная ошибка", test_long_passwords_are_not_truncated_silently),
    ("token_version отзывает старые JWT", test_token_version_revokes_old_tokens),
    ("секреты маскируются", test_secrets_are_masked),
    ("логи редактируются", test_log_records_are_redacted),
    ("recovery-токен распознаётся по tv=0", test_env_fallback_token_is_recognised),
    ("ключи кэшей не содержат секретов", test_cache_keys_do_not_keep_secrets),
    ("кэш TTL не растёт бесконечно", test_ttl_cache_is_bounded),
]

if __name__ == "__main__":
    failures = 0
    for name, fn in _TESTS:
        try:
            fn()
            print(f"OK    {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    total = len(_TESTS)
    print(f"{total - failures}/{total} тестов пройдено")
    sys.exit(1 if failures else 0)

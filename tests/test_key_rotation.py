"""후속2 — Fernet 키 회전: 구 키 병행 복호화 + rotate-secrets 재암호화."""
from cryptography.fernet import Fernet, InvalidToken
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app import security

ADMIN = {"x-api-key": "test-admin-key"}

OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()


def _set_keys(monkeypatch, primary: str, old: str = ""):
    monkeypatch.setenv("PAAS_FERNET_KEY", primary)
    monkeypatch.setenv("PAAS_FERNET_KEYS_OLD", old)
    get_settings.cache_clear()
    monkeypatch.setattr(security, "_fernet", None)


def test_old_key_still_decrypts_after_rotation_config(monkeypatch, fresh_settings):
    _set_keys(monkeypatch, OLD_KEY)
    token = security.encrypt_value("secret-v1")

    # 새 키로 교체 + 구 키를 old 목록으로 → 기존 암호문 복호화 가능
    _set_keys(monkeypatch, NEW_KEY, old=OLD_KEY)
    assert security.decrypt_value(token) == "secret-v1"
    # 새 암호화는 새 키로만 복호화됨
    new_token = security.encrypt_value("secret-v2")
    assert Fernet(NEW_KEY.encode()).decrypt(new_token.encode()) == b"secret-v2"
    monkeypatch.setattr(security, "_fernet", None)


def test_rotate_secrets_endpoint_reencrypts_all(monkeypatch, fresh_settings):
    # 1) 구 키 시절에 EnvVar·프로바이더·모듈 시크릿 저장
    _set_keys(monkeypatch, OLD_KEY)
    c = TestClient(create_app())
    pid = c.post("/paas/api/v1/projects", json={
        "name": "rot-app", "type": "python", "git_url": "https://git.example.com/x",
    }, headers=ADMIN).json()["id"]
    c.put(f"/paas/api/v1/projects/{pid}/env", json={"key": "TOKEN", "value": "old-secret"}, headers=ADMIN)
    c.post("/paas/api/v1/llm/providers", json={
        "name": "p1", "kind": "external", "base_url": "https://x", "api_key": "pk", "model": "m",
    }, headers=ADMIN)
    c.post("/paas/api/v1/modules", json={
        "name": "m1", "type": "external_api", "config": {"url": "https://y", "api_key": "mk"},
    }, headers=ADMIN)

    # 2) 키 교체(구 키는 old로) 후 재암호화 실행
    _set_keys(monkeypatch, NEW_KEY, old=OLD_KEY)
    r = c.post("/paas/api/v1/admin/rotate-secrets", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["rotated"] == 3  # env 1 + provider 1 + module 1

    # 3) 구 키를 완전히 제거해도(새 키 단독) 복호화 가능해야 회전 완료
    _set_keys(monkeypatch, NEW_KEY)
    from app.db import SessionLocal
    from app.models import EnvVar

    with SessionLocal() as db:
        row = db.query(EnvVar).filter_by(key="TOKEN").one()
        assert security.decrypt_value(row.value_encrypted) == "old-secret"
        # 구 키 단독으로는 더 이상 풀 수 없음 (재암호화 확인)
        try:
            Fernet(OLD_KEY.encode()).decrypt(row.value_encrypted.encode())
            raise AssertionError("old key should not decrypt rotated token")
        except InvalidToken:
            pass
    monkeypatch.setattr(security, "_fernet", None)


def test_rotate_requires_admin(monkeypatch, fresh_settings):
    _set_keys(monkeypatch, NEW_KEY)
    c = TestClient(create_app())
    member = c.post("/paas/api/v1/keys", json={"name": "m"}, headers=ADMIN).json()["key"]
    assert c.post("/paas/api/v1/admin/rotate-secrets", headers={"x-api-key": member}).status_code == 403
    monkeypatch.setattr(security, "_fernet", None)

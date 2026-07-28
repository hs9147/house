"""파일 저장소 창구 — 목록·업로드·다운로드·삭제와 경로 탈출 차단."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import storage

ADMIN = {"x-api-key": "test-admin-key"}


def _client(monkeypatch, tmp_path) -> TestClient:
    from app.config import get_settings

    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    return TestClient(create_app())


def _storage_module(c: TestClient, name="assets") -> None:
    r = c.post("/paas/api/v1/modules", json={
        "name": name, "type": "file_storage", "config": {"sub_folder": name},
    }, headers=ADMIN)
    assert r.status_code == 201, r.text


def test_upload_list_download_delete(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    _storage_module(c)

    r = c.post("/paas/api/v1/storage/assets/files",
               files={"file": ("logo.png", b"binary-bytes")}, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["path"] == "logo.png"

    listing = c.get("/paas/api/v1/storage/assets/files", headers=ADMIN).json()
    assert listing["files"] == [{"path": "logo.png", "size": 12}]
    # 목록에 실제 디렉터리가 아니라 창구 URL이 실린다
    assert listing["url"].endswith("/paas/api/v1/storage/assets")
    assert str(tmp_path) not in str(listing)

    dl = c.get("/paas/api/v1/storage/assets/files/content?path=logo.png", headers=ADMIN)
    assert dl.status_code == 200
    assert dl.content == b"binary-bytes"

    # 사내망 전제로 별도 자격증명은 요구하지 않되, 꺼내 간 주체는 감사 로그에 남는다
    audit = c.get("/paas/api/v1/audit", headers=ADMIN).json()
    actions = {(row["action"], row["actor"]) for row in audit}
    assert ("storage.upload", "bootstrap-admin") in actions
    assert ("storage.download", "bootstrap-admin") in actions

    assert c.delete("/paas/api/v1/storage/assets/files?path=logo.png",
                    headers=ADMIN).status_code == 204
    assert c.get("/paas/api/v1/storage/assets/files", headers=ADMIN).json()["files"] == []


def test_upload_accepts_nested_path(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    _storage_module(c)
    r = c.post("/paas/api/v1/storage/assets/files",
               files={"file": ("x.txt", b"hi")}, data={"path": "img/icons/x.txt"}, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["path"] == "img/icons/x.txt"
    assert (tmp_path / "assets" / "img" / "icons" / "x.txt").read_bytes() == b"hi"


@pytest.mark.parametrize("bad", ["../escape.txt", "img/../../escape.txt", "/etc/passwd"])
def test_path_traversal_is_blocked(monkeypatch, tmp_path, fresh_settings, bad):
    c = _client(monkeypatch, tmp_path)
    _storage_module(c)
    r = c.post("/paas/api/v1/storage/assets/files",
               files={"file": ("x", b"pwn")}, data={"path": bad}, headers=ADMIN)
    assert r.status_code == 400
    assert not (tmp_path / "escape.txt").exists()
    assert c.get(f"/paas/api/v1/storage/assets/files/content?path={bad}",
                 headers=ADMIN).status_code in (400, 404)


def test_non_storage_module_rejected(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    c.post("/paas/api/v1/modules", json={
        "name": "mail", "type": "external_api", "config": {"url": "https://x"},
    }, headers=ADMIN)
    assert c.get("/paas/api/v1/storage/mail/files", headers=ADMIN).status_code == 400
    assert c.get("/paas/api/v1/storage/nope/files", headers=ADMIN).status_code == 404


def test_root_falls_back_to_storage_root_when_endpoint_is_a_url(monkeypatch, tmp_path, fresh_settings):
    """로컬 경로만 저장소 루트가 될 수 있다 — 레거시 URL endpoint는 무시한다."""
    from app.config import get_settings
    from app.models import Module, ModuleType
    from app.services import modules as svc

    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    m = Module(name="legacy", type=ModuleType.file_storage,
               config=svc.encrypt_config({"endpoint": "http://seaweed:8333", "bucket": "assets"}))
    assert storage.root_for(m) == (tmp_path / "assets").resolve()

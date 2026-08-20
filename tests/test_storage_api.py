"""파일 저장소 창구 — 저장소 목록·업로드·다운로드·삭제와 경로 탈출 차단.

저장소는 모듈로 등록하지 않는다. PAAS_STORAGE_ROOT가 내부 저장소 하나(쓰기 가능),
PAAS_DOC_ROOTS가 사내 문서 폴더(읽기 전용)를 정한다.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import storage

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def _client(monkeypatch, tmp_path, doc_roots="") -> TestClient:
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_ROOTS", doc_roots)
    get_settings.cache_clear()
    return TestClient(create_app())


def test_upload_list_download_delete(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)

    r = c.post(f"{API}/storage/internal/files",
               files={"file": ("logo.png", b"binary-bytes")}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["path"] == "logo.png"

    listing = c.get(f"{API}/storage/internal/files", headers=ADMIN).json()
    assert listing["files"] == [{"path": "logo.png", "size": 12}]
    # 파일 목록에는 실제 디렉터리가 아니라 창구 URL이 실린다(경로는 /storage/stores에서만)
    assert listing["url"].endswith("/paas/api/v1/storage/internal")
    assert str(tmp_path) not in str(listing)

    dl = c.get(f"{API}/storage/internal/files/content?path=logo.png", headers=ADMIN)
    assert dl.status_code == 200
    assert dl.content == b"binary-bytes"

    # 사내망 전제로 별도 자격증명은 요구하지 않되, 꺼내 간 주체는 감사 로그에 남는다
    audit = c.get(f"{API}/audit", headers=ADMIN).json()
    actions = {(row["action"], row["actor"]) for row in audit}
    assert ("storage.upload", "bootstrap-admin") in actions
    assert ("storage.download", "bootstrap-admin") in actions

    assert c.delete(f"{API}/storage/internal/files?path=logo.png",
                    headers=ADMIN).status_code == 204
    assert c.get(f"{API}/storage/internal/files", headers=ADMIN).json()["files"] == []


def test_upload_accepts_nested_path(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    r = c.post(f"{API}/storage/internal/files",
               files={"file": ("x.txt", b"hi")}, data={"path": "img/icons/x.txt"}, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["path"] == "img/icons/x.txt"
    assert (tmp_path / "internal" / "img" / "icons" / "x.txt").read_bytes() == b"hi"


@pytest.mark.parametrize("bad", ["../escape.txt", "img/../../escape.txt", "/etc/passwd"])
def test_path_traversal_is_blocked(monkeypatch, tmp_path, fresh_settings, bad):
    c = _client(monkeypatch, tmp_path)
    r = c.post(f"{API}/storage/internal/files",
               files={"file": ("x", b"pwn")}, data={"path": bad}, headers=ADMIN)
    assert r.status_code == 400
    assert not (tmp_path / "escape.txt").exists()
    assert c.get(f"{API}/storage/internal/files/content?path={bad}",
                 headers=ADMIN).status_code in (400, 404)


def test_unknown_store_is_404(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    assert c.get(f"{API}/storage/nope/files", headers=ADMIN).status_code == 404


def test_stores_listing_shows_what_the_env_vars_opened(monkeypatch, tmp_path, fresh_settings):
    """환경변수로 정하는 값이라 되비춰 주지 않으면 운영자가 확인할 방법이 없다."""
    docs = tmp_path / "사내문서"
    docs.mkdir()
    c = _client(monkeypatch, tmp_path, doc_roots=f"rules={docs},{tmp_path / 'Missing Folder'}")

    rows = {s["name"]: s for s in c.get(f"{API}/storage/stores", headers=ADMIN).json()}
    assert list(rows) == ["internal", "rules", "missing-folder"]
    assert rows["internal"]["read_only"] is False
    assert rows["rules"] == {
        "name": "rules", "root": str(docs), "read_only": True, "exists": True,
        "url": rows["rules"]["url"],
    }
    # 없는 경로도 목록에서 빼지 않는다 — 빠지면 "설정이 안 먹었다"와 구분이 안 된다
    assert rows["missing-folder"]["exists"] is False


def test_doc_roots_are_read_only(monkeypatch, tmp_path, fresh_settings):
    """읽으러 붙인 폴더다 — 콘솔에서 실수로 지워지는 일까지 막힌다."""
    docs = tmp_path / "shared"
    docs.mkdir()
    (docs / "규정.txt").write_text("연차 규정", encoding="utf-8")
    c = _client(monkeypatch, tmp_path, doc_roots=str(docs))

    assert c.get(f"{API}/storage/shared/files", headers=ADMIN).json()["files"] == [
        {"path": "규정.txt", "size": len("연차 규정".encode())}]
    assert c.post(f"{API}/storage/shared/files",
                  files={"file": ("x.txt", b"hi")}, headers=ADMIN).status_code == 403
    assert c.delete(f"{API}/storage/shared/files?path=규정.txt",
                    headers=ADMIN).status_code == 403
    assert (docs / "규정.txt").exists()


def test_broken_doc_roots_say_which_entry_is_wrong(monkeypatch, tmp_path, fresh_settings):
    """조용히 빼면 '그 폴더에 문서가 없다'와 구분되지 않는다 — 항목을 그대로 보여 준다."""
    c = _client(monkeypatch, tmp_path, doc_roots="=/some/path")
    r = c.get(f"{API}/storage/stores", headers=ADMIN)
    assert r.status_code == 500
    assert "'=/some/path'" in r.json()["detail"]


def test_internal_store_name_cannot_be_reused(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("PAAS_DOC_ROOTS", f"internal={tmp_path}")
    get_settings.cache_clear()
    with pytest.raises(storage.StorageError, match="internal"):
        storage.stores()


def test_bare_windows_path_takes_its_last_folder_as_the_name(monkeypatch, fresh_settings):
    """운영 서버가 윈도우다 — 역슬래시 경로에서도 이름이 나와야 한다."""
    monkeypatch.setenv("PAAS_DOC_ROOTS", r"D:\shared\Company Docs")
    get_settings.cache_clear()
    assert [s.name for s in storage.stores()] == ["internal", "company-docs"]


def test_a_korean_folder_asks_for_an_explicit_name(monkeypatch, fresh_settings):
    """이름은 URL 조각이자 모듈 이름이 된다 — 만들 수 없으면 쓰라고 말한다.

    (조용히 넘기면 목록에서 사라지고, 운영자는 "설정이 안 먹었다"고만 보게 된다.)"""
    monkeypatch.setenv("PAAS_DOC_ROOTS", r"D:\공유\사내규정")
    get_settings.cache_clear()
    with pytest.raises(storage.StorageError, match="이름=경로"):
        storage.stores()

    monkeypatch.setenv("PAAS_DOC_ROOTS", r"rules=D:\공유\사내규정")
    get_settings.cache_clear()
    assert [s.name for s in storage.stores()] == ["internal", "rules"]

"""zip/폴더 업로드로 프로젝트 등록 — POST /projects/upload E2E(실제 로컬 git push 사용)."""
import io
import subprocess
import zipfile

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import gitea

ADMIN = {"x-api-key": "test-admin-key"}


def _client_with_org(monkeypatch, tmp_path):
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "workspaces"))
    get_settings.cache_clear()

    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    clone_url = f"file://{bare}"

    monkeypatch.setattr(
        gitea.httpx, "post",
        lambda url, **kw: type(
            "R", (), {"status_code": 201, "text": "",
                      "json": lambda self=None: {"clone_url": clone_url}}
        )(),
    )
    c = TestClient(create_app())
    org_id = c.post("/paas/api/v1/orgs", json={"name": "shop-team"}, headers=ADMIN).json()["id"]
    return c, org_id, bare


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_upload_zip_creates_project_and_pushes(monkeypatch, fresh_settings, tmp_path):
    c, org_id, bare = _client_with_org(monkeypatch, tmp_path)
    data = _zip_bytes({"app.py": b"print('hi')\n", "README.md": b"hi"})

    r = c.post(
        "/paas/api/v1/projects/upload",
        data={"name": "up-app", "type": "python", "organization_id": str(org_id)},
        files={"zip_file": ("app.zip", data, "application/zip")},
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "up-app"
    assert body["organization_id"] == org_id

    clone = tmp_path / "verify-clone"
    subprocess.run(["git", "clone", "-q", "--branch", "main", str(bare), str(clone)], check=True)
    assert (clone / "app.py").read_text() == "print('hi')\n"


def test_upload_folder_creates_project(monkeypatch, fresh_settings, tmp_path):
    c, org_id, bare = _client_with_org(monkeypatch, tmp_path)

    r = c.post(
        "/paas/api/v1/projects/upload",
        data={"name": "up-folder", "type": "node", "organization_id": str(org_id)},
        files=[
            ("files", ("src/index.js", b"console.log(1)\n", "text/javascript")),
            ("files", ("package.json", b"{}", "application/json")),
        ],
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text

    clone = tmp_path / "verify-clone2"
    subprocess.run(["git", "clone", "-q", "--branch", "main", str(bare), str(clone)], check=True)
    assert (clone / "src" / "index.js").read_text() == "console.log(1)\n"


def test_upload_requires_exactly_one_source(monkeypatch, fresh_settings, tmp_path):
    c, org_id, _ = _client_with_org(monkeypatch, tmp_path)
    r = c.post(
        "/paas/api/v1/projects/upload",
        data={"name": "up-none", "type": "python", "organization_id": str(org_id)},
        headers=ADMIN,
    )
    assert r.status_code == 422


def test_upload_rejects_zip_slip(monkeypatch, fresh_settings, tmp_path):
    c, org_id, _ = _client_with_org(monkeypatch, tmp_path)
    data = _zip_bytes({"../evil.txt": b"x"})
    r = c.post(
        "/paas/api/v1/projects/upload",
        data={"name": "up-evil", "type": "python", "organization_id": str(org_id)},
        files={"zip_file": ("evil.zip", data, "application/zip")},
        headers=ADMIN,
    )
    assert r.status_code == 422


def test_upload_duplicate_name_rejected(monkeypatch, fresh_settings, tmp_path):
    c, org_id, bare = _client_with_org(monkeypatch, tmp_path)
    data = _zip_bytes({"a.py": b"1"})
    r1 = c.post(
        "/paas/api/v1/projects/upload",
        data={"name": "dup-app", "type": "python", "organization_id": str(org_id)},
        files={"zip_file": ("a.zip", data, "application/zip")},
        headers=ADMIN,
    )
    assert r1.status_code == 201, r1.text
    r2 = c.post(
        "/paas/api/v1/projects/upload",
        data={"name": "dup-app", "type": "python", "organization_id": str(org_id)},
        files={"zip_file": ("a.zip", data, "application/zip")},
        headers=ADMIN,
    )
    assert r2.status_code == 409

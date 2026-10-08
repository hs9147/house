"""DB 저장소 이관, 다중 부서 권한, 온톨로지와 .ready 분리."""
import json
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import docready, docsearch


API = "/paas/api/v1"
ADMIN = {"x-api-key": "test-admin-key"}


def _member(client: TestClient, email: str, org_ids: list[int]) -> dict:
    assert client.post(f"{API}/auth/register", json={
        "email": email, "name": email, "password": "pw12345",
    }).status_code == 201
    account_id = next(a["id"] for a in client.get(f"{API}/auth/accounts", headers=ADMIN).json()
                      if a["email"] == email)
    client.post(f"{API}/auth/accounts/{account_id}/approve", headers=ADMIN)
    for org_id in org_ids:
        client.post(f"{API}/auth/accounts/{account_id}/organizations/modify",
                    json={"organization_id": org_id, "action": "add"}, headers=ADMIN)
    token = client.post(f"{API}/auth/login", json={
        "email": email, "password": "pw12345",
    }).json()["key"]
    return {"x-api-key": token}


def test_migration_membership_and_ontology(monkeypatch, tmp_path, fresh_settings):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "rule.md").write_text("# 인사 규정\n## 연차\n연차 규정입니다.\n", encoding="utf-8")
    (second / "rule.md").write_text("# 재무 규정\n## 정산\n정산 규정입니다.\n", encoding="utf-8")
    monkeypatch.setenv("PAAS_DOC_ROOTS", f"first={first}")
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("PAAS_DOC_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "")
    get_settings.cache_clear()
    from app.db import Base, SessionLocal, engine
    from app.models import DocumentStore, DocumentStoreRegistry
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.query(DocumentStore).delete()
        db.query(DocumentStoreRegistry).delete()
        db.commit()
    client = TestClient(create_app())

    # 레거시 경로는 이름을 보존해 DB로 한 번만 들어오고, 미배정 동안 관리자 전용이다.
    stores = client.get(f"{API}/storage/stores", headers=ADMIN).json()
    assert [(s["name"], s["organization_id"]) for s in stores] == [("first", None)]
    from app.models import Organization
    with SessionLocal() as db:
        db.add_all([Organization(name="hr"), Organization(name="finance")])
        db.commit()
        orgs = {o.name: o.id for o in db.query(Organization).all()}

    hr = _member(client, "hr@example.test", [orgs["hr"]])
    assert client.get(f"{API}/storage/stores", headers=hr).json() == []
    assert client.get(f"{API}/storage/first/files", headers=hr).status_code == 404
    assert client.post(f"{API}/storage/stores", json={
        "name": "outside", "root": str(tmp_path.parent),
        "organization_id": orgs["hr"], "read_only": False,
    }, headers=ADMIN).status_code == 400

    assert client.patch(f"{API}/storage/stores/first", json={
        "organization_id": orgs["hr"], "read_only": False,
    }, headers=ADMIN).status_code == 200
    assert client.post(f"{API}/storage/stores", json={
        "name": "second", "root": str(second), "organization_id": orgs["finance"],
        "read_only": True,
    }, headers=ADMIN).status_code == 201
    docsearch.reindex("first", first)
    docsearch.reindex("second", second)

    both = _member(client, "both@example.test", list(orgs.values()))
    assert [s["name"] for s in client.get(f"{API}/storage/stores", headers=hr).json()] == ["first"]
    assert client.get(f"{API}/storage/second/files", headers=hr).status_code == 404
    assert client.get(f"{API}/ontology/stores/second/graph", headers=hr).status_code == 404
    assert [s["store"] for s in client.get(f"{API}/ontology/overview", headers=hr).json()["stores"]] == ["first"]
    assert [s["name"] for s in client.get(f"{API}/storage/stores", headers=both).json()] == ["first", "second"]
    assert {s["store"] for s in client.get(f"{API}/ontology/overview", headers=both).json()["stores"]} == {"first", "second"}
    assert client.get(f"{API}/storage/second/files", headers=both).status_code == 200
    assert client.get(f"{API}/storage/stores", headers=both).json()[0]["root"] == ""
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "list_sources", "arguments": {}}}
    result = client.post(f"{API}/mcp/docs", json=rpc, headers=hr).json()
    names = [s["source"] for s in json.loads(result["result"]["content"][0]["text"])["sources"]]
    assert names == ["first"]
    assert client.post(f"{API}/mcp/storage/second", json=rpc, headers=hr).status_code == 404
    graph = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "find_nodes", "arguments": {"q": "규정"}}}
    result = client.post(f"{API}/mcp/graph", json=graph, headers=hr).json()
    assert {n["source"] for n in json.loads(result["result"]["content"][0]["text"])} == {"first"}


def test_ready_cache_rejects_other_origin(tmp_path, monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    get_settings.cache_clear()
    a = tmp_path / "a" / "same.md"
    b = tmp_path / "b" / "same.md"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_text("# 원본 A", encoding="utf-8")
    b.write_text("# 원본 B", encoding="utf-8")
    import os
    os.utime(b, ns=(a.stat().st_atime_ns, a.stat().st_mtime_ns))
    assert docready.read("stable", "same.md", a) == "# 원본 A"
    assert docready.read("stable", "same.md", b) == "# 원본 B"

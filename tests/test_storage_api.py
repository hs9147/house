"""파일 저장소 창구 — 저장소 목록·업로드·다운로드·삭제와 경로 탈출 차단.

저장소는 모듈로 등록하지 않는다. 목록에 나오는 것은 PAAS_DOC_ROOTS가 정한 사내 문서
폴더들이고 기본이 읽기/쓰기다(PAAS_DOC_ROOTS_READONLY에 적은 것만 잠긴다).
PAAS_STORAGE_ROOT가 정하는 플랫폼 저장소 `internal`은 목록에서 숨기되 이름으로는 닿는다.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import docsearch, storage

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def _client(monkeypatch, tmp_path, doc_roots="", readonly="") -> TestClient:
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    # 업로드·삭제가 색인을 바로 갱신하므로 색인 자리도 tmp_path 안으로 옮긴다
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("PAAS_DOC_ROOTS", doc_roots)
    monkeypatch.setenv("PAAS_DOC_ROOTS_READONLY", readonly)
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
    # 플랫폼 저장소(internal)는 사람이 고를 폴더가 아니라 목록에 없다
    assert list(rows) == ["rules", "missing-folder"]
    assert rows["rules"] == {
        "name": "rules", "root": str(docs), "read_only": False, "exists": True,
        "url": rows["rules"]["url"],
    }
    # 없는 경로도 목록에서 빼지 않는다 — 빠지면 "설정이 안 먹었다"와 구분이 안 된다
    assert rows["missing-folder"]["exists"] is False


def test_doc_roots_are_read_write_by_default(monkeypatch, tmp_path, fresh_settings):
    """기본은 읽기/쓰기다 — 잠글 폴더만 목록으로 따로 막는다."""
    docs = tmp_path / "shared"
    docs.mkdir()
    (docs / "규정.txt").write_text("연차 규정", encoding="utf-8")
    c = _client(monkeypatch, tmp_path, doc_roots=str(docs))

    assert c.get(f"{API}/storage/shared/files", headers=ADMIN).json()["files"] == [
        {"path": "규정.txt", "size": len("연차 규정".encode())}]
    assert c.post(f"{API}/storage/shared/files",
                  files={"file": ("x.txt", b"hi")}, headers=ADMIN).status_code == 201
    assert c.delete(f"{API}/storage/shared/files?path=규정.txt",
                    headers=ADMIN).status_code == 204
    # 지운 것은 사라지지 않고 폴더 안 휴지통으로 간다
    assert not (docs / "규정.txt").exists()
    assert (docs / storage.TRASH_DIRNAME / "규정.txt").read_text(encoding="utf-8") == "연차 규정"


def test_internal_is_hidden_from_the_list_but_still_reachable(monkeypatch, tmp_path,
                                                              fresh_settings):
    """숨긴다는 것은 "고를 목록에 없다"이지 "닿지 않는다"가 아니다.

    막아 버리면 예전에 파일 관리로 여기 올린 파일을 꺼낼 방법이 없어진다 — 숨긴 것이
    아니라 잃은 것이 된다.
    """
    docs = tmp_path / "shared"
    docs.mkdir()
    c = _client(monkeypatch, tmp_path, doc_roots=str(docs))

    listed = [s["name"] for s in c.get(f"{API}/storage/stores", headers=ADMIN).json()]
    assert "internal" not in listed

    assert c.post(f"{API}/storage/internal/files",
                  files={"file": ("예전.txt", "내용".encode())},
                  headers=ADMIN).status_code == 201
    assert c.get(f"{API}/storage/internal/files",
                 headers=ADMIN).json()["files"] == [{"path": "예전.txt", "size": 6}]


def test_delete_moves_to_trash_and_can_be_recovered(monkeypatch, tmp_path, fresh_settings):
    """삭제는 되돌릴 수 있어야 한다.

    사내 공유 폴더에는 되돌리기가 없다 — 서비스 계정이 SMB로 지우면 윈도우 휴지통에
    가지 않고 그대로 사라진다. 그런데 이 경로는 사람뿐 아니라 LLM도 부른다(MCP의
    delete_file). 한 번의 잘못된 호출이 복구 불가능하면 안 된다.
    """
    c = _client(monkeypatch, tmp_path)
    root = tmp_path / "internal"
    c.post(f"{API}/storage/internal/files",
           files={"file": ("보고서.txt", "중요한 내용".encode())}, headers=ADMIN)

    assert c.delete(f"{API}/storage/internal/files?path=보고서.txt",
                    headers=ADMIN).status_code == 204

    # 원래 자리에서는 사라지고 목록에도 안 나온다
    assert not (root / "보고서.txt").exists()
    assert c.get(f"{API}/storage/internal/files", headers=ADMIN).json()["files"] == []

    # 그러나 내용은 살아 있다 — 사람이 그 폴더에서 되돌릴 수 있다
    grave = root / storage.TRASH_DIRNAME / "보고서.txt"
    assert grave.read_text(encoding="utf-8") == "중요한 내용"


def test_deleting_the_same_name_twice_keeps_both(monkeypatch, tmp_path, fresh_settings):
    """먼저 지운 것을 덮어쓰면 되돌릴 것이 하나 사라진다."""
    c = _client(monkeypatch, tmp_path)
    root = tmp_path / "internal"
    for body in ("첫 번째", "두 번째"):
        c.post(f"{API}/storage/internal/files",
               files={"file": ("메모.txt", body.encode())}, headers=ADMIN)
        assert c.delete(f"{API}/storage/internal/files?path=메모.txt",
                        headers=ADMIN).status_code == 204

    trashed = sorted(p.read_text(encoding="utf-8")
                     for p in (root / storage.TRASH_DIRNAME).iterdir())
    assert trashed == ["두 번째", "첫 번째"]


def test_a_doc_root_can_be_locked(monkeypatch, tmp_path, fresh_settings):
    """폴더별 opt-out — 목록에 적은 폴더만 잠긴다."""
    open_dir, locked_dir = tmp_path / "scratch", tmp_path / "contract"
    open_dir.mkdir()
    locked_dir.mkdir()
    (locked_dir / "계약.txt").write_text("원본", encoding="utf-8")
    c = _client(monkeypatch, tmp_path,
                doc_roots=f"scratch={open_dir},contract={locked_dir}", readonly="contract")

    rows = {s["name"]: s for s in c.get(f"{API}/storage/stores", headers=ADMIN).json()}
    assert rows["scratch"]["read_only"] is False
    assert rows["contract"]["read_only"] is True   # 적은 폴더만 잠긴다

    assert c.post(f"{API}/storage/scratch/files",
                  files={"file": ("메모.txt", "내용".encode())},
                  headers=ADMIN).status_code == 201
    assert (open_dir / "메모.txt").read_text(encoding="utf-8") == "내용"
    assert c.delete(f"{API}/storage/scratch/files?path=메모.txt",
                    headers=ADMIN).status_code == 204

    # 잠근 폴더는 막힌다
    assert c.post(f"{API}/storage/contract/files",
                  files={"file": ("x.txt", b"hi")}, headers=ADMIN).status_code == 403
    assert (locked_dir / "계약.txt").read_text(encoding="utf-8") == "원본"


def test_readonly_name_that_is_not_a_doc_root_is_rejected(monkeypatch, tmp_path, fresh_settings):
    """오타를 조용히 넘기면 잠근 줄 알았던 폴더가 열린 채로 남는다.

    잠금이 안 먹는 것과 설정이 틀린 것은 밖에서 구분되지 않는다 — 그래서 막는다.
    """
    docs = tmp_path / "scratch"
    docs.mkdir()
    c = _client(monkeypatch, tmp_path, doc_roots=f"scratch={docs}", readonly="scrach")
    r = c.get(f"{API}/storage/stores", headers=ADMIN)
    assert r.status_code == 500
    assert "scrach" in r.json()["detail"]
    assert "scratch" in r.json()["detail"]  # 있는 이름을 함께 보여 준다


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


def test_quoted_windows_paths_are_accepted(monkeypatch, fresh_settings):
    """윈도우 경로는 따옴표로 감싸 적는 습관이 있다 — 벗기지 않으면 따옴표가 경로의
    일부가 되어 없는 폴더를 가리키고, 목록에는 exists: false로만 나와서 "폴더가 비었다"와
    구분되지 않는다. 문서에서 복사하면 붙는 굽은 따옴표도 같이 벗긴다."""
    for raw in (r'contract="D:\1.계약품의"',
                'contract=\u201cD:\\1.계약품의\u201d',
                r'"contract=D:\1.계약품의"',
                r" contract = 'D:\1.계약품의' "):
        monkeypatch.setenv("PAAS_DOC_ROOTS", raw)
        get_settings.cache_clear()
        found = [s for s in storage.stores() if s.name != "internal"]
        assert [s.name for s in found] == ["contract"], raw
        assert str(found[0].root).endswith("D:\\1.계약품의"), raw


def test_spaces_inside_a_path_survive(monkeypatch, tmp_path, fresh_settings):
    """공유 폴더 이름에 공백은 흔하다("cost db", "2026 1분기").

    항목·이름·경로의 **양끝** 공백만 다듬고 안쪽은 건드리지 않는다 — `=` 옆을 띄워
    적어도 되고, 폴더 이름의 공백은 그대로 남아야 한다.
    """
    docs = tmp_path / "cost db" / "2026 1분기"
    docs.mkdir(parents=True)
    (docs / "원가표.txt").write_text("재료비 12500", encoding="utf-8")

    for raw in (f"costdb={tmp_path / 'cost db'}",
                f" costdb = {tmp_path / 'cost db'} ",
                f'costdb="{tmp_path / "cost db"}"'):
        monkeypatch.setenv("PAAS_DOC_ROOTS", raw)
        get_settings.cache_clear()
        found = [s for s in storage.stores() if s.name != "internal"]
        assert [s.name for s in found] == ["costdb"], raw
        assert found[0].root == tmp_path / "cost db", raw
        # 공백이 든 하위 경로까지 그대로 열린다
        assert [f["path"] for f in storage.list_files(found[0].root)] == [
            "2026 1분기/원가표.txt"], raw


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


# --- 업로드·삭제가 색인을 바로 갱신한다 ---

def test_upload_is_searchable_immediately(monkeypatch, tmp_path, fresh_settings):
    """예전에는 주기 색인(15분)을 기다려야 방금 올린 문서가 검색에 잡혔다."""
    docs = tmp_path / "규정"
    docs.mkdir()
    c = _client(monkeypatch, tmp_path, doc_roots=f"rules={docs}")

    assert c.post(f"{API}/storage/rules/files",
                  files={"file": ("반출절차.md", "# 반출절차\n\n승인 후 3일 내".encode())},
                  headers=ADMIN).status_code == 201

    hits = docsearch.search("rules", "승인")["hits"]
    assert [h["path"] for h in hits] == ["반출절차.md"]
    # 온톨로지도 같이 선다 — /mcp/graph가 방금 올린 문서를 안다
    assert docsearch.find_nodes("rules", kind="document", q="반출절차")


def test_delete_stops_being_findable_immediately(monkeypatch, tmp_path, fresh_settings):
    """지운 쪽이 더 나쁘다: 검색에는 남고 읽으러 가면 '파일이 없습니다'가 났다."""
    docs = tmp_path / "규정"
    docs.mkdir()
    (docs / "폐기.md").write_text("# 폐기\n\n반출 대장 폐기", encoding="utf-8")
    c = _client(monkeypatch, tmp_path, doc_roots=f"rules={docs}")
    docsearch.reindex("rules", docs)
    assert docsearch.search("rules", "폐기")["hits"]

    assert c.delete(f"{API}/storage/rules/files?path=폐기.md",
                    headers=ADMIN).status_code == 204
    assert docsearch.search("rules", "폐기")["hits"] == []
    assert docsearch.find_nodes("rules", kind="document", q="폐기") == []


def test_upload_succeeds_even_if_indexing_fails(monkeypatch, tmp_path, fresh_settings):
    """파일은 이미 디스크에 있다 — 색인이 터졌다고 500을 주면 사용자는 다시 올린다.
    (색인은 파생 데이터고 주기 작업이 어차피 맞춘다.)"""
    docs = tmp_path / "규정"
    docs.mkdir()
    c = _client(monkeypatch, tmp_path, doc_roots=f"rules={docs}")

    def _boom(*args, **kwargs):
        raise OSError("색인 디스크가 가득 찼습니다")

    monkeypatch.setattr(docsearch, "index_one", _boom)
    r = c.post(f"{API}/storage/rules/files",
               files={"file": ("메모.md", "# 메모\n".encode())}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert (docs / "메모.md").read_text(encoding="utf-8") == "# 메모\n"

"""돌고 있는 커밋 읽기 — git 명령 없이 .git만 보고.

이 값이 틀리면 "SW 업데이트가 먹었나"를 다시 라우트 유무로 되짚게 된다. 실제 저장소
모양(느슨한 ref / packed-refs / detached / 워크트리 / .git 없음)을 각각 세워 확인한다.
"""
import pytest

from app.services import buildinfo

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _clear_cache():
    """head()는 기동 시 한 번 읽도록 캐시된다 — 케이스마다 비운다."""
    buildinfo.head.cache_clear()
    yield
    buildinfo.head.cache_clear()


def _repo(tmp_path, head_text, *, loose=None, packed=None):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(head_text, encoding="utf-8")
    if loose:
        for ref, sha in loose.items():
            path = git / ref
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sha + "\n", encoding="utf-8")
    if packed:
        (git / "packed-refs").write_text(packed, encoding="utf-8")
    return tmp_path


def test_loose_ref(tmp_path):
    root = _repo(tmp_path, "ref: refs/heads/main\n",
                 loose={"refs/heads/main": SHA})
    assert buildinfo.head(root) == (SHA[:buildinfo.SHORT_LEN], "main")


def test_packed_refs_when_loose_ref_is_absent(tmp_path):
    """clone·gc 직후에는 느슨한 ref가 없다 — 여기만 안 보면 갓 복제한 서버가 빈 값이 된다."""
    root = _repo(tmp_path, "ref: refs/heads/main\n",
                 packed=f"# pack-refs with: peeled fully-peeled sorted \n"
                        f"{SHA} refs/heads/main\n"
                        f"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa refs/remotes/origin/main\n")
    assert buildinfo.head(root) == (SHA[:buildinfo.SHORT_LEN], "main")


def test_packed_refs_peeled_line_is_not_mistaken_for_a_ref(tmp_path):
    """'^'로 시작하는 줄은 태그가 가리키는 커밋이지 ref가 아니다."""
    root = _repo(tmp_path, "ref: refs/heads/main\n",
                 packed=f"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb refs/tags/v1\n"
                        f"^cccccccccccccccccccccccccccccccccccccccc\n"
                        f"{SHA} refs/heads/main\n")
    assert buildinfo.head(root)[0] == SHA[:buildinfo.SHORT_LEN]


def test_branch_name_with_slashes_is_kept_whole(tmp_path):
    """feature/x·claude/y처럼 슬래시가 든 이름을 자르면 대조용 값이 실제와 달라진다."""
    root = _repo(tmp_path, "ref: refs/heads/claude/agent-planning\n",
                 loose={"refs/heads/claude/agent-planning": SHA})
    assert buildinfo.head(root) == (SHA[:buildinfo.SHORT_LEN], "claude/agent-planning")


def test_detached_head(tmp_path):
    """태그나 커밋으로 직접 체크아웃한 설치본 — 브랜치는 없지만 리비전은 답해야 한다."""
    root = _repo(tmp_path, SHA + "\n")
    assert buildinfo.head(root) == (SHA[:buildinfo.SHORT_LEN], "")


def test_gitdir_file_is_followed(tmp_path):
    """워크트리·서브모듈에서는 .git이 디렉터리가 아니라 경로를 담은 파일이다."""
    real = tmp_path / "real"
    (real / "refs" / "heads").mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/deploy\n", encoding="utf-8")
    (real / "refs" / "heads" / "deploy").write_text(SHA + "\n", encoding="utf-8")

    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")

    assert buildinfo.head(work) == (SHA[:buildinfo.SHORT_LEN], "deploy")


def test_value_is_snapshotted_at_first_read(tmp_path):
    """pull만 하고 재시작하지 않았을 때 **디스크가 아니라 돌고 있는 커밋**을 답해야 한다.

    여기가 뒤집히면 이 기능은 정확히 없애려던 착각을 만든다 — 헬스체크는 새 sha를
    말하는데 메모리에는 옛 코드가 떠 있는 상태.
    """
    root = _repo(tmp_path, "ref: refs/heads/main\n", loose={"refs/heads/main": SHA})
    assert buildinfo.head(root)[0] == SHA[:buildinfo.SHORT_LEN]

    newer = "f" * 40
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text(newer, encoding="utf-8")
    assert buildinfo.head(root)[0] == SHA[:buildinfo.SHORT_LEN], "디스크를 다시 읽으면 안 된다"


def test_missing_git_is_not_an_error(tmp_path):
    """tarball로 푼 설치본에는 .git이 없다 — 헬스체크가 이것 때문에 죽으면 안 된다."""
    assert buildinfo.head(tmp_path) == ("", "")


def test_unresolvable_ref_returns_empty(tmp_path):
    """HEAD는 있는데 가리키는 ref가 없는 깨진 상태에서도 조용히 빈 값."""
    root = _repo(tmp_path, "ref: refs/heads/missing\n")
    assert buildinfo.head(root) == ("", "missing")


def test_health_reports_running_revision():
    """헬스체크가 실제로 이 값을 싣는지 — 여기가 끊기면 서버에 물어볼 수단이 사라진다."""
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).get("/paas/health").json()
    assert "revision" in body and "branch" in body
    # 이 저장소에서 돌리므로 실제 커밋이 나와야 한다(빈 값이면 읽기가 깨진 것이다)
    assert len(body["revision"]) == buildinfo.SHORT_LEN

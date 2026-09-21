"""커밋 → 작업 지시 연결 — 리포를 보고 진행 현황을 판정한다(결정론, LLM 없음)."""
import subprocess

from app.services import taskmatch, workspace


def test_reference_forms_that_people_actually_write():
    """규약을 한 형태로 못 박으면 지켰는데도 안 잡히는 일이 생긴다."""
    assert taskmatch.refs_in_message("feat: 로그인 (task #3)") == {3}
    assert taskmatch.refs_in_message("Task#12 정리") == {12}
    assert taskmatch.refs_in_message("[TASK # 7] 수정") == {7}
    # 본문·trailer에 적어도 잡힌다(제목만 읽지 않는 이유)
    assert taskmatch.refs_in_message("fix: 정리\n\n상세 설명\n\nTask #21") == {21}
    # 한 커밋이 여러 작업을 끝낼 수 있다
    assert taskmatch.refs_in_message("task #1, task #2 함께") == {1, 2}


def test_unrelated_numbers_are_not_task_references():
    """`#3`만으로 잡으면 PR 번호·이슈 번호가 작업으로 오인된다."""
    assert taskmatch.refs_in_message("Merge pull request #42") == set()
    assert taskmatch.refs_in_message("fix #7") == set()
    assert taskmatch.refs_in_message("") == set()


def test_first_commit_wins_for_a_task():
    """처음 반영된 커밋이 그 작업이 들어온 시점이다 — 나중 수정 커밋이 아니다.

    log_messages는 최신 순으로 주므로, 목록 뒤쪽(더 오래된 것)이 남아야 한다.
    """
    entries = [
        ("newsha", "refactor: 정리 (task #5)"),   # 최신
        ("oldsha", "feat: 처음 구현 (task #5)"),  # 더 오래됨
    ]
    assert taskmatch.commit_by_task(entries) == {5: "oldsha"}


def test_commits_without_references_are_not_guessed():
    """커밋 메시지나 변경 파일로 작업을 짐작하면 엉뚱한 작업이 완료로 바뀐다."""
    entries = [("sha1", "chore: 의존성 올림"), ("sha2", "wip")]
    assert taskmatch.commit_by_task(entries) == {}


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=repo, check=True, capture_output=True)


def test_log_messages_reads_shas_and_full_messages(tmp_path):
    """메시지에 개행이 있어도 잘리지 않아야 한다 — 본문의 참조를 놓치면 판정이 빈다."""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("1", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: 첫 구현\n\n상세 설명 줄\n\nTask #4")
    (repo / "a.txt").write_text("2", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: 정리")

    entries = workspace.log_messages(repo, "main")
    assert len(entries) == 2
    # 최신 순
    assert entries[0][1].startswith("chore: 정리")
    assert "Task #4" in entries[1][1]
    assert taskmatch.commit_by_task(entries) == {4: entries[1][0]}


def test_log_messages_on_a_bad_ref_is_empty_not_an_error(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    assert workspace.log_messages(repo, "nope") == []

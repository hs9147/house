"""돌고 있는 코드가 어느 커밋인지 — 서버가 스스로 말하게 한다.

**왜 필요한가.** "SW 업데이트가 먹었나"를 지금까지 라우트가 있나 없나로 되짚고 있었다.
404 하나를 두고 코드가 옛날 것인지, 프록시가 막은 것인지, 경로를 잘못 친 것인지 매번
가려야 했다. 리비전을 헬스체크가 그대로 말해 주면 그 질문이 사라진다.

**왜 git 명령을 부르지 않나.** 서비스 계정의 PATH에 git이 없을 수 있고(nssm 서비스는
로그인 셸의 PATH를 물려받지 않는다), 헬스체크마다 프로세스를 띄우는 것도 곤란하다.
.git을 직접 읽으면 둘 다 없다.

**언제 읽는가 — 기동할 때 한 번.** 이 값은 "체크아웃된 커밋"이 아니라 **"이 프로세스가
적재한 커밋"**이어야 한다. pull만 하고 재시작하지 않으면 디스크는 새 커밋인데 돌고 있는
코드는 옛것이고, 그때 새 sha를 답하면 정확히 우리가 없애려는 그 착각을 만든다. 그래서
create_app에서 한 번 읽어 캐시에 박아 둔다(app/main.py).
"""
from __future__ import annotations

import functools
from pathlib import Path

# 사람이 눈으로 대조하기에 충분하고, 사내 저장소 규모에서 충돌하지 않는 길이.
SHORT_LEN = 12


def _git_dir(root: Path) -> Path | None:
    """저장소의 .git 디렉터리. 워크트리·서브모듈이면 파일 안의 경로를 따라간다."""
    dot = root / ".git"
    if dot.is_dir():
        return dot
    try:
        line = dot.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    target = Path(line.split(":", 1)[1].strip())
    if not target.is_absolute():
        target = (root / target).resolve()
    return target if target.is_dir() else None


def _resolve_ref(git_dir: Path, ref: str) -> str:
    """refs/heads/... → sha. 느슨한 ref가 없으면 packed-refs를 본다.

    packed-refs를 함께 보는 이유: `git gc`나 clone 직후에는 느슨한 파일이 없고 전부
    packed-refs에 들어 있다 — 거기만 안 보면 갓 복제한 서버에서 빈 값이 나온다.
    """
    try:
        return (git_dir / ref).read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        packed = (git_dir / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in packed.splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref:
            return sha.strip()
    return ""


@functools.lru_cache(maxsize=4)
def head(root: Path) -> tuple[str, str]:
    """(리비전, 브랜치). 읽을 수 없으면 둘 다 빈 문자열.

    실패해도 예외를 올리지 않는다 — 헬스체크는 이것 때문에 죽으면 안 된다. tarball로
    푼 설치본처럼 .git이 아예 없는 경우도 정상 동작에 속한다.
    """
    git_dir = _git_dir(Path(root))
    if git_dir is None:
        return "", ""
    try:
        head_text = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return "", ""
    if not head_text.startswith("ref:"):
        # detached HEAD — 태그나 특정 커밋으로 직접 체크아웃한 경우
        return head_text[:SHORT_LEN], ""
    ref = head_text.split(":", 1)[1].strip()
    sha = _resolve_ref(git_dir, ref)
    # 앞의 refs/heads/만 떼어낸다. 마지막 조각만 취하면 feature/x·claude/y처럼 슬래시가
    # 든 이름이 잘려서, 대조하려고 만든 값이 실제 브랜치와 달라진다.
    prefix = "refs/heads/"
    branch = ref[len(prefix):] if ref.startswith(prefix) else ref
    return sha[:SHORT_LEN], branch

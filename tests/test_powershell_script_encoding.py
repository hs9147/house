"""한글이 든 .ps1은 UTF-8 BOM으로 저장돼야 한다.

**왜 테스트로 박아 두나.** Windows PowerShell 5.1(Server 2016 기본)은 BOM 없는 .ps1을
UTF-8이 아니라 **ANSI 코드페이지**로 읽는다. 한국어판이면 cp949라, UTF-8로 저장한 한글이
전부 깨져 나온다:

    코드 터미널이 안 열릴 때 ...  →  肄섏넄 �꽣誘몃꼸�씠 �븞 �뿴由� ...

진단 스크립트에서 안내문이 안 읽히면 그 스크립트는 존재 이유가 없어진다. 그런데 이건
리눅스에서 편집·검증할 때는 **전혀 드러나지 않는다** — pwsh 7은 BOM이 없어도 UTF-8로
읽기 때문이다. 서버에 올려 봐야만 알게 되는 종류라 여기서 막는다.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOM = b"\xef\xbb\xbf"


def _scripts():
    return [p for p in sorted(REPO_ROOT.rglob("*.ps1"))
            if "node_modules" not in p.parts and ".venv" not in p.parts]


def _scripts_with_non_ascii():
    found = []
    for path in _scripts():
        raw = path.read_bytes()
        body = raw[len(BOM):] if raw.startswith(BOM) else raw
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            found.append((path, raw))  # UTF-8도 아니면 그것부터 문제다
            continue
        if any(ord(ch) > 127 for ch in text):
            found.append((path, raw))
    return found


def test_glob_actually_finds_scripts():
    """경로 규칙이 깨지면 아래 검사가 **조용히 통과**한다 — 그걸 막는다.

    한글이 든 .ps1이 하나도 없는 것은 정상이다(오히려 권장이다 — 콘솔 코드페이지에
    기대지 않으려면 ASCII가 낫다). 하지만 .ps1을 아예 못 찾는 것은 규칙이 깨진 것이다.
    """
    assert _scripts(), ".ps1을 하나도 못 찾았다 — rglob 경로 규칙을 확인할 것"


@pytest.mark.parametrize("path,raw", _scripts_with_non_ascii(),
                         ids=lambda v: v.name if isinstance(v, Path) else "")
def test_non_ascii_script_starts_with_utf8_bom(path, raw):
    assert raw.startswith(BOM), (
        f"{path.relative_to(REPO_ROOT)}에 UTF-8 BOM이 없습니다. "
        "Windows PowerShell 5.1이 cp949로 읽어 한글이 깨집니다."
    )

"""생성된 start.cmd를 **실제 cmd.exe로 실행해** 본다.

문자열 단정은 배치 파싱을 잡지 못한다. 실측으로 확인된 두 가지가 그 증거다.

1. **인코딩.** cmd는 이 파일을 콘솔 코드페이지로 읽는다(한국어 윈도우는 949). UTF-8 한글이
   섞이면 2바이트 판독이 어긋나 줄 경계가 밀리고, 다음 줄의 REM이 먹혀 주석이 명령으로
   실행된다 — 템플릿은 기동 전에 rc=255로 죽었고 화면에는 nssm의 "SERVICE_PAUSED"만
   남았다(negowith). 그래서 생성되는 배치는 ASCII만 쓴다.
2. **인자 전달.** `npm run dev -- ...` 뒤의 인자가 dev 스크립트까지 닿는지는 npm·cmd·
   블록 중첩이 함께 정한다. 닿지 않으면 dev 서버가 플랫폼이 준 포트가 아닌 자기 기본
   포트(5173)로 떠서 헬스체크만 실패하고, 원인은 어디에도 적히지 않는다.

이 파일의 테스트는 윈도우 + node가 있을 때만 돈다(그 조합이 이 스크립트가 실제로 도는
환경이다). 없으면 건너뛴다 — CI가 리눅스라도 나머지 테스트는 그대로 유효하다.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.build import write_start_script

pytestmark = pytest.mark.skipif(
    os.name != "nt" or shutil.which("node") is None or shutil.which("npm") is None,
    reason="윈도우 + node/npm이 있을 때만 의미가 있다(이 스크립트가 도는 환경)",
)


def _run(script: Path, cwd: Path, **env_extra) -> subprocess.CompletedProcess:
    env = dict(os.environ, PORT="54321", HOST="127.0.0.1",
               PAAS_BASE_PATH="/apps/_/x/dev/", **env_extra)
    # Git Bash가 심는 값 — 켜져 있으면 cmd가 현재 폴더의 배치를 찾지 못한다(테스트 잡음).
    env.pop("NoDefaultCurrentDirectoryInExePath", None)
    return subprocess.run(
        ["cmd", "/c", str(script)], cwd=str(cwd), env=env,
        capture_output=True, text=True, errors="replace", timeout=180,
    )


def test_generated_script_is_ascii_only(tmp_path):
    """한글이 섞이면 cmd의 DBCS 판독이 어긋나 스크립트 자체가 깨진다(위 주석 1)."""
    content = write_start_script(tmp_path).read_text(encoding="utf-8")
    offenders = [ln for ln in content.splitlines() if not ln.isascii()]
    assert not offenders, offenders


def test_development_profile_runs_the_projects_dev_script_with_platform_args(tmp_path):
    """dev 프로필은 프로젝트의 `npm run dev`로 뜨고, 플랫폼 인자가 그대로 닿는다."""
    (tmp_path / "echo-args.js").write_text(
        'console.log("DEV-ARGS:" + process.argv.slice(2).join(" "));', encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"name": "x", "private": true, "scripts": {"dev": "node echo-args.js"}}',
        encoding="utf-8")
    # node_modules가 없으면 스크립트가 npm install을 먼저 돈다(마지막 보루) — 테스트에서
    # 네트워크를 타지 않게 빈 폴더를 둔다.
    (tmp_path / "node_modules").mkdir()
    script = write_start_script(tmp_path)

    done = _run(script, tmp_path, PAAS_PROFILE="development")

    assert done.returncode == 0, (done.stdout[-800:], done.stderr[-400:])
    assert 'running "npm run dev"' in done.stdout
    args = next((ln for ln in done.stdout.splitlines() if "DEV-ARGS:" in ln), "")
    assert "--port 54321" in args, done.stdout[-800:]
    assert "--host 127.0.0.1" in args
    assert "--base /apps/_/x/dev/" in args
    assert "--config paas-preview.config.mjs" in args


def test_unknown_repo_fails_instead_of_serving_files(tmp_path):
    """실행 방법을 모르는 리포는 실패해야 한다 — 정적 서빙하면 소스가 공개된다."""
    done = _run(write_start_script(tmp_path), tmp_path)

    assert done.returncode == 1, (done.stdout[-400:], done.stderr[-400:])
    assert "Cannot tell how to start this repo" in done.stdout
    # 괄호 이스케이프(^(…^))가 맞아야 안내가 읽히는 문장으로 나온다.
    assert "(node/react)" in done.stdout

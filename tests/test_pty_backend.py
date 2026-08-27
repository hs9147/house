"""PTY 백엔드 선택 — Server 2016에는 ConPTY가 없다.

이 판정은 윈도우에서만 의미가 있는데 CI는 리눅스에서 돈다. 그래서 os.name과
sys.getwindowsversion()을 가짜로 세워 판정 자체를 확인한다 — 틀리면 셸이 열리자마자
죽고(WebSocket은 101로 붙는다) 화면에는 "세션이 끝났습니다"만 남아서, 서버까지
들어가 보기 전에는 원인을 알 수 없다.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import pty_terminal


def _windows(build: int):
    """빌드 번호가 build인 윈도우인 척한다."""
    return (patch.object(pty_terminal.os, "name", "nt"),
            patch.object(pty_terminal.sys, "getwindowsversion",
                         lambda: SimpleNamespace(build=build), create=True))


@pytest.mark.parametrize("build,expected", [
    (14393, pty_terminal.BACKENDS["winpty"]),   # Server 2016 — ConPTY 없음
    (17762, pty_terminal.BACKENDS["winpty"]),   # 1809 직전
    (17763, None),                              # 1809 / Server 2019 — ConPTY 있음
    (20348, None),                              # Server 2022
])
def test_default_backend_by_windows_build(build, expected):
    name_patch, ver_patch = _windows(build)
    with name_patch, ver_patch:
        assert pty_terminal.default_backend_code() == expected


def test_default_backend_is_none_off_windows():
    """POSIX는 표준 라이브러리 pty를 쓰므로 고를 백엔드가 없다."""
    with patch.object(pty_terminal.os, "name", "posix"):
        assert pty_terminal.default_backend_code() is None


def test_empty_setting_falls_back_to_os_default():
    """설정이 비어 있으면 자동 판정으로 내려간다 — 이게 끊기면 2016에서 다시 ConPTY다."""
    name_patch, ver_patch = _windows(14393)
    with name_patch, ver_patch:
        assert pty_terminal.backend_code("") == pty_terminal.BACKENDS["winpty"]
        assert pty_terminal.backend_code("   ") == pty_terminal.BACKENDS["winpty"]


def test_explicit_setting_wins_over_os_default():
    """못 박은 값은 그대로 쓴다 — 판정이 틀렸을 때 사람이 덮을 수 있어야 한다."""
    name_patch, ver_patch = _windows(14393)
    with name_patch, ver_patch:
        assert pty_terminal.backend_code("conpty") == pty_terminal.BACKENDS["conpty"]
        assert pty_terminal.backend_code("WinPTY") == pty_terminal.BACKENDS["winpty"]


def test_unknown_backend_name_is_rejected():
    with pytest.raises(pty_terminal.PtyUnavailable):
        pty_terminal.backend_code("conptyy")


def test_unreadable_windows_version_falls_back_to_pywinpty():
    """빌드를 못 읽으면 우리가 고르지 않고 pywinpty에 맡긴다(멋대로 winpty로 내리지 않는다)."""
    def boom():
        raise OSError("no version")

    with patch.object(pty_terminal.os, "name", "nt"), \
            patch.object(pty_terminal.sys, "getwindowsversion", boom, create=True):
        assert pty_terminal.default_backend_code() is None


class TestProbeReasonCodes:
    """probe()의 reason은 **계약**이다 — infra/terminal-doctor.ps1이 이걸로 판정한다.

    error·hint는 브라우저에 띄울 한국어 문장이라 언제든 다듬는다. 그런데 서버에서 도는
    진단 스크립트는 그 문장을 쓸 수 없다: Windows 콘솔 코드페이지에서 한글이 깨져 정작
    가장 중요한 줄을 못 읽는다. 문장을 파싱하게 두면 문구를 손볼 때마다 스크립트가 조용히
    깨지므로, 사유를 값으로 주고 그 값을 여기서 고정한다.
    """

    def test_shell_that_cannot_be_executed(self):
        info = pty_terminal.probe("/nonexistent-shell-xyz", "")
        assert info["ok"] is False
        assert info["reason"] == pty_terminal.REASON_SHELL_EXITED
        # 127 = exec 실패. 스크립트가 이 값으로 "경로를 확인하라"를 가른다.
        assert info["exit_status"] == 127

    def test_shell_that_exits_immediately(self):
        info = pty_terminal.probe("/bin/false", "")
        assert info["reason"] == pty_terminal.REASON_SHELL_EXITED
        assert info["exit_status"] == 1

    def test_unknown_backend_name(self):
        info = pty_terminal.probe("/bin/sh", "nonsense")
        assert info["reason"] == pty_terminal.REASON_BAD_BACKEND
        assert info["exit_status"] is None

    def test_missing_websocket_library(self):
        with patch.object(pty_terminal, "websocket_library", lambda: ""):
            info = pty_terminal.probe("/bin/sh", "")
        assert info["reason"] == pty_terminal.REASON_NO_WS_LIBRARY

    def test_backend_cannot_be_opened(self):
        def boom(*a, **kw):
            raise pty_terminal.PtyUnavailable("no backend")

        with patch.object(pty_terminal, "PtyTerminal", boom):
            info = pty_terminal.probe("/bin/sh", "")
        assert info["reason"] == pty_terminal.REASON_NO_BACKEND

    def test_healthy_shell_has_no_reason(self):
        info = pty_terminal.probe("/bin/sh", "")
        assert info["ok"] is True
        assert info["reason"] == ""
        assert info["exit_status"] is None

"""런타임 공통 — 어느 런타임을 쓰든 같아야 하는 값.

`upstream_host`가 여기 있는 이유: 프록시 설정·포트 확인·화면 표시가 모두 같은 호스트
이름을 써야 한다. 한 군데라도 다르면(특히 Windows에서 localhost는 ::1로, 127.0.0.1은
IPv4로 풀린다) 이미 쓰는 포트가 비어 보이거나, 화면이 설정 파일과 다른 주소를 말한다.
"""


def upstream_host(settings) -> str:
    """프록시가 실제로 쓰는 업스트림 호스트 이름 — 런타임이 정한다.

    windows_service는 localhost로 통일했고(그 모듈의 UPSTREAM_HOST), docker 런타임은
    포트를 127.0.0.1에 명시적으로 바인드하므로 그쪽은 127.0.0.1이 맞다.
    """
    if settings.runtime_backend == "windows_service":
        from .windows_service_runtime import UPSTREAM_HOST  # noqa: PLC0415

        return UPSTREAM_HOST
    return "127.0.0.1"

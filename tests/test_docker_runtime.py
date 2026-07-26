"""docker 런타임 — host_port publish가 127.0.0.1(루프백)로 제한되는지 확인.

바인드 주소를 명시하지 않으면 Docker가 0.0.0.0(모든 인터페이스)에 publish해 리버스
프록시를 거치지 않고 외부에서 host_port로 바로 접근할 수 있게 된다 — 프로젝트별
배포가 항상 프록시의 단일 외부 포트로만 도달 가능해야 한다는 요건을 깨는 것이므로
회귀 방지 테스트로 고정한다.
"""
from app.config import get_settings
from app.models import BuildProfile
from app.services.runtime import docker_runtime as dr
from app.services.runtime.base import RuntimeSpec
from app.services.runtime.docker_runtime import DockerRuntime


def _spec(project_name="shop") -> RuntimeSpec:
    return RuntimeSpec(project_name, "img:latest", 8000, BuildProfile.release, "shop.apps.test")


class _FakeContainer:
    def __init__(self, name):
        self.name = name

    def logs(self, tail=50):
        return b""

    def remove(self, force=True):
        pass


class _FakeContainers:
    def __init__(self):
        self.run_kwargs: list[dict] = []
        self._running: list[_FakeContainer] = []

    def run(self, **kwargs):
        self.run_kwargs.append(kwargs)
        c = _FakeContainer(kwargs["name"])
        self._running.append(c)
        return c

    def list(self, all=True, filters=None):
        name = filters["name"]
        return [c for c in self._running if c.name == name]


class _FakeClient:
    def __init__(self):
        self.containers = _FakeContainers()


def _env(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_PORT_RANGE_START", "9200")
    monkeypatch.setenv("PAAS_PORT_RANGE_END", "9299")
    get_settings.cache_clear()
    fake = _FakeClient()
    monkeypatch.setattr(dr, "_docker_client", lambda: fake)
    monkeypatch.setattr(DockerRuntime, "_wait_healthy", staticmethod(lambda *a, **kw: True))
    return fake


def test_start_publishes_host_port_on_loopback_only(monkeypatch, tmp_path, fresh_settings):
    fake = _env(monkeypatch, tmp_path, fresh_settings)
    endpoint = DockerRuntime().start(_spec())

    assert endpoint.host == "127.0.0.1"
    assert 9200 <= endpoint.port <= 9299
    kwargs = fake.containers.run_kwargs[0]
    assert kwargs["ports"] == {"8000/tcp": ("127.0.0.1", endpoint.port)}


def test_blue_green_switch_also_binds_loopback(monkeypatch, tmp_path, fresh_settings):
    fake = _env(monkeypatch, tmp_path, fresh_settings)
    DockerRuntime().start(_spec())
    DockerRuntime().start(_spec())

    assert len(fake.containers.run_kwargs) == 2
    for kwargs in fake.containers.run_kwargs:
        port = kwargs["ports"]["8000/tcp"][1]
        assert kwargs["ports"] == {"8000/tcp": ("127.0.0.1", port)}

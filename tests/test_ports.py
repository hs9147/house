"""호스트 포트 배정 대장 — 고정 배정, 경쟁, 대장 밖 점유, 사용현황 조회.

런타임이 이 배정을 실제로 쓰는지는 test_docker_runtime.py에서 확인한다."""
import socket

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import BuildProfile, PortAllocation, Project, ProjectType
from app.services import ports

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


@pytest.fixture
def db(monkeypatch, fresh_settings):
    """좁은 범위로 고정해 경계 동작을 그대로 확인한다."""
    monkeypatch.setenv("PAAS_PORT_RANGE_START", "8100")
    monkeypatch.setenv("PAAS_PORT_RANGE_END", "8104")
    get_settings.cache_clear()
    create_app()  # 테이블 생성
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _project(db, name="shop-web") -> Project:
    row = Project(name=name, type=ProjectType.react,
              git_url="https://git.example.com/x", branch="main")
    db.add(row)
    db.commit()
    return row


def _no_probe(monkeypatch):
    """포트 점유 탐지를 끈다 — 대장 동작만 보는 테스트에서 실제 호스트 상태에 흔들리지 않게."""
    monkeypatch.setattr(ports, "is_listening", lambda host, port, timeout=0.2: False)


def test_allocation_is_sticky_per_owner(db, monkeypatch):
    """같은 주인은 몇 번을 물어도 같은 포트 — 멈췄다 켜도 프록시 설정이 그대로다."""
    _no_probe(monkeypatch)
    project = _project(db)
    first = ports.allocate(db, project.id, BuildProfile.release)
    assert first == 8100
    assert ports.allocate(db, project.id, BuildProfile.release) == 8100


def test_profiles_and_components_get_their_own_ports(db, monkeypatch):
    _no_probe(monkeypatch)
    project = _project(db)
    assert ports.allocate(db, project.id, BuildProfile.release) == 8100
    assert ports.allocate(db, project.id, BuildProfile.development) == 8101
    assert ports.allocate(db, project.id, BuildProfile.release, "backend") == 8102
    assert ports.allocate(db, project.id, BuildProfile.release, "frontend") == 8103


def test_allocation_is_not_reused_while_the_owner_holds_it(db, monkeypatch):
    """멈춘 배포(아무도 리슨하지 않는 상태)의 포트를 남에게 넘기지 않는다 — 예전 방식이
    바로 이 자리에서 같은 포트를 다시 내줬다."""
    _no_probe(monkeypatch)
    a, b = _project(db, "a"), _project(db, "b")
    held = ports.allocate(db, a.id, BuildProfile.release)
    assert ports.allocate(db, b.id, BuildProfile.release) != held


def test_ports_in_use_outside_the_registry_are_skipped(db, monkeypatch):
    """플랫폼이 모르는 서비스가 잡고 있는 포트는 건너뛴다(대장이 우선, 탐지는 보조)."""
    monkeypatch.setattr(ports, "is_listening",
                        lambda host, port, timeout=0.2: port in (8100, 8101))
    project = _project(db)
    assert ports.allocate(db, project.id, BuildProfile.release) == 8102


def test_exhausted_range_says_what_to_do(db, monkeypatch):
    _no_probe(monkeypatch)
    for i in range(5):
        ports.allocate(db, _project(db, f"p{i}").id, BuildProfile.release)
    with pytest.raises(ports.PortExhausted, match="PAAS_PORT_RANGE"):
        ports.allocate(db, _project(db, "over").id, BuildProfile.release)


def test_race_falls_through_to_the_next_port(db, monkeypatch):
    """다른 배포가 방금 같은 포트를 가져간 경우 — 삽입이 충돌하면 다음 후보로 넘어간다.
    확인과 사용 사이가 벌어져 있어서 생기는 문제라, 확인을 더 하는 것으로는 못 막는다."""
    _no_probe(monkeypatch)
    other = _project(db, "other")
    project = _project(db, "mine")

    original_add = db.add
    hijacked = {"done": False}

    def add_but_steal_first(obj):
        # 우리가 8100을 담으려는 순간 다른 세션이 먼저 8100을 차지한 상황을 만든다
        if not hijacked["done"] and isinstance(obj, PortAllocation) and obj.port == 8100:
            hijacked["done"] = True
            thief = SessionLocal()
            try:
                thief.add(PortAllocation(port=8100, project_id=other.id,
                                         profile=BuildProfile.release, component=""))
                thief.commit()
            finally:
                thief.close()
        original_add(obj)

    monkeypatch.setattr(db, "add", add_but_steal_first)
    assert ports.allocate(db, project.id, BuildProfile.release) == 8101
    assert hijacked["done"] is True


def test_reallocates_when_the_range_moves(db, monkeypatch):
    """범위를 바꾸면 밖에 남은 배정은 놓아준다 — 방화벽을 범위 기준으로 열어 둔 구성에서
    옛 포트를 계속 쓰면 조용히 막힌다."""
    _no_probe(monkeypatch)
    project = _project(db)
    assert ports.allocate(db, project.id, BuildProfile.release) == 8100

    monkeypatch.setenv("PAAS_PORT_RANGE_START", "8200")
    monkeypatch.setenv("PAAS_PORT_RANGE_END", "8204")
    get_settings.cache_clear()
    assert ports.allocate(db, project.id, BuildProfile.release) == 8200
    assert db.query(PortAllocation).count() == 1


def test_release_project_frees_every_port(db, monkeypatch):
    _no_probe(monkeypatch)
    project = _project(db)
    ports.allocate(db, project.id, BuildProfile.release)
    ports.allocate(db, project.id, BuildProfile.development)
    assert ports.release_project(db, project.id) == 2
    db.commit()
    assert db.query(PortAllocation).count() == 0


def test_usage_reports_registry_and_listening_state(db, monkeypatch):
    monkeypatch.setattr(ports, "is_listening",
                        lambda host, port, timeout=0.2: port == 8100)
    project = _project(db)
    ports.allocate(db, project.id, BuildProfile.release)          # 8100 — 리슨 중이라 건너뛰고
    ports.allocate(db, project.id, BuildProfile.development)      # 8101
    report = ports.usage(db, probe_host="127.0.0.1")
    assert report["range"] == {"start": 8100, "end": 8104, "size": 5}
    assert report["allocated"] == 2 and report["free"] == 3
    listening = {a["port"]: a["listening"] for a in report["allocations"]}
    assert listening == {8101: False, 8102: False}
    assert "listening_outside_registry" not in report

    deep = ports.usage(db, probe_host="127.0.0.1", probe_range=True)
    assert deep["listening_outside_registry"] == [8100]


def test_is_listening_detects_a_real_socket(fresh_settings):
    """탐지 자체가 동작하는지 — 목킹 없이 한 번은 확인한다."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert ports.is_listening("127.0.0.1", port) is True
    assert ports.is_listening("127.0.0.1", port) is False


# --- 배포 경로 연결 ---

def test_deploy_spec_carries_the_allocated_port(db, monkeypatch):
    """런타임은 대장이 정한 포트를 그대로 쓴다."""
    _no_probe(monkeypatch)
    from app.services import deployer

    project = _project(db)
    spec = deployer.make_spec(db, project, "img:1", BuildProfile.release)
    assert spec.host_port == 8100
    again = deployer.make_spec(db, project, "img:2", BuildProfile.release)
    assert again.host_port == 8100


def test_enterprise_tier_does_not_allocate_host_ports(db, monkeypatch):
    """2차(k8s)는 호스트 포트를 쓰지 않는다 — 배정하면 범위만 갉아먹는다."""
    _no_probe(monkeypatch)
    monkeypatch.setenv("PAAS_TIER", "enterprise")
    get_settings.cache_clear()
    from app.services import deployer

    spec = deployer.make_spec(db, _project(db), "img:1", BuildProfile.release)
    assert spec.host_port is None
    assert db.query(PortAllocation).count() == 0


# --- 조회 API ---

def test_port_usage_endpoint(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_PORT_RANGE_START", "8100")
    monkeypatch.setenv("PAAS_PORT_RANGE_END", "8104")
    get_settings.cache_clear()
    monkeypatch.setattr(ports, "is_listening", lambda host, port, timeout=0.2: False)
    c = TestClient(create_app())
    pid = c.post(f"{API}/projects", json={
        "name": "shop-web", "type": "react", "git_url": "https://git.example.com/x",
    }, headers=ADMIN).json()["id"]

    session = SessionLocal()
    try:
        ports.allocate(session, pid, BuildProfile.release)
    finally:
        session.close()

    body = c.get(f"{API}/ports", headers=ADMIN).json()
    assert body["allocated"] == 1
    assert body["allocations"][0]["project"] == "shop-web"
    assert body["allocations"][0]["profile"] == "release"
    assert body["free"] == 4


def test_deleting_a_project_frees_its_ports(monkeypatch, fresh_settings):
    monkeypatch.setattr(ports, "is_listening", lambda host, port, timeout=0.2: False)
    get_settings.cache_clear()
    c = TestClient(create_app())
    pid = c.post(f"{API}/projects", json={
        "name": "shop-web", "type": "react", "git_url": "https://git.example.com/x",
    }, headers=ADMIN).json()["id"]
    session = SessionLocal()
    try:
        ports.allocate(session, pid, BuildProfile.release)
    finally:
        session.close()
    assert c.get(f"{API}/ports", headers=ADMIN).json()["allocated"] == 1

    assert c.delete(f"{API}/projects/{pid}", headers=ADMIN).status_code == 204
    assert c.get(f"{API}/ports", headers=ADMIN).json()["allocated"] == 0

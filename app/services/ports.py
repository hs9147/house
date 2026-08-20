"""호스트 포트 배정 대장.

지금까지는 배포할 때마다 범위를 훑어 "지금 아무도 리슨하지 않는 첫 포트"를 골랐다
(runtime/*.allocate_port). 그 방식에 세 가지 문제가 있다:

- **경쟁** — 배포 워커가 둘(PAAS_DEPLOY_WORKERS 기본 2)이라 동시 배포가 같은 포트를
  동시에 고른다. 둘 다 "비어 있다"고 보고, 나중에 기동하는 쪽이 포트를 못 잡아 조용히
  실패한다. 확인과 사용 사이가 벌어져 있어서 생기는 문제라 확인을 더 정교하게 해도
  없어지지 않는다.
- **망각** — 멈춰 있는 배포는 리슨하지 않으므로 그 포트가 다음 배포에서 남에게 넘어간다.
  다시 켜면 포트가 바뀌고, 프록시 설정을 손으로 고쳐 둔 곳이 있으면 엉뚱한 앱을 가리킨다.
- **불투명** — "8123은 누가 쓰는가"에 답할 수 있는 곳이 없다.

그래서 배정을 DB 대장으로 옮긴다. port에 unique가 걸려 있어 경쟁은 삽입 충돌로 드러나고
(다음 후보로 넘어간다), 한 번 받은 포트는 프로젝트가 지워질 때까지 그 프로젝트 것이다 —
멈췄다 켜도 같은 포트로 올라온다.

대장에 없는 점유(플랫폼 밖 서비스가 쓰는 포트)는 여전히 있을 수 있어서, 후보를 고를 때
실제로 리슨 중인지도 함께 본다. 대장이 우선이고 탐지는 보조다.
"""
import socket

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import BuildProfile, PortAllocation, Project

# 포트가 열려 있는지 확인하는 데 쓰는 상한. 로컬 접속이라 정상이면 즉시 끝나고, 방화벽
# 때문에 응답이 없는 경우에도 배포가 이 확인 하나로 오래 멈추지 않게 한다.
PROBE_TIMEOUT = 0.2


class PortExhausted(RuntimeError):
    """설정된 범위에 남은 포트가 없다."""


def is_listening(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _owner_row(db: Session, project_id: int, profile: BuildProfile,
               component: str) -> PortAllocation | None:
    return db.execute(
        select(PortAllocation).where(
            PortAllocation.project_id == project_id,
            PortAllocation.profile == profile,
            PortAllocation.component == component,
        )
    ).scalar_one_or_none()


def allocate(db: Session, project_id: int, profile: BuildProfile, component: str = "",
             *, probe_host: str = "127.0.0.1") -> int:
    """이 주인의 포트를 돌려준다 — 없으면 새로 배정한다.

    이미 배정된 포트가 있으면 그대로 쓴다(고정). 설정 범위가 바뀌어 옛 배정이 범위 밖이
    됐을 때만 놓아주고 다시 받는다 — 그러지 않으면 범위를 줄인 뒤에도 밖의 포트를 계속
    쓰게 되고, 방화벽을 범위 기준으로 열어 둔 구성에서 조용히 막힌다.
    """
    settings = get_settings()
    low, high = settings.port_range_start, settings.port_range_end
    component = component or ""

    existing = _owner_row(db, project_id, profile, component)
    if existing is not None:
        if low <= existing.port <= high:
            return existing.port
        db.delete(existing)
        db.commit()

    taken = set(db.execute(select(PortAllocation.port)).scalars().all())
    for port in range(low, high + 1):
        if port in taken or is_listening(probe_host, port):
            continue
        db.add(PortAllocation(port=port, project_id=project_id, profile=profile,
                              component=component))
        try:
            db.commit()
        except IntegrityError:
            # 다른 배포가 방금 이 포트(또는 이 주인의 자리)를 가져갔다.
            db.rollback()
            winner = _owner_row(db, project_id, profile, component)
            if winner is not None:
                return winner.port
            taken.add(port)
            continue
        return port

    raise PortExhausted(
        f"배정할 포트가 없습니다 — 범위 {low}~{high}에서 {len(taken)}개가 이미 배정돼 "
        "있습니다. PAAS_PORT_RANGE_START/END를 넓히거나 쓰지 않는 프로젝트를 정리하세요."
    )


def release_project(db: Session, project_id: int) -> int:
    """프로젝트가 지워질 때 그 프로젝트의 배정을 모두 놓아준다."""
    rows = db.execute(
        select(PortAllocation).where(PortAllocation.project_id == project_id)
    ).scalars().all()
    for row in rows:
        db.delete(row)
    return len(rows)


def usage(db: Session, *, probe_host: str = "127.0.0.1", probe_range: bool = False) -> dict:
    """포트 사용현황 — 대장 + (요청하면) 대장 밖 점유까지.

    기본은 배정된 포트만 확인한다. 범위 전체 훑기는 포트 수만큼 접속을 시도하므로
    (기본 범위가 900개다) 눌러서 볼 때만 하도록 옵션으로 둔다.
    """
    settings = get_settings()
    low, high = settings.port_range_start, settings.port_range_end
    rows = db.execute(
        select(PortAllocation, Project.name)
        .join(Project, PortAllocation.project_id == Project.id)
        .order_by(PortAllocation.port)
    ).all()

    allocations = [
        {
            "port": row.PortAllocation.port,
            "project": row.name,
            "project_id": row.PortAllocation.project_id,
            "profile": row.PortAllocation.profile.value,
            "component": row.PortAllocation.component or None,
            "listening": is_listening(probe_host, row.PortAllocation.port),
            "in_range": low <= row.PortAllocation.port <= high,
        }
        for row in rows
    ]

    result = {
        "range": {"start": low, "end": high, "size": high - low + 1},
        "probe_host": probe_host,
        "allocated": len(allocations),
        "free": max(0, (high - low + 1) - sum(1 for a in allocations if a["in_range"])),
        "allocations": allocations,
    }
    if probe_range:
        known = {a["port"] for a in allocations}
        result["listening_outside_registry"] = [
            port for port in range(low, high + 1)
            if port not in known and is_listening(probe_host, port)
        ]
    return result

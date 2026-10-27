"""시스템·GPU 스냅샷. psutil / nvidia-ml-py는 선택 의존성 — 없으면 해당 항목을 생략한다."""
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOP = 5                     # 자원마다 보이는 주요 점유 항목 수
SAMPLE_SECONDS = 0.5        # 프로세스 CPU를 재는 간격(두 번 읽은 차이)
DIR_TTL = 600               # 폴더 크기를 다시 재는 간격(초) — 재는 데 오래 걸리고 천천히 바뀐다
MAX_ENTRIES = 500_000       # 폴더 하나를 잴 때 보는 항목 상한 — 넘으면 "그 이상"으로 표시
MAX_PARTITIONS = 8


def snapshot() -> dict[str, Any]:
    """시스템 상태 — **지금 이 설치본의 실제 구성**을 말한다.

    예전에는 host.py의 OS 능력 매트릭스를 그대로 내보냈다. 그래서 Windows면 런타임·프록시
    설정과 무관하게 늘 "Docker Desktop + WSL2 (기업 규모에 따라 유료) · GPU 지원"이 떴다.
    windows_service 런타임은 정의상 Docker를 쓰지 않는데(nssm으로 네이티브 프로세스를
    등록한다) Docker 라이선스 안내가 뜨고, GPU가 없는 서버에서 "GPU 지원"이라고 하면서
    바로 아래에 "GPU 없음"을 함께 보여 주는 자기모순이 있었다.

    OS 능력은 여전히 상한이다(macOS는 GPU 컨테이너를 못 쓴다). 다만 상한과 실제 구성은
    다른 것이므로, 화면에는 실제 구성을 보낸다.
    """
    from ..config import get_settings  # noqa: PLC0415
    from .deployer import GPU_CAPABLE_RUNTIMES, runtime_name  # noqa: PLC0415
    from .host import get_host_caps, gpu_allowed  # noqa: PLC0415

    settings = get_settings()
    caps = get_host_caps()
    runtime = runtime_name()
    data: dict[str, Any] = {
        "host_os": caps.os,
        # 무엇으로 돌고 무엇으로 프록시하는지 — 이게 사람이 확인하려던 값이다.
        "runtime_backend": runtime,
        "proxy_backend": settings.proxy_backend,
        # GPU는 OS만으로 정해지지 않는다: 런타임이 배정할 수 있어야 하고(GPU_CAPABLE_RUNTIMES),
        # 그 위에 OS 능력·PAAS_FORCE_GPU가 걸린다. 실제 장치 유무는 아래 gpus가 말한다.
        "gpu_supported": runtime in GPU_CAPABLE_RUNTIMES and gpu_allowed(),
        # Docker 라이선스 안내는 Docker 런타임일 때만 의미가 있다.
        "docker_hint": caps.docker_hint if runtime == "docker" else "",
    }
    try:
        import psutil  # noqa: PLC0415

        vm = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        # 프로세스 표본을 재는 동안이 전체 CPU의 표본 구간이기도 하다(같은 0.5초).
        data["cpu_percent"], top_cpu, top_memory = _processes(psutil)
        data["memory"] = {"total": vm.total, "used": vm.used, "percent": vm.percent}
        data["disk"] = {"total": disk.total, "used": disk.used, "percent": disk.percent}
        data["top"] = {"cpu": top_cpu, "memory": top_memory,
                       "disks": _partitions(psutil), "folders": folder_sizes()}
    except ImportError:
        data["system"] = "psutil not installed"

    data["gpus"] = _gpu_snapshot()
    return data


# ---------------------------------------------------------------- 주요 점유 항목
#
# 전체 사용률만으로는 무엇을 줄일지 모른다. 프로세스를 **누가 쓰는지로 묶는다**: 작업 폴더가 배포 폴더
# (work_dir/{프로젝트}, development는 {프로젝트}~dev — build.deploy_dir) 안이면 그 앱, 플랫폼 저장소 안이면
# 플랫폼, 아니면 프로세스 이름. 앱 하나가 node·python 여러 개로 뜨면 하나로 합친다. 명령줄은 싣지 않는다 —
# 인자에 비밀번호·토큰이 들어 있는 프로그램이 있다.


def _owner(p, work_dir: Path, repo: Path) -> tuple[str, str]:
    """(종류, 이름) — app(배포한 앱)·platform(이 플랫폼)·process(그 밖의 프로세스)."""
    try:
        cwd = Path(p.cwd()).resolve()
    except Exception:  # noqa: BLE001 — 권한 없는 시스템 프로세스·이미 끝난 프로세스
        cwd = None
    if cwd is not None:
        if cwd.is_relative_to(work_dir) and cwd != work_dir:
            return "app", cwd.relative_to(work_dir).parts[0]
        if cwd.is_relative_to(repo):
            return "platform", "플랫폼"
    info = getattr(p, "info", None) or {}
    return "process", info.get("name") or f"pid {p.pid}"


def _processes(psutil) -> tuple[float, list[dict], list[dict]]:
    """(전체 CPU %, CPU 상위, 메모리 상위). CPU는 두 번 읽은 차이라 SAMPLE_SECONDS 동안 잰다 — 프로세스의
    값은 코어 하나 기준이라 코어 수로 나눠 전체 대비로 맞춘다."""
    from ..config import get_settings  # noqa: PLC0415

    settings = get_settings()
    work_dir, repo = settings.work_dir.resolve(), settings.resolved_repo_root.resolve()
    procs = []
    for p in psutil.process_iter(["name"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except psutil.Error:
            pass
    psutil.cpu_percent(None)
    time.sleep(SAMPLE_SECONDS)
    total = psutil.cpu_percent(None)
    cores = psutil.cpu_count() or 1
    groups: dict[tuple[str, str], dict] = {}
    for p in procs:
        if p.pid == 0:   # Windows의 System Idle Process — 쉬는 몫이 점유로 보인다
            continue
        try:
            cpu, rss = p.cpu_percent(None) / cores, p.memory_info().rss
            kind, label = _owner(p, work_dir, repo)
        except psutil.Error:
            continue
        g = groups.setdefault((kind, label), {"kind": kind, "label": label, "cpu": 0.0, "memory": 0, "count": 0})
        g["cpu"] += cpu
        g["memory"] += rss
        g["count"] += 1
    rows = [{**g, "cpu": round(g["cpu"], 1)} for g in groups.values()]
    top_cpu = [r for r in sorted(rows, key=lambda r: -r["cpu"]) if r["cpu"] >= 0.1][:TOP]
    top_memory = sorted(rows, key=lambda r: -r["memory"])[:TOP]
    return total, top_cpu, top_memory


def _partitions(psutil) -> list[dict]:
    """드라이브(마운트)마다의 사용률 — 전체 디스크 값은 플랫폼이 깔린 드라이브 하나뿐이다."""
    out, seen = [], set()
    for part in psutil.disk_partitions(all=False):
        if not part.fstype or "cdrom" in (part.opts or "") or part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        try:
            u = psutil.disk_usage(part.mountpoint)
        except (OSError, psutil.Error):
            continue   # 꽂히지 않은 드라이브·권한 없는 마운트
        out.append({"mount": part.mountpoint, "total": u.total, "used": u.used, "percent": u.percent})
    return sorted(out, key=lambda d: -d["percent"])[:MAX_PARTITIONS]


def _du(path: Path) -> tuple[int, bool]:
    """폴더 크기 — (바이트, 다 쟀는가). 링크는 따라가지 않는다(정션이 다른 드라이브·제 자신을 가리킨다)."""
    total, seen, stack = 0, 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    seen += 1
                    if seen > MAX_ENTRIES:
                        return total, False
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total, True


def _folder_targets() -> list[tuple[str, str, Path]]:
    """(종류, 이름, 경로) — 앱 배포 폴더마다, 그리고 플랫폼이 쌓는 자리."""
    from ..config import get_settings  # noqa: PLC0415

    s = get_settings()
    out: list[tuple[str, str, Path]] = []
    try:
        out += [("app", d.name, d) for d in sorted(s.work_dir.iterdir()) if d.is_dir()]
    except OSError:
        pass
    for label, path in (("빌드·앱 로그", s.build_log_dir), ("파일 저장소", Path(s.storage_root or "./data/storage")),
                        ("문서 색인", s.doc_index_dir), ("개인 업무 맥락", s.personal_root)):
        out.append(("platform", label, Path(path)))
    return [(k, n, p) for k, n, p in out if p.is_dir()]


_folders: dict[str, Any] = {"at": 0.0, "items": [], "measured_at": None, "running": False}
_folders_lock = threading.Lock()


def _measure_folders() -> None:
    try:
        items = []
        for kind, label, path in _folder_targets():
            size, complete = _du(path)
            items.append({"kind": kind, "label": label, "path": str(path.resolve()), "bytes": size,
                          "complete": complete})
        items.sort(key=lambda i: -i["bytes"])
        with _folders_lock:
            _folders.update(items=items[:TOP + 3], measured_at=datetime.now(timezone.utc).isoformat())
    finally:
        with _folders_lock:
            _folders.update(at=time.monotonic(), running=False)


def folder_sizes() -> dict[str, Any]:
    """폴더 크기 — **지난번에 잰 것**을 돌려주고, 오래됐으면 뒤에서 다시 잰다(대시보드가 기다리지 않게).
    처음에는 비어 있고 measuring이 참이다 — 화면이 "재는 중"을 보이고 다음 새로고침에 채운다."""
    with _folders_lock:
        stale = not _folders["measured_at"] or time.monotonic() - _folders["at"] > DIR_TTL
        if stale and not _folders["running"]:
            _folders["running"] = True
            threading.Thread(target=_measure_folders, name="monitor-folders", daemon=True).start()
        return {"items": list(_folders["items"]), "measured_at": _folders["measured_at"],
                "measuring": _folders["running"]}


def _gpu_snapshot() -> list[dict[str, Any]]:
    try:
        import pynvml  # noqa: PLC0415
    except ImportError:
        return []
    try:
        pynvml.nvmlInit()
        gpus = []
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            name = pynvml.nvmlDeviceGetName(h)
            gpus.append({
                "index": i,
                "name": name.decode() if isinstance(name, bytes) else name,
                "vram_total": mem.total,
                "vram_used": mem.used,
                "util_percent": util.gpu,
            })
        pynvml.nvmlShutdown()
        return gpus
    except Exception:
        return []


def free_vram_bytes() -> int | None:
    """LLM 배포 전 VRAM 사전 검사용. GPU가 없으면 None."""
    gpus = _gpu_snapshot()
    if not gpus:
        return None
    return max(g["vram_total"] - g["vram_used"] for g in gpus)

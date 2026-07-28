"""파일 저장소 — 로컬 경로를 감추고 URL로만 다루게 하는 계층.

file_storage 모듈이 실제로 어느 디렉터리에 얹혀 있는지는 플랫폼 내부 사정이다.
배포된 앱도 콘솔도 `{P}_URL`이 가리키는 /storage/{모듈} 창구만 쓴다.
"""
from pathlib import Path

from ..config import get_settings
from ..models import Module


class StorageError(Exception):
    """경로 탈출 등 저장소 규약 위반."""


def root_for(module: Module) -> Path:
    """모듈이 얹힌 실제 디렉터리. 호출자 밖으로 새어 나가면 안 된다.

    endpoint가 URL이거나 비어 있으면 PAAS_STORAGE_ROOT를 쓴다 — 로컬 경로만
    저장소 루트가 될 수 있다.
    """
    from .modules import decrypt_config  # noqa: PLC0415 — 순환 import 회피

    cfg = decrypt_config(module.config or {})
    settings = get_settings()
    endpoint = str(cfg.get("endpoint") or "")
    base = endpoint if endpoint and "://" not in endpoint else (settings.storage_root or "./data/storage")
    sub = str(cfg.get("sub_folder") or cfg.get("bucket") or "").strip("/\\")
    return (Path(base) / sub).resolve()


def url_for(module_name: str) -> str:
    """모듈 저장소의 공개 주소. 바인딩된 앱에 {P}_URL로 주입된다."""
    settings = get_settings()
    base = settings.platform_public_url.rstrip("/") or f"https://{settings.base_domain}"
    return f"{base}/paas/api/v1/storage/{module_name}"


def resolve(root: Path, rel: str) -> Path:
    """root 안의 경로로만 해석한다. 벗어나거나 절대 경로면 StorageError.

    절대 경로를 조용히 상대 경로로 바꾸지 않는다 — "/etc/passwd"를 저장소 안의
    "etc/passwd"로 받아 주면 호출자가 무엇을 쓴 건지 알 수 없다.
    """
    if rel.startswith(("/", "\\")) or Path(rel).is_absolute():
        raise StorageError(f"절대 경로는 쓸 수 없습니다: {rel}")
    target = (root / rel).resolve()
    if target != root and not target.is_relative_to(root):
        raise StorageError(f"경로가 저장소를 벗어납니다: {rel}")
    return target


def list_files(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    return sorted(
        (
            {"path": p.relative_to(root).as_posix(), "size": p.stat().st_size}
            for p in root.rglob("*")
            if p.is_file()
        ),
        key=lambda f: f["path"],
    )


def write_file(root: Path, rel: str, data: bytes) -> str:
    target = resolve(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target.relative_to(root).as_posix()


def delete_file(root: Path, rel: str) -> None:
    target = resolve(root, rel)
    if not target.is_file():
        raise FileNotFoundError(rel)
    target.unlink()

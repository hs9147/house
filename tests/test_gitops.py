"""후속1 — GitOps(ArgoCD) 연계: 매니페스트가 GitOps 리포에 커밋·푸시되는지 검증."""
import subprocess

import pytest

from app.config import get_settings
from app.models import BuildProfile
from app.services.runtime.base import RuntimeSpec
from app.services.runtime.k8s_runtime import K8sRuntime


def _spec(image: str = "shop:abc", secret_keys: frozenset[str] = frozenset()) -> RuntimeSpec:
    return RuntimeSpec(
        "shop", image, 8000, BuildProfile.release, "shop.apps.test", secret_keys=secret_keys
    )


@pytest.fixture
def gitops_env(monkeypatch, tmp_path, fresh_settings):
    bare = tmp_path / "gitops.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    monkeypatch.setenv("PAAS_K8S_GITOPS_REPO", str(bare))
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("PAAS_K8S_MANIFEST_DIR", str(tmp_path / "k8s-manifests"))
    get_settings.cache_clear()
    return bare


def _log(bare) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%s", "main"], cwd=bare, capture_output=True, text=True
    )
    return out.stdout.strip().splitlines()


def test_deploy_pushes_manifest_to_gitops_repo(gitops_env, tmp_path):
    endpoint = K8sRuntime().start(_spec())
    assert endpoint.port == 80

    checkout = tmp_path / "verify"
    subprocess.run(["git", "clone", "-q", str(gitops_env), str(checkout)], check=True)
    manifest = checkout / "apps" / "paas-shop.yaml"
    assert manifest.exists()
    content = manifest.read_text(encoding="utf-8")
    assert "kind: Deployment" in content and "shop:abc" in content
    assert _log(gitops_env) == ["paas: deploy paas-shop (shop:abc)"]


def test_redeploy_same_manifest_skips_commit(gitops_env):
    K8sRuntime().start(_spec())
    K8sRuntime().start(_spec())  # 동일 내용 — 커밋 없음
    assert len(_log(gitops_env)) == 1

    K8sRuntime().start(_spec(image="shop:def456"))  # 새 이미지 — 커밋 추가
    assert len(_log(gitops_env)) == 2


def test_secrets_never_pushed_to_gitops_repo(gitops_env, tmp_path):
    """보안수정 — env가 secret_keys에 해당하면 GitOps 커밋에 절대 포함되지 않는다."""
    spec = _spec(secret_keys=frozenset({"DB_PASSWORD"}))
    spec.env = {"DB_PASSWORD": "s3cr3t-value", "PUBLIC_URL": "https://x"}

    K8sRuntime().start(spec)

    checkout = tmp_path / "verify"
    subprocess.run(["git", "clone", "-q", str(gitops_env), str(checkout)], check=True)
    manifest = (checkout / "apps" / "paas-shop.yaml").read_text(encoding="utf-8")
    assert "s3cr3t-value" not in manifest
    assert "kind: Secret" not in manifest
    # 전체 git 히스토리 어디에도 시크릿 값이 없어야 함 (blob 검색)
    grep = subprocess.run(
        ["git", "grep", "-q", "s3cr3t-value", *_log_all_commits(gitops_env)],
        cwd=gitops_env, capture_output=True,
    )
    assert grep.returncode != 0, "시크릿 값이 GitOps 리포 히스토리에 존재함"


def _log_all_commits(bare) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%H", "main"], cwd=bare, capture_output=True, text=True
    )
    return out.stdout.strip().splitlines()


def test_secrets_written_to_local_file_only(gitops_env):
    from app.config import get_settings

    settings = get_settings()
    spec = _spec(secret_keys=frozenset({"DB_PASSWORD"}))
    spec.env = {"DB_PASSWORD": "s3cr3t-value", "PUBLIC_URL": "https://x"}

    K8sRuntime().start(spec)

    local_secret_file = settings.k8s_manifest_dir / "paas-shop-secrets.local.yaml"
    assert local_secret_file.exists()
    content = local_secret_file.read_text(encoding="utf-8")
    assert "s3cr3t-value" in content
    assert "kind: Secret" in content

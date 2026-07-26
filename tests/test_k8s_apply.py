"""갭4 — K8s apply 에러 정책: 접근 불가는 파일 폴백, apply 실패는 재시도 후 표면화."""
import sys
import types

import pytest

from app.models import BuildProfile
from app.services.runtime import k8s_runtime
from app.services.runtime.base import RuntimeSpec
from app.services.runtime.k8s_runtime import K8sApplyError, K8sRuntime


def _spec() -> RuntimeSpec:
    return RuntimeSpec("shop", "shop:abc", 8000, BuildProfile.release, "shop.apps.test")


def _fake_kubernetes(create_from_dict):
    """`from kubernetes import client, config, utils`가 동작하는 가짜 패키지."""
    pkg = types.ModuleType("kubernetes")
    pkg.client = types.SimpleNamespace(ApiClient=lambda: object())
    pkg.config = types.SimpleNamespace(
        load_incluster_config=lambda: (_ for _ in ()).throw(RuntimeError("not in cluster")),
        load_kube_config=lambda: None,
    )
    pkg.utils = types.SimpleNamespace(create_from_dict=create_from_dict)
    return pkg


def test_no_package_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "kubernetes", None)  # import 실패 유도
    monkeypatch.setenv("PAAS_K8S_MANIFEST_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    endpoint = K8sRuntime().start(_spec())
    assert endpoint.port == 80
    assert (tmp_path / "paas-shop.yaml").exists()
    get_settings.cache_clear()


def test_apply_failure_retries_then_raises(monkeypatch):
    calls = []

    def failing(k8s, manifest, apply):
        calls.append(1)
        raise RuntimeError("connection reset")

    monkeypatch.setitem(sys.modules, "kubernetes", _fake_kubernetes(failing))
    monkeypatch.setattr(k8s_runtime.time, "sleep", lambda s: None)  # 백오프 생략

    with pytest.raises(K8sApplyError, match="3회 재시도"):
        K8sRuntime().start(_spec())
    assert len(calls) == 3  # 매니페스트 첫 건에서 3회 시도


def test_apply_success_no_file_fallback(monkeypatch, tmp_path):
    applied = []
    monkeypatch.setitem(
        sys.modules, "kubernetes",
        _fake_kubernetes(lambda k8s, manifest, apply: applied.append(manifest["kind"])),
    )
    monkeypatch.setenv("PAAS_K8S_MANIFEST_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    K8sRuntime().start(_spec())
    assert applied == ["Deployment", "Service", "Ingress"]
    assert not (tmp_path / "paas-shop.yaml").exists()  # 성공 시 파일 폴백 없음
    get_settings.cache_clear()

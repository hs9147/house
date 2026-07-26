"""후속3 — 네임스페이스 부트스트랩: Quota 설정 시 ResourceQuota·LimitRange 생성."""
from app.config import get_settings
from app.services.runtime.k8s_runtime import namespace_manifests


def test_namespace_only_without_quota(fresh_settings):
    get_settings.cache_clear()
    manifests = namespace_manifests()
    assert [m["kind"] for m in manifests] == ["Namespace"]
    assert manifests[0]["metadata"]["name"] == "paas-apps"


def test_quota_and_limitrange_when_configured(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_K8S_QUOTA_CPU", "20")
    monkeypatch.setenv("PAAS_K8S_QUOTA_MEMORY", "64Gi")
    get_settings.cache_clear()
    manifests = namespace_manifests()
    assert [m["kind"] for m in manifests] == ["Namespace", "ResourceQuota", "LimitRange"]
    quota = manifests[1]["spec"]["hard"]
    assert quota == {
        "requests.cpu": "20", "limits.cpu": "20",
        "requests.memory": "64Gi", "limits.memory": "64Gi",
    }
    limits = manifests[2]["spec"]["limits"][0]
    assert limits["default"]["memory"] == "512Mi"


def test_cpu_only_quota(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_K8S_QUOTA_CPU", "8")
    get_settings.cache_clear()
    manifests = namespace_manifests()
    hard = manifests[1]["spec"]["hard"]
    assert hard == {"requests.cpu": "8", "limits.cpu": "8"}

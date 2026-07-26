"""2차(enterprise) 매니페스트 생성 — 프로필별 replicas/전략/리소스 차등 검증."""
from app.models import BuildProfile
from app.services.runtime.base import RuntimeSpec
from app.services.runtime.k8s_runtime import build_manifests


def _spec(profile: BuildProfile) -> RuntimeSpec:
    return RuntimeSpec(
        project_name="shop",
        image_tag="shop:abc123",
        internal_port=8000,
        profile=profile,
        domain="shop.apps.test",
        env={"APP_ENV": "production"},
        memory_limit="1g",
        cpu_limit=1.0,
        replicas=2,
        health_check_path="/healthz",
    )


def test_release_manifests():
    dep, svc, ing = build_manifests(_spec(BuildProfile.release))
    assert dep["kind"] == "Deployment"
    assert dep["spec"]["replicas"] == 2
    assert dep["spec"]["strategy"]["type"] == "RollingUpdate"
    assert dep["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert svc["spec"]["ports"][0]["targetPort"] == 8000
    assert ing["spec"]["rules"][0]["host"] == "shop.apps.test"
    assert "cert-manager.io/cluster-issuer" in ing["metadata"]["annotations"]


def test_development_manifests_are_scaled_down():
    dep, _, _ = build_manifests(_spec(BuildProfile.development))
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["strategy"]["type"] == "Recreate"
    assert dep["metadata"]["name"].endswith("-dev")
    limits = dep["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    # release 1g/1cpu 대비 development는 절반
    assert limits["cpu"] == "500m"


def test_gpu_request():
    spec = _spec(BuildProfile.release)
    spec.gpu = True
    dep, _, _ = build_manifests(spec)
    limits = dep["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert limits["nvidia.com/gpu"] == 1


def test_isolation_adds_network_policy(monkeypatch, fresh_settings):
    """갭6 — k8s_isolation 시 유닛별 NetworkPolicy가 추가된다."""
    monkeypatch.setenv("PAAS_K8S_ISOLATION", "true")
    monkeypatch.setenv("PAAS_K8S_INGRESS_NAMESPACE", "traefik")
    from app.config import get_settings

    get_settings.cache_clear()
    manifests = build_manifests(_spec(BuildProfile.release))
    assert [m["kind"] for m in manifests] == [
        "Deployment", "Service", "Ingress", "NetworkPolicy",
    ]
    np = manifests[3]
    assert np["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "shop"
    allowed = [
        f["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
        for f in np["spec"]["ingress"][0]["from"]
    ]
    assert allowed == ["traefik", "paas-apps"]


def test_isolation_off_by_default(fresh_settings):
    from app.config import get_settings

    get_settings.cache_clear()
    manifests = build_manifests(_spec(BuildProfile.release))
    assert len(manifests) == 3


def test_secret_keys_split_into_separate_secret_object():
    """시크릿 정보 유출 방지 — secret_keys에 해당하는 env는 Deployment에 평문으로
    들어가지 않고 별도 Secret 오브젝트(stringData)로 분리되어야 한다."""
    spec = _spec(BuildProfile.release)
    spec.env = {"APP_ENV": "production", "DB_PASSWORD": "s3cr3t", "PUBLIC_URL": "https://x"}
    spec.secret_keys = frozenset({"DB_PASSWORD"})

    manifests = build_manifests(spec)
    kinds = [m["kind"] for m in manifests]
    assert kinds == ["Secret", "Deployment", "Service", "Ingress"]

    secret = manifests[0]
    assert secret["metadata"]["name"] == "paas-shop-secrets"
    assert secret["stringData"] == {"DB_PASSWORD": "s3cr3t"}

    dep = manifests[1]
    container = dep["spec"]["template"]["spec"]["containers"][0]
    env_names = {e["name"] for e in container["env"]}
    assert "DB_PASSWORD" not in env_names
    assert env_names == {"APP_ENV", "PUBLIC_URL"}
    assert container["envFrom"] == [{"secretRef": {"name": "paas-shop-secrets"}}]


def test_no_secret_object_when_no_secret_keys():
    manifests = build_manifests(_spec(BuildProfile.release))
    assert "Secret" not in [m["kind"] for m in manifests]
    container = manifests[0]["spec"]["template"]["spec"]["containers"][0]
    assert "envFrom" not in container

"""2차(enterprise) 런타임 — Kubernetes.

플랫폼은 매니페스트 생성기 역할만 한다. 롤링 업데이트·자가치유·스케줄링은 K8s에 위임.
kubernetes 패키지 + 클러스터 접근이 가능하면 직접 apply하고,
아니면 매니페스트 YAML을 k8s_manifest_dir에 기록한다 (kubectl apply -f 또는 GitOps 연계).

에러 정책(갭4): "클러스터 접근 불가"(패키지 없음/설정 없음)만 파일 폴백이고,
클러스터에 연결된 상태의 apply 실패는 3회 재시도 후 K8sApplyError로 표면화한다 —
조용한 폴백은 배포 성공으로 오인되므로 금지.

프로필 반영:
  development → replicas 1, 리소스 절반, Recreate 전략, {name}-dev 도메인
  release     → replicas 2(기본), 리소스 전량, RollingUpdate(maxUnavailable 0)
"""
import subprocess
import time
from pathlib import Path


class K8sApplyError(RuntimeError):
    pass


class GitOpsError(RuntimeError):
    pass

import yaml

from ...config import get_settings
from ...models import BuildProfile
from ..build import PROFILES
from .base import Endpoint, Runtime, RuntimeSpec


def _mem_k8s(limit: str, factor: float) -> str:
    units = {"k": "Ki", "m": "Mi", "g": "Gi"}
    unit = limit[-1].lower()
    if unit in units:
        return f"{int(float(limit[:-1]) * factor * 1024)}Mi" if unit == "g" else \
               f"{int(float(limit[:-1]) * factor)}{units[unit]}"
    return str(int(float(limit) * factor))


def _cpu_k8s(cpu: float, factor: float) -> str:
    return f"{int(cpu * factor * 1000)}m"


def build_manifests(spec: RuntimeSpec) -> list[dict]:
    settings = get_settings()
    profile_spec = PROFILES[spec.profile]
    factor = profile_spec.resource_factor
    ns = settings.k8s_namespace
    name = spec.unit_name
    labels = {
        "app.kubernetes.io/name": spec.project_name,
        "app.kubernetes.io/managed-by": "paas",
        "paas/profile": spec.profile.value,
    }
    image = (
        f"{settings.k8s_registry}/{spec.image_tag}" if settings.k8s_registry else spec.image_tag
    )
    replicas = spec.replicas if spec.profile == BuildProfile.release else 1
    is_release = spec.profile == BuildProfile.release

    resources = {
        "requests": {
            "memory": _mem_k8s(spec.memory_limit, factor * 0.5),
            "cpu": _cpu_k8s(spec.cpu_limit, factor * 0.5),
        },
        "limits": {
            "memory": _mem_k8s(spec.memory_limit, factor),
            "cpu": _cpu_k8s(spec.cpu_limit, factor),
        },
    }
    if spec.gpu:
        resources["limits"]["nvidia.com/gpu"] = 1

    # 시크릿(spec.secret_keys)은 일반 env로 매니페스트에 평문으로 넣지 않고
    # 별도 Secret 오브젝트로 분리한다 — GitOps 모드에서 외부 git에 평문 커밋되는 것을 막기 위함.
    plain_env = {k: v for k, v in spec.env.items() if k not in spec.secret_keys}
    secret_env = {k: v for k, v in spec.env.items() if k in spec.secret_keys}

    container = {
        "name": "app",
        "image": image,
        "ports": [{"containerPort": spec.internal_port}],
        "env": [{"name": k, "value": v} for k, v in sorted(plain_env.items())],
        "resources": resources,
        "readinessProbe": {
            "httpGet": {"path": spec.health_check_path, "port": spec.internal_port},
            "initialDelaySeconds": 5,
            "periodSeconds": 10,
        },
    }
    if secret_env:
        container["envFrom"] = [{"secretRef": {"name": f"{name}-secrets"}}]

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app.kubernetes.io/name": spec.project_name,
                                          "paas/profile": spec.profile.value}},
            "strategy": (
                {"type": "RollingUpdate",
                 "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}}
                if is_release
                else {"type": "Recreate"}
            ),
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [container],
                    "securityContext": {"runAsNonRoot": is_release} if is_release else {},
                },
            },
        },
    }

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "selector": {"app.kubernetes.io/name": spec.project_name,
                         "paas/profile": spec.profile.value},
            "ports": [{"port": 80, "targetPort": spec.internal_port}],
        },
    }

    ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": labels,
            "annotations": {
                "cert-manager.io/cluster-issuer": settings.k8s_cluster_issuer,
            },
        },
        "spec": {
            "ingressClassName": settings.k8s_ingress_class,
            "tls": [{"hosts": [spec.domain], "secretName": f"{name}-tls"}],
            "rules": [{
                "host": spec.domain,
                "http": {"paths": [{
                    "path": "/",
                    "pathType": "Prefix",
                    "backend": {"service": {"name": name, "port": {"number": 80}}},
                }]},
            }],
        },
    }
    manifests: list[dict] = []
    if secret_env:
        manifests.append({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": f"{name}-secrets", "namespace": ns, "labels": labels},
            "type": "Opaque",
            "stringData": dict(sorted(secret_env.items())),
        })
    manifests += [deployment, service, ingress]

    if settings.k8s_isolation:
        # 갭6 — 유닛별 기본 차단 NetworkPolicy: ingress 컨트롤러 네임스페이스와
        # 동일 네임스페이스(사이드카·헬스체크)에서 오는 트래픽만 허용
        network_policy = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": ns, "labels": labels},
            "spec": {
                "podSelector": {"matchLabels": {
                    "app.kubernetes.io/name": spec.project_name,
                    "paas/profile": spec.profile.value,
                }},
                "policyTypes": ["Ingress"],
                "ingress": [{
                    "from": [
                        {"namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": settings.k8s_ingress_namespace,
                        }}},
                        {"namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": ns,
                        }}},
                    ],
                }],
            },
        }
        manifests.append(network_policy)

    return manifests


def namespace_manifests() -> list[dict]:
    """네임스페이스 부트스트랩(후속3) — Namespace + (설정 시) ResourceQuota·LimitRange.

    앱 유닛 매니페스트와 달리 네임스페이스당 1회만 필요한 리소스.
    GitOps 모드에서는 _namespace.yaml로 함께 커밋되고, 직접 apply 모드에서는
    첫 배포 시 함께 적용된다.
    """
    settings = get_settings()
    ns = settings.k8s_namespace
    manifests: list[dict] = [{
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": ns, "labels": {"app.kubernetes.io/managed-by": "paas"}},
    }]
    if settings.k8s_quota_cpu or settings.k8s_quota_memory:
        hard: dict[str, str] = {}
        if settings.k8s_quota_cpu:
            hard["requests.cpu"] = settings.k8s_quota_cpu
            hard["limits.cpu"] = settings.k8s_quota_cpu
        if settings.k8s_quota_memory:
            hard["requests.memory"] = settings.k8s_quota_memory
            hard["limits.memory"] = settings.k8s_quota_memory
        manifests.append({
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "paas-quota", "namespace": ns},
            "spec": {"hard": hard},
        })
        # Quota가 걸린 네임스페이스는 limits 미지정 파드가 거부되므로 기본값을 깔아준다
        manifests.append({
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": "paas-defaults", "namespace": ns},
            "spec": {"limits": [{
                "type": "Container",
                "default": {"cpu": "500m", "memory": "512Mi"},
                "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
            }]},
        })
    return manifests


def _gitops_git(cwd: Path | None, *args: str) -> None:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitOpsError(
            f"gitops git {args[0]} 실패: {(proc.stderr or proc.stdout).strip()[:500]}"
        )


class K8sRuntime(Runtime):
    def start(self, spec: RuntimeSpec) -> Endpoint:
        settings = get_settings()
        manifests = build_manifests(spec)
        if settings.k8s_gitops_repo:
            # GitOps(ArgoCD) 모드: 직접 apply 대신 매니페스트를 리포에 커밋·푸시
            self._gitops_push(spec, manifests)
        elif not self._apply(manifests):
            self._write_manifests(spec, manifests)
        # 트래픽은 Ingress가 받으므로 프록시 전환 불필요. Service 주소를 참고용으로 반환.
        return Endpoint(host=f"{spec.unit_name}.{get_settings().k8s_namespace}.svc", port=80)

    # --- GitOps(ArgoCD) 연계 ---

    def _gitops_push(self, spec: RuntimeSpec, manifests: list[dict]) -> None:
        """매니페스트를 GitOps 리포에 커밋·푸시한다.

        Secret 오브젝트는 절대 여기 포함하지 않는다 — 외부 git 리포 히스토리에
        평문(stringData) 시크릿이 영구히 남는 것을 막기 위함. Secret은
        _write_local_secrets()로 로컬 전용 파일에만 쓰고, 클러스터 적용은
        운영자가 별도 채널(kubectl apply, External Secrets Operator 등)로 수행한다.
        """
        settings = get_settings()
        git_manifests = [m for m in manifests if m.get("kind") != "Secret"]
        secret_manifests = [m for m in manifests if m.get("kind") == "Secret"]

        repo_dir = self._sync_gitops_repo()
        target_dir = repo_dir / settings.k8s_gitops_path
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "_namespace.yaml").write_text(
            yaml.safe_dump_all(namespace_manifests(), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        (target_dir / f"{spec.unit_name}.yaml").write_text(
            yaml.safe_dump_all(git_manifests, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        if secret_manifests:
            self._write_local_secrets(spec, secret_manifests)
        _gitops_git(repo_dir, "add", "-A")
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True
        )
        if not status.stdout.strip():
            return  # 동일 매니페스트 재배포 — 커밋할 변경 없음
        _gitops_git(
            repo_dir,
            "-c", "user.name=paas-bot", "-c", "user.email=paas-bot@localhost",
            "commit", "-m", f"paas: deploy {spec.unit_name} ({spec.image_tag})",
        )
        _gitops_git(repo_dir, "push", "-u", "origin", settings.k8s_gitops_branch)

    def _sync_gitops_repo(self) -> Path:
        settings = get_settings()
        repo_dir = settings.work_dir / "_gitops"
        if not (repo_dir / ".git").exists():
            import shutil  # noqa: PLC0415

            shutil.rmtree(repo_dir, ignore_errors=True)
            _gitops_git(None, "clone", settings.k8s_gitops_repo, str(repo_dir))
        _gitops_git(repo_dir, "fetch", "origin")
        remote = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", settings.k8s_gitops_branch],
            cwd=repo_dir, capture_output=True, text=True,
        )
        if remote.stdout.strip():
            _gitops_git(repo_dir, "checkout", "-B", settings.k8s_gitops_branch,
                        f"origin/{settings.k8s_gitops_branch}")
        else:
            _gitops_git(repo_dir, "checkout", "-B", settings.k8s_gitops_branch)
        return repo_dir

    def stop(self, project_name: str, profile: BuildProfile) -> None:
        spec = RuntimeSpec(project_name, "", 0, profile, "")
        api = self._apps_api()
        if api is None:
            return
        settings = get_settings()
        try:
            api.patch_namespaced_deployment_scale(
                spec.unit_name, settings.k8s_namespace, {"spec": {"replicas": 0}}
            )
        except Exception:
            pass

    def status(self, project_name: str, profile: BuildProfile) -> str:
        spec = RuntimeSpec(project_name, "", 0, profile, "")
        api = self._apps_api()
        if api is None:
            return "unknown (no cluster access)"
        try:
            dep = api.read_namespaced_deployment(spec.unit_name, get_settings().k8s_namespace)
        except Exception:
            return "stopped"
        ready = dep.status.ready_replicas or 0
        want = dep.spec.replicas or 0
        return "running" if want and ready >= want else f"progressing ({ready}/{want})"

    def logs(self, project_name: str, profile: BuildProfile, tail: int = 200) -> str:
        spec = RuntimeSpec(project_name, "", 0, profile, "")
        core = self._core_api()
        if core is None:
            return ""
        settings = get_settings()
        pods = core.list_namespaced_pod(
            settings.k8s_namespace,
            label_selector=f"app.kubernetes.io/name={project_name},paas/profile={profile.value}",
        )
        chunks = []
        for pod in pods.items:
            chunks.append(
                core.read_namespaced_pod_log(
                    pod.metadata.name, settings.k8s_namespace, tail_lines=tail
                )
            )
        return "\n---\n".join(chunks)

    # --- kubernetes 패키지는 선택 의존성: 없으면 매니페스트 파일 출력으로 대체 ---

    def _apply(self, manifests: list[dict]) -> bool:
        try:
            from kubernetes import client, config, utils  # noqa: PLC0415
        except ImportError:
            return False  # 패키지 없음 → 파일 폴백 (GitOps 경로)
        try:
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
        except Exception:
            return False  # 클러스터 설정 없음 → 파일 폴백

        # 여기부터는 클러스터에 연결된 상태 — 실패를 삼키지 않는다
        k8s = client.ApiClient()
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                for m in manifests:
                    utils.create_from_dict(k8s, m, apply=True)
                return True
            except Exception as e:  # noqa: BLE001 — 재시도 후 표면화
                last_error = e
                time.sleep(0.5 * (attempt + 1))
        raise K8sApplyError(
            f"K8s apply 실패 (3회 재시도 후): {str(last_error)[:500]}"
        )

    def _write_local_secrets(self, spec: RuntimeSpec, secret_manifests: list[dict]) -> Path:
        """Secret 매니페스트 전용 로컬 파일. git 리포(GitOps)에는 절대 커밋되지 않는다.

        운영자가 kubectl apply -f 또는 External Secrets Operator/OpenBao 연동으로
        클러스터에 직접 반영해야 한다.
        """
        out = get_settings().k8s_manifest_dir / f"{spec.unit_name}-secrets.local.yaml"
        out.write_text(
            yaml.safe_dump_all(secret_manifests, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return out

    def _write_manifests(self, spec: RuntimeSpec, manifests: list[dict]) -> Path:
        out = get_settings().k8s_manifest_dir / f"{spec.unit_name}.yaml"
        out.write_text(
            yaml.safe_dump_all(manifests, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return out

    @staticmethod
    def _apps_api():
        try:
            from kubernetes import client, config  # noqa: PLC0415
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            return client.AppsV1Api()
        except Exception:
            return None

    @staticmethod
    def _core_api():
        try:
            from kubernetes import client, config  # noqa: PLC0415
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            return client.CoreV1Api()
        except Exception:
            return None

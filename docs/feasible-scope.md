# 진행 가능 목록 — paas

> 단독 구현 세션에서 **지금 착수해 검증까지 끝낼 수 있는 것**과 **막혀 있는 것**을 가른다.
> **기준일:** 2026-08-10
> **근거:** [총괄 목록](https://github.com/hs9147/chofam/blob/main/docs/feasible-scope.md) · [생태계 위치](ecosystem-position.md)

---

## 결론 — **범위 밖. 진행 가능 항목 없음**

paas는 **환경 C(별건)**이고, [1차 구현 계획](https://github.com/hs9147/chofam/blob/main/docs/implementation-plan-build.md)의 **어느 Phase에도 paas 작업이 없다.**

| 환경 | 범위 | 1차 구현 |
|---|---|---|
| A | a_guild 단독 개발·테스트 | 아님 |
| B | society 계열 (mentor·ethos·slime·society·terra) | **목표** |
| **C** | **paas — 별건, 자체 호스트** | **아님** |

**배포 플랫폼 역할 요구는 철회됐다.** 다시 입력이 필요해지는 시점은 다수 호스트 운영이 필요해질 때다.

---

## 검증 기반 — 실행하지 않았다

**이번 라운드에서 paas 테스트는 돌리지 않았다.** 범위 밖이라 검증할 변경이 없기 때문이며, **통과 여부를 확인했다는 뜻이 아니다.**

paas는 이 세션에서 검증한 6개 저장소와 스택이 다르다(Python·Alembic). 활발히 개발되고 있으나 **그 진행은 1차 계획과 무관하게 독립적으로 간다.**

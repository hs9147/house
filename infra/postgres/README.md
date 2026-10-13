# 공용 DB (PostgreSQL)

Gitea처럼 **플랫폼과 같은 서버에서 도는 외부 솔루션**이다. 플랫폼은 이 서버에 앱마다 전용 DB와
계정을 만들어 주고(`app/services/shareddb.py`), 여러 앱이 함께 읽는 공용 자료 DB(`paas_common`)를
둔다. PostgreSQL License(MIT 계열)라 사내·상용 사용에 제약이 없다.

| 무엇 | 누가 만드나 | 권한 |
| --- | --- | --- |
| 앱 전용 DB `app_{프로젝트}` / 개발용 `app_{프로젝트}_dev` | 플랫폼(배포할 때) | 그 앱 계정만 접속·소유 |
| 공용 자료 DB `paas_common` | 플랫폼(처음 쓸 때) | 모든 앱 **읽기만**, 관리자가 맡긴 앱의 운영 계정만 쓰기 |
| 관리 계정 `paas_admin` | 사람(아래 3절) | CREATEDB·CREATEROLE — **슈퍼유저가 아니다** |

프로젝트를 지워도 DB는 **지우지 않는다**(데이터는 되돌릴 수 없다). 남은 DB는
`GET /paas/api/v1/shared-db`에서 `orphan: true`로 보인다 — 지울지는 사람이 정한다(5절).

## 1. 설치 (Windows 서버)

1. [EDB 설치본](https://www.postgresql.org/download/windows/)에서 PostgreSQL 17(이상)을 받아 설치한다.
   - 서비스로 등록된다(`postgresql-x64-17`). 포트는 기본 5432.
   - 설치 중에 정하는 `postgres`(슈퍼유저) 비밀번호는 **이 설치에만** 쓴다 — 플랫폼에 주지 않는다.
   - Stack Builder는 필요 없다.
2. 데이터 폴더(기본 `C:\Program Files\PostgreSQL\17\data`)를 백업 대상 디스크로 옮기려면 설치할 때
   Data Directory를 바꾼다(예: `D:\pgdata`).

## 2. 외부 접속 막기 — 같은 서버에서만

플랫폼과 앱이 모두 이 서버에서 돈다. 밖에서 붙을 이유가 없다.

- `postgresql.conf`: `listen_addresses = 'localhost'`
- `pg_hba.conf`: 아래 두 줄만 남긴다(나머지 `host` 줄은 지운다).

  ```
  host    all    all    127.0.0.1/32    scram-sha-256
  host    all    all    ::1/128         scram-sha-256
  ```
- 방화벽에서 5432를 열지 않는다.
- 바꾼 뒤 `Restart-Service postgresql-x64-17`.

## 3. 관리 계정

`psql -U postgres`(설치 폴더 `bin\psql.exe`)로 접속해 한 번만 실행한다.

```sql
CREATE ROLE paas_admin LOGIN PASSWORD '긴-무작위-비밀번호' CREATEDB CREATEROLE;
-- PostgreSQL 16+: 이 계정이 만든 계정을 이 계정이 "될" 수 있게 — 그래야 그 계정 소유의 DB를
-- 만들고(OWNER), 백업·스키마 요약 때 읽는다. 플랫폼도 만들 때마다 GRANT하지만 기본값으로 둔다.
ALTER ROLE paas_admin SET createrole_self_grant = 'set, inherit';
```

플랫폼 `.env`:

```
PAAS_SHARED_DB_ADMIN_URL=postgresql+psycopg://paas_admin:긴-무작위-비밀번호@127.0.0.1:5432/postgres
PAAS_PG_DUMP_PATH=C:\Program Files\PostgreSQL\17\bin\pg_dump.exe
# 선택: 백업 자리(비우면 PAAS_WORK_DIR\db-backups), DB마다 남길 개수(기본 7)
# PAAS_SHARED_DB_BACKUP_DIR=D:\gp\db-backups
# PAAS_SHARED_DB_BACKUP_KEEP=7
```

비밀번호에 `@ : / %`가 있으면 URL 인코딩한다(`@` → `%40`). 「SW 업데이트」가 드라이버
(`psycopg[binary]`, requirements.txt)를 설치하고, 재시작하면 「공용 DB」 모듈이 생긴다.

## 4. 쓰는 법

1. 기획 에이전트의 「솔루션 구성」에서 「공용 DB」(`shared-db`)를 바인딩한다 — 모델이 가용 자원과
   사용 규약, 공용 자료 DB에 이미 있는 표를 보고 고른다.
2. 배포하면 플랫폼이 그 프로필의 DB·계정을 만들고 `{PREFIX}_DSN`(예: `DB_DSN`)을 넣는다.
   앱은 기동할 때 마이그레이션으로 표를 만든다.
3. 공용 자료는 같은 계정으로 DSN의 DB 이름만 `paas_common`으로 바꿔 읽는다.
4. 공용 자료를 **채울** 앱이 생기면 관리자가 쓰기를 준다(운영 계정에만):
   `POST /paas/api/v1/projects/{id}/shared-db/common-write {"on": true}` — 그 앱이 만든 표는
   모든 앱이 읽을 수 있게 된다.
5. 에이전트(스마트워크·외주 빌더)는 DB 조회 MCP `/paas/mcp/db/shared-db`로 공용 자료를 읽는다
   (SELECT 전용, 읽기 계정). 앱 전용 DB는 여기서 열리지 않는다.

## 5. 백업과 정리

- 플랫폼 스케줄러가 DB마다 하루 한 번 `pg_dump -Fc`로 백업한다(작업 이름 `db_backup:{DB}`,
  대시보드의 스케줄러 표에서 결과를 본다). 최근 N개만 남긴다.
- 복원: `pg_restore -U postgres -d {DB} --clean {파일}.dump`
- 지운 프로젝트의 DB를 정말 지울 때(되돌릴 수 없다 — 먼저 백업 파일을 확인한다):

  ```sql
  DROP DATABASE "app_이름";
  DROP ROLE "app_이름";
  ```
  플랫폼의 `shared_databases` 행도 함께 지운다(남아 있으면 백업 작업이 계속 실패로 뜬다).

## 6. 점검 — 설치 뒤 한 번

```powershell
# 1) 밖에서 안 붙는지(다른 PC에서): Test-NetConnection {서버} -Port 5432  → 실패해야 맞다
# 2) 관리 계정이 슈퍼유저가 아닌지
& "C:\Program Files\PostgreSQL\17\bin\psql.exe" -U paas_admin -h 127.0.0.1 -d postgres -c "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
#    → f | t | t
```

3) 시험 프로젝트에 「공용 DB」를 바인딩해 배포하고, 앱 계정으로 **다른 앱의 DB에 붙지 못하는지**
확인한다(`psql -U app_a -d app_b` → `permission denied for database`).

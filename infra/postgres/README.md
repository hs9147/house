# 공용 DB (PostgreSQL)

Gitea처럼 **플랫폼과 같은 서버에서 도는 외부 솔루션**이다. 플랫폼은 이 서버에 자료 DB 하나
(`paas_data`)를 두고, 「공용 DB」를 바인딩한 과제마다 자기 스키마와 계정을 만든다
(`app/services/shareddb.py`). PostgreSQL License(MIT 계열)라 사내·상용 사용에 제약이 없다.

| 무엇 | 누가 만드나 | 권한 |
| --- | --- | --- |
| 과제 스키마 `{과제}`(운영) / `{과제}_dev`(개발) | 플랫폼(배포할 때) | 그 과제 계정이 주인 |
| 같은 **공유 단위** 과제의 운영 표 | — | **기본으로 읽기** |
| 공유 단위 밖 과제의 표 | 관리자가 표 단위로 준다 | 읽기 또는 읽기·쓰기, 만료일. 그 전에는 스키마에 **들어가지도 못한다** |
| 관리 계정 `paas_admin` | 사람(아래 4절) | CREATEDB·CREATEROLE — **슈퍼유저가 아니다** |

- **공유 단위**는 기본이 자기 부서다. 관리자가 콘솔의 조직 관리 → 「공용 DB 공유 단위」에서 부서마다
  "함께 쓰는 부서"(예: 상위 부서)를 고르면 그 부서와 같은 단위가 된다 — 데이터는 옮기지 않고 권한만 바뀐다.
- **분리**: 한 DB에서는 다른 부서의 표 **이름**(데이터가 아니라)이 카탈로그로 보인다. 그것도 안 되는
  공유 단위는 「분리」를 켠다 — 그 단위만 자기 DB(`paas_org_{번호}`)를 쓰고, 그 DB에는 그 단위만 접속한다.
  대가로 다른 단위의 표와 묶어 쓸 수 없다. **자료가 생긴 뒤에는 분리를 켜고 끌 수 없다**(6절의 수동 이전).
- 과제 계정마다 연결 수(기본 10)와 질의 시간(기본 60초)에 상한이 있다 — 여러 부서가 같은 서버를 쓴다
  (`PAAS_SHARED_DB_CONNECTION_LIMIT`, `PAAS_SHARED_DB_STATEMENT_TIMEOUT`).
- 개발 계정은 자기 개발 스키마에만 쓰고, 다른 표는 쓰기 권한을 받았어도 **읽기만** 한다.
- 권한의 원천은 플랫폼의 표(공유 단위 `org_data_policies`, 표 권한 `data_grants`)다. 플랫폼이 한 시간마다
  DB를 그 표에 맞춘다 — 손으로 준 권한과 만료된 권한은 거둔다.
- 과제를 지워도 스키마는 **지우지 않는다**(데이터는 되돌릴 수 없다).
  `GET /paas/api/v1/shared-db`에서 `orphan: true`로 보인다(5절).

## 1. 버전 — Windows Server 2016이면 15

EDB 설치본의 시험 대상([postgresql.org/download/windows](https://www.postgresql.org/download/windows/)):

| 버전 | 시험된 Windows | 지원 종료 |
| --- | --- | --- |
| 18 | 2025, 2022 | 2030-11 |
| 17 | 2022, 2019 | 2029-11 |
| 16 | 2022, 2019 | 2028-11 |
| **15** | **2019, 2016** | **2027-11-11** |
| 14 | 2019, 2016 | 2026-11-12 |

**Windows Server 2016에는 15를 설치한다**(2016에서 시험된 가장 새 버전). 14는 곧 지원이 끝난다.
서버를 2019 이상으로 올리면 그때 17로 옮긴다(`pg_dump` → 새 버전에 `pg_restore`, 5절).
플랫폼이 쓰는 SQL은 15에서 모두 동작한다.

## 2. 설치

1. [EDB 설치본](https://www.enterprisedb.com/downloads/postgres-postgresql-downloads)에서 PostgreSQL
   **15.x**(Windows x86-64)를 받아 설치한다. 설치 화면에서 정할 것:

   | 화면 | 값 |
   | --- | --- |
   | Installation Directory | `C:\tools\PostgreSQL\15` |
   | Select Components | PostgreSQL Server, Command Line Tools만. **Stack Builder는 끈다**, pgAdmin은 선택 |
   | Data Directory | `C:\tools\PostgreSQL\15\data` |
   | Password | `postgres`(슈퍼유저) 비밀번호 — **이 설치에만** 쓴다, 플랫폼에 주지 않는다 |
   | Port | 5432 |
   | Locale | Default locale |

   - **Stack Builder는 설치하지 않는다.** PostGIS·드라이버 같은 부가 기능을 내려받는 도구인데, 플랫폼은
     본체만 쓰고 드라이버(`psycopg`)는 파이썬 쪽에서 설치한다. 설치 마지막의 "Launch Stack Builder"
     체크도 끄고 마친다.
   - 서비스로 등록된다(`postgresql-x64-15`). 데이터 폴더는 설치본이 서비스 계정(NETWORK SERVICE)에
     권한을 줘서 만든다 — 나중에 다른 곳으로 옮기면 그 권한도 함께 줘야 서비스가 뜬다.
   - 실행 파일은 `C:\tools\PostgreSQL\15\bin`(`psql.exe`·`pg_dump.exe`·`pg_restore.exe`)에 있다.

## 3. 외부 접속 막기 — 같은 서버에서만

- `C:\tools\PostgreSQL\15\data\postgresql.conf`: `listen_addresses = 'localhost'`
- `C:\tools\PostgreSQL\15\data\pg_hba.conf`: 아래 두 줄만 남긴다(나머지 `host` 줄은 지운다).

  ```
  host    all    all    127.0.0.1/32    scram-sha-256
  host    all    all    ::1/128         scram-sha-256
  ```
- 방화벽에서 5432를 열지 않는다. 바꾼 뒤 `Restart-Service postgresql-x64-15`.

## 4. 관리 계정과 플랫폼 설정

`C:\tools\PostgreSQL\15\bin\psql.exe -U postgres -h 127.0.0.1`로 접속해 한 번만 실행한다.

```sql
CREATE ROLE paas_admin LOGIN PASSWORD '긴-무작위-비밀번호' CREATEDB CREATEROLE;
```

(15는 CREATEROLE 계정이 슈퍼유저가 아닌 계정을 자유롭게 GRANT할 수 있다. 플랫폼은 만든 앱 계정을
매번 자신에게 GRANT해, 그 계정 소유의 스키마를 만들고 백업·대조 때 읽는다.)

플랫폼 `.env`:

```
PAAS_SHARED_DB_ADMIN_URL=postgresql+psycopg://paas_admin:긴-무작위-비밀번호@127.0.0.1:5432/postgres
PAAS_PG_DUMP_PATH=C:\tools\PostgreSQL\15\bin\pg_dump.exe
# 선택: 백업 자리(비우면 PAAS_WORK_DIR\db-backups), 남길 개수(기본 7)
# PAAS_SHARED_DB_BACKUP_DIR=D:\gp\db-backups
# PAAS_SHARED_DB_BACKUP_KEEP=7
```

비밀번호에 `@ : / %`가 있으면 URL 인코딩한다(`@` → `%40`). 「SW 업데이트」가 드라이버
(`psycopg[binary]`)를 설치하고, 재시작하면 「공용 DB」 모듈(`shared-db`)이 생긴다.

## 5. 쓰는 법

0. (처음 한 번) 관리자가 조직 관리 → 「공용 DB 공유 단위」에서 부서마다 함께 쓰는 부서와 분리 여부를
   정한다. **분리는 그 부서에 자료가 생기기 전에** 정한다.
1. 에이전트 기획의 「솔루션 구성」에서 「공용 DB」를 바인딩한다. 기획 에이전트는 사용 규약과 함께,
   그 과제가 읽을 수 있는 표(열까지)와 공유 단위 밖의 표(이름·설명만 — 신청 대상)를 본다. 개발원칙
   단계에는 DB 사용 원칙이 붙는다.
2. 배포하면 플랫폼이 스키마·계정을 만들고 `{PREFIX}_DSN`(예: `DB_DSN`)을 넣는다. 검색 경로가 자기
   스키마로 잡혀 있어 앱은 표 이름만 쓴다. 마이그레이션이 표를 만들 때 `COMMENT ON TABLE`로 설명을
   단다 — 다른 과제가 그 설명을 보고 쓴다.
3. 같은 공유 단위 과제의 표는 `스키마.표`로 바로 읽는다.
4. 공유 단위 밖의 과제에 표를 줄 때는 프로젝트 개요의 「공용 DB」 패널에서 표마다 「다른 과제에 주기」
   (관리자) — 읽기/읽기·쓰기, 만료일. 분리된 단위의 표는 밖으로 줄 수 없다.

## 6. 백업·복원·정리

- 플랫폼 스케줄러가 자료 DB마다 하루 한 번 `pg_dump -Fc`를 남긴다(작업 `db_backup:paas_data`,
  분리된 단위는 `db_backup:paas_org_{번호}`). 최근 N개만 남긴다. 권한 대조는 `db_grants`(한 시간마다) —
  DB가 바뀌어야 하는 과제가 있으면 이 작업이 실패로 뜬다(아래 수동 이전).
- 복원: `C:\tools\PostgreSQL\15\bin\pg_restore.exe -U postgres -h 127.0.0.1 -d paas_data --clean {파일}.dump`
- 과제 하나만 복원: `pg_restore … -n {스키마} …`(다른 과제의 표는 건드리지 않는다).
- **자료가 있는 부서를 다른 DB로 옮길 때**(분리를 켜고 끄거나, 분리된 단위로 옮기거나 빼낼 때 — 플랫폼은
  거부한다). 서비스를 멈춘 시간에 한다:

  ```powershell
  # 1) 옮길 과제 스키마를 뜬다(운영·개발 각각)
  & "C:\tools\PostgreSQL\15\bin\pg_dump.exe" -U postgres -h 127.0.0.1 -Fc -n {스키마} -f {스키마}.dump {옛 DB}
  # 2) 새 DB에 복원한다(새 DB가 없으면 먼저 CREATE DATABASE)
  & "C:\tools\PostgreSQL\15\bin\pg_restore.exe" -U postgres -h 127.0.0.1 -d {새 DB} {스키마}.dump
  # 3) 확인한 뒤 옛 DB의 스키마를 지운다: DROP SCHEMA "{스키마}" CASCADE;
  ```
  4) 플랫폼 DB의 `data_schemas.db_name`을 새 DB로 고치고, 콘솔에서 공유 단위 설정을 바꾼 뒤 그 과제를
  다시 배포한다(접속 주소의 DB 이름이 바뀐다).
- 지운 과제의 스키마를 정말 지울 때(되돌릴 수 없다 — 먼저 백업을 확인한다):

  ```sql
  DROP SCHEMA "이름" CASCADE;
  DROP ROLE "app_이름";
  ```
  플랫폼의 `data_schemas` 행도 함께 지운다.

## 7. 점검 — 설치 뒤 한 번

```powershell
# 1) 밖에서 안 붙는지(다른 PC에서): Test-NetConnection {서버} -Port 5432  → 실패해야 맞다
# 2) 관리 계정이 슈퍼유저가 아닌지
& "C:\tools\PostgreSQL\15\bin\psql.exe" -U paas_admin -h 127.0.0.1 -d postgres -c "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
#    → f | t | t
```

3) 서로 다른 공유 단위의 시험 과제 둘에 「공용 DB」를 바인딩해 배포한 뒤, 한쪽 계정으로 다른 쪽 표를
읽으면 `permission denied for schema`가 나야 한다. 같은 공유 단위 과제끼리는 읽혀야 한다.
4) 분리한 단위가 있으면, 다른 단위의 계정으로 그 DB에 접속하면 `permission denied for database`가
나야 한다.

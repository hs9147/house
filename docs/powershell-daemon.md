# PowerShell 상주 데몬 — 실행·사용 가이드

관리자용 PowerShell 실행을 **명령마다 `powershell.exe`를 새로 띄우던 방식**에서 **장수(long-lived)
상주 데몬**으로 분리했다. 이 문서는 데몬의 동작·실행·설정·문제해결을 설명한다.

- 서비스 구현: `app/services/powershell_daemon.py`
- 엔드포인트: `app/api/system.py` (`/system/powershell/exec`, `/system/powershell/ws`)
- 콘솔 화면: `console/src/pages/PowerShellConsole.tsx` (좌측 메뉴 **PowerShell**)

## 1. 왜 분리했나

기존에는 `/exec`·`/ws`가 명령마다 `subprocess.run(["powershell.exe", ...])`으로 프로세스를 새로
띄웠다. 그래서:

- **세션 상태가 유지되지 않았다** — `cd`, 변수(`$x=1`), `Import-Module`이 다음 명령에 남지 않음.
- WebSocket 터미널이 **동기 `subprocess.run`**이라 명령 실행 30초 동안 **백엔드 이벤트 루프를 블로킹**했다.

상주 데몬은 프로세스를 하나 띄워 재사용하므로 **세션이 유지**되고, 실행을 워커 스레드로 돌려
**이벤트 루프를 블로킹하지 않는다**.

## 2. 동작 방식

```
[요청] ──명령──▶ PowerShellDaemon
                   │ stdin: "<명령>\n"  +  "Write-Output '<sentinel> $LASTEXITCODE'\n"
                   ▼
             powershell.exe -NoProfile -NoLogo   (장수 프로세스, 파이프 stdin REPL)
                   │ stdout(+stderr)
                   ▼
             리더 스레드 ──라인──▶ Queue
                   ▲
[응답] ◀─ run(): sentinel 줄이 나올 때까지 Queue에서 라인 수집 → 출력·종료코드
```

- **경계 판정**: 명령 뒤에 고유 sentinel(`__PAAS_PS_DONE_<uuid>__ $LASTEXITCODE`)을 출력시켜,
  그 명령의 출력이 어디서 끝나는지 안다. 같은 프로세스라 세션 상태가 유지된다.
- **에코 필터**: 파이프 stdin에서 PowerShell REPL은 입력을 프롬프트와 함께 에코한다
  (`PS D:\proj> ...`). sentinel은 **줄 시작에서만** 인식하고 프롬프트/에코 줄은 버려, 출력이
  다음 명령으로 밀리는 desync를 막는다.
- **타임아웃**: 기본 30초. 초과 시 `TimeoutError` → API는 504로 응답한다.
- **직렬화**: 데몬당 한 번에 한 명령(`_lock`).

## 3. 두 가지 데몬 스코프

| 용도 | 엔드포인트 | 데몬 | 세션 범위 |
| --- | --- | --- | --- |
| 단발 실행 / 콘솔 명령 | `POST /system/powershell/exec` | **공유 데몬**(프로세스 전역) | 호출 간 세션 유지 |
| 실시간 터미널 | `WebSocket /system/powershell/ws` | **연결별 데몬** | 그 연결 동안 세션 유지, 종료 시 정리 |

콘솔의 **PowerShell** 탭은 `/exec`(공유 데몬)를 쓰므로, 콘솔에서 친 명령들 사이에
`cd`·변수가 유지된다.

## 4. 실행·사용

### 4.1 콘솔에서 (권장)

1. 좌측 메뉴 **PowerShell** 진입(관리자 전용).
2. **연결(Connect)** 후 프롬프트에 명령 입력. 세션이 유지되므로 예:
   ```
   PS > $env:MYVAR = 'hello'
   PS > Write-Output $env:MYVAR      # → hello
   PS > cd C:\; Get-Location         # 이후 명령도 C:\ 기준
   ```

### 4.2 REST API

```bash
curl -X POST https://<플랫폼>/paas/api/v1/system/powershell/exec \
  -H "x-api-key: <admin-key>" -H "content-type: application/json" \
  -d '{"command": "Get-Process | Select-Object -First 3"}'
# → {"command": "...", "returncode": 0, "output": "...", "cwd": "..."}
```

- admin 키 필요. 반환: `returncode`(네이티브 명령의 `$LASTEXITCODE`, 순수 cmdlet이면 null일 수 있음),
  `output`(에코·프롬프트가 걸러진 명령 출력).

### 4.3 WebSocket 터미널

`WebSocket /paas/api/v1/system/powershell/ws` — 텍스트로 명령 전송, 텍스트로 결과 수신.
`exit`/`quit`로 세션 종료. 연결이 끊기면 그 연결의 데몬은 자동 정리된다.

## 5. 설정

| 설정 | 환경변수 | 기본값 | 설명 |
| --- | --- | --- | --- |
| 시작 디렉터리 | `PAAS_POWERSHELL_START_DIR` | (빈 값=프로세스 CWD) | 데몬이 기동할 작업 디렉터리 |
| 실행기 | — (`powershell_daemon.POWERSHELL_EXE`) | `powershell.exe` | 분리의 단일 지점. 필요 시 이 상수에서 조정 |

## 6. 수명주기

- **지연 기동**: 공유 데몬은 첫 `/exec` 호출 때 뜨고, 죽어 있으면 다음 호출에서 재기동된다.
- **정리**: 프로세스 종료 시 `atexit`로 공유 데몬을 정리한다(`app/main.py`). 연결별 데몬은
  WebSocket 종료 시 정리된다.
- **프로세스 사멸**: 데몬 프로세스가 죽으면 리더 스레드가 EOF를 큐에 넣어 `run()`이 그 상태를
  감지하고, 공유 데몬은 다음 호출에서 다시 뜬다.

## 7. 플랫폼별 주의

- Windows 전용 `powershell.exe`를 사용한다. `powershell.exe`가 없는 환경(비-Windows)에서는
  데몬 기동이 실패하며, 관련 자동화 테스트(`tests/test_powershell_daemon.py`)는 자동으로 skip된다.
- 크로스플랫폼(`pwsh`)이 필요하면 `powershell_daemon.POWERSHELL_EXE`를 바꾸면 된다(분리의 단일 지점).

## 8. 문제해결

| 증상 | 원인·조치 |
| --- | --- |
| 504 timeout | 명령이 30초를 초과. 장기 작업은 잘게 나누거나 백그라운드 작업으로 실행 |
| 출력이 비어 있음 | 정상 완료지만 출력이 없는 명령 — 콘솔/WS는 `(completed)`로 표시 |
| 세션이 초기화됨 | 데몬 프로세스가 죽었다가 재기동된 경우(공유 데몬). 변수/CWD는 재설정된다 |
| `powershell.exe 실행 실패` | 비-Windows이거나 PATH에 없음 — §7 참고 |

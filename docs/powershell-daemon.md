# PowerShell 상주 데몬 — 실행·사용 가이드

관리자용 PowerShell 실행을 **명령마다 `powershell.exe`를 새로 띄우던 방식**에서 **장수(long-lived)
상주 데몬**으로 분리했다. 이 문서는 데몬의 동작·실행·설정·문제해결을 설명한다.

- 서비스 구현: `app/services/powershell_daemon.py`, `app/services/ps_broker.py`
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

`/ws`(연결별 데몬)는 paas가 powershell.exe를 직접 물고 있고, `/exec`(공유 데몬)는 paas와
분리된 **브로커 프로세스**(`ps_broker.py`)가 powershell.exe를 물고 있다 — paas가 재시작해도
`/exec`의 세션이 죽지 않아야 하기 때문이다(§6.5).

**`/ws` — paas가 직접 파이프로 문다:**

```
[웹소켓] ──명령──▶ PowerShellDaemon
                   │ stdin: "<명령>\n"  +  "Write-Output '<sentinel> $LASTEXITCODE'\n"
                   ▼
             powershell.exe -NoProfile -NoLogo   (장수 프로세스, 파이프 stdin REPL)
                   │ stdout(+stderr)
                   ▼
             리더 스레드 ──라인──▶ Queue
                   ▲
[응답] ◀─ run(): sentinel 줄이 나올 때까지 Queue에서 라인 수집 → 출력·종료코드
```

**`/exec` — paas는 로컬 TCP로 독립 브로커에 붙는다:**

```
paas(BrokeredPowerShellDaemon) ──TCP(127.0.0.1:PAAS_PS_BROKER_PORT)──▶ ps_broker.py(독립 프로세스)
                                                                              │ stdin/stdout PIPE
                                                                              ▼
                                                                    powershell.exe (장수 프로세스)
```

브로커는 명령 프로토콜을 모른다 — 소켓과 powershell.exe의 파이프 사이를 그대로 중계할 뿐이다.
paas가 재시작해 새 `BrokeredPowerShellDaemon`을 만들어도 같은 고정 포트로 다시 연결되므로,
브로커가 물고 있는 powershell.exe의 세션(cd·변수)이 그대로 이어진다. sentinel 판정·에코 필터는
paas 쪽(클라이언트)에서 하므로 `/ws`와 동일하다.

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
| 단발 실행 / 콘솔 명령 | `POST /system/powershell/exec` | **공유 데몬**(프로세스 전역, 독립 브로커 경유) | 호출 간·**paas 재시작 간**에도 세션 유지 |
| 실시간 터미널 | `WebSocket /system/powershell/ws` | **연결별 데몬**(paas가 직접 소유) | 그 연결 동안 세션 유지, 종료 시 정리 |

콘솔의 **PowerShell** 탭은 `/exec`(공유 데몬)를 쓰므로, 콘솔에서 친 명령들 사이에
`cd`·변수가 유지되고, 그 사이 paas가 재시작돼도(SW 업데이트 등) 세션은 끊기지 않는다.
`/ws`는 웹소켓 연결 자체가 이미 paas의 생사에 묶여 있으므로 같은 보장이 필요 없다 — 연결이
끊기면 사용자가 다시 접속해야 하는 것은 `/ws`나 브라우저 탭이 새로고침된 것과 마찬가지다.

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

### 4.3 WebSocket 터미널 (PTY)

`WebSocket /paas/api/v1/system/powershell/ws` — 셸을 **의사 터미널(PTY)**에 붙여 키 입력과
화면 출력을 바이트로 그대로 중계한다(`services/pty_terminal.py`). 콘솔의 "터미널" 탭이
xterm.js로 이 소켓에 붙는다.

예전에는 명령 한 줄을 받아 끝날 때까지 기다렸다가 출력을 통째로 돌려주는 방식이었다.
그래서 **되묻는 명령**(`Read-Host`, `git commit`, `python` REPL)이 그대로 멈추고,
**Ctrl+C**가 없고, **30초를 넘는 작업**은 진행 상황을 볼 수 없었다. PTY로 바꾸면 셋 다
풀린다.

**인증은 accept 전에 끝낸다.** 이 엔드포인트는 관리자 셸을 그대로 내주므로, 판정은 REST와
같은 경로(`security.resolve_token`)를 쓰고 관리자 키만 통과시킨다. 브라우저는 WebSocket
핸드셰이크에 임의 헤더를 붙일 수 없어 키를 **서브프로토콜**로 싣는다 — 쿼리스트링으로
보내면 IIS/ARR 접근 로그에 관리자 키가 그대로 남는다.

```js
new WebSocket(url, ['paas-terminal', 'paas-key.' + key])   // 서버는 'paas-terminal'을 되돌려 준다
```

프로토콜: **보낼 때** JSON — `{"type":"input","data":"ls\r"}` / `{"type":"resize","cols":120,"rows":30}`.
**받을 때**는 터미널 출력 그대로. 키 입력에는 어떤 바이트든 올 수 있어 구분자를 둘 자리가
없으므로 보내는 쪽만 감싼다. 규약에 없는 프레임은 셸로 흘려보내지 않고 버린다.

**백엔드.** 윈도우는 `pywinpty`(선택 의존성), POSIX는 표준 라이브러리 `pty`를 쓴다.
pywinpty 휠에는 ConPTY와 winpty 백엔드가 모두 들어 있는데 **ConPTY는 Windows 10 1809 /
Server 2019부터**다 — Server 2016이면 `PAAS_PTY_BACKEND=winpty`로 못 박아야 할 수 있다.
백엔드를 못 열면 빈 화면을 남기지 않고 무엇을 설치하면 되는지 터미널에 찍어 준다.

> **IIS/ARR 뒤에서는 WebSocket 통과가 전제다.** IIS에 "WebSocket Protocol" 기능
> (`Install-WindowsFeature Web-WebSockets`)이 설치돼 있어야 하고 앱풀이 통합 모드여야 한다.
> 안 되면 터미널 탭만 연결에 실패하고 나머지 기능은 그대로 동작한다.

## 5. 설정

| 설정 | 환경변수 | 기본값 | 설명 |
| --- | --- | --- | --- |
| 시작 디렉터리 | `PAAS_POWERSHELL_START_DIR` | (빈 값=프로세스 CWD) | 데몬이 기동할 작업 디렉터리 |
| 브로커 포트 | `PAAS_PS_BROKER_PORT` | `47231` | `/exec` 공유 데몬이 붙는 로컬 TCP 포트. paas가 재시작해도 이 고정 포트로 다시 붙어 세션을 잇는다 |
| 실행기 | — (`powershell_daemon.POWERSHELL_EXE`) | `powershell.exe` | 분리의 단일 지점. 필요 시 이 상수에서 조정 |
| 터미널 셸 | `PAAS_PTY_SHELL` | `powershell.exe` | "터미널" 탭이 띄울 셸(`pwsh.exe`·`cmd.exe`로 교체 가능) |
| PTY 백엔드 | `PAAS_PTY_BACKEND` | (빈 값=자동) | `conpty` \| `winpty`. Server 2016은 ConPTY가 없어 `winpty` |

## 6. 수명주기

- **지연 기동**: 공유 데몬(브로커 클라이언트)은 첫 `/exec` 호출 때 브로커에 연결을 시도하고,
  브로커가 없으면(첫 실행이거나 아무도 재연결하지 않아 §6.5의 idle timeout으로 정리된 뒤) 새로
  띄운 뒤 연결한다.
- **paas 종료 시 정리 범위**: `atexit`(`app/main.py`)가 `shutdown_shared()`를 부르지만, 이제
  **이 paas 프로세스의 브로커 연결만 닫는다** — 브로커와 그 안의 powershell.exe 세션은 그대로
  둔다. 다음(재시작된) paas가 `/exec`를 처음 호출하면 같은 포트로 다시 붙어 세션이 이어진다.
  연결별 데몬(`/ws`)은 여전히 웹소켓 종료 시 그 자리에서 정리된다(powershell.exe에 "exit"를
  보내고 종료를 기다림).
- **브로커 자체의 정리**: 아무도 재연결하지 않고 `ps_broker.IDLE_TIMEOUT_SECONDS`(기본 30분)가
  지나면 브로커가 powershell.exe와 함께 스스로 종료한다(영구 orphan 방지). 명시적으로 지금
  끄고 싶다면 `powershell_daemon.kill_broker(port)`(내부 유틸 — "exit"를 흘려보내 powershell.exe를
  끝내면 브로커도 뒤따라 종료한다).
- **powershell.exe 사멸**: 브로커가 물고 있는 powershell.exe가 죽으면(비정상 종료 등) 브로커도
  뒤따라 정리·종료하고, 공유 데몬은 다음 `/exec` 호출에서 새 브로커를 다시 띄운다 — 이 경우
  세션(변수·cd)은 물론 새로 시작된다.

## 6.5 paas와의 프로세스 분리 · self-kill 방지 · `/exec` 세션의 생존

재시작 작업과 `/exec`의 브로커는 **paas 프로세스와 분리된 독립 프로세스**로 띄운다. Windows에서
paas가 nssm 등으로 **Job Object**에 묶여 있으면, paas 서비스가 stop/restart될 때 그 Job의 하위
프로세스가 함께 종료된다. 그 프로세스가 같은 Job 안에 있으면 paas가 자기 자신을 재시작하는
순간 재시작을 수행하던 프로세스까지 죽는 **self-kill**이 발생한다.

이를 막기 위해 프로세스를 생성할 때 `CREATE_BREAKAWAY_FROM_JOB`(+ `DETACHED_PROCESS` 또는
`CREATE_NO_WINDOW`, `CREATE_NEW_PROCESS_GROUP`)로 **Job에서 breakaway**시킨다. 관련 상수·헬퍼는
`app/services/powershell_daemon.py`의 `_creation_flags()`에 모여 있다(분리의 단일 지점).

- **자기 재시작 / SW 업데이트**(`run_detached_script`): `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP |
  CREATE_BREAKAWAY_FROM_JOB`로 fire-and-forget 실행. paas 프로세스가 내려가도 `git pull` ·
  포트 해제 · `Restart-Service`가 **끝까지 진행**된다. `/system/restart`·`/system/sw-update`가 이 경로를 쓴다.
- **`/exec`의 브로커**(`_spawn_broker`): 같은 방식(breakaway, paas와 stdin/stdout/stderr 미공유)으로
  `ps_broker.py`를 독립 프로세스로 띄운다. paas가 죽어도(정상/강제 종료 모두) 브로커와 그 안의
  powershell.exe는 살아남는다.
- Job이 breakaway를 불허하면(`JOB_OBJECT_LIMIT_BREAKAWAY_OK` 미설정) 생성이 `OSError`로 실패하므로,
  플래그를 빼고 자동 재시도한다(비-Windows는 플래그 0).

**breakaway만으로는 부족했던 이유** — `/ws`가 쓰는 `PowerShellDaemon`처럼 paas가 powershell.exe의
stdin/stdout을 **직접 PIPE로** 물면, breakaway로 강제 종료 캐스케이드를 막아도 소용없다. 그 파이프의
쓰기측 핸들을 paas가 들고 있으므로, paas 프로세스가 죽으면(정상 종료든 강제 종료든) OS가 그
핸들을 닫고 powershell.exe는 표준입력 EOF로 스스로 끝난다 — Job과 무관한 별개 경로다. `/exec`가
이 문제를 피하는 방법은 애초에 paas가 그 파이프를 직접 물지 않는 것이다: 파이프는 독립
브로커(`ps_broker.py`)가 갖고, paas는 로컬 TCP 클라이언트(`BrokeredPowerShellDaemon`)로 붙을 뿐이다
— paas가 사라져도 브로커가 쥔 파이프는 안 끊긴다.

## 7. 플랫폼별 주의

- Windows 전용 `powershell.exe`를 사용한다. `powershell.exe`가 없는 환경(비-Windows)에서는
  데몬 기동이 실패하며, 관련 자동화 테스트(`tests/test_powershell_daemon.py`)는 자동으로 skip된다.
- 크로스플랫폼(`pwsh`)이 필요하면 `powershell_daemon.POWERSHELL_EXE`를 바꾸면 된다(분리의 단일 지점).

## 8. 문제해결

| 증상 | 원인·조치 |
| --- | --- |
| 504 timeout | 명령이 30초를 초과. 장기 작업은 잘게 나누거나 백그라운드 작업으로 실행 |
| 출력이 비어 있음 | 정상 완료지만 출력이 없는 명령 — 콘솔/WS는 `(completed)`로 표시 |
| 세션이 초기화됨 | 브로커의 powershell.exe가 죽었다가 재기동된 경우, 또는 아무도 재연결하지 않아 브로커 자체가 idle timeout으로 정리된 경우(§6). 변수/CWD는 재설정된다 |
| `PowerShell 브로커에 연결할 수 없습니다` | `PAAS_PS_BROKER_PORT`가 방화벽에 막혔거나, 다른 프로세스가 그 포트를 이미 쓰고 있음. `ps_broker.py`는 포트 바인드에 실패하면 조용히 종료한다(로그 확인) |
| `powershell.exe 실행 실패` | 비-Windows이거나 PATH에 없음 — §7 참고 |

<#
콘솔 터미널이 안 열릴 때 — 원인을 한 번에 가른다.

이 고장은 겉모습이 전부 똑같다("연결 안 됨"). 정작 원인은 네 갈래이고 손볼 곳이 각각
완전히 다르다:

  1. 코드가 옛것            → SW 업데이트가 실제로 안 먹었다
  2. WS 라이브러리 없음      → HTTP는 전부 정상인데 소켓만 404
  3. 셸이 못 뜬다           → PTY 백엔드(Server 2016의 ConPTY) 또는 pywinpty 미설치
  4. 프록시가 안 넘긴다      → IIS의 WebSocket 기능

브라우저는 이 넷을 구분해 주지 않는다(닫힘 코드 1006 하나뿐이다). 그래서 여기서는
같은 것을 **백엔드에 직접 한 번, IIS를 거쳐 한 번** 물어 둘을 비교한다 — 직접은 되는데
IIS가 안 되면 그 사이가 원인이고, 직접부터 안 되면 서버가 원인이다.

사용:
  .\terminal-doctor.ps1 -Key <관리자키>
  .\terminal-doctor.ps1 -Key <관리자키> -Port 8000 -Proxy http://paas.lge.com

Windows PowerShell 5.1(Server 2016 기본)에서 도는 문법만 쓴다.
#>
param(
  [Parameter(Mandatory = $true)][string]$Key,
  # 0이면 서비스 설정과 열린 포트에서 찾아본다
  [int]$Port = 0,
  # IIS 등 앞단을 거쳐 들어오는 주소. 브라우저가 쓰는 그 주소여야 의미가 있다.
  [string]$Proxy = 'http://localhost',
  [string]$Service = 'paas'
)

$ErrorActionPreference = 'Continue'
$PATH_PREFLIGHT = '/paas/api/v1/system/terminal/preflight'
$PATH_HEALTH = '/paas/health'
$PATH_WS = '/paas/api/v1/system/powershell/ws'
# 열리자마자 끝난 세션은 사용자가 나간 것이 아니라 셸이 못 뜬 것으로 본다
# (app/api/system.py의 SHORT_SESSION_SECONDS와 같은 기준).
$SHORT_SESSION_SECONDS = 3

function Write-Section($text) {
  Write-Host ''
  Write-Host "== $text" -ForegroundColor Cyan
}

function Write-Ok($text) { Write-Host "   [OK] $text" -ForegroundColor Green }
function Write-Bad($text) { Write-Host "   [!!] $text" -ForegroundColor Red }
function Write-Info($text) { Write-Host "   $text" }

function Resolve-BackendPort {
  <#
    서비스 인자에 적힌 --port가 정답이다. nssm이 없거나 인자에 없으면 열려 있는
    포트로 찾아본다 — 여기서 틀린 포트를 잡으면 그 뒤 진단이 전부 헛돈다.
  #>
  param([string]$ServiceName)

  $nssm = Get-Command nssm -ErrorAction SilentlyContinue
  if ($nssm) {
    $args_ = & nssm get $ServiceName AppParameters 2>$null
    if ($LASTEXITCODE -eq 0 -and $args_) {
      # nssm은 UTF-16으로 뱉는 경우가 있어 널 바이트가 섞인다
      $text = ($args_ -join ' ') -replace "`0", ''
      $m = [regex]::Match($text, '--port\s+(\d+)')
      if ($m.Success) { return [int]$m.Groups[1].Value }
    }
  }
  foreach ($candidate in 8000, 7000) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $candidate -ErrorAction SilentlyContinue
    if ($conn) { return $candidate }
  }
  return 0
}

function Get-Json($url, $headers) {
  try {
    return @{ Ok = $true; Data = (Invoke-RestMethod -Headers $headers -Uri $url -TimeoutSec 20) }
  } catch {
    $status = ''
    if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
    return @{ Ok = $false; Status = $status; Message = $_.Exception.Message }
  }
}

function Test-TerminalSocket {
  <#
    브라우저와 똑같은 모양으로 붙어 본다 — 키는 헤더가 아니라 서브프로토콜로 싣는다
    (app/api/system.py의 WS_KEY_PREFIX). 붙은 뒤 첫 출력까지 받아 봐야 "핸드셰이크는
    됐는데 셸이 즉사한다"를 가려낼 수 있다. 101만 보고 성공이라 하면 그 경우를 놓친다.
  #>
  param([string]$Url, [string]$ApiKey)

  $ws = New-Object System.Net.WebSockets.ClientWebSocket
  $ws.Options.AddSubProtocol('paas-terminal')
  $ws.Options.AddSubProtocol("paas-key.$ApiKey")
  $cts = New-Object System.Threading.CancellationTokenSource 20000
  $watch = [Diagnostics.Stopwatch]::StartNew()

  try {
    $ws.ConnectAsync([Uri]$Url, $cts.Token).GetAwaiter().GetResult() | Out-Null
  } catch {
    $e = $_.Exception
    while ($e.InnerException) { $e = $e.InnerException }
    return @{ Connected = $false; Message = $e.Message }
  }

  $result = @{ Connected = $true; Subprotocol = $ws.SubProtocol; Output = ''; Closed = $false }
  try {
    $buf = New-Object 'System.ArraySegment[byte]' -ArgumentList @(, (New-Object byte[] 4096))
    # 첫 출력을 기다린다. 셸이 살아 있으면 프롬프트가 곧바로 온다.
    $r = $ws.ReceiveAsync($buf, $cts.Token).GetAwaiter().GetResult()
    if ($r.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) {
      $result.Closed = $true
    } else {
      $result.Output = [Text.Encoding]::UTF8.GetString($buf.Array, 0, $r.Count)
    }
  } catch {
    $result.Closed = $true
  }
  $result.Seconds = [math]::Round($watch.Elapsed.TotalSeconds, 1)

  try { $ws.Dispose() } catch { }
  return $result
}

# ---------------------------------------------------------------- 1. 포트

Write-Section '백엔드 포트'
if ($Port -eq 0) { $Port = Resolve-BackendPort -ServiceName $Service }
if ($Port -eq 0) {
  Write-Bad "백엔드 포트를 찾지 못했습니다. -Port 로 직접 지정하세요."
  Write-Info "확인: nssm get $Service AppParameters"
  return
}
$direct = "http://127.0.0.1:$Port"
Write-Ok "직접 접속 주소 $direct / 앞단 $Proxy"

# ---------------------------------------------------------------- 2. 리비전

Write-Section '돌고 있는 코드'
$health = Get-Json "$direct$PATH_HEALTH" @{}
if (-not $health.Ok) {
  Write-Bad "백엔드가 응답하지 않습니다: $($health.Message)"
  Write-Info "서비스가 떠 있는지 보세요: Get-Service $Service"
  return
}
if ($health.Data.PSObject.Properties.Name -notcontains 'revision') {
  Write-Bad "health에 revision이 없습니다 — 옛 코드가 돌고 있습니다."
  Write-Info "SW 업데이트가 실제로 반영되지 않았습니다. logs\sw-update.log의 git pull 출력을 보세요."
  return
}
Write-Ok "revision $($health.Data.revision) ($($health.Data.branch))"

# ---------------------------------------------------------------- 3. preflight

$headers = @{ 'x-api-key' = $Key }
Write-Section 'preflight — 서버가 셸을 열 수 있나'
$pf = Get-Json "$direct$PATH_PREFLIGHT" $headers
if (-not $pf.Ok) {
  if ($pf.Status -eq 401 -or $pf.Status -eq 403) {
    Write-Bad "인증 실패(HTTP $($pf.Status)) — 관리자 키가 아닙니다."
  } else {
    Write-Bad "preflight 실패: $($pf.Message)"
  }
  return
}
$d = $pf.Data
# resolved_backend는 윈도우에서만 값이 있다(POSIX는 표준 pty라 고를 백엔드가 없다).
# 빈 값을 "실제=" 로 찍으면 읽는 쪽이 빠진 값으로 오해한다.
$line = "셸=$($d.shell)  설정=$($d.backend)"
if ($d.resolved_backend) { $line += "  실제=$($d.resolved_backend)" }
Write-Info "$line  ws라이브러리=$($d.websocket_library)"

if (-not $d.websocket_library) {
  Write-Bad '이 서버에 WebSocket 구현이 없습니다 — IIS를 아무리 고쳐도 소켓은 404입니다.'
  Write-Info '고치기: .venv\Scripts\python.exe -m pip install "uvicorn[standard]" 후 서비스 재시작'
  return
}
if ($d.ok) {
  Write-Ok '서버는 셸을 열 수 있습니다.'
} else {
  Write-Bad "셸을 열지 못했습니다: $($d.error)"
  Write-Info $d.hint
  if ($d.error -match '백엔드가 없습니다|pywinpty') {
    Write-Info '고치기: .venv\Scripts\python.exe -m pip install pywinpty 후 서비스 재시작'
  }
  # 셸이 안 열리면 소켓 결과는 볼 필요가 없다 — 원인이 이미 나왔다.
  return
}

# ---------------------------------------------------------------- 4. 소켓

Write-Section 'WebSocket — 직접 vs 앞단'
$results = @{}
foreach ($pair in @(@('직접', $direct), @('앞단', $Proxy))) {
  $label = $pair[0]
  $scheme = 'ws'
  if ($pair[1].StartsWith('https')) { $scheme = 'wss' }
  $url = ($pair[1] -replace '^https?', $scheme) + $PATH_WS

  $r = Test-TerminalSocket -Url $url -ApiKey $Key
  $results[$label] = $r
  if (-not $r.Connected) {
    Write-Bad "$label ($url) — $($r.Message)"
  } elseif ($r.Output) {
    Write-Ok ("$label — 붙었고 첫 출력을 받았습니다: " + ($r.Output -replace '[^\x20-\x7e가-힣]', '.'))
  } else {
    Write-Bad "$label — 붙었지만 출력 없이 $($r.Seconds)초 만에 끝났습니다(셸이 즉사)."
  }
}

# ---------------------------------------------------------------- 5. 판정

Write-Section '판정'
$dir = $results['직접']
$via = $results['앞단']

if ($dir.Connected -and $dir.Output -and $via.Connected -and $via.Output) {
  Write-Ok '양쪽 다 정상입니다. 브라우저에서도 열려야 합니다(캐시된 옛 콘솔이면 강제 새로고침).'
} elseif ($dir.Connected -and $dir.Output -and -not ($via.Connected -and $via.Output)) {
  Write-Bad '백엔드는 정상, 앞단이 WebSocket을 넘기지 않습니다.'
  Write-Info 'IIS: Install-WindowsFeature Web-WebSockets 후 iisreset. 앱풀은 통합 모드여야 합니다.'
  Write-Info 'ARR을 쓴다면 Sec-WebSocket-Protocol 헤더를 지우지 않는지도 보세요(키가 그리로 실립니다).'
} elseif ($dir.Connected -and -not $dir.Output) {
  Write-Bad "핸드셰이크는 되는데 셸이 즉사합니다. preflight는 통과했으므로 세션 쪽 조건입니다."
  Write-Info "현재 백엔드=$($d.resolved_backend). Server 2016이면 winpty여야 합니다."
  Write-Info '콘솔 터미널 탭을 열면 종료코드가 화면에 그대로 찍힙니다 — 그 줄이 원인을 특정합니다.'
} elseif (-not $dir.Connected) {
  Write-Bad '백엔드에 직접 붙는 것부터 실패합니다 — 앞단 문제가 아닙니다.'
  # 여기까지 왔으면 라이브러리 유무와 키 판정 결과를 이미 알고 있다. 선택지를 늘어놓지
  # 말고 그 정보로 하나를 짚는다.
  if ($dir.Message -match '404') {
    Write-Bad "404입니다. preflight는 ws라이브러리=$($d.websocket_library)라고 답했으므로 라이브러리는 있습니다 —"
    Write-Info "  기동 명령에 --ws none이 붙어 있습니다. 빼고 재시작하세요: nssm get $Service AppParameters"
  } elseif ($dir.Message -match '403') {
    Write-Bad '403입니다. 이 키로 REST는 통과했으므로 키 자체는 관리자 키입니다 —'
    Write-Info '  서브프로토콜(Sec-WebSocket-Protocol)이 전달되지 않는 경로를 의심하세요.'
  } elseif ($dir.Message -match 'refused|거부|actively') {
    Write-Info "  $direct 에 아무도 없습니다. 포트를 다시 확인하세요."
  } else {
    Write-Info "  $($dir.Message)"
  }
}

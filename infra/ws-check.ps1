# 콘솔 터미널 WebSocket 진단 — 핸드셰이크가 **어디서** 끊기는지 가른다.
#
# 브라우저는 WebSocket 실패 이유를 알려주지 않는다(닫힘 코드 1006 하나뿐). 같은 연결을
# 백엔드에 직접 한 번, IIS를 거쳐 한 번 해 보면 원인이 갈린다:
#
#   직접 OK · IIS 실패  → IIS/ARR이 업그레이드를 넘기지 않는다
#   둘 다 403          → 키가 관리자 키가 아니거나 Sec-WebSocket-Protocol이 잘려 나간다
#   직접부터 실패       → 백엔드 문제 (GET /system/terminal/preflight로 이어서 확인)
#
# 사용:
#   .\ws-check.ps1 -Url ws://127.0.0.1:7000/paas/api/v1/system/powershell/ws -Key <admin-key>
#   .\ws-check.ps1 -Url ws://<서버>/paas/api/v1/system/powershell/ws          -Key <admin-key>
param(
  [Parameter(Mandatory = $true)][string]$Url,
  [Parameter(Mandatory = $true)][string]$Key
)

$ws = New-Object System.Net.WebSockets.ClientWebSocket
# 브라우저가 보내는 것과 같은 모양 — 키는 헤더가 아니라 서브프로토콜로 실린다
# (app/api/system.py의 WS_KEY_PREFIX 참고).
$ws.Options.AddSubProtocol('paas-terminal')
$ws.Options.AddSubProtocol("paas-key.$Key")
$cts = New-Object System.Threading.CancellationTokenSource 15000

try {
  $ws.ConnectAsync([Uri]$Url, $cts.Token).GetAwaiter().GetResult() | Out-Null
  Write-Host "[OK] 연결됨 — 협상된 서브프로토콜: $($ws.SubProtocol)" -ForegroundColor Green

  $buf = New-Object 'System.ArraySegment[byte]' -ArgumentList @(, (New-Object byte[] 4096))
  $r = $ws.ReceiveAsync($buf, $cts.Token).GetAwaiter().GetResult()
  $text = [Text.Encoding]::UTF8.GetString($buf.Array, 0, $r.Count)
  Write-Host "[OK] 첫 출력: $($text -replace '[^ -~가-힣]', '.')"
  $ws.CloseAsync('NormalClosure', 'bye', $cts.Token).GetAwaiter().GetResult() | Out-Null
  Write-Host "[OK] 이 경로는 WebSocket이 통합니다." -ForegroundColor Green
}
catch {
  $e = $_.Exception
  while ($e.InnerException) { $e = $e.InnerException }
  Write-Host "[실패] $($e.GetType().Name): $($e.Message)" -ForegroundColor Red
  Write-Host ""
  Write-Host "다음으로 볼 곳:" -ForegroundColor Yellow
  Write-Host "  '404'      → 백엔드가 WebSocket을 받지 않는다. HTTP는 전부 정상이라 이것만으로는"
  Write-Host "               프록시 문제와 구분되지 않는다. 둘 중 하나다:"
  Write-Host "                 . 라이브러리 없음   → pip install 'uvicorn[standard]' 후 재시작"
  Write-Host "                 . 있는데 --ws none → 기동 명령(nssm 서비스 인자)에서 그 플래그를 뺀다"
  Write-Host "               (preflight의 websocket_library로 앞쪽을 가른다)"
  Write-Host "  '403'      → 관리자 키가 맞는지, 프록시가 Sec-WebSocket-Protocol을 지우지 않는지"
  Write-Host "  '400'/'500' → IIS/ARR이 업그레이드를 넘기지 않는다(Web-WebSockets 기능·앱풀 통합 모드)"
  Write-Host "  Connection refused → 그 주소에 아무도 없다(포트·경로 확인)"
}

<#
Console terminal doctor -- tells the four failure modes apart in one run.

They all look identical from the browser ("it won't connect"), but the fix is
completely different in each case:

  1. Stale code        -> the SW update never actually landed
  2. No WS library     -> every HTTP route is fine, only the socket 404s
  3. Shell won't start -> PTY backend (ConPTY on Server 2016) or pywinpty missing
  4. Proxy won't relay -> IIS: WebSocket feature/module, ARR older than 3.0,
                          Classic app pool, or outbound rewrite rules

The browser cannot tell them apart -- it only ever reports close code 1006. So
this asks the same question twice: once straight at the backend, once through
the front end. Direct OK + proxy fail means the proxy; failing direct means the
server.

Usage:
  .\terminal-doctor.ps1 -Key <admin-key>
  .\terminal-doctor.ps1 -Key <admin-key> -Port 8000 -Proxy http://paas.example.com

ASCII ONLY -- deliberately, unlike the rest of this codebase. Windows PowerShell
5.1 reads a .ps1 without a BOM using the ANSI code page (cp949 on Korean
Windows), and even with a BOM the console code page and font can still mangle
non-ASCII output. This is the script you run when the box is already broken, so
it must not depend on encoding working. Keep it ASCII.

Uses only syntax that Windows PowerShell 5.1 (Server 2016 default) accepts.
#>
param(
  [Parameter(Mandatory = $true)][string]$Key,
  # 0 = look it up from the service config and listening ports
  [int]$Port = 0,
  # The front door, i.e. the address the browser actually uses.
  [string]$Proxy = 'http://localhost',
  [string]$Service = 'paas'
)

$ErrorActionPreference = 'Continue'
$PATH_PREFLIGHT = '/paas/api/v1/system/terminal/preflight'
$PATH_HEALTH = '/paas/health'
$PATH_WS = '/paas/api/v1/system/powershell/ws'

function Write-Section($text) {
  Write-Host ''
  Write-Host "== $text" -ForegroundColor Cyan
}

function Write-Ok($text) { Write-Host "   [OK] $text" -ForegroundColor Green }
function Write-Bad($text) { Write-Host "   [!!] $text" -ForegroundColor Red }
function Write-Info($text) { Write-Host "   $text" }

function Resolve-BackendPort {
  <#
    The --port in the service arguments is the authoritative answer. If nssm is
    missing or the argument is not there, fall back to probing. Getting this
    wrong makes every later step meaningless.
  #>
  param([string]$ServiceName)

  $nssm = Get-Command nssm -ErrorAction SilentlyContinue
  if ($nssm) {
    $svcArgs = & nssm get $ServiceName AppParameters 2>$null
    if ($LASTEXITCODE -eq 0 -and $svcArgs) {
      # nssm sometimes emits UTF-16, which leaves stray null bytes behind.
      $text = ($svcArgs -join ' ') -replace "`0", ''
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

function Test-IisPrereq {
  <#
    Once we know the backend is fine and the front end is not relaying, the
    remaining causes are a short, checkable list. Check them here instead of
    leaving the operator to guess.

    Everything is best-effort: on a box without IIS (or without appcmd) each
    probe reports "cannot check" rather than failing the run.
  #>
  # $env:OS works on both 5.1 and 7; $IsWindows does not exist in 5.1.
  if ($env:OS -ne 'Windows_NT') {
    Write-Info 'Not running on Windows -- skipping IIS checks.'
    return
  }
  $sysRoot = $env:windir
  if (-not $sysRoot) { $sysRoot = 'C:\Windows' }
  # Plain concatenation, not Join-Path: Join-Path validates that the drive
  # exists and throws when it does not, which turns a skippable check into a
  # red error.
  $appcmd = "$sysRoot\system32\inetsrv\appcmd.exe"

  # 1. The WebSocket Protocol feature. NOT installed by default, and this is the
  #    single most common reason an otherwise-working ARR proxy drops upgrades.
  try {
    $feature = Get-WindowsFeature -Name Web-WebSockets -ErrorAction Stop
    if ($feature.Installed) {
      Write-Ok 'IIS WebSocket Protocol feature: installed'
    } else {
      Write-Bad 'IIS WebSocket Protocol feature: NOT installed  <-- most likely cause'
      Write-Info '  Fix: Install-WindowsFeature Web-WebSockets ; iisreset'
    }
  } catch {
    Write-Info 'IIS WebSocket Protocol feature: cannot check (Get-WindowsFeature unavailable)'
  }


  # 2. ARR version. WebSocket proxying arrived in ARR 3.0 -- with 2.x every HTTP
  #    route proxies perfectly and upgrades are simply never forwarded, which
  #    looks exactly like this failure.
  #
  #    Do NOT guess the path. ARR lives under Program Files in some versions and
  #    under inetsrv in others; hardcoding one produces a confident "not
  #    installed" for a server that plainly has it. Ask IIS where the module is.
  try {
    $arr = ''
    if (Test-Path $appcmd) {
      $gm = (& $appcmd list config -section:system.webServer/globalModules 2>$null) -join "`n"
      # Attribute order is not fixed -- match it both ways.
      $m = [regex]::Match($gm, 'name="ApplicationRequestRouting"[^>]*?image="([^"]+)"')
      if (-not $m.Success) {
        $m = [regex]::Match($gm, 'image="([^"]+)"[^>]*?name="ApplicationRequestRouting"')
      }
      if ($m.Success) { $arr = [Environment]::ExpandEnvironmentVariables($m.Groups[1].Value) }
    }
    if (-not $arr) {
      foreach ($candidate in @("$sysRoot\system32\inetsrv\requestRouter.dll",
                               "$env:ProgramFiles\IIS\Application Request Routing\requestRouter.dll")) {
        if (Test-Path $candidate) { $arr = $candidate; break }
      }
    }
    if ($arr -and (Test-Path $arr)) {
      $ver = (Get-Item $arr).VersionInfo.ProductVersion
      Write-Info "ARR module version: $ver  (WebSocket proxying requires ARR 3.0 or newer)"
      Write-Info "  at $arr"
    } elseif ($arr) {
      Write-Bad "IIS registers the ARR module at '$arr' but that file is missing."
    } else {
      Write-Bad 'ARR module is not registered in IIS globalModules.'
      Write-Info '  Without ARR nothing is forwarded to the backend at all -- but plain HTTP'
      Write-Info '  works, so check whether the rewrite target is served some other way.'
    }
  } catch {
    Write-Info 'ARR version: cannot check'
  }

  if (-not (Test-Path $appcmd)) {
    Write-Info "appcmd not found at $appcmd -- skipping IIS config checks"
    return
  }

  # 3. webSocket section may be present but explicitly turned off.
  try {
    $cfg = (& $appcmd list config -section:system.webServer/webSocket 2>$null) -join ' '
    if ($cfg -match 'enabled="false"') {
      Write-Bad 'IIS config has webSocket enabled="false"  <-- upgrades are refused'
      Write-Info "  Fix: $appcmd set config -section:system.webServer/webSocket /enabled:True /commit:apphost"
    } elseif ($cfg) {
      Write-Ok 'IIS config: webSocket enabled'
    }
  } catch {
    Write-Info 'IIS webSocket section: cannot check'
  }

  # 4. ARR proxy. Without it the rewrite rule matches but nothing is forwarded
  #    (see infra/gitea/web.config.example and docs/deployment-guide.md).
  try {
    $proxy = (& $appcmd list config -section:system.webServer/proxy 2>$null) -join ' '
    if ($proxy -match 'enabled="true"') {
      Write-Ok 'ARR proxy: enabled'
    } elseif ($proxy) {
      Write-Bad 'ARR proxy: disabled -- rules match but nothing is forwarded'
      Write-Info "  Fix: $appcmd set config -section:system.webServer/proxy /enabled:True /commit:apphost"
    } else {
      Write-Info 'ARR proxy: section not present (ARR may not be installed)'
    }
  } catch {
    Write-Info 'ARR proxy: cannot check'
  }

  # 5. Classic-mode app pools cannot serve WebSockets. Report which application
  #    each Classic pool actually serves -- "some pool is Classic" is not
  #    actionable when a server hosts a dozen of them.
  try {
    $seen = @{}
    $appLines = & $appcmd list app 2>$null
    foreach ($appLine in $appLines) {
      $m = [regex]::Match($appLine, 'APP\s+"([^"]+)"\s+\(applicationPool:([^)]+)\)')
      if (-not $m.Success) { continue }
      $appPath = $m.Groups[1].Value
      $pool = $m.Groups[2].Value
      $mode = (& $appcmd list apppool "$pool" /text:managedPipelineMode 2>$null) -join ''
      if ($mode.Trim() -eq 'Classic') {
        if (-not $seen.ContainsKey($pool)) {
          Write-Bad "App pool '$pool' is Classic -- WebSockets require Integrated mode."
          Write-Info "  Fix: $appcmd set apppool `"$pool`" /managedPipelineMode:Integrated"
          Write-Info "       $appcmd recycle apppool `"$pool`""
          Write-Info '  Check first that nothing else in this pool needs Classic (legacy ASP.NET).'
          $seen[$pool] = $true
        }
        Write-Info "    serves: $appPath"
      }
    }
    if ($seen.Count -eq 0 -and $appLines) {
      Write-Ok 'App pools serving sites: all Integrated'
    }
  } catch {
    Write-Info 'App pool pipeline mode: cannot check'
  }

  # 6. The WebSocket module has to be loaded, not merely installed. A site can
  #    drop it in <modules>, and then upgrades fail with the feature present.
  try {
    $modules = (& $appcmd list modules 2>$null) -join ' '
    if ($modules -match 'WebSocketModule') {
      Write-Ok 'WebSocketModule: loaded'
    } else {
      Write-Bad 'WebSocketModule is NOT loaded even though the feature may be installed.'
      Write-Info '  Check <system.webServer><modules> in the site web.config for a <remove>.'
    }
  } catch {
    Write-Info 'WebSocketModule: cannot check'
  }

  # 7. Per-site overrides. Server level can say enabled while a site says false,
  #    and outbound rewrite rules force IIS to buffer the response -- which a
  #    WebSocket upgrade cannot survive (see infra/gitea/web.config.example).
  try {
    $sites = & $appcmd list site /text:site.name 2>$null
    foreach ($site in $sites) {
      if (-not $site) { continue }
      $siteCfg = (& $appcmd list config "$site/" -section:system.webServer/webSocket 2>$null) -join ' '
      if ($siteCfg -match 'enabled="false"') {
        Write-Bad "Site '$site' sets webSocket enabled=`"false`""
      }
      # Not every outbound rule matters. Ones that rewrite a response HEADER
      # (serverVariable="RESPONSE_...") need no body buffering and leave an
      # upgrade alone. Ones that rewrite the response BODY (filterByTags, or no
      # serverVariable at all) force IIS to buffer the response, which a 101
      # upgrade cannot survive. Naming the rule is the difference between a
      # finding you can act on and one you cannot.
      $outbound = (& $appcmd list config "$site/" -section:system.webServer/rewrite/outboundRules 2>$null) -join "`n"
      foreach ($m in [regex]::Matches($outbound, '<rule\s+name="([^"]+)"([^>]*)>([\s\S]*?)</rule>')) {
        $ruleName = $m.Groups[1].Value
        $pm = [regex]::Match($m.Groups[2].Value, 'preCondition="([^"]+)"')
        if ($pm.Success) { $pre = $pm.Groups[1].Value } else { $pre = '(none)' }
        $ruleBody = $m.Groups[3].Value
        if ($ruleBody -match 'serverVariable="RESPONSE_' -and $ruleBody -notmatch 'filterByTags') {
          Write-Info "Site '$site' outbound rule '$ruleName' rewrites a header only -- harmless."
        } else {
          Write-Bad "Site '$site' outbound rule '$ruleName' rewrites the response body (preCondition=$pre)."
          Write-Info '  Body rewriting buffers the response, which breaks WebSocket upgrades.'
          Write-Info '  Scope it with a preCondition so it does not apply to the terminal path.'
        }
      }
    }
  } catch {
    Write-Info 'Per-site webSocket/outbound rules: cannot check'
  }
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
    Connect exactly the way the browser does -- the key rides in a subprotocol,
    not a header (see WS_KEY_PREFIX in app/api/system.py). Then wait for the
    first output: stopping at the 101 would miss "handshake succeeded but the
    shell died instantly", which is a real and common failure here.
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
    # A live shell sends its prompt immediately.
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

# ------------------------------------------------------------ 1. port

Write-Section 'Backend port'
if ($Port -eq 0) { $Port = Resolve-BackendPort -ServiceName $Service }
if ($Port -eq 0) {
  Write-Bad 'Could not determine the backend port. Pass -Port explicitly.'
  Write-Info "Check: nssm get $Service AppParameters"
  return
}
$direct = "http://127.0.0.1:$Port"
Write-Ok "direct=$direct  front=$Proxy"

# ------------------------------------------------------------ 2. revision

Write-Section 'Running code'
$health = Get-Json "$direct$PATH_HEALTH" @{}
if (-not $health.Ok) {
  Write-Bad "Backend is not responding: $($health.Message)"
  Write-Info "Is the service up?  Get-Service $Service"
  return
}
if ($health.Data.PSObject.Properties.Name -notcontains 'revision') {
  Write-Bad 'health has no revision field -- an older build is running.'
  Write-Info 'The SW update did not land. Check the git pull output in logs\sw-update.log.'
  return
}
Write-Ok "revision $($health.Data.revision) ($($health.Data.branch))"

# Does plain HTTP even reach the same platform through the front door? Without
# this the next section is untrustworthy: a wrong -Proxy (or a site bound to a
# hostname, so http://localhost lands on a different site) fails the WebSocket
# leg for reasons that have nothing to do with WebSockets, and the verdict then
# blames the proxy's upgrade handling.
$frontHealth = Get-Json "$Proxy$PATH_HEALTH" @{}
if (-not $frontHealth.Ok) {
  Write-Bad "HTTP through the front ($Proxy) fails: $($frontHealth.Message)"
  Write-Info '  This is NOT a WebSocket problem -- plain HTTP does not get through either.'
  Write-Info '  Either -Proxy is not the address the browser uses, or the site does not route /paas.'
  Write-Info '  Check the site bindings: appcmd list site'
  Write-Info '  Re-run with -Proxy set to the exact URL you open in the browser.'
  return
}
if ($frontHealth.Data.revision -ne $health.Data.revision) {
  Write-Bad "The front door reaches a DIFFERENT backend (revision $($frontHealth.Data.revision))."
  Write-Info '  The rewrite rule points somewhere other than the service you just checked.'
  return
}
Write-Ok "HTTP through the front reaches the same backend"

# ------------------------------------------------------------ 3. preflight

$headers = @{ 'x-api-key' = $Key }
Write-Section 'Preflight -- can the server open a shell?'
$pf = Get-Json "$direct$PATH_PREFLIGHT" $headers
if (-not $pf.Ok) {
  if ($pf.Status -eq 401 -or $pf.Status -eq 403) {
    Write-Bad "Auth failed (HTTP $($pf.Status)) -- that is not an admin key."
  } else {
    Write-Bad "Preflight failed: $($pf.Message)"
  }
  return
}
$d = $pf.Data
# resolved_backend is only meaningful on Windows (POSIX uses the stdlib pty, so
# there is no backend to pick). Printing an empty value reads like a bug.
$line = "shell=$($d.shell)  configured=$($d.backend)"
if ($d.resolved_backend) { $line += "  resolved=$($d.resolved_backend)" }
Write-Info "$line  ws-library=$($d.websocket_library)"

if ($d.ok) {
  Write-Ok 'The server can open a shell.'
} else {
  # The server's error/hint are Korean prose meant for the browser; a Windows
  # console will mangle them. Switch on the machine-readable reason instead
  # (pty_terminal.REASON_*) so the important line is always legible here.
  switch ($d.reason) {
    'no_ws_library' {
      Write-Bad 'No WebSocket library -- no amount of IIS tuning will help; the socket 404s.'
      Write-Info 'Fix: .venv\Scripts\python.exe -m pip install "uvicorn[standard]"; then restart the service.'
    }
    'no_pty_backend' {
      Write-Bad 'No usable PTY backend -- pywinpty is missing or the shell could not be spawned.'
      Write-Info 'Fix: .venv\Scripts\python.exe -m pip install pywinpty; then restart the service.'
      Write-Info 'requirements.txt keeps it commented out, so the SW update does not install it.'
    }
    'bad_backend' {
      Write-Bad "PAAS_PTY_BACKEND is not a valid value (configured=$($d.backend))."
      Write-Info 'Use conpty or winpty, or leave it empty to let the build number decide.'
    }
    'shell_exited' {
      Write-Bad "The shell started and exited immediately (exit code $($d.exit_status))."
      if ($d.exit_status -eq 127) {
        Write-Info "'$($d.shell)' could not be executed -- check the path (PAAS_PTY_SHELL)."
      } else {
        if ($d.resolved_backend) {
          Write-Info "Backend in use = $($d.resolved_backend). On Server 2016 this must be winpty;"
        } else {
          Write-Info 'On Server 2016 the backend must be winpty;'
        }
        Write-Info '  ConPTY does not exist before build 17763 and the shell dies on start.'
        Write-Info '  Pin it: set PAAS_PTY_BACKEND=winpty and restart.'
      }
    }
    default {
      # Unknown reason -- fall back to the server text even though it may mangle.
      Write-Bad "Could not open a shell (reason=$($d.reason))."
      Write-Info $d.error
    }
  }
  # No point testing sockets -- the cause is already established.
  return
}

# ------------------------------------------------------------ 4. sockets

Write-Section 'WebSocket -- direct vs front'
$results = @{}
foreach ($pair in @(@('direct', $direct), @('front ', $Proxy))) {
  $label = $pair[0]
  $scheme = 'ws'
  if ($pair[1].StartsWith('https')) { $scheme = 'wss' }
  $url = ($pair[1] -replace '^https?', $scheme) + $PATH_WS

  $r = Test-TerminalSocket -Url $url -ApiKey $Key
  $results[$label.Trim()] = $r
  if (-not $r.Connected) {
    Write-Bad "$label -- $($r.Message)"
    Write-Info "         ($url)"
  } elseif ($r.Output) {
    Write-Ok ("$label -- connected, first output: " + ($r.Output -replace '[^\x20-\x7e]', '.'))
  } else {
    Write-Bad "$label -- connected but ended after $($r.Seconds)s with no output (shell died on start)."
  }
}

# ------------------------------------------------------------ 5. verdict

Write-Section 'Verdict'
$dir = $results['direct']
$via = $results['front']

if ($dir.Connected -and $dir.Output -and $via.Connected -and $via.Output) {
  Write-Ok 'Both paths work. The browser should work too (hard-refresh if it is serving a cached console).'
} elseif ($dir.Connected -and $dir.Output -and -not ($via.Connected -and $via.Output)) {
  Write-Bad 'Backend is fine; the front end is not relaying WebSocket upgrades.'
  Write-Section 'IIS prerequisites'
  Test-IisPrereq
  Write-Info ''
  Write-Info 'If all of the above are green, check that ARR is not stripping Sec-WebSocket-Protocol'
  Write-Info '  -- the admin key rides in that header, and losing it shows up as 403, not as a hang.'
} elseif ($dir.Connected -and -not $dir.Output) {
  Write-Bad 'Handshake succeeds but the shell dies instantly. Preflight passed, so it is session-specific.'
  Write-Info "Resolved backend = $($d.resolved_backend). On Server 2016 this must be winpty."
  Write-Info 'Open the console Terminal tab -- it prints the exit code, which pins the cause.'
} elseif (-not $dir.Connected) {
  Write-Bad 'Even the direct connection fails -- this is not a front-end problem.'
  # We already know whether the library exists and whether the key is valid, so
  # name one cause instead of listing options.
  if ($dir.Message -match '404') {
    Write-Bad "404. Preflight reported ws-library=$($d.websocket_library), so the library IS present --"
    Write-Info "  the server was started with --ws none. Remove it: nssm get $Service AppParameters"
  } elseif ($dir.Message -match '403') {
    Write-Bad '403. This key passed the REST call, so the key itself is an admin key --'
    Write-Info '  something on this path is dropping Sec-WebSocket-Protocol.'
  } elseif ($dir.Message -match 'refused|actively') {
    Write-Info "  Nothing is listening on $direct. Re-check the port."
  } else {
    Write-Info "  $($dir.Message)"
  }
}

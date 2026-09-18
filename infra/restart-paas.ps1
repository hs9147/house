<#
Restart the paas backend from the console terminal -- without killing the restart
itself halfway through.

WHY THIS EXISTS. Typing `Restart-Service paas` in the console terminal restarts
the very process that is serving the terminal. Two things go wrong:

  1. The HTTP call behind the terminal (/system/powershell/exec) is waiting for
     this command's output. paas goes down, the response never arrives, and the
     console reports a failure even though the restart is still running.
  2. On the WebSocket PTY terminal the shell is a child of paas, so it dies
     mid-command and the service may never come back up.

So this script does not restart anything itself: it re-launches itself detached
(-Now) and returns immediately. The caller gets its output before paas goes
down, and the detached copy survives paas exiting. Same trick the backend uses
(app/services/powershell_daemon.py run_detached_script).

WHAT IT RESTARTS. A registered service if there is one (nssm on this platform --
the service definition already holds the correct start command), otherwise it
re-launches uvicorn on the port paas is actually listening on. The same policy
lives in app/services/selfrestart.py for the /system/restart endpoint -- keep
the two in step.

The port is never hardcoded. Hardcoding it is what broke this before: the
restart endpoint assumed 8000 while the platform serves 7000, so restarting
made the backend vanish from where the console and IIS look for it.

Usage (from the console terminal or a shell on the server):
  .\infra\restart-paas.ps1
  .\infra\restart-paas.ps1 -Service paas -Port 7000
  .\infra\restart-paas.ps1 -Now          # restart here instead of detaching

ASCII ONLY -- same reason as terminal-doctor.ps1: Windows PowerShell 5.1 reads a
BOM-less .ps1 using the ANSI code page (cp949 on Korean Windows), and this is a
script you run when the box is already broken. Windows PowerShell 5.1 syntax only.
#>
param(
    # Service names to try, in order. Matches PAAS_SW_UPDATE_SERVICES.
    [string[]]$Service = @('paas', 'paas-console'),
    # 0 = discover the port paas is listening on.
    [int]$Port = 0,
    # Bind address for the uvicorn fallback. Matches PAAS_BIND_HOST.
    [string]$BindHost = '127.0.0.1',
    # Repo root. Defaults to the parent of this script's folder.
    [string]$RepoRoot = '',
    # Interpreter for the uvicorn fallback. The backend passes its own
    # sys.executable, which is the only certain answer to "which venv"; when a
    # human runs this from a terminal there is nobody to ask, so we discover it.
    [string]$Python = '',
    # Seconds to wait before touching anything, so the caller can exit first.
    [int]$SettleSeconds = 0,
    # Do the restart in this process instead of detaching.
    [switch]$Now
)

$ErrorActionPreference = 'Continue'

if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath) }
$LogDir = Join-Path $RepoRoot 'logs'
$LogFile = Join-Path $LogDir 'restart-paas.log'

function Write-Step($message) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $message
    Write-Host $line
    try {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
    } catch { }
}

# Which service is actually registered? nssm registers an ordinary Windows
# service, so Get-Service is the whole test.
function Get-RegisteredServices($names) {
    $found = @()
    foreach ($n in $names) {
        if (-not $n) { continue }
        if (Get-Service -Name $n -ErrorAction SilentlyContinue) { $found += $n }
    }
    return $found
}

# The port paas is listening on, read from the process itself. Asking the OS
# beats asking a config file: the config can disagree with how uvicorn was
# actually started, and then the restart lands on the wrong port.
function Find-PaasListener {
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'app\.main:app' }
    foreach ($p in $procs) {
        $conn = Get-NetTCPConnection -OwningProcess $p.ProcessId -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($conn) {
            return [pscustomobject]@{
                ProcessId   = $p.ProcessId
                Port        = $conn.LocalPort
                CommandLine = $p.CommandLine
            }
        }
    }
    return $null
}

function Find-Python {
    if ($Python -and (Test-Path $Python)) { return $Python }
    $venv = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path $venv) { return $venv }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# --- Detach unless we were asked to do it here ---------------------------------
# Reading the port BEFORE detaching matters: once paas is down there is no
# listener left to discover it from.
if (-not $Now) {
    $listener = $null
    if ($Port -le 0) { $listener = Find-PaasListener }
    if ($listener) { $Port = $listener.Port }

    # $args is an automatic variable -- use our own name so nothing shadows it.
    $childArgs = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $PSCommandPath,
        '-BindHost', $BindHost, '-Port', $Port, '-RepoRoot', $RepoRoot, '-Now'
    )
    # One comma-joined value: repeating -Service would keep only the last name.
    $names = @($Service | Where-Object { $_ })
    if ($names.Count -gt 0) { $childArgs += @('-Service', ($names -join ',')) }
    Start-Process -FilePath 'powershell.exe' -ArgumentList $childArgs -WindowStyle Hidden | Out-Null

    $where = Get-RegisteredServices $Service
    if ($where.Count -gt 0) {
        Write-Step ("scheduled: Restart-Service {0}" -f ($where -join ', '))
    } elseif ($Port -gt 0) {
        Write-Step ("scheduled: uvicorn restart on {0}:{1}" -f $BindHost, $Port)
    } else {
        Write-Step 'scheduled: no service registered and no listening paas found -- see log'
    }
    Write-Step ("log: {0}" -f $LogFile)
    exit 0
}

# --- Actually restart ----------------------------------------------------------
Write-Step '--- paas restart ---'
Set-Location $RepoRoot
# The caller (POST /system/restart) exits its own process right after replying,
# so give it time to let go of the port before we start a new listener.
if ($SettleSeconds -gt 0) {
    Write-Step ("settling {0}s before restart" -f $SettleSeconds)
    Start-Sleep -Seconds $SettleSeconds
}

$found = Get-RegisteredServices $Service
if ($found.Count -gt 0) {
    Write-Step ("Restart-Service: {0}" -f ($found -join ', '))
    foreach ($n in $found) {
        Restart-Service -Name $n -Force -ErrorAction Continue
        $svc = Get-Service -Name $n -ErrorAction SilentlyContinue
        Write-Step ("  {0} -> {1}" -f $n, $(if ($svc) { $svc.Status } else { 'unknown' }))
    }
    Write-Step 'done (service)'
    exit 0
}

Write-Step 'no registered service -- restarting uvicorn directly'
$listener = Find-PaasListener
if ($Port -le 0 -and $listener) { $Port = $listener.Port }
if ($Port -le 0) {
    Write-Step 'FAILED: cannot tell which port to use. Pass -Port explicitly.'
    exit 1
}

if ($listener) {
    Write-Step ("stopping pid {0} (port {1})" -f $listener.ProcessId, $listener.Port)
    Stop-Process -Id $listener.ProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
} else {
    Write-Step 'no running paas found -- starting a new one'
}

$python = Find-Python
if (-not $python) {
    Write-Step 'FAILED: no python found (.venv\Scripts\python.exe or PATH).'
    exit 1
}

Write-Step ("starting: {0} -m uvicorn app.main:app --host {1} --port {2}" -f $python, $BindHost, $Port)
Start-Process -FilePath $python `
    -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', $BindHost, '--port', $Port) `
    -WorkingDirectory $RepoRoot -WindowStyle Hidden | Out-Null

# Poll, do not check once. Startup here took ~10s (migrations, scheduler, console
# bootstrap), so a single check a few seconds in reports a false failure on a
# backend that is in fact coming up. Probe 127.0.0.1 rather than $BindHost --
# 0.0.0.0 is a bind address, not an address you can connect to.
$healthUrl = "http://127.0.0.1:{0}/paas/health" -f $Port
$deadline = (Get-Date).AddSeconds(40)
$ok = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    try {
        $ok = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 4
        break
    } catch { }
}
if ($ok) {
    Write-Step ("done (uvicorn) -- health ok, revision {0}" -f $ok.revision)
} else {
    Write-Step ("started, but {0} did not answer within 40s -- check logs" -f $healthUrl)
}

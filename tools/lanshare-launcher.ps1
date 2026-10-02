# Starts LANShare and opens the page, so the host never has to type a command.
#
# Two things here are deliberate rather than incidental:
#
#   * the server runs via `python -m lanshare`, never a bare `uvicorn` command,
#     because only the module launcher applies `keep_alive_timeout` - see
#     vault/03-Decisions/ADR-0011. A launcher that gets this wrong would
#     reintroduce a fixed bug on every double-click.
#   * the page opens only once the server answers /api/health. Opening it
#     immediately races the port bind and shows the browser's own error page,
#     which reads like LANShare is broken.
#
# Windows PowerShell 5.1 is the floor: no ternary, no ?? , no -AsHashtable.

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

function Write-Step($message) {
    Write-Host "  $message" -ForegroundColor DarkGray
}

function Fail($message, $hint) {
    Write-Host ''
    Write-Host "  LANShare could not start" -ForegroundColor Red
    Write-Host "  $message"
    if ($hint) {
        Write-Host ''
        Write-Host "  $hint" -ForegroundColor Yellow
    }
    Write-Host ''
    exit 1
}

function Get-Port {
    # The address shown to the user has to match the port the server binds, so
    # this reads the same two sources config.py does, in the same order.
    if ($env:LANSHARE_PORT) { return $env:LANSHARE_PORT }
    $envFile = Join-Path $root '.env'
    if (Test-Path $envFile) {
        $match = Select-String -Path $envFile -Pattern '^\s*LANSHARE_PORT\s*=\s*(\d+)' |
            Select-Object -First 1
        if ($match) { return $match.Matches[0].Groups[1].Value }
    }
    return '8080'
}

function Test-Health($url) {
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

$port = Get-Port
$page = "http://localhost:$port"
$health = "http://127.0.0.1:$port/api/health"

Write-Host ''
Write-Host '  LANShare' -ForegroundColor Cyan

# An already-running server is the common case for a second double-click.
# Starting another one would just fail to bind the port, so open the page
# instead and say why nothing started.
if (Test-Health $health) {
    Write-Step 'already running - opening the page'
    Start-Process $page
    exit 0
}

if (-not (Test-Path $python)) {
    Fail "No virtualenv at $python" @'
Run the one-time setup first, from the project folder:

    py -3 -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -e ".[dev]"
'@
}

Write-Step 'starting the server'
Start-Process -FilePath $python -ArgumentList '-m', 'lanshare' -WorkingDirectory $root

# Startup does real work - schema migrations, mDNS, the QR - so this waits
# rather than assuming. 30s is long enough for a cold first run on a slow disk.
$deadline = (Get-Date).AddSeconds(30)
$up = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 400
    if (Test-Health $health) { $up = $true; break }
}

if (-not $up) {
    Fail 'The server did not answer within 30 seconds.' @'
Look at the server window that just opened - it prints the reason. The usual one
is "[Errno 10048] only one usage of each socket address", meaning another program
already holds the port.
'@
}

Write-Step "opening $page"
Start-Process $page
Write-Host ''
Write-Host '  The server keeps running in its own window. Close it or press Ctrl+C there to stop.' -ForegroundColor DarkGray
Write-Host ''
exit 0

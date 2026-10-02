# Stops the LANShare server, so quitting is a click rather than a hunt for a
# console window.
#
# The dangerous mistake here would be to stop whatever holds the port, or every
# python.exe on the machine. The port is only the clue. The warrant is the
# command line: this project's interpreter running this project's module. If
# something else is on the port, this says what it is and stops nothing.
#
# This terminates the process rather than sending Ctrl+C, so the server skips
# its graceful shutdown: the mDNS record is not withdrawn and no UDP goodbye is
# sent, and other devices keep a stale entry until it expires. That is survivable
# by design - see ADR-0003 and ADR-0004 - and SQLite in WAL mode is safe under a
# hard stop. A half-written upload is cleaned up by the chunk sweep (ADR-0014).
#
# Windows PowerShell 5.1 is the floor: no ternary, no ?? , no -AsHashtable.

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

function Write-Step($message) {
    Write-Host "  $message" -ForegroundColor DarkGray
}

function Fail($message) {
    Write-Host ''
    Write-Host '  LANShare could not stop the server' -ForegroundColor Red
    Write-Host "  $message"
    Write-Host ''
    Write-Host '  Set LANSHARE_PORT to a number between 1 and 65535, or clear it to' -ForegroundColor Yellow
    Write-Host '  fall back to .env (or to 8080).' -ForegroundColor Yellow
    Write-Host ''
    exit 1
}

function Test-Port($value) {
    # The port decides what gets stopped, so a value that is not a port must be
    # refused rather than coerced. Five digits at most, so the range check
    # cannot overflow the cast.
    if ($value -notmatch '^\d{1,5}$') { return $false }
    $number = [int]$value
    return ($number -ge 1 -and $number -le 65535)
}

function Get-Port {
    # The same two sources config.py reads, in the same order, so this looks at
    # the port the server actually bound.
    if ($env:LANSHARE_PORT) {
        if (-not (Test-Port $env:LANSHARE_PORT)) {
            Fail "LANSHARE_PORT is not a port number: $($env:LANSHARE_PORT)"
        }
        return $env:LANSHARE_PORT
    }
    $envFile = Join-Path $root '.env'
    if (Test-Path $envFile) {
        $match = Select-String -Path $envFile -Pattern '^\s*LANSHARE_PORT\s*=\s*(\d+)' |
            Select-Object -First 1
        if ($match) {
            $value = $match.Matches[0].Groups[1].Value
            if (-not (Test-Port $value)) {
                Fail "LANSHARE_PORT in .env is not a port number: $value"
            }
            return $value
        }
    }
    return '8080'
}

function Test-IsOurServer($process) {
    # An exact prefix match, not a wildcard: a project folder could contain a
    # character that -like would read as a pattern. Quotes are stripped because
    # the launcher's cmd shim quotes the interpreter path and a hand-typed
    # `python -m lanshare` does not.
    if (-not $process) { return $false }
    $line = $process.CommandLine
    if (-not $line) { return $false }
    # Runs of whitespace are collapsed: cmd leaves two spaces after the quoted
    # interpreter, so the launcher's own server would not match a literal
    # comparison. Found on a real server, not in a test.
    $normalized = ($line.Replace('"', '') -replace '\s+', ' ').Trim()
    $expected = "$python -m lanshare"
    return $normalized.StartsWith($expected, [StringComparison]::OrdinalIgnoreCase)
}

function Get-ProcessInfo($processId) {
    return @(Get-CimInstance Win32_Process -Filter "ProcessId=$processId") | Select-Object -First 1
}

function Get-ServerTree($listenerProcess) {
    # Only the serving process and its immediate relatives, all of which have to
    # look like this project's server. The venv's python.exe is a stub that execs
    # the real interpreter, so the server is two processes; a second LANShare on
    # another port is none of this script's business and is left alone.
    $tree = @($listenerProcess)
    $parent = Get-ProcessInfo $listenerProcess.ParentProcessId
    if (Test-IsOurServer $parent) { $tree += $parent }
    foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($listenerProcess.ProcessId)")) {
        if (Test-IsOurServer $child) { $tree += $child }
    }
    return $tree
}

function Get-ListenerPid($port) {
    # Returns the pid listening on the port, or $null. Get-NetTCPConnection
    # throws rather than returning nothing when there is no match.
    try {
        $connections = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop)
    } catch {
        return $null
    }
    if ($connections.Count -eq 0) { return $null }
    return [int]$connections[0].OwningProcess
}

$port = Get-Port
$listener = Get-ListenerPid $port

Write-Host ''
Write-Host '  LANShare' -ForegroundColor Cyan

# The port decides *whether* to stop anything. Matching a command line alone
# would find a LANShare serving some other port and stop that one instead.
if ($null -eq $listener) {
    Write-Step "not running - nothing is listening on port $port"
    Write-Host ''
    exit 0
}

$listenerProcess = Get-ProcessInfo $listener

# ...and the command line decides *what* may be stopped. Something else on the
# port is the one case where doing nothing is the right answer.
if (-not (Test-IsOurServer $listenerProcess)) {
    $name = 'unknown'
    if ($listenerProcess) { $name = $listenerProcess.Name }

    Write-Host ''
    Write-Host "  Port $port is held by something that is not LANShare" -ForegroundColor Yellow
    Write-Host "  $name (pid $listener) - left running."
    Write-Host ''
    Write-Host '  Nothing was stopped. Close that program, or set LANSHARE_PORT' -ForegroundColor DarkGray
    Write-Host '  in .env to move LANShare to another port.' -ForegroundColor DarkGray
    Write-Host ''
    exit 1
}

# The listener first, so the port frees before the venv's launcher stub goes.
foreach ($process in @(Get-ServerTree $listenerProcess)) {
    Write-Step "stopping pid $($process.ProcessId)"
    try {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    } catch {
        # A parent that exited with its child is the ordinary case, not a fault.
        if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) {
            Write-Host ''
            Write-Host "  Could not stop pid $($process.ProcessId)" -ForegroundColor Red
            Write-Host "  $($_.Exception.Message)"
            Write-Host ''
            exit 1
        }
    }
}

$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    if ($null -eq (Get-ListenerPid $port)) { break }
    Start-Sleep -Milliseconds 200
}

$remaining = Get-ListenerPid $port
if ($null -ne $remaining) {
    Write-Host ''
    Write-Host "  Stopped the server, but port $port is still held by pid $remaining" -ForegroundColor Yellow
    Write-Host ''
    exit 1
}

Write-Host '  stopped' -ForegroundColor DarkGray
Write-Host ''
exit 0

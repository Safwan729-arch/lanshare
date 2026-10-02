<#
.SYNOPSIS
    Creates the LANShare app icons.

.DESCRIPTION
    Writes two .lnk files in the project folder with the generated icon:
    LANShare.lnk, pointing at tools\LANShare.cmd, and "LANShare Stop.lnk",
    pointing at tools\LANShare-Stop.cmd. Closing the browser tab does not stop
    the server - it runs in its own window - so the second icon is how it is
    quit without going to look for that window.

    Those two are the app: copy them, or right-click and choose "Create
    shortcut", to put launchers anywhere on the machine. A copied .lnk keeps both
    the icon and the target, so the copies need nothing from here.

    Re-running this overwrites the file, which is how to repair it after moving
    the project folder - and the copies, which point at the project folder rather
    than at this .lnk, need repairing the same way.

    The .lnk stores absolute paths, so it is specific to this machine and is not
    committed.

.EXAMPLE
    .\tools\install-shortcut.ps1
    Creates LANShare.lnk in the project folder.
.EXAMPLE
    .\tools\install-shortcut.ps1 -Desktop -StartMenu
    Also puts one on the desktop and in Start. These write outside the repo,
    which is why they are opt-in and why this is a separate script rather than
    something the launcher does.
.EXAMPLE
    .\tools\install-shortcut.ps1 -Desktop -Remove
    Deletes the ones it created, from the same places.
#>
[CmdletBinding()]
param(
    [switch]$Desktop,
    [switch]$StartMenu,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$icon = Join-Path $PSScriptRoot 'lanshare.ico'

# Two icons, because starting was one click and stopping was a hunt for a
# console window. They are installed and removed together: a Start with no Stop
# beside it is how the server ends up running for days unnoticed.
$shortcuts = @(
    @{
        Link        = 'LANShare.lnk'
        Target      = Join-Path $PSScriptRoot 'LANShare.cmd'
        Description = 'Start LANShare and open it in the browser'
    },
    @{
        Link        = 'LANShare Stop.lnk'
        Target      = Join-Path $PSScriptRoot 'LANShare-Stop.cmd'
        Description = 'Stop the LANShare server'
    }
)

foreach ($entry in $shortcuts) {
    if (-not (Test-Path $entry.Target)) { throw "Missing launcher: $($entry.Target)" }
}

# The project folder always, so there is one icon that belongs to the project
# and everything else is a copy of it.
$locations = @($root)
if ($Desktop) { $locations += [Environment]::GetFolderPath('Desktop') }
if ($StartMenu) {
    $locations += Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
}

$shell = New-Object -ComObject WScript.Shell
foreach ($folder in $locations) {
    foreach ($entry in $shortcuts) {
        $link = Join-Path $folder $entry.Link

        if ($Remove) {
            if (Test-Path $link) {
                Remove-Item $link -Force
                Write-Host "Removed $link"
            }
            continue
        }

        $shortcut = $shell.CreateShortcut($link)
        $shortcut.TargetPath = $entry.Target
        $shortcut.WorkingDirectory = $root
        $shortcut.Description = $entry.Description
        if (Test-Path $icon) { $shortcut.IconLocation = "$icon,0" }
        $shortcut.Save()
        Write-Host "Created $link"
    }
}

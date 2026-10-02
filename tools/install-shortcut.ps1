<#
.SYNOPSIS
    Creates the LANShare app icon.

.DESCRIPTION
    Writes LANShare.lnk in the project folder, pointing at tools\LANShare.cmd
    with the generated icon. That one is the app: copy it, or right-click it and
    choose "Create shortcut", to put a launcher anywhere on the machine. A copied
    .lnk keeps both the icon and the target, so the copies need nothing from here.

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
$target = Join-Path $PSScriptRoot 'LANShare.cmd'
$icon = Join-Path $PSScriptRoot 'lanshare.ico'

if (-not (Test-Path $target)) { throw "Missing launcher: $target" }

# The project folder always, so there is one icon that belongs to the project
# and everything else is a copy of it.
$locations = @($root)
if ($Desktop) { $locations += [Environment]::GetFolderPath('Desktop') }
if ($StartMenu) {
    $locations += Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
}

$shell = New-Object -ComObject WScript.Shell
foreach ($folder in $locations) {
    $link = Join-Path $folder 'LANShare.lnk'

    if ($Remove) {
        if (Test-Path $link) {
            Remove-Item $link -Force
            Write-Host "Removed $link"
        }
        continue
    }

    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $target
    $shortcut.WorkingDirectory = $root
    $shortcut.Description = 'Start LANShare and open it in the browser'
    if (Test-Path $icon) { $shortcut.IconLocation = "$icon,0" }
    $shortcut.Save()
    Write-Host "Created $link"
}

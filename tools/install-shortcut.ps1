<#
.SYNOPSIS
    Puts a LANShare shortcut on the desktop.

.DESCRIPTION
    Creates LANShare.lnk pointing at tools\LANShare.cmd, with the generated
    icon. Re-running it overwrites the existing shortcut, so this is also how to
    repair one after moving the project folder.

    This is the only thing in the project that writes outside the repo, which is
    why it is a separate script you run once rather than part of the launcher.

.EXAMPLE
    .\tools\install-shortcut.ps1
.EXAMPLE
    .\tools\install-shortcut.ps1 -StartMenu
    Also adds a Start-menu entry, so typing "LANShare" in Start finds it.
#>
[CmdletBinding()]
param(
    [switch]$StartMenu,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $PSScriptRoot 'LANShare.cmd'
$icon = Join-Path $PSScriptRoot 'lanshare.ico'

if (-not (Test-Path $target)) { throw "Missing launcher: $target" }

$locations = @([Environment]::GetFolderPath('Desktop'))
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

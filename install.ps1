# Crew Monitor installer: copies the plugin into Hermes Desktop, installs + starts the status service.
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
#   ... -HermesHome D:\Hermes\data  the Hermes data folder (the one containing "profiles"), if not found
#   ... -Port 8100                 another port (the installed plugin copy is pointed at it)
#   ... -Python C:\path\pythonw.exe  a specific Python 3.9+ (default: Hermes' bundled one, then PATH)
#   ... -AllowOrigin app://hermes  only this page origin may read the service (repeatable, comma-separated)
#   ... -EnableControl             let the panel start/stop the local servers listed in crew-monitor.json
#   ... -DisableControl            back to read-only
#   ... -Uninstall                 stops the service, removes the logon task, the plugin and the service copy
param(
    [switch]   $Uninstall,
    [switch]   $EnableControl,
    [switch]   $DisableControl,
    [string]   $HermesHome  = "",
    [int]      $Port        = 8094,
    [string]   $Python      = "",
    [string[]] $AllowOrigin = @(),
    [string]   $InstallDir  = (Join-Path $env:LOCALAPPDATA "HermesCrewMonitor")
)
$ErrorActionPreference = "Stop"
$Here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName   = "HermesCrewMonitor"

# The Hermes data folder: -HermesHome, then $env:HERMES_HOME, then the usual places. It is recognised
# by what Hermes keeps in it; nothing is guessed if none of them looks like one.
function Find-HermesHome {
    $Looks = { param($p) $p -and ((Test-Path -LiteralPath (Join-Path $p "profiles")) -or (Test-Path -LiteralPath (Join-Path $p "config.yaml")) -or (Test-Path -LiteralPath (Join-Path $p "desktop-plugins"))) }
    if ($HermesHome) {
        if (-not (Test-Path -LiteralPath $HermesHome)) { throw "Hermes data folder not found: $HermesHome" }
        if (-not (& $Looks $HermesHome)) { Write-Warning "$HermesHome has no 'profiles' folder or config.yaml: is it the Hermes data folder?" }
        return $HermesHome
    }
    $Candidates = @(
        $env:HERMES_HOME,
        [Environment]::GetEnvironmentVariable("HERMES_HOME", "User"),
        (Join-Path $env:USERPROFILE ".hermes"),
        (Join-Path $env:LOCALAPPDATA "hermes"),
        (Join-Path $env:APPDATA "hermes")
    )
    foreach ($C in $Candidates) { if (& $Looks $C) { return $C } }
    return ""
}
$HermesHome = (Find-HermesHome).TrimEnd('\')
$PluginDir  = if ($HermesHome) { Join-Path $HermesHome "desktop-plugins\crew-monitor" } else { "" }
$Service    = Join-Path $InstallDir "crew_status.py"
$Config     = Join-Path $InstallDir "crew-monitor.json"
$Log        = Join-Path $InstallDir "crew-monitor.log"
$TokenFile  = Join-Path $InstallDir "control-token.txt"
$Utf8       = New-Object System.Text.UTF8Encoding $false   # no BOM

function Stop-CrewService {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*crew_status.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Find-Python {
    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python)) { throw "Python not found: $Python" }
        return $Python
    }
    # Hermes' own Python, whatever build is installed: no exact version in this script.
    $Bundled = if (-not $HermesHome) { $null } else { Get-ChildItem -Path (Join-Path $HermesHome "tools") -Filter "python-*" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "pythonw.exe" } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Sort-Object { (Get-Item -LiteralPath $_).LastWriteTime } -Descending |
        Select-Object -First 1 }
    if ($Bundled) { return $Bundled }
    # Not the Microsoft Store placeholder in WindowsApps: that one opens the Store instead of running.
    $OnPath = Get-Command pythonw.exe -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike "*\WindowsApps\*" } | Select-Object -First 1
    if ($OnPath) { return $OnPath.Source }
    throw "No Python found. Install Python 3.9 or newer from python.org, or pass -Python <path to pythonw.exe>."
}

if ($Uninstall) {
    Stop-CrewService
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    if (-not $PluginDir) { Write-Warning "Hermes data folder not found, so the plugin file was not removed. Re-run with -HermesHome <folder> or delete desktop-plugins\crew-monitor yourself." }
    elseif (Test-Path -LiteralPath $PluginDir) { Remove-Item -LiteralPath $PluginDir -Recurse -Force }
    if (Test-Path -LiteralPath $InstallDir) {
        # the config is yours: it stays unless it is the only thing you ever had there
        Get-ChildItem -LiteralPath $InstallDir -Force | Where-Object { $_.Name -ne "crew-monitor.json" } | Remove-Item -Recurse -Force
        if (-not (Get-ChildItem -LiteralPath $InstallDir -Force)) { Remove-Item -LiteralPath $InstallDir -Force }
        else { Write-Host "Kept your settings: $Config" }
    }
    Write-Host "Crew Monitor removed. Restart Hermes Desktop (or run 'Reload desktop plugins')."
    exit 0
}

if (-not $HermesHome) {
    throw "Could not find your Hermes data folder (the one containing 'profiles'). Re-run with: -HermesHome <that folder>"
}
Write-Host "Hermes data folder: $HermesHome"
$PythonW = Find-Python
$Console = Join-Path (Split-Path -Parent $PythonW) "python.exe"   # pythonw prints nothing
$Version = if (Test-Path -LiteralPath $Console) { & $Console -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null } else { $null }
if ($Version -and ([version]$Version -lt [version]"3.9")) { throw "Python $Version at $PythonW is too old: 3.9 or newer is needed. Pass -Python <path>." }

# Start/stop control is opt-in and stays on across updates until -DisableControl. The secret is
# shared by the service (token file) and the installed plugin copy; nothing else can send it.
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if ($DisableControl) { Remove-Item -LiteralPath $TokenFile -Force -ErrorAction SilentlyContinue }
elseif ($EnableControl -and -not (Test-Path -LiteralPath $TokenFile)) {
    [System.IO.File]::WriteAllText($TokenFile, ([guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")), $Utf8)
}
$Token = if (Test-Path -LiteralPath $TokenFile) { [System.IO.File]::ReadAllText($TokenFile, $Utf8).Trim() } else { "" }

# 1. Plugin: a single ESM file, loaded uncompiled by Desktop. The port is the one thing set at install.
New-Item -ItemType Directory -Force -Path $PluginDir | Out-Null
$Js = [System.IO.File]::ReadAllText((Join-Path $Here "plugin\plugin.js"), $Utf8)
$Patched = [regex]::Replace($Js, 'const SERVICE_PORT = \d+', "const SERVICE_PORT = $Port")
if ($Port -ne 8094 -and $Patched -eq $Js) { throw "Could not set the port in plugin.js (SERVICE_PORT line not found)." }
if ($Token) {
    $WithToken = [regex]::Replace($Patched, "const CONTROL_TOKEN = '[^']*'", "const CONTROL_TOKEN = '$Token'")
    if ($WithToken -eq $Patched) { throw "Could not set the control token in plugin.js (CONTROL_TOKEN line not found)." }
    $Patched = $WithToken
}
[System.IO.File]::WriteAllText((Join-Path $PluginDir "plugin.js"), $Patched, $Utf8)
Write-Host "Plugin installed: $PluginDir"

# 2. Service: copied out of this checkout (so the repo can move), run by a per-user logon task with
#    limited rights that restarts it if it dies. Read-only, 127.0.0.1 only.
Stop-CrewService
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -LiteralPath (Join-Path $Here "service\crew_status.py") -Destination $Service -Force
if (-not (Test-Path -LiteralPath $Config)) {
    Copy-Item -LiteralPath (Join-Path $Here "crew-monitor.example.json") -Destination $Config
}
$Busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($Busy) {
    $Owner = (Get-Process -Id $Busy[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    throw "Port $Port is already in use by '$Owner' (pid $($Busy[0].OwningProcess)). Re-run with -Port <free port>."
}
$Arguments = "`"$Service`" --home `"$HermesHome`" --port $Port --log `"$Log`""
if ($Token) { $Arguments += " --control --token-file `"$TokenFile`"" }
foreach ($Origin in ($AllowOrigin | ForEach-Object { $_ -split ',' } | Where-Object { $_ })) { $Arguments += " --allow-origin `"$Origin`"" }
$Action    = New-ScheduledTaskAction -Execute $PythonW -Argument $Arguments -WorkingDirectory $InstallDir
$Trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings  = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Service installed: $InstallDir (Python: $PythonW)"

# 3. Verify
$Deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Seconds 1
    try {
        $H = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        $S = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/crew" -TimeoutSec 3
        $Jobs = ($S.bots | ForEach-Object { $_.jobs.Count } | Measure-Object -Sum).Sum
        Write-Host ("Crew Monitor service {0} OK: {1} bots, {2} live background job(s). GPU: {3}" -f $H.version, $S.bots.Count, $Jobs, $S.gpu.label)
        Write-Host "Settings: $Config   Log: $Log"
        Write-Host ("Start/stop of listed local instances: {0}" -f $(if ($H.control) { "ON" } else { "off (read-only). Turn on with -EnableControl" }))
        Write-Host "Restart Hermes Desktop to load the plugin."
        exit 0
    } catch { }
} while ((Get-Date) -lt $Deadline)
if (Test-Path -LiteralPath $Log) { Write-Host "--- last lines of $Log"; Get-Content -LiteralPath $Log -Tail 15 }
throw "Service did not answer on port $Port. Run: `"$PythonW`" `"$Service`" --home `"$HermesHome`" --port $Port (use python.exe to see errors)."

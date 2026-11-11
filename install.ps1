# Crew Monitor installer: copies the plugin into Hermes Desktop, installs + starts the status service.
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
#   ... -Port 8100                 another port (the installed plugin copy is pointed at it)
#   ... -Python C:\path\pythonw.exe  a specific Python (default: newest under <HermesHome>\tools, then PATH)
#   ... -AllowOrigin app://hermes  only this page origin may read the service (repeatable, comma-separated)
#   ... -Uninstall                 stops the service, removes the logon task, the plugin and the service copy
param(
    [switch]   $Uninstall,
    [string]   $HermesHome  = "F:\Hermes\data",
    [int]      $Port        = 8094,
    [string]   $Python      = "",
    [string[]] $AllowOrigin = @(),
    [string]   $InstallDir  = (Join-Path $env:LOCALAPPDATA "HermesCrewMonitor")
)
$ErrorActionPreference = "Stop"
$Here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName   = "HermesCrewMonitor"
$HermesHome = $HermesHome.TrimEnd('\')
$PluginDir  = Join-Path $HermesHome "desktop-plugins\crew-monitor"
$Service    = Join-Path $InstallDir "crew_status.py"
$Config     = Join-Path $InstallDir "crew-monitor.json"
$Log        = Join-Path $InstallDir "crew-monitor.log"
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
    $Bundled = Get-ChildItem -Path (Join-Path $HermesHome "tools") -Filter "python-*" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "pythonw.exe" } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Sort-Object { (Get-Item -LiteralPath $_).LastWriteTime } -Descending |
        Select-Object -First 1
    if ($Bundled) { return $Bundled }
    $OnPath = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($OnPath) { return $OnPath.Source }
    throw "No pythonw.exe under $HermesHome\tools or on PATH. Pass -Python <path to pythonw.exe>."
}

if ($Uninstall) {
    Stop-CrewService
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $PluginDir) { Remove-Item -LiteralPath $PluginDir -Recurse -Force }
    if (Test-Path -LiteralPath $InstallDir) {
        # the config is yours: it stays unless it is the only thing you ever had there
        Get-ChildItem -LiteralPath $InstallDir -Force | Where-Object { $_.Name -ne "crew-monitor.json" } | Remove-Item -Recurse -Force
        if (-not (Get-ChildItem -LiteralPath $InstallDir -Force)) { Remove-Item -LiteralPath $InstallDir -Force }
        else { Write-Host "Kept your settings: $Config" }
    }
    Write-Host "Crew Monitor removed. Restart Hermes Desktop (or run 'Reload desktop plugins')."
    exit 0
}

$PythonW = Find-Python

# 1. Plugin: a single ESM file, loaded uncompiled by Desktop. The port is the one thing set at install.
New-Item -ItemType Directory -Force -Path $PluginDir | Out-Null
$Js = [System.IO.File]::ReadAllText((Join-Path $Here "plugin\plugin.js"), $Utf8)
$Patched = [regex]::Replace($Js, 'const SERVICE_PORT = \d+', "const SERVICE_PORT = $Port")
if ($Port -ne 8094 -and $Patched -eq $Js) { throw "Could not set the port in plugin.js (SERVICE_PORT line not found)." }
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
        Write-Host "Restart Hermes Desktop to load the plugin."
        exit 0
    } catch { }
} while ((Get-Date) -lt $Deadline)
if (Test-Path -LiteralPath $Log) { Write-Host "--- last lines of $Log"; Get-Content -LiteralPath $Log -Tail 15 }
throw "Service did not answer on port $Port. Run: `"$PythonW`" `"$Service`" --home `"$HermesHome`" --port $Port (use python.exe to see errors)."

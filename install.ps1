# Crew Monitor installer: copies the plugin into Hermes Desktop, registers + starts the status service.
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
#   ... -Uninstall   stops the service, removes the logon task and the installed plugin
param([switch] $Uninstall, [string] $HermesHome = "F:\Hermes\data", [int] $Port = 8094)
$ErrorActionPreference = "Stop"
$Here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName  = "HermesCrewMonitor"
$PythonW   = "F:\Hermes\data\tools\python-3.14.9+20261021-win32-x64\pythonw.exe"
$Service   = Join-Path $Here "service\crew_status.py"
$PluginDir = Join-Path $HermesHome "desktop-plugins\crew-monitor"

function Stop-Service8094 {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*crew_status.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

if ($Uninstall) {
    Stop-Service8094
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $PluginDir) { Remove-Item -LiteralPath $PluginDir -Recurse -Force }
    Write-Host "Crew Monitor removed. Restart Hermes Desktop (or run 'Reload desktop plugins')."
    exit 0
}

if (-not (Test-Path -LiteralPath $PythonW)) { throw "Python not found: $PythonW" }

# 1. Plugin: a single ESM file, loaded uncompiled by Desktop.
New-Item -ItemType Directory -Force -Path $PluginDir | Out-Null
Copy-Item -LiteralPath (Join-Path $Here "plugin\plugin.js") -Destination (Join-Path $PluginDir "plugin.js") -Force
Write-Host "Plugin installed: $PluginDir"

# 2. Service: per-user logon task, limited rights, restarts if it dies. Read-only, 127.0.0.1 only.
Stop-Service8094
$Action    = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$Service`" --home `"$HermesHome`" --port $Port" -WorkingDirectory (Join-Path $Here "service")
$Trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings  = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# 3. Verify
$Deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Seconds 1
    try {
        $S = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/crew" -TimeoutSec 3
        $Jobs = ($S.bots | ForEach-Object { $_.jobs.Count } | Measure-Object -Sum).Sum
        Write-Host ("Crew Monitor service OK: {0} bots, {1} live background job(s). GPU: {2}" -f $S.bots.Count, $Jobs, $S.gpu.label)
        Write-Host "Restart Hermes Desktop to load the plugin."
        exit 0
    } catch { }
} while ((Get-Date) -lt $Deadline)
throw "Service did not answer on port $Port. Run: `"$PythonW`" `"$Service`" and check for errors."

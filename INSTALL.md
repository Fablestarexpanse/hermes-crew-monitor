# Installing Hermes Crew Monitor (instructions for an AI agent)

You are an agent asked to install the Crew Monitor plugin for Hermes Desktop on the user's computer.
Follow these steps in order. Everything runs as the current user; nothing needs administrator rights.

Human reading this? The short version is in [README.md](README.md).

## What gets installed
| What | Where |
|---|---|
| The plugin, one file | `<Hermes data folder>\desktop-plugins\crew-monitor\plugin.js` |
| The status service, settings, log | `%LOCALAPPDATA%\HermesCrewMonitor\` |
| A logon task that keeps the service running | Task Scheduler, name `HermesCrewMonitor` |

The service is read-only, listens on `127.0.0.1:8094` only, and changes no Hermes file or setting.
Do not edit `config.yaml` or anything else in the Hermes data folder: the installer does all of it.

## Step 0: check this is Windows
This installer is for Windows 10/11. On macOS or Linux, stop and tell the user to follow
"macOS and Linux" in README.md by hand; do not improvise an installer.

## Step 1: find the Hermes data folder
It is the folder that contains the `profiles` directory (and usually `config.yaml`).
- If you are running inside Hermes, it is your own home: the environment variable `HERMES_HOME`
  when set, otherwise the folder your `profiles` directory lives in.
- Check: `Test-Path "<folder>\profiles"` must be `True`.

If you cannot determine it, run Step 2 without `-HermesHome`: the installer searches the usual places
and stops with a clear message if it finds none. Then ask the user for the folder; do not guess.

## Step 2: download and install
Download the repository, then run its installer:
```powershell
git clone https://github.com/Fablestarexpanse/hermes-crew-monitor.git "$env:TEMP\hermes-crew-monitor"
```
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\hermes-crew-monitor\install.ps1" -HermesHome "<Hermes data folder>"
```
Replace `<Hermes data folder>` with the path from Step 1, or drop `-HermesHome "..."` to let the
installer search. It takes under a minute, and is safe to run again; that is also how to update
(`git -C "$env:TEMP\hermes-crew-monitor" pull` first). The downloaded folder can be deleted afterwards.

Without git, download and unpack the ZIP instead of cloning:
```powershell
Invoke-WebRequest -UseBasicParsing https://github.com/Fablestarexpanse/hermes-crew-monitor/archive/refs/heads/main.zip -OutFile "$env:TEMP\hermes-crew-monitor.zip"
```
```powershell
Expand-Archive "$env:TEMP\hermes-crew-monitor.zip" "$env:TEMP\hermes-crew-monitor" -Force
```
The installer is then `"$env:TEMP\hermes-crew-monitor\hermes-crew-monitor-main\install.ps1"`.

## Step 3: confirm it worked
Success is exit code 0 and output ending like this:
```
Crew Monitor service 0.2.0 OK: 11 bots, 0 live background job(s). GPU: ...
Restart Hermes Desktop to load the plugin.
```
The bot count must match the user's bots. `0 bots` means the wrong folder was used: run the
installer again with the right `-HermesHome`.

Check independently:
```powershell
(Invoke-RestMethod http://127.0.0.1:8094/health).ok
```
must print `True`, and `Test-Path "<Hermes data folder>\desktop-plugins\crew-monitor\plugin.js"`
must be `True`.

## Step 4: tell the user to restart Hermes Desktop
The panel appears only after Hermes Desktop restarts. **Do not restart or close Hermes yourself**:
if you are running inside it, that ends your own session and any work in progress. Tell the user:

> Crew Monitor is installed. Restart Hermes Desktop when convenient; a "Crew" panel will appear to
> the right of the chat.

## If something fails
| Message | What to do |
|---|---|
| `Could not find your Hermes data folder` | Run the installer again with `-HermesHome "<folder containing profiles>"`. |
| `Port 8094 is already in use by ...` | Run the installer again adding `-Port 8095` (or another free port). |
| `No Python found` | Ask the user to install Python 3.9+ from python.org, or pass `-Python "<path to pythonw.exe>"`. Do not install Python yourself without asking. |
| `Service did not answer on port ...` | Read the log lines the installer printed (also in `%LOCALAPPDATA%\HermesCrewMonitor\crew-monitor.log`) and report them to the user. |
| Download blocked or no network | Ask the user to download the ZIP from the GitHub page and tell you where it is. |

Do not work around a failure by copying files by hand, running the service from the temporary
folder, or changing Hermes settings. Report what the installer said.

## Uninstall
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\hermes-crew-monitor\install.ps1" -Uninstall -HermesHome "<Hermes data folder>"
```
The user's `crew-monitor.json` settings are kept.

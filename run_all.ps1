# run_all.ps1 — start dashboard + bot in separate titled windows and tee logs
$root = "C:\Users\Joeyv\jobflow-cloud-mini"
$venv = Join-Path $root ".venv\Scripts\Activate.ps1"
$logs = Join-Path $root "output\logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

# Note: escape $host as `$host so the parent shell doesn't expand it early.

# Dashboard
Start-Process powershell -ArgumentList @(
  "-NoExit","-Command",
  " `$host.UI.RawUI.WindowTitle = 'JobFlow • Dashboard';" +
  " cd '$root'; & '$venv';" +
  " python .\master.py *>&1 | Tee-Object -FilePath '$logs\dashboard.log'"
) -WorkingDirectory $root

# Bot
Start-Process powershell -ArgumentList @(
  "-NoExit","-Command",
  " `$host.UI.RawUI.WindowTitle = 'JobFlow • Telegram Bot';" +
  " cd '$root'; & '$venv';" +
  " python .\bot.py *>&1 | Tee-Object -FilePath '$logs\bot.log'"
) -WorkingDirectory $root

Write-Host "Launched. Logs in $logs"

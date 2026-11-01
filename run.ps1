# Install CareerSurge on the first run, then start it (run.sh is the macOS and Linux version).
# Needs the README's prerequisites: Python 3.12 with the py launcher, Node.js 22+ and git.
# After a `git pull`, run it with --update to reinstall. Other arguments go to the app, e.g. --no-window.
# Stop makes a missing program (no py, no npm) end the script instead of printing an error and carrying on.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$update = $args -contains '--update'
$appArgs = @($args | Where-Object { $_ -ne '--update' })
# pip, playwright and npm report failure only through their exit code, so each step checks it and stops.
if ($update -or -not (Test-Path backend\venv\.installed)) {
    Remove-Item backend\venv\.installed -ErrorAction Ignore  # so a failed --update is retried on the next run too
    py -3.12 -m venv backend\venv; if ($LASTEXITCODE) { exit 1 }
    .\backend\venv\Scripts\python -m pip install -r backend\requirements.txt; if ($LASTEXITCODE) { exit 1 }
    .\backend\venv\Scripts\python -m playwright install chromium; if ($LASTEXITCODE) { exit 1 }
    npm ci --prefix frontend; if ($LASTEXITCODE) { exit 1 }
    npm run build --prefix frontend; if ($LASTEXITCODE) { exit 1 }
    New-Item backend\venv\.installed -ItemType File -Force | Out-Null
}
Set-Location backend
.\venv\Scripts\python -m app.local_main @appArgs
exit $LASTEXITCODE

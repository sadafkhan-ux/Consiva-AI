# Deploy (or rebuild) the Consiva stack from Windows PowerShell. The same job as scripts/deploy.sh, without needing bash.
#
#   scripts\deploy.ps1                     # production, docker-compose.prod.yml
#   scripts\deploy.ps1 -Service frontend   # rebuild and restart only that service (and what it needs)
#   scripts\deploy.ps1 -Service backend    # rebuilds backend AND worker together: they are one image and must run the same code
#
# Why this exists: `migrate` is a one-shot container that Compose recreates on every deploy (it renames the old one to <hash>_consiva-migrate-1,
# creates the new one, then deletes the old). If that is interrupted, or two `docker compose up` commands run at the same time, the renamed leftover
# blocks the name and the next run fails with:
#   Conflict. The container name "/<hash>_consiva-migrate-1" is already in use
# A finished or dead one-shot holds no data (migrations are recorded in the database), so this script first removes any `migrate` container that is
# NOT running (a migration in progress is never touched), and refuses to start while another deploy is running. It never touches volumes, the
# database or .env.
param(
    [string]$ComposeFile = "docker-compose.prod.yml",
    [string]$Project = "consiva",
    [string]$Service = "",
    [int]$WaitTimeout = 900
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# one deploy at a time: two overlapping `up` runs are what leave a renamed migrate container behind
$created = $false
$mutex = New-Object System.Threading.Mutex($true, "Global\consiva-deploy", [ref]$created)
if (-not $created) { Write-Error "Another deploy is already running in this session; wait for it to finish, then run this again."; exit 1 }

try {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Write-Error "Cannot talk to the Docker daemon. Is Docker Desktop running?"; exit 1 }

    $stale = docker ps -aq --filter "label=com.docker.compose.project=$Project" --filter "label=com.docker.compose.service=migrate" --filter "status=exited" --filter "status=created" --filter "status=dead"
    if ($stale) {
        Write-Host "==> Removing finished 'migrate' container(s) left by an earlier deploy (not running; holds no data)"
        docker rm -f $stale | Out-Null
    }

    Write-Host "==> Building, migrating and starting"
    $composeArgs = @("compose", "-f", $ComposeFile, "up", "-d", "--build", "--remove-orphans", "--wait", "--wait-timeout", "$WaitTimeout")
    # backend and worker are the same image and must never run different code (a worker left on the previous build scored new scans with old scanner code), so naming either rebuilds both
    if ($Service -in @("backend", "worker")) { $composeArgs += @("backend", "worker") }
    elseif ($Service) { $composeArgs += $Service }
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "==> DEPLOYMENT FAILED. Status:" -ForegroundColor Red
        docker compose -f $ComposeFile ps -a
        Write-Host "==> migrate log:"
        docker compose -f $ComposeFile logs --no-color --tail 80 migrate
        exit 1
    }
    docker compose -f $ComposeFile ps
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}

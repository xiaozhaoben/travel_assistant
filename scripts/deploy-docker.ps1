$ServerHost = ""
$SshPort = 22

$ServerUser = "root"
$RemoteDirectory = "/usr/travel/app"
$GitRepository = "https://github.com/xiaozhaoben/travel_assistant.git"
$GitBranch = "master"
$ComposeProject = "travel"
$BackendPort = 8081
$FrontendPort = 8080

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-LastExitCode {
    param([Parameter(Mandatory = $true)][string]$Operation)

    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE."
    }
}

function Invoke-RemoteBash {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string]$Operation
    )

    $LfScript = $Script.Replace("`r`n", "`n").Replace("`r", "`n")
    $ScriptBytes = [System.Text.Encoding]::UTF8.GetBytes($LfScript)
    $EncodedScript = [Convert]::ToBase64String($ScriptBytes)
    $RemoteCommand = "printf '%s' '$EncodedScript' | base64 --decode | bash"

    & ssh -p $SshPort $SshTarget $RemoteCommand
    Assert-LastExitCode $Operation
}

foreach ($CommandName in @("ssh", "scp", "tar")) {
    if (-not (Get-Command $CommandName -ErrorAction SilentlyContinue)) {
        throw "Required command '$CommandName' was not found."
    }
}

$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendEnv = Join-Path $RepositoryRoot "backend\.env"
$FrontendEnv = Join-Path $RepositoryRoot "frontend\.env"

foreach ($EnvPath in @($BackendEnv, $FrontendEnv)) {
    if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
        throw "Required environment file is missing: $EnvPath"
    }
}

$DeploymentId = [Guid]::NewGuid().ToString("N")
$ArchivePath = Join-Path ([System.IO.Path]::GetTempPath()) "travel-env-$DeploymentId.tar.gz"
$RemoteArchive = "/root/.travel-env-$DeploymentId.tar.gz"
$SshTarget = "{0}@{1}" -f $ServerUser, $ServerHost

$BootstrapScript = @'
set -euo pipefail
REMOTE_DIR='__REMOTE_DIR__'
GIT_REPOSITORY='__GIT_REPOSITORY__'

mkdir -p "$(dirname "$REMOTE_DIR")"
if [ -d "$REMOTE_DIR/.git" ]; then
    git -C "$REMOTE_DIR" pull --ff-only origin master
elif [ -e "$REMOTE_DIR" ]; then
    if [ -n "$(find "$REMOTE_DIR" -mindepth 1 -print -quit)" ]; then
        echo "Deployment directory exists but is not a Git checkout." >&2
        exit 1
    fi
    rmdir "$REMOTE_DIR"
    git clone --branch master --single-branch "$GIT_REPOSITORY" "$REMOTE_DIR"
else
    git clone --branch master --single-branch "$GIT_REPOSITORY" "$REMOTE_DIR"
fi
'@
$BootstrapScript = $BootstrapScript.Replace("__REMOTE_DIR__", $RemoteDirectory).Replace("__GIT_REPOSITORY__", $GitRepository)

$DeployScript = @'
set -euo pipefail
REMOTE_DIR='__REMOTE_DIR__'
REMOTE_ARCHIVE='__REMOTE_ARCHIVE__'
trap 'rm -f -- "$REMOTE_ARCHIVE"' EXIT

tar -xzf "$REMOTE_ARCHIVE" -C "$REMOTE_DIR" backend/.env frontend/.env
chmod 600 "$REMOTE_DIR/backend/.env" "$REMOTE_DIR/frontend/.env"
cd "$REMOTE_DIR"

export BACKEND_PORT=8081
export FRONTEND_PORT=8080
docker compose -p travel --env-file frontend/.env up -d --build --remove-orphans
docker compose -p travel --env-file frontend/.env ps

wait_for_http() {
    url="$1"
    attempts=30
    while [ "$attempts" -gt 0 ]; do
        if curl --fail --silent --show-error --max-time 5 "$url" >/dev/null 2>&1; then
            return 0
        fi
        attempts=$((attempts - 1))
        sleep 2
    done
    echo "Health check failed." >&2
    return 1
}

wait_for_http "http://127.0.0.1:${BACKEND_PORT}/api/health"
wait_for_http "http://127.0.0.1:${FRONTEND_PORT}/"
'@
$DeployScript = $DeployScript.Replace("__REMOTE_DIR__", $RemoteDirectory).Replace("__REMOTE_ARCHIVE__", $RemoteArchive)

try {
    & tar -czf $ArchivePath -C $RepositoryRoot "backend/.env" "frontend/.env"
    Assert-LastExitCode "Creating the environment archive"

    Invoke-RemoteBash -Script $BootstrapScript -Operation "Updating the remote Git checkout"

    & scp -P $SshPort -- $ArchivePath "${SshTarget}:$RemoteArchive"
    Assert-LastExitCode "Uploading the environment archive"

    Invoke-RemoteBash -Script $DeployScript -Operation "Rebuilding and checking the Docker services"

    Write-Host "Deployment completed successfully."
}
finally {
    if (Test-Path -LiteralPath $ArchivePath) {
        Remove-Item -LiteralPath $ArchivePath -Force
    }
}

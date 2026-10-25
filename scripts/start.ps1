# start.ps1 - Khoi dong AI Agent + Observability stack
$ErrorActionPreference = "Stop"
$AgentProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $AgentProjectRoot
$AgentEnvFile = if ($env:AGENT_ENV_FILE) { $env:AGENT_ENV_FILE } else { Join-Path $AgentProjectRoot ".env" }
$AgentEnvExample = Join-Path $AgentProjectRoot ".env.example"
$AgentComposeFile = Join-Path $AgentProjectRoot "deploy/docker/docker-compose.yml"
$AgentVenvPython = Join-Path $AgentProjectRoot ".venv/Scripts/python.exe"
$AgentPython = if (Test-Path $AgentVenvPython) { $AgentVenvPython } else { "python" }

function Info([string]$msg) {
    Write-Host "[INFO]  $msg" -ForegroundColor Green
}

function Warn([string]$msg) {
    Write-Host "[WARN]  $msg" -ForegroundColor Yellow
}

function Section([string]$msg) {
    Write-Host ""
    Write-Host "--------------------------------------------" -ForegroundColor Yellow
    Write-Host "  $msg" -ForegroundColor Yellow
    Write-Host "--------------------------------------------" -ForegroundColor Yellow
}

function Wait-Url([string]$url, [string]$name, [int]$maxSec = 60) {
    Info "Waiting for $name..."
    $elapsed = 0
    while ($elapsed -lt $maxSec) {
        try {
            Invoke-WebRequest -Uri $url -TimeoutSec 2 -ErrorAction Stop | Out-Null
            Info "$name ready (${elapsed}s)"
            return
        } catch { }
        Start-Sleep -Seconds 2
        $elapsed += 2
        Write-Host "." -NoNewline
    }
    Write-Host ""
    Warn "$name not responding after ${maxSec}s"
}

# --------------------------------------------------
Section "1. Checking prerequisites"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker not found. Install Docker Desktop first."
}
docker info | Out-Null
Info "Docker OK"

$pyVer = & $AgentPython --version 2>&1
Info "Python: $pyVer"

& $AgentPython -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python < 3.9. Run: conda activate agent-class-agentops"
}
Info "Python version OK"

# --------------------------------------------------
Section "2. Checking .env"

if (-not (Test-Path $AgentEnvFile)) {
    Warn ".env not found - copying from .env.example"
    Copy-Item $AgentEnvExample $AgentEnvFile
    Warn "Fill in API keys in .env then press Enter..."
    Read-Host | Out-Null
}

$envContent = Get-Content $AgentEnvFile -Raw
if ($envContent -notmatch "OTEL_EXPORTER_OTLP_ENDPOINT=http") {
    Add-Content $AgentEnvFile "`nOTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318"
    Info "Added OTEL_EXPORTER_OTLP_ENDPOINT to .env"
}
Info ".env OK"

# --------------------------------------------------
Section "3. Installing Python dependencies"

& $AgentPython -m pip install --quiet --upgrade pip
& $AgentPython -m pip install --quiet -r requirements.txt
Info "Python packages installed"

# --------------------------------------------------
Section "4. Starting observability stack"

docker compose -f $AgentComposeFile up -d
Info "Containers started"

Wait-Url "http://localhost:4318" "OTel Collector"
Wait-Url "http://localhost:3000/api/health" "Grafana"

# --------------------------------------------------
Section "5. Service URLs"

Write-Host "  Grafana    -> " -NoNewline; Write-Host "http://localhost:3000" -ForegroundColor Cyan
Write-Host "  Prometheus -> " -NoNewline; Write-Host "http://localhost:9090" -ForegroundColor Cyan
Write-Host "  Tempo      -> " -NoNewline; Write-Host "http://localhost:3200" -ForegroundColor Cyan
Write-Host "  Collector  -> " -NoNewline; Write-Host "http://localhost:4318" -ForegroundColor Cyan
Write-Host "  Agent API  -> " -NoNewline; Write-Host "http://localhost:9004" -ForegroundColor Cyan

# --------------------------------------------------
Section "6. Starting AI Agent server (port 9004)"

Get-Content $AgentEnvFile | ForEach-Object {
    if ($_ -match "^\s*([^#=][^=]*)=(.*)$") {
        [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
    }
}

Info "Starting server..."
& $AgentPython -m src.api.server

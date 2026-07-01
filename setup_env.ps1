# Bootstrap hermes/.env depuis .env.example, export Coolify ou tunnel SSH recette.
# Usage:
#   .\setup_env.ps1                          # copie .env.example -> .env (chemins Windows)
#   .\setup_env.ps1 -FromFile C:\path\hermes.env
#   .\setup_env.ps1 -Tunnel -SshHost user@coolify.nexerp.fr
#   .\setup_env.ps1 -Interactive             # saisie DATABASE_URL + S3 (ne pas coller en chat public)
#   .\setup_env.ps1 -LocalDev                # .env.local.example -> .env (Docker compose local)

param(
    [string]$FromFile,
    [switch]$Tunnel,
    [switch]$LocalDev,
    [string]$SshHost = "user@coolify.nexerp.fr",
    [int]$PgLocalPort = 5433,
    [int]$MinioLocalPort = 9000,
    [switch]$Interactive
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Example = Join-Path $Root ".env.example"
$LocalExample = Join-Path $Root ".env.local.example"
$Target = Join-Path $Root ".env"

if ($LocalDev) {
    if (-not (Test-Path $LocalExample)) { throw "Fichier introuvable : $LocalExample" }
    $Example = $LocalExample
    Write-Host "Mode LocalDev (docker-compose.local.yml)"
}

if (-not (Test-Path $Example)) {
    throw "Fichier introuvable : $Example"
}

function Read-DotEnv([string]$Path) {
    $map = @{}
    Get-Content $Path -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        if ($line -match "^([^=]+)=(.*)$") {
            $map[$Matches[1].Trim()] = $Matches[2].Trim()
        }
    }
    return $map
}

function Write-DotEnv([string]$Path, [hashtable]$Map, [string[]]$Order) {
    $lines = @(
        "# --- Hermes : genere par setup_env.ps1 le $(Get-Date -Format 'dd/MM/yyyy HH:mm') ---",
        "# Ne pas committer (.gitignore). Valeurs depuis Coolify recette ou tunnel SSH.",
        ""
    )
    foreach ($key in $Order) {
        if ($Map.ContainsKey($key)) {
            $lines += "$key=$($Map[$key])"
        }
    }
    foreach ($key in ($Map.Keys | Sort-Object)) {
        if ($key -notin $Order) {
            $lines += "$key=$($Map[$key])"
        }
    }
    $lines += ""
    Set-Content -Path $Path -Value $lines -Encoding UTF8
    Write-Host "OK: $Path"
}

$order = @(
    "DATABASE_URL",
    "EMBED_MODEL", "EMBED_DIM", "EMBED_CACHE_DIR",
    "S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_SECURE", "S3_BUCKET",
    "ANTHROPIC_API_KEY", "ANSWER_MODEL",
    "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
    "CHUNK_SIZE", "CHUNK_OVERLAP",
    "TS_CONFIG", "RERANK_ENABLED", "RERANK_MODEL",
    "ROLE_POLICY", "PERMISSIONS_DEFAULT"
)

$envMap = Read-DotEnv $Example

# Chemins Windows pour dev local (embeddings cache)
$cacheDir = Join-Path $Root "data\models"
$envMap["EMBED_CACHE_DIR"] = $cacheDir.Replace("\", "/")
if (-not (Test-Path $cacheDir)) {
    New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
}

if ($FromFile) {
    if (-not (Test-Path $FromFile)) { throw "Export introuvable : $FromFile" }
    $imported = Read-DotEnv $FromFile
    foreach ($k in $imported.Keys) {
        if ($imported[$k] -and $imported[$k] -ne "CHANGEME") {
            $envMap[$k] = $imported[$k]
        }
    }
    Write-Host "Import depuis $FromFile"
}

if ($Tunnel) {
    # Remplace les hotes Docker recette par localhost (tunnel SSH requis).
    if ($envMap["DATABASE_URL"] -match "@([^:/]+):(\d+)/") {
        $envMap["DATABASE_URL"] = $envMap["DATABASE_URL"] -replace "@([^:/]+):(\d+)/", "@localhost:${PgLocalPort}/"
    }
    if ($envMap["S3_ENDPOINT"] -match "^([^:]+):") {
        $envMap["S3_ENDPOINT"] = "localhost:${MinioLocalPort}"
    }
    if ($envMap["LANGFUSE_HOST"] -match "http://([^:/]+)") {
        $envMap["LANGFUSE_HOST"] = "http://localhost:3000"
    }
    Write-Host "Mode tunnel : DATABASE_URL -> localhost:$PgLocalPort, S3 -> localhost:$MinioLocalPort"
    Write-Host ""
    Write-Host "Ouvrir les tunnels SSH (autre terminal) :"
    Write-Host "  ssh -N -L ${PgLocalPort}:ysprg0oqzl86voh0kv0u6b6q:5432 $SshHost"
    Write-Host "  ssh -N -L ${MinioLocalPort}:minio-r625prgazwx67rtb157fa316:9000 $SshHost"
}

if ($Interactive) {
    Write-Host "Saisie interactive (Ctrl+C pour annuler). Ne pas partager ces valeurs."
    $db = Read-Host "DATABASE_URL (postgresql://...)"
    if ($db) { $envMap["DATABASE_URL"] = $db }
    $ak = Read-Host "S3_ACCESS_KEY"
    if ($ak) { $envMap["S3_ACCESS_KEY"] = $ak }
    $sk = Read-Host "S3_SECRET_KEY"
    if ($sk) { $envMap["S3_SECRET_KEY"] = $sk }
    $api = Read-Host "ANTHROPIC_API_KEY (optionnel, Entree pour ignorer)"
    if ($api) { $envMap["ANTHROPIC_API_KEY"] = $api }
}

Write-DotEnv $Target $envMap $order

$missing = @()
if (-not $envMap["DATABASE_URL"] -or $envMap["DATABASE_URL"] -match "CHANGEME") { $missing += "DATABASE_URL" }
if (-not $envMap["S3_ACCESS_KEY"] -or $envMap["S3_ACCESS_KEY"] -eq "CHANGEME") { $missing += "S3_ACCESS_KEY" }
if (-not $envMap["S3_SECRET_KEY"] -or $envMap["S3_SECRET_KEY"] -eq "CHANGEME") { $missing += "S3_SECRET_KEY" }

if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "ATTENTION : secrets encore manquants : $($missing -join ', ')"
    Write-Host "Recuperer depuis Coolify (app Hermes, env recette) ou :"
    Write-Host "  .\setup_env.ps1 -FromFile C:\chemin\hermes.env"
    Write-Host "  .\setup_env.ps1 -Interactive"
    exit 1
}

Write-Host ""
Write-Host "Configuration prete. Tester :"
Write-Host "  cd $Root"
Write-Host "  python -c `"from app.config import settings; print('db', settings.db_configured, 's3', settings.s3_configured)`""
Write-Host "  python ..\09-CEGID\_ingest_corpus_hermes.py"

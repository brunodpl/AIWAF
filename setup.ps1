# =============================================================
# INSTALADOR PLUG-AND-PLAY - Sistema de Asientos Automatizado
# Ejecutar en PowerShell desde la raiz del proyecto:
#   .\setup.ps1
# =============================================================

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Sistema de Asientos Automatizado - Instalacion inicial" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Verificar Docker
Write-Host "[1/4] Verificando Docker..." -ForegroundColor Yellow
try {
    $dockerVersion = docker --version 2>&1
    Write-Host "      OK: $dockerVersion" -ForegroundColor Green
} catch {
    Write-Host "      ERROR: Docker no esta instalado o no esta en ejecucion." -ForegroundColor Red
    Write-Host "      Descarga Docker Desktop desde: https://www.docker.com/products/docker-desktop/" -ForegroundColor Red
    exit 1
}

try {
    docker info 2>&1 | Out-Null
    Write-Host "      OK: Docker Desktop esta corriendo" -ForegroundColor Green
} catch {
    Write-Host "      ERROR: Docker Desktop no esta corriendo. Arrancalo e intenta de nuevo." -ForegroundColor Red
    exit 1
}

# 2. Localizar credenciales de Google Cloud
Write-Host ""
Write-Host "[2/4] Configurando credenciales de Google Cloud..." -ForegroundColor Yellow

$credentialsDir  = "sistema-de-asientos-automatizado\credentials"
$credentialsFile = "$credentialsDir\service_account.json"

if (-not (Test-Path $credentialsFile)) {
    Write-Host ""
    Write-Host "      No se encontro el archivo de credenciales en:" -ForegroundColor Red
    Write-Host "      $((Get-Location).Path)\$credentialsFile" -ForegroundColor Red
    Write-Host ""
    Write-Host "      Coloca el archivo service_account.json (proporcionado por el administrador)" -ForegroundColor Yellow
    Write-Host "      en la siguiente carpeta y vuelve a ejecutar este script:" -ForegroundColor Yellow
    Write-Host "      $((Get-Location).Path)\$credentialsDir\" -ForegroundColor White
    Write-Host ""
    New-Item -ItemType Directory -Force -Path $credentialsDir | Out-Null
    Write-Host "      (La carpeta ya ha sido creada, solo pega el archivo ahi)" -ForegroundColor Green
    exit 1
}

$absoluteCredentials    = (Resolve-Path $credentialsFile).Path
$credentialsForDocker   = $absoluteCredentials -replace '\\', '/'

Write-Host "      OK: Credenciales encontradas" -ForegroundColor Green
Write-Host "      Ruta: $absoluteCredentials" -ForegroundColor Gray

# 3. Generar archivos de configuracion
Write-Host ""
Write-Host "[3/4] Generando archivos de configuracion..." -ForegroundColor Yellow

# --- .env raiz ---
if (-not (Test-Path ".env")) {
    Add-Content ".env" "# Generado automaticamente por setup.ps1"
    Add-Content ".env" ""
    Add-Content ".env" "# Ruta al archivo de credenciales de Google Cloud"
    Add-Content ".env" "CREDENTIALS_PATH=$credentialsForDocker"
    Add-Content ".env" ""
    Add-Content ".env" "# URL de la API accesible desde el navegador"
    Add-Content ".env" "# - Uso local:    http://localhost:8000"
    Add-Content ".env" "# - Red local:    http://192.168.x.x:8000  (cambia la IP por la del servidor)"
    Add-Content ".env" "NEXT_PUBLIC_API_URL=http://localhost:8000"
    Add-Content ".env" ""
    Add-Content ".env" "# Origenes CORS permitidos (separados por coma)"
    Add-Content ".env" "ALLOWED_ORIGINS=http://localhost:3000,http://interfaz-asientos:3000"
    Write-Host "      OK: .env creado" -ForegroundColor Green
} else {
    Write-Host "      OK: .env ya existe, no se sobreescribe" -ForegroundColor Green
}

# --- .env.docker ---
$envDockerSrc = "sistema-de-asientos-automatizado\.env.docker.example"
$envDockerDst = "sistema-de-asientos-automatizado\.env.docker"

if (-not (Test-Path $envDockerDst)) {
    if (Test-Path $envDockerSrc) {
        Copy-Item $envDockerSrc $envDockerDst
        Write-Host "      OK: .env.docker creado desde plantilla" -ForegroundColor Green
    } else {
        Write-Host "      AVISO: no se encontro .env.docker.example, creando minimo" -ForegroundColor Yellow
        Add-Content $envDockerDst "GOOGLE_CLOUD_PROJECT_ID=tu-project-id-de-gcp"
    }

    Write-Host ""
    $projectId = Read-Host "      Introduce tu Google Cloud Project ID (o pulsa Enter para hacerlo despues)"
    if ($projectId -ne "") {
        (Get-Content $envDockerDst) -replace 'tu-project-id-de-gcp', $projectId | Set-Content $envDockerDst
        Write-Host "      OK: Project ID configurado" -ForegroundColor Green
    }
} else {
    Write-Host "      OK: .env.docker ya existe, no se sobreescribe" -ForegroundColor Green
}

# 4. Arrancar con Docker Compose
Write-Host ""
Write-Host "[4/4] Arrancando el sistema..." -ForegroundColor Yellow
Write-Host "      (La primera vez puede tardar 3-5 minutos mientras se construyen las imagenes)" -ForegroundColor Gray
Write-Host ""

docker compose up --build -d

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  Sistema arrancado correctamente" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Interfaz de revision:  http://localhost:3000" -ForegroundColor Cyan
    Write-Host "  API del pipeline:      http://localhost:8000" -ForegroundColor Cyan
    Write-Host "  Health check:          http://localhost:8000/health" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Para ver los logs en tiempo real:"
    Write-Host "    docker compose logs -f"
    Write-Host ""
    Write-Host "  Para detener el sistema:"
    Write-Host "    docker compose down"
    Write-Host ""
} else {
    Write-Host ""
    Write-Host "  ERROR durante el arranque. Revisa los logs:" -ForegroundColor Red
    Write-Host "    docker compose logs" -ForegroundColor Yellow
    exit 1
}

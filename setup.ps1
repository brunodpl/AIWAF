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

# ---------------------------------------------------------------
# [1/7] Verificar Docker
# ---------------------------------------------------------------
Write-Host "[1/7] Verificando Docker..." -ForegroundColor Yellow
try {
    $dockerVersion = docker --version 2>&1
    Write-Host "      OK: $dockerVersion" -ForegroundColor Green
} catch {
    Write-Host "      ERROR: Docker no esta instalado o no esta en ejecucion." -ForegroundColor Red
    Write-Host "      Descarga Docker Desktop desde: https://www.docker.com/products/docker-desktop/" -ForegroundColor Red
    Write-Host "      O instalalo con: winget install Docker.DockerDesktop" -ForegroundColor Yellow
    exit 1
}

try {
    docker info 2>&1 | Out-Null
    Write-Host "      OK: Docker Desktop esta corriendo" -ForegroundColor Green
} catch {
    Write-Host "      ERROR: Docker Desktop no esta corriendo. Arrancalo e intenta de nuevo." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------
# [2/7] Localizar credenciales de Google Cloud
# ---------------------------------------------------------------
Write-Host ""
Write-Host "[2/7] Configurando credenciales de Google Cloud..." -ForegroundColor Yellow
Write-Host "      La cuenta de servicio necesita los roles:" -ForegroundColor Gray
Write-Host "        - Cloud Vision API User" -ForegroundColor Gray
Write-Host "        - Vertex AI User" -ForegroundColor Gray

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

$absoluteCredentials  = (Resolve-Path $credentialsFile).Path
$credentialsForDocker = $absoluteCredentials -replace '\\', '/'

Write-Host "      OK: Credenciales encontradas" -ForegroundColor Green
Write-Host "      Ruta: $absoluteCredentials" -ForegroundColor Gray

# ---------------------------------------------------------------
# [3/7] Crear estructura de carpetas
# ---------------------------------------------------------------
Write-Host ""
Write-Host "[3/7] Creando estructura de carpetas..." -ForegroundColor Yellow

$sandboxBase = "sistema-de-asientos-automatizado\horeca_sandbox"
$sandboxFolders = @(
    "00_PENDIENTE_CLASIFICAR",
    "20_COMPRAS_GASTOS",
    "21_VENTAS_INGRESOS",
    "22_BIENES_INVERSION",
    "90_PROCESADAS",
    "99_INCIDENCIAS"
)

foreach ($folder in $sandboxFolders) {
    $path = "$sandboxBase\$folder"
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Force -Path $path | Out-Null
        Write-Host "      Creada: $path" -ForegroundColor Gray
    }
}

$otherFolders = @(
    "sistema-de-asientos-automatizado\logs",
    "sistema-de-asientos-automatizado\logs\audit",
    "sistema-de-asientos-automatizado\data\output"
)

foreach ($folder in $otherFolders) {
    if (-not (Test-Path $folder)) {
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        Write-Host "      Creada: $folder" -ForegroundColor Gray
    }
}

Write-Host "      OK: Estructura de carpetas lista" -ForegroundColor Green

# ---------------------------------------------------------------
# [4/7] Generar archivos de configuracion
# ---------------------------------------------------------------
Write-Host ""
Write-Host "[4/7] Generando archivos de configuracion..." -ForegroundColor Yellow

# --- .env raiz (sin NEXT_PUBLIC_API_URL, el proxy lo gestiona) ---
if (-not (Test-Path ".env")) {
    Add-Content ".env" "# Generado automaticamente por setup.ps1"
    Add-Content ".env" ""
    Add-Content ".env" "# Ruta al archivo de credenciales de Google Cloud"
    Add-Content ".env" "CREDENTIALS_PATH=$credentialsForDocker"
    Add-Content ".env" ""
    Add-Content ".env" "# Origenes CORS permitidos (separados por coma)"
    Add-Content ".env" "# Para red local: agregar http://192.168.x.x:3000"
    Add-Content ".env" "ALLOWED_ORIGINS=http://localhost:3003,http://interfaz-asientos:3000"
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
        Add-Content $envDockerDst "GEMINI_OCR_MODEL=gemini-2.5-flash"
        Add-Content $envDockerDst "GEMINI_OCR_LOCATION=europe-west1"
        Add-Content $envDockerDst "GEMINI_ARBITRO_MODEL=gemini-2.5-flash"
        Add-Content $envDockerDst "GEMINI_ARBITRO_LOCATION=europe-west1"
    }

    Write-Host ""
    $projectId = Read-Host "      Introduce tu Google Cloud Project ID"
    if ($projectId -ne "") {
        (Get-Content $envDockerDst) -replace 'tu-project-id-de-gcp', $projectId | Set-Content $envDockerDst
        Write-Host "      OK: Project ID configurado" -ForegroundColor Green
    }

    Write-Host ""
    $region = Read-Host "      Region Vertex AI (pulsa Enter para usar europe-west1)"
    if ($region -ne "" -and $region -ne "europe-west1") {
        (Get-Content $envDockerDst) -replace 'europe-west1', $region | Set-Content $envDockerDst
        Write-Host "      OK: Region configurada: $region" -ForegroundColor Green
    } else {
        Write-Host "      OK: Region: europe-west1 (por defecto)" -ForegroundColor Green
    }
} else {
    Write-Host "      OK: .env.docker ya existe, no se sobreescribe" -ForegroundColor Green
}

# ---------------------------------------------------------------
# [5/7] Arrancar con Docker Compose
# ---------------------------------------------------------------
Write-Host ""
Write-Host "[5/7] Arrancando el sistema..." -ForegroundColor Yellow
Write-Host "      (La primera vez puede tardar 3-5 minutos mientras se construyen las imagenes)" -ForegroundColor Gray
Write-Host ""

docker compose up --build -d

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  ERROR durante el arranque. Revisa los logs:" -ForegroundColor Red
    Write-Host "    docker compose logs" -ForegroundColor Yellow
    exit 1
}

# ---------------------------------------------------------------
# [6/7] Verificar salud de los servicios
# ---------------------------------------------------------------
Write-Host ""
Write-Host "[6/7] Esperando que los servicios esten listos..." -ForegroundColor Yellow

$maxWait = 120
$waited  = 0
$ready   = $false

while ($waited -lt $maxWait) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:8003/health" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch { }

    Write-Host "      Esperando... ($waited s)" -ForegroundColor Gray
    Start-Sleep -Seconds 10
    $waited += 10
}

if (-not $ready) {
    Write-Host "      AVISO: El sistema no responde tras ${maxWait}s. Comprueba los logs:" -ForegroundColor Yellow
    Write-Host "        docker compose logs pipeline-api" -ForegroundColor Yellow
} else {
    Write-Host "      OK: API activa en http://localhost:8003/health" -ForegroundColor Green
}

# ---------------------------------------------------------------
# [7/7] Registrar inicio automatico al arrancar Windows
# ---------------------------------------------------------------
Write-Host ""
Write-Host "[7/7] Configurando inicio automatico..." -ForegroundColor Yellow

$projectPath = (Get-Location).Path
$taskName    = "HorecaApp-AutoStart"
$action      = New-ScheduledTaskAction -Execute "docker" -Argument "compose -f `"$projectPath\docker-compose.yml`" up -d" -WorkingDirectory $projectPath
$trigger     = New-ScheduledTaskTrigger -AtLogOn
$principal   = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest -LogonType Interactive

try {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal | Out-Null
    Write-Host "      OK: Inicio automatico registrado ($taskName)" -ForegroundColor Green
    Write-Host "      Los contenedores se iniciaran automaticamente al arrancar Windows" -ForegroundColor Gray
} catch {
    Write-Host "      AVISO: No se pudo registrar el inicio automatico (requiere permisos de administrador)" -ForegroundColor Yellow
    Write-Host "      Puedes iniciarlo manualmente con: docker compose up -d" -ForegroundColor Gray
}

# ---------------------------------------------------------------
# Resumen final
# ---------------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Sistema instalado correctamente" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Interfaz de revision:    http://localhost:3003" -ForegroundColor Cyan
Write-Host "  API del pipeline:        http://localhost:8003" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Comandos utiles:" -ForegroundColor White
Write-Host "    make start       -> iniciar servicios" -ForegroundColor Gray
Write-Host "    make stop        -> parar servicios" -ForegroundColor Gray
Write-Host "    make logs        -> ver logs en tiempo real" -ForegroundColor Gray
Write-Host "    make status      -> estado de los contenedores" -ForegroundColor Gray
Write-Host "    .\verify.ps1     -> verificar que todo funciona" -ForegroundColor Gray
Write-Host ""
Write-Host "  SIGUIENTE PASO: Abre http://localhost:3003 en el navegador" -ForegroundColor Yellow
Write-Host ""

# =============================================================
# VERIFICACION POST-INSTALACION — Sistema de Asientos Automatizado
# Ejecutar en PowerShell desde la raiz del proyecto:
#   .\verify.ps1
# =============================================================

$ok   = 0
$warn = 0
$fail = 0

function Check-OK   ($msg) { Write-Host "  [OK]   $msg" -ForegroundColor Green;  $global:ok++ }
function Check-WARN ($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow; $global:warn++ }
function Check-FAIL ($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red;    $global:fail++ }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Verificacion del sistema — Sistema de Asientos Automatizado" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# --- API backend ---
try {
    $res = Invoke-WebRequest -Uri "http://localhost:8003/health" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
    if ($res.StatusCode -eq 200) {
        Check-OK "API activa en http://localhost:8003"
    } else {
        Check-FAIL "API responde con status $($res.StatusCode)"
    }
} catch {
    Check-FAIL "API no responde en http://localhost:8003/health — ejecuta: docker compose up -d"
}

# --- Frontend ---
try {
    $res = Invoke-WebRequest -Uri "http://localhost:3003" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
    if ($res.StatusCode -eq 200) {
        Check-OK "Interfaz activa en http://localhost:3003"
    } else {
        Check-FAIL "Interfaz responde con status $($res.StatusCode)"
    }
} catch {
    Check-FAIL "Interfaz no responde en http://localhost:3003 — ejecuta: docker compose up -d"
}

# --- Credenciales GCP ---
$credFile = "sistema-de-asientos-automatizado\credentials\service_account.json"
if (Test-Path $credFile) {
    Check-OK "Credenciales GCP encontradas (service_account.json)"
} else {
    Check-FAIL "Credenciales GCP no encontradas en $credFile"
}

# --- Configuracion GCP ---
$envDocker = "sistema-de-asientos-automatizado\.env.docker"
if (Test-Path $envDocker) {
    $projectId = (Get-Content $envDocker | Select-String "GOOGLE_CLOUD_PROJECT_ID=") -replace "GOOGLE_CLOUD_PROJECT_ID=", ""
    if ($projectId -and $projectId -ne "tu-project-id-de-gcp") {
        Check-OK "Google Cloud Project ID configurado"
    } else {
        Check-WARN "GOOGLE_CLOUD_PROJECT_ID no configurado en .env.docker — edita el archivo y define el valor"
    }
} else {
    Check-FAIL ".env.docker no existe — ejecuta .\setup.ps1"
}

# --- Carpetas de trabajo ---
$sandboxFolders = @(
    "sistema-de-asientos-automatizado\horeca_sandbox\00_PENDIENTE_CLASIFICAR",
    "sistema-de-asientos-automatizado\horeca_sandbox\20_COMPRAS_GASTOS",
    "sistema-de-asientos-automatizado\horeca_sandbox\21_VENTAS_INGRESOS",
    "sistema-de-asientos-automatizado\horeca_sandbox\22_BIENES_INVERSION",
    "sistema-de-asientos-automatizado\horeca_sandbox\90_PROCESADAS",
    "sistema-de-asientos-automatizado\horeca_sandbox\99_INCIDENCIAS"
)

$missingFolders = $sandboxFolders | Where-Object { -not (Test-Path $_) }
if ($missingFolders.Count -eq 0) {
    Check-OK "Carpetas de trabajo creadas (horeca_sandbox)"
} else {
    Check-WARN "Faltan $($missingFolders.Count) carpetas en horeca_sandbox — ejecuta .\setup.ps1"
}

# --- Clientes ---
$clientsFile = "sistema-de-asientos-automatizado\data\clients.json"
if (Test-Path $clientsFile) {
    $content = Get-Content $clientsFile -Raw
    if ($content -and $content.Trim() -ne "[]" -and $content.Trim() -ne "") {
        Check-OK "data/clients.json tiene datos de clientes"
    } else {
        Check-WARN "data/clients.json esta vacio — añade los clientes de la gestoria antes de procesar facturas"
    }
} else {
    Check-WARN "data/clients.json no existe — el pipeline no podra resolver clientes destino"
}

# --- Versión instalada (endpoint /api/system/version) ---
try {
    $resp = Invoke-WebRequest -Uri "http://localhost:8003/api/system/version" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
    $info = $resp.Content | ConvertFrom-Json
    if ($info.version -and $info.version -ne "0.0.0-dev") {
        Check-OK "Versión instalada: $($info.version)"
    } else {
        Check-WARN "Versión instalada: $($info.version) — define AIWAF_VERSION en docker-compose.yml para releases"
    }
    if ($info.gestoria_nombre) {
        Check-OK "Gestoría identificada: $($info.gestoria_nombre)"
    } else {
        Check-WARN "AIWAF_GESTORIA_NOMBRE no configurado — los reportes de feedback no incluirán identificación"
    }
} catch {
    Check-WARN "No se pudo leer /api/system/version (¿reinicia el contenedor tras configurar?)"
}

# --- Webhook feedback configurado ---
$envRoot = ".env"
if (Test-Path $envRoot) {
    $webhookLine = (Get-Content $envRoot | Select-String "FEEDBACK_WEBHOOK_URL=") -replace "FEEDBACK_WEBHOOK_URL=", ""
    if ($webhookLine -and $webhookLine.Trim() -ne "") {
        Check-OK "Webhook de feedback configurado"
    } else {
        Check-WARN "FEEDBACK_WEBHOOK_URL vacío — los reportes solo se guardarán localmente"
    }
}

# --- Maestros ---
$maestros = @(
    "sistema-de-asientos-automatizado\data\maestros\maestro_clientes.yaml",
    "sistema-de-asientos-automatizado\data\maestros\catalogo_semantica.yaml"
)
$missingMaestros = $maestros | Where-Object { -not (Test-Path $_) }
if ($missingMaestros.Count -eq 0) {
    Check-OK "Archivos maestros presentes"
} else {
    Check-WARN "Faltan maestros: $($missingMaestros -join ', ')"
}

# --- Resumen ---
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Resumen:" -ForegroundColor Cyan
Write-Host "    OK:   $ok" -ForegroundColor Green
Write-Host "    WARN: $warn" -ForegroundColor Yellow
Write-Host "    FAIL: $fail" -ForegroundColor Red
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

if ($fail -gt 0) {
    Write-Host "  El sistema tiene problemas criticos. Revisa los mensajes FAIL." -ForegroundColor Red
    Write-Host "  Ejecuta .\setup.ps1 si aun no has completado la instalacion." -ForegroundColor Yellow
    exit 1
} elseif ($warn -gt 0) {
    Write-Host "  El sistema esta operativo pero hay avisos. Revisa los mensajes WARN." -ForegroundColor Yellow
    exit 0
} else {
    Write-Host "  Todo en orden. El sistema esta listo para usar." -ForegroundColor Green
    Write-Host "  Abre http://localhost:3003 en el navegador." -ForegroundColor Cyan
    exit 0
}

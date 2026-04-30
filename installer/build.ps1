<#
.SYNOPSIS
  Construye el instalador aiwaf-setup-X.Y.Z.exe usando Inno Setup.

.DESCRIPTION
  Requisitos previos en la máquina de Bruno (build):
    1. Inno Setup 6+ instalado (https://jrsoftware.org/isdl.php)
    2. ISCC.exe en el PATH o en C:\Program Files (x86)\Inno Setup 6\
    3. (Opcional) Variable de entorno AIWAF_FEEDBACK_WEBHOOK con la URL del webhook Discord

.PARAMETER Version
  Versión a inyectar en el .iss y en .env (ej: 0.2.0).

.PARAMETER Webhook
  URL del webhook Discord para feedback. Si se omite, se lee de $env:AIWAF_FEEDBACK_WEBHOOK.

.EXAMPLE
  .\build.ps1 -Version 0.2.0
  .\build.ps1 -Version 0.2.0 -Webhook "https://discord.com/api/webhooks/.../..."
#>
param(
  [Parameter(Mandatory = $true)] [string]$Version,
  [string]$Webhook = $env:AIWAF_FEEDBACK_WEBHOOK
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$iss = Join-Path $here 'aiwaf-setup.iss'

# Localizar ISCC
$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
  $candidate = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
  if (Test-Path $candidate) { $iscc = $candidate }
}
if (-not $iscc) { throw "ISCC.exe no encontrado. Instala Inno Setup 6 desde https://jrsoftware.org/isdl.php" }

# Inyectar Webhook como variable de entorno (lo lee el .iss en CurStepChanged)
if ($Webhook) {
  $env:AIWAF_FEEDBACK_WEBHOOK = $Webhook
  Write-Host "Webhook embebido en el instalador."
} else {
  Write-Warning "No se proporcionó webhook. El feedback solo se guardará localmente."
}

# Pasar versión al .iss via /D
$out = Join-Path (Split-Path -Parent $here) 'dist'
New-Item -ItemType Directory -Path $out -Force | Out-Null

& $iscc /Q "/DMyAppVersion=$Version" $iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup falló con código $LASTEXITCODE" }

$exe = Join-Path $out "aiwaf-setup-$Version.exe"
if (Test-Path $exe) {
  Write-Host "Instalador generado: $exe" -ForegroundColor Green
} else {
  Write-Warning "El instalador se compiló pero no se encontró el .exe esperado en $exe"
}

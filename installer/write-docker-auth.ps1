<#
.SYNOPSIS
  Escribe un docker config.json mínimo con auth base64 explícita en $ConfigDir.
  Watchtower (contenedor Linux) lo monta como /config.json para autenticarse en GHCR.

.NOTES
  No reusamos ~/.docker/config.json porque Docker Desktop en Windows almacena el token
  en el credential manager del SO, no en el archivo. Watchtower no puede leer ese
  credential manager desde un contenedor Linux.
#>
param(
  [Parameter(Mandatory = $true)] [string]$Token,
  [Parameter(Mandatory = $true)] [string]$ConfigDir,
  [string]$Username = 'brunodpl'
)

$ErrorActionPreference = 'Stop'
$auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$Username`:$Token"))
$json = '{"auths":{"ghcr.io":{"auth":"' + $auth + '"}}}'
New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
Set-Content -Path (Join-Path $ConfigDir 'config.json') -Value $json -Encoding ascii -NoNewline
Write-Host "Docker auth para Watchtower escrito en $ConfigDir\config.json"

#!/bin/bash
# =============================================================
# INSTALADOR PLUG-AND-PLAY — Sistema de Asientos Automatizado
# Ejecutar desde la raiz del proyecto:
#   bash setup.sh
# =============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
GRAY='\033[0;37m'
NC='\033[0m'

echo ""
echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}  Sistema de Asientos Automatizado — Instalacion inicial${NC}"
echo -e "${CYAN}============================================================${NC}"
echo ""

# ---------------------------------------------------------------
# [1/7] Verificar Docker
# ---------------------------------------------------------------
echo -e "${YELLOW}[1/7] Verificando Docker...${NC}"
if ! command -v docker &> /dev/null; then
    echo -e "${RED}      ERROR: Docker no esta instalado.${NC}"
    echo -e "${RED}      Instala Docker desde: https://docs.docker.com/get-docker/${NC}"
    exit 1
fi

if ! docker info &> /dev/null; then
    echo -e "${RED}      ERROR: Docker no esta corriendo. Arrancalo e intenta de nuevo.${NC}"
    exit 1
fi

echo -e "${GREEN}      OK: $(docker --version)${NC}"

# ---------------------------------------------------------------
# [2/7] Localizar credenciales
# ---------------------------------------------------------------
echo ""
echo -e "${YELLOW}[2/7] Configurando credenciales de Google Cloud...${NC}"
echo -e "${GRAY}      La cuenta de servicio necesita los roles:${NC}"
echo -e "${GRAY}        - Cloud Vision API User${NC}"
echo -e "${GRAY}        - Vertex AI User${NC}"

CREDENTIALS_DIR="sistema-de-asientos-automatizado/credentials"
CREDENTIALS_FILE="$CREDENTIALS_DIR/service_account.json"

if [ ! -f "$CREDENTIALS_FILE" ]; then
    echo -e "${RED}      No se encontro el archivo de credenciales en:${NC}"
    echo -e "${RED}      $(pwd)/$CREDENTIALS_FILE${NC}"
    echo ""
    echo -e "${YELLOW}      Coloca el archivo service_account.json en:${NC}"
    echo -e "      $(pwd)/$CREDENTIALS_DIR/"
    mkdir -p "$CREDENTIALS_DIR"
    echo -e "${GREEN}      (La carpeta ya ha sido creada, solo pega el archivo ahi)${NC}"
    exit 1
fi

ABSOLUTE_CREDENTIALS="$(cd "$(dirname "$CREDENTIALS_FILE")" && pwd)/$(basename "$CREDENTIALS_FILE")"
echo -e "${GREEN}      OK: Credenciales encontradas${NC}"
echo -e "${GRAY}      Ruta: $ABSOLUTE_CREDENTIALS${NC}"

# ---------------------------------------------------------------
# [3/7] Crear estructura de carpetas
# ---------------------------------------------------------------
echo ""
echo -e "${YELLOW}[3/7] Creando estructura de carpetas...${NC}"

SANDBOX_DIRS=(
    "sistema-de-asientos-automatizado/horeca_sandbox/00_PENDIENTE_CLASIFICAR"
    "sistema-de-asientos-automatizado/horeca_sandbox/20_COMPRAS_GASTOS"
    "sistema-de-asientos-automatizado/horeca_sandbox/21_VENTAS_INGRESOS"
    "sistema-de-asientos-automatizado/horeca_sandbox/22_BIENES_INVERSION"
    "sistema-de-asientos-automatizado/horeca_sandbox/90_PROCESADAS"
    "sistema-de-asientos-automatizado/horeca_sandbox/99_INCIDENCIAS"
    "sistema-de-asientos-automatizado/logs"
    "sistema-de-asientos-automatizado/logs/audit"
    "sistema-de-asientos-automatizado/data/output"
)

for dir in "${SANDBOX_DIRS[@]}"; do
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir"
        echo -e "${GRAY}      Creada: $dir${NC}"
    fi
done

echo -e "${GREEN}      OK: Estructura de carpetas lista${NC}"

# ---------------------------------------------------------------
# [4/7] Generar archivos de configuracion
# ---------------------------------------------------------------
echo ""
echo -e "${YELLOW}[4/7] Generando archivos de configuracion...${NC}"

# --- .env raiz (sin NEXT_PUBLIC_API_URL — el proxy lo gestiona) ---
if [ ! -f ".env" ]; then
    cat > .env << EOF
# Generado automaticamente por setup.sh

# Ruta al archivo de credenciales de Google Cloud
CREDENTIALS_PATH=$ABSOLUTE_CREDENTIALS

# Origenes CORS permitidos (separados por coma)
# Para red local: añade http://192.168.x.x:3000
ALLOWED_ORIGINS=http://localhost:3000,http://interfaz-asientos:3000
EOF
    echo -e "${GREEN}      OK: .env creado${NC}"
else
    echo -e "${GREEN}      OK: .env ya existe, no se sobreescribe${NC}"
fi

# --- .env.docker ---
ENV_DOCKER_SRC="sistema-de-asientos-automatizado/.env.docker.example"
ENV_DOCKER_DST="sistema-de-asientos-automatizado/.env.docker"

if [ ! -f "$ENV_DOCKER_DST" ]; then
    # Fallback si no existe el ejemplo
    if [ -f "$ENV_DOCKER_SRC" ]; then
        cp "$ENV_DOCKER_SRC" "$ENV_DOCKER_DST"
        echo -e "${GREEN}      OK: .env.docker creado desde plantilla${NC}"
    else
        echo -e "${YELLOW}      AVISO: no se encontro .env.docker.example, creando minimo${NC}"
        touch "$ENV_DOCKER_DST"
        echo "GOOGLE_CLOUD_PROJECT_ID=tu-project-id-de-gcp" >> "$ENV_DOCKER_DST"
        echo "GEMINI_OCR_MODEL=gemini-2.5-flash" >> "$ENV_DOCKER_DST"
        echo "GEMINI_OCR_LOCATION=europe-west1" >> "$ENV_DOCKER_DST"
        echo "GEMINI_ARBITRO_MODEL=gemini-2.5-flash" >> "$ENV_DOCKER_DST"
        echo "GEMINI_ARBITRO_LOCATION=europe-west1" >> "$ENV_DOCKER_DST"
    fi

    echo ""
    echo -e "${YELLOW}      Introduce tu Google Cloud Project ID:${NC}"
    read -r PROJECT_ID
    if [ -n "$PROJECT_ID" ]; then
        sed -i "s/tu-project-id-de-gcp/$PROJECT_ID/" "$ENV_DOCKER_DST"
        echo -e "${GREEN}      OK: Project ID configurado${NC}"
    fi

    echo ""
    echo -e "${YELLOW}      Region Vertex AI (pulsa Enter para usar europe-west1):${NC}"
    read -r REGION
    if [ -n "$REGION" ] && [ "$REGION" != "europe-west1" ]; then
        sed -i "s/europe-west1/$REGION/g" "$ENV_DOCKER_DST"
        echo -e "${GREEN}      OK: Region configurada: $REGION${NC}"
    else
        echo -e "${GREEN}      OK: Region: europe-west1 (por defecto)${NC}"
    fi
else
    echo -e "${GREEN}      OK: .env.docker ya existe, no se sobreescribe${NC}"
fi

# ---------------------------------------------------------------
# [5/7] Arrancar con Docker Compose
# ---------------------------------------------------------------
echo ""
echo -e "${YELLOW}[5/7] Arrancando el sistema...${NC}"
echo -e "${GRAY}      (La primera vez puede tardar 3-5 minutos)${NC}"
echo ""

docker compose up --build -d

# ---------------------------------------------------------------
# [6/7] Verificar salud de los servicios
# ---------------------------------------------------------------
echo ""
echo -e "${YELLOW}[6/7] Esperando que los servicios esten listos...${NC}"

MAX_WAIT=120
WAITED=0
READY=false

while [ "$WAITED" -lt "$MAX_WAIT" ]; do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        READY=true
        break
    fi
    echo -e "${GRAY}      Esperando... (${WAITED}s)${NC}"
    sleep 10
    WAITED=$((WAITED + 10))
done

if [ "$READY" = false ]; then
    echo -e "${YELLOW}      AVISO: El sistema no responde tras ${MAX_WAIT}s. Comprueba los logs:${NC}"
    echo "        docker compose logs pipeline-api"
else
    echo -e "${GREEN}      OK: API activa en http://localhost:8000/health${NC}"
fi

# ---------------------------------------------------------------
# [7/7] Registrar inicio automatico (systemd)
# ---------------------------------------------------------------
echo ""
echo -e "${YELLOW}[7/7] Configurando inicio automatico (systemd)...${NC}"

PROJECT_PATH="$(pwd)"
SERVICE_FILE="/etc/systemd/system/horeca-app.service"

if command -v systemctl &> /dev/null && [ "$(id -u)" -eq 0 ]; then
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Sistema de Asientos Automatizado HORECA
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$PROJECT_PATH
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
User=$SUDO_USER

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable horeca-app.service
    echo -e "${GREEN}      OK: Servicio systemd registrado y habilitado${NC}"
    echo -e "${GRAY}      Los contenedores se iniciaran automaticamente con el sistema${NC}"
elif command -v systemctl &> /dev/null; then
    echo -e "${YELLOW}      AVISO: Para registrar el inicio automatico, ejecuta como root:${NC}"
    echo -e "${GRAY}        sudo bash setup.sh${NC}"
else
    echo -e "${GRAY}      INFO: systemd no disponible. Inicia manualmente con: docker compose up -d${NC}"
fi

# ---------------------------------------------------------------
# Resumen final
# ---------------------------------------------------------------
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  Sistema instalado correctamente${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo -e "  ${CYAN}Interfaz de revision:    http://localhost:3000${NC}"
echo -e "  ${CYAN}Gestion de contenedores: http://localhost:9000  (Portainer)${NC}"
echo -e "  ${CYAN}API del pipeline:        http://localhost:8000${NC}"
echo ""
echo "  Comandos utiles:"
echo "    make start       — iniciar servicios"
echo "    make stop        — parar servicios"
echo "    make logs        — ver logs en tiempo real"
echo "    make status      — estado de los contenedores"
echo "    bash verify.sh   — verificar que todo funciona"
echo ""
echo -e "  ${YELLOW}SIGUIENTE PASO: Abre http://localhost:3000 en el navegador${NC}"
echo ""

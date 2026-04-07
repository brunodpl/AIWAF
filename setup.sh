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

# 1. Verificar Docker
echo -e "${YELLOW}[1/4] Verificando Docker...${NC}"
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

# 2. Localizar credenciales
echo ""
echo -e "${YELLOW}[2/4] Configurando credenciales de Google Cloud...${NC}"

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

# 3. Generar archivos de configuracion
echo ""
echo -e "${YELLOW}[3/4] Generando archivos de configuracion...${NC}"

if [ ! -f ".env" ]; then
    cat > .env << EOF
# Generado automaticamente por setup.sh

# Ruta al archivo de credenciales de Google Cloud
CREDENTIALS_PATH=$ABSOLUTE_CREDENTIALS

# URL de la API accesible desde el navegador
NEXT_PUBLIC_API_URL=http://localhost:8000

# Origenes CORS permitidos
ALLOWED_ORIGINS=http://localhost:3000,http://interfaz-asientos:3000
EOF
    echo -e "${GREEN}      OK: .env creado${NC}"
else
    echo -e "${GREEN}      OK: .env ya existe, no se sobreescribe${NC}"
fi

ENV_DOCKER_SRC="sistema-de-asientos-automatizado/.env.docker.example"
ENV_DOCKER_DST="sistema-de-asientos-automatizado/.env.docker"

if [ ! -f "$ENV_DOCKER_DST" ]; then
    cp "$ENV_DOCKER_SRC" "$ENV_DOCKER_DST"
    echo -e "${GREEN}      OK: .env.docker creado desde plantilla${NC}"
    echo ""
    echo -e "${YELLOW}      Introduce tu Google Cloud Project ID (o pulsa Enter para hacerlo despues):${NC}"
    read -r PROJECT_ID
    if [ -n "$PROJECT_ID" ]; then
        sed -i "s/tu-project-id-de-gcp/$PROJECT_ID/" "$ENV_DOCKER_DST"
        echo -e "${GREEN}      OK: Project ID configurado${NC}"
    fi
else
    echo -e "${GREEN}      OK: .env.docker ya existe, no se sobreescribe${NC}"
fi

# 4. Arrancar
echo ""
echo -e "${YELLOW}[4/4] Arrancando el sistema...${NC}"
echo -e "${GRAY}      (La primera vez puede tardar 3-5 minutos)${NC}"
echo ""

docker compose up --build -d

echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  Sistema arrancado correctamente${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo -e "  ${CYAN}Interfaz de revision:  http://localhost:3000${NC}"
echo -e "  ${CYAN}API del pipeline:      http://localhost:8000${NC}"
echo -e "  ${CYAN}Health check:          http://localhost:8000/health${NC}"
echo ""
echo "  Para ver logs en tiempo real:"
echo "    docker compose logs -f"
echo ""
echo "  Para detener el sistema:"
echo "    docker compose down"
echo ""

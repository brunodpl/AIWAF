#!/bin/bash
# =============================================================
# VERIFICACION POST-INSTALACION — Sistema de Asientos Automatizado
# Ejecutar desde la raiz del proyecto:
#   bash verify.sh
# =============================================================

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

OK_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

check_ok()   { echo -e "  ${GREEN}[OK]${NC}   $1"; OK_COUNT=$((OK_COUNT + 1)); }
check_warn() { echo -e "  ${YELLOW}[WARN]${NC} $1"; WARN_COUNT=$((WARN_COUNT + 1)); }
check_fail() { echo -e "  ${RED}[FAIL]${NC} $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

echo ""
echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}  Verificacion del sistema — Sistema de Asientos Automatizado${NC}"
echo -e "${CYAN}============================================================${NC}"
echo ""

# --- API backend ---
if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    check_ok "API activa en http://localhost:8000"
else
    check_fail "API no responde en http://localhost:8000/health — ejecuta: docker compose up -d"
fi

# --- Frontend ---
if curl -sf http://localhost:3000 > /dev/null 2>&1; then
    check_ok "Interfaz activa en http://localhost:3000"
else
    check_fail "Interfaz no responde en http://localhost:3000 — ejecuta: docker compose up -d"
fi

# --- Portainer ---
if curl -sf http://localhost:9000 > /dev/null 2>&1; then
    check_ok "Portainer activo en http://localhost:9000"
else
    check_warn "Portainer no responde en http://localhost:9000 (opcional)"
fi

# --- Credenciales GCP ---
CRED_FILE="sistema-de-asientos-automatizado/credentials/service_account.json"
if [ -f "$CRED_FILE" ]; then
    check_ok "Credenciales GCP encontradas (service_account.json)"
else
    check_fail "Credenciales GCP no encontradas en $CRED_FILE"
fi

# --- Configuracion GCP ---
ENV_DOCKER="sistema-de-asientos-automatizado/.env.docker"
if [ -f "$ENV_DOCKER" ]; then
    PROJECT_ID=$(grep "GOOGLE_CLOUD_PROJECT_ID=" "$ENV_DOCKER" | cut -d'=' -f2)
    if [ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "tu-project-id-de-gcp" ]; then
        check_ok "Google Cloud Project ID configurado"
    else
        check_warn "GOOGLE_CLOUD_PROJECT_ID no configurado en .env.docker — edita el archivo"
    fi
else
    check_fail ".env.docker no existe — ejecuta bash setup.sh"
fi

# --- Carpetas de trabajo ---
SANDBOX_DIRS=(
    "sistema-de-asientos-automatizado/horeca_sandbox/00_PENDIENTE_CLASIFICAR"
    "sistema-de-asientos-automatizado/horeca_sandbox/20_COMPRAS_GASTOS"
    "sistema-de-asientos-automatizado/horeca_sandbox/21_VENTAS_INGRESOS"
    "sistema-de-asientos-automatizado/horeca_sandbox/22_BIENES_INVERSION"
    "sistema-de-asientos-automatizado/horeca_sandbox/90_PROCESADAS"
    "sistema-de-asientos-automatizado/horeca_sandbox/99_INCIDENCIAS"
)

MISSING=0
for dir in "${SANDBOX_DIRS[@]}"; do
    [ ! -d "$dir" ] && MISSING=$((MISSING + 1))
done

if [ "$MISSING" -eq 0 ]; then
    check_ok "Carpetas de trabajo creadas (horeca_sandbox)"
else
    check_warn "Faltan $MISSING carpetas en horeca_sandbox — ejecuta bash setup.sh"
fi

# --- Clientes ---
CLIENTS_FILE="sistema-de-asientos-automatizado/data/clients.json"
if [ -f "$CLIENTS_FILE" ]; then
    CONTENT=$(cat "$CLIENTS_FILE" | tr -d '[:space:]')
    if [ "$CONTENT" != "[]" ] && [ -n "$CONTENT" ]; then
        check_ok "data/clients.json tiene datos de clientes"
    else
        check_warn "data/clients.json esta vacio — añade los clientes de la gestoria antes de procesar facturas"
    fi
else
    check_warn "data/clients.json no existe — el pipeline no podra resolver clientes destino"
fi

# --- Maestros ---
for maestro in \
    "sistema-de-asientos-automatizado/data/maestros/maestro_clientes.yaml" \
    "sistema-de-asientos-automatizado/data/maestros/catalogo_semantica.yaml"; do
    if [ ! -f "$maestro" ]; then
        check_warn "Falta maestro: $maestro"
    fi
done

if [ $WARN_COUNT -eq 0 ] && [ $FAIL_COUNT -eq 0 ]; then
    check_ok "Archivos maestros presentes"
fi

# --- Resumen ---
echo ""
echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}  Resumen:${NC}"
echo -e "    ${GREEN}OK:   $OK_COUNT${NC}"
echo -e "    ${YELLOW}WARN: $WARN_COUNT${NC}"
echo -e "    ${RED}FAIL: $FAIL_COUNT${NC}"
echo -e "${CYAN}============================================================${NC}"
echo ""

if [ "$FAIL_COUNT" -gt 0 ]; then
    echo -e "${RED}  El sistema tiene problemas criticos. Revisa los mensajes FAIL.${NC}"
    echo -e "${YELLOW}  Ejecuta bash setup.sh si aun no has completado la instalacion.${NC}"
    exit 1
elif [ "$WARN_COUNT" -gt 0 ]; then
    echo -e "${YELLOW}  El sistema esta operativo pero hay avisos. Revisa los mensajes WARN.${NC}"
    exit 0
else
    echo -e "${GREEN}  Todo en orden. El sistema esta listo para usar.${NC}"
    echo -e "${CYAN}  Abre http://localhost:3000 en el navegador.${NC}"
    exit 0
fi

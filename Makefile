# =============================================================
# Makefile — Sistema de Asientos Automatizado HORECA
# Uso: make <comando>
# =============================================================

.PHONY: start stop restart rebuild logs status update reset-output help

## Iniciar todos los servicios (sin rebuild)
start:
	docker compose up -d

## Parar todos los servicios
stop:
	docker compose down

## Reiniciar todos los servicios (sin rebuild)
restart:
	docker compose restart

## Rebuild completo + iniciar (necesario tras cambios en código)
rebuild:
	docker compose up --build -d

## Ver logs en tiempo real (Ctrl+C para salir)
logs:
	docker compose logs -f

## Ver logs solo del pipeline (backend)
logs-api:
	docker compose logs -f pipeline-api

## Ver logs solo de la interfaz (frontend)
logs-ui:
	docker compose logs -f interfaz

## Estado de los contenedores y health checks
status:
	docker compose ps

## Descargar última versión de las imágenes e iniciar
update:
	docker compose pull && docker compose up -d

## Borrar resultados de ejecuciones anteriores
## (mantiene maestros, clientes y logs de auditoría)
reset-output:
	rm -rf sistema-de-asientos-automatizado/data/output/*

## Mostrar esta ayuda
help:
	@echo ""
	@echo "Comandos disponibles:"
	@echo "  make start         Iniciar servicios"
	@echo "  make stop          Parar servicios"
	@echo "  make restart       Reiniciar sin rebuild"
	@echo "  make rebuild       Rebuild completo (tras cambios en código)"
	@echo "  make logs          Ver todos los logs en tiempo real"
	@echo "  make logs-api      Ver logs del backend"
	@echo "  make logs-ui       Ver logs del frontend"
	@echo "  make status        Estado de los contenedores"
	@echo "  make update        Actualizar a la última versión"
	@echo "  make reset-output  Borrar resultados previos"
	@echo ""

.DEFAULT_GOAL := help

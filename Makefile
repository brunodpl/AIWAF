# =============================================================
# Makefile — Sistema de Asientos Automatizado HORECA
# Uso: make <comando>
# =============================================================

.PHONY: start stop restart rebuild logs status update reset-output release deadcode deadcode-front deadcode-back help

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

## Publicar nueva release: tag git + push (la GitHub Action construye y publica las imágenes)
## Uso:  make release VERSION=1.2.3
release:
	@if [ -z "$(VERSION)" ]; then echo "ERROR: define VERSION (ej: make release VERSION=1.2.3)"; exit 1; fi
	@echo "Publicando release v$(VERSION)..."
	git tag -a v$(VERSION) -m "Release v$(VERSION)"
	git push origin v$(VERSION)
	@echo "Tag v$(VERSION) empujado. La GitHub Action construirá y publicará las imágenes."

## Detectar código muerto en el frontend (knip)
deadcode-front:
	cd interfaz-asientos-automatizados && pnpm exec knip

## Detectar código muerto en el backend (ruff + vulture)
deadcode-back:
	cd sistema-de-asientos-automatizado && ruff check src tests && vulture src --min-confidence 80

## Detectar código muerto en todo el repo (frontend + backend)
deadcode: deadcode-front deadcode-back

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
	@echo "  make release VERSION=x.y.z  Taggear y publicar una release"
	@echo "  make deadcode      Detectar código muerto (frontend + backend)"
	@echo ""

.DEFAULT_GOAL := help

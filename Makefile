# Makefile for easy Docker management

.PHONY: help build up down restart logs clean

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Available targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build: ## Build Docker images
	docker compose build

up: ## Start services in background
	docker compose up -d

down: ## Stop all services
	docker compose down

restart: ## Restart bot service
	docker compose restart bot

logs: ## Show bot logs (follow mode)
	docker compose logs -f bot

clean: ## Remove containers, volumes, and images
	docker compose down -v --rmi local

# Production targets
prod-build: ## Build production Docker images
	docker compose -f docker-compose.prod.yml build

prod-up: ## Start production services
	docker compose -f docker-compose.prod.yml up -d

prod-down: ## Stop production services
	docker compose -f docker-compose.prod.yml down

prod-logs: ## Show production bot logs
	docker compose -f docker-compose.prod.yml logs -f bot

prod-restart: ## Restart production bot
	docker compose -f docker-compose.prod.yml restart bot

# Database management
db-backup: ## Backup SQLite database
	docker compose cp bot:/app/bot.db ./backup-bot-$$(date +%Y%m%d-%H%M%S).db

db-pg-backup: ## Backup PostgreSQL database
	docker compose exec postgres pg_dump -U modbot modbot > backup-$$(date +%Y%m%d-%H%M%S).sql

# Development helpers
dev: ## Start in development mode with logs
	docker compose up --build

shell: ## Open shell in bot container
	docker compose exec bot /bin/bash

ps: ## Show running containers
	docker compose ps

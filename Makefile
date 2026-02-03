ifndef APP_ENV
    include .env
endif

.DEFAULT_GOAL := help
.PHONY: help
help:
	@awk 'BEGIN {FS = ":.*?## "}; /^[a-zA-Z0-9_-]+:.*?## .*$$/ {printf "\033[32m%-20s\033[0m %s\n", $$1, $$2}' Makefile | sort

###> backend/docker ###
up: ## Start backend services (server + mcp + ollama)
	@touch .env.local
	docker compose --env-file .env.local up -d

stop: ## Stop running containers
	docker compose stop

down: ## Stop and remove containers
	docker compose down

ps: ## Show container status
	docker compose ps

logs: ## Follow all logs
	docker compose logs -f --tail=200

logs-server: ## Follow server logs
	docker compose logs -f server

logs-mcp: ## Follow MCP logs
	docker compose logs -f mcp

logs-ollama: ## Follow Ollama logs
	docker compose logs -f ollama

build: ## Build images
	docker compose build

rebuild: ## Rebuild images (no cache)
	docker compose build --no-cache

shell-server: ## Shell into server container
	docker compose exec server sh

shell-mcp: ## Shell into MCP container
	docker compose exec mcp sh

shell-ollama: ## Shell into Ollama container
	docker compose exec ollama sh

pull: ## Pull a model into Ollama (usage: make pull MODEL=llama3.1:8b)
	@test -n "$(MODEL)" || (echo "ERROR: set MODEL, e.g. make pull MODEL=llama3.1:8b" && exit 1)
	docker compose exec ollama ollama pull $(MODEL)

pull-many: ## Pull multiple models (usage: make pull-many MODELS="llama3.1:8b mistral:7b")
	@test -n "$(MODELS)" || (echo "ERROR: set MODELS, e.g. make pull-many MODELS=\"llama3.1:8b mistral:7b\"" && exit 1)
	@for m in $(MODELS); do \
		echo "==> Pulling $$m"; \
		docker compose exec ollama ollama pull $$m; \
	done

ollama-list: ## List locally available Ollama models
	docker compose exec ollama ollama list

restart: down up ## Restart all services
###< backend/docker ###

# 자주 쓰는 명령 모음. `make` 또는 `make help` 로 목록 확인.
#
# 모든 docker compose 명령에 현재 커밋 해시(GIT_COMMIT)를 자동으로 넘긴다.
# 커밋하지 않은 변경이 있으면 "-dirty" 가 붙어, Langfuse 프롬프트 버전이
# 정확히 어떤 코드에서 나왔는지 구분할 수 있다.

GIT_COMMIT := $(shell git rev-parse --short HEAD 2>/dev/null)$(shell test -z "$$(git status --porcelain 2>/dev/null)" || echo -dirty)
export GIT_COMMIT

COMPOSE := docker compose
ENV_PORT = $(or $(shell grep -E '^$(1)=' .env 2>/dev/null | cut -d= -f2),$(2))

.DEFAULT_GOAL := help
.PHONY: help up down ps logs backend frontend prompt-sync prompt-check test lint

help: ## 명령 목록
	@echo "사용법: make <명령>   (현재 GIT_COMMIT=$(GIT_COMMIT))"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- 실행 ---------------------------------------------------------------------------------------

up: ## 전체 실행 (변경된 이미지는 다시 빌드)
	@$(COMPOSE) up -d --build
	@echo
	@echo "  채팅 UI   http://localhost:$(call ENV_PORT,FRONTEND_PORT,3000)"
	@echo "  Langfuse  http://localhost:$(call ENV_PORT,LANGFUSE_PORT,3100)"

down: ## 전체 중지 (데이터는 유지)
	@$(COMPOSE) down

ps: ## 컨테이너 상태
	@$(COMPOSE) ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'

logs: ## 백엔드 로그 보기 (Ctrl+C 로 종료)
	@$(COMPOSE) logs -f --tail 100 backend

backend: ## 백엔드만 다시 빌드/재시작 (코드·프롬프트 수정 후)
	@$(COMPOSE) up -d --build backend

frontend: ## 프론트엔드만 다시 빌드/재시작
	@$(COMPOSE) up -d --build frontend

# --- 프롬프트 -----------------------------------------------------------------------------------

prompt-sync: ## 프롬프트를 Langfuse 로 동기화 (바뀐 것만 새 버전)
	@$(COMPOSE) exec -e GIT_COMMIT=$(GIT_COMMIT) backend python -m app.prompts.sync

prompt-check: ## 동기화하면 무엇이 바뀌는지만 확인 (dry-run)
	@$(COMPOSE) exec -e GIT_COMMIT=$(GIT_COMMIT) backend python -m app.prompts.sync --dry-run

# --- 개발 ---------------------------------------------------------------------------------------

test: ## 백엔드 테스트 (LLM 불필요)
	@cd backend && uv run pytest -q

lint: ## 백엔드 린트·포맷 검사, 프론트엔드 타입 검사·빌드
	@cd backend && uv run ruff check app tests && uv run ruff format --check app tests
	@cd frontend && npm run build --silent

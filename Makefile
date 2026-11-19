# 자주 쓰는 명령 모음. `make` 또는 `make help` 로 목록 확인.
#
# 모든 docker compose 명령에 현재 커밋 해시(GIT_COMMIT)를 자동으로 넘긴다.
# 커밋하지 않은 변경이 있으면 "-dirty" 가 붙어, Langfuse 프롬프트 버전이
# 정확히 어떤 코드에서 나왔는지 구분할 수 있다.

GIT_COMMIT := $(shell git rev-parse --short HEAD 2>/dev/null)$(shell test -z "$$(git status --porcelain 2>/dev/null)" || echo -dirty)
export GIT_COMMIT

COMPOSE := docker compose
# .env 값 읽기: $(call ENV,KEY,기본값)
ENV = $(or $(shell grep -E '^$(1)=' .env 2>/dev/null | cut -d= -f2),$(2))

# 호스트에서 로컬 Postgres(127.0.0.1)에 붙는 URL — 마이그레이션 생성/검사는 호스트의 코드 기준으로 한다.
HOST_DATABASE_URL = postgresql+asyncpg://$(call ENV,POSTGRES_USER,agent):$(call ENV,POSTGRES_PASSWORD,agent)@127.0.0.1:$(call ENV,POSTGRES_PORT,5432)/$(call ENV,POSTGRES_DB,agent)
ALEMBIC = cd backend && DATABASE_URL=$(HOST_DATABASE_URL) uv run alembic

.DEFAULT_GOAL := help
.PHONY: help up down ps logs backend frontend prompt-sync prompt-check db-revision db-upgrade db-downgrade db-check db-status test lint

help: ## 명령 목록
	@echo "사용법: make <명령>   (현재 GIT_COMMIT=$(GIT_COMMIT))"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- 실행 ---------------------------------------------------------------------------------------

up: ## 전체 실행 (변경된 이미지는 다시 빌드)
	@$(COMPOSE) up -d --build
	@echo
	@echo "  채팅 UI   http://localhost:$(call ENV,FRONTEND_PORT,3000)"
	@echo "  Langfuse  http://localhost:$(call ENV,LANGFUSE_PORT,3100)"

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

# --- DB 마이그레이션 (Alembic) ------------------------------------------------------------------
# 앱(backend)은 시작할 때 자동으로 최신 마이그레이션을 적용한다. 아래는 직접 다룰 때 쓰는 명령.

db-revision: ## 모델 변경 → 마이그레이션 파일 생성. 예: make db-revision m="add tags"
	@test -n "$(m)" || (echo '설명이 필요합니다: make db-revision m="무엇을 바꿨는지"' && exit 1)
	@$(ALEMBIC) revision --autogenerate -m "$(m)"

db-upgrade: ## 최신 마이그레이션 적용
	@$(ALEMBIC) upgrade head

db-downgrade: ## 마이그레이션 한 단계 되돌리기
	@$(ALEMBIC) downgrade -1

db-check: ## 모델과 마이그레이션이 어긋났는지 검사 (어긋나면 실패)
	@$(ALEMBIC) check

db-status: ## 현재 적용된 리비전과 이력
	@$(ALEMBIC) current
	@$(ALEMBIC) history --indicate-current

# --- 개발 ---------------------------------------------------------------------------------------

test: ## 백엔드 테스트 (LLM 불필요)
	@cd backend && uv run pytest -q

lint: ## 백엔드 린트·포맷 검사, 프론트엔드 타입 검사·빌드
	@cd backend && uv run ruff check app tests migrations && uv run ruff format --check app tests migrations
	@cd frontend && npm run build --silent

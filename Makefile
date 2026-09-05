.PHONY: setup dev-backend dev-frontend test run replay stage prove

V ?= v1
N ?= 1
SPEED ?= 0
S ?= mandate-v1-01
DECIDER ?= llm
PRISM ?= on

setup:
	cd backend && uv sync
	cd frontend && npm install

dev-backend:
	cd backend && uv run uvicorn app:app --host 0.0.0.0 --port 8000 --reload

dev-frontend:
	cd frontend && npm run dev

test:
	cd backend && uv run pytest -q
	cd frontend && npx tsc --noEmit

run:
	cd backend && MANDATE_DECIDER=$(DECIDER) PRISM_HANDLERS=$(PRISM) uv run python -m mandate.runner run --version $(V) --index $(N) --speed $(SPEED)

replay:
	cd backend && uv run python -m mandate.runner replay --session $(S) --speed $(SPEED)

stage:
	cd backend && uv run python -m mandate.runner stage --session $(S)

prove:
	cd backend && uv run python -m mandate.runner prove

explain-data:
	cd backend && uv run python -m explain.datagen

meeting:
	cd backend && uv run python -m explain.meeting --mode $(V) --index $(N)

ingest:
	cd backend && uv run python -m explain.ingest $(addprefix ../,$(FILES)) --out ../$(OUT)

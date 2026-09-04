.PHONY: setup dev-backend dev-frontend test

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

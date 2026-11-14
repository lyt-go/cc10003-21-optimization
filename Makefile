.PHONY: install backend frontend test

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/pip install -q pytest && .venv/bin/python -m pytest tests/ -q
	cd frontend && npm run typecheck

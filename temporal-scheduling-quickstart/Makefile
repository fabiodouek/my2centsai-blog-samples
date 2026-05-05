.PHONY: help install dev worker schedule cron pause unpause trigger backfill list describe delete clean compose-up compose-down

SHELL := /bin/bash

help:
	@echo "Targets:"
	@echo "  install        uv sync the project"
	@echo "  dev            start the Temporal CLI dev server (gRPC :7233, UI :8233)"
	@echo "  worker         run the Python worker"
	@echo "  schedule       create the every-2-min interval schedule"
	@echo "  cron           create the weekday-09:00 America/New_York calendar schedule"
	@echo "  pause          pause the interval schedule"
	@echo "  unpause        unpause the interval schedule"
	@echo "  trigger        trigger an ad-hoc run"
	@echo "  backfill       backfill the last 30 minutes"
	@echo "  list           list all schedules"
	@echo "  describe       describe the interval schedule"
	@echo "  delete         delete both schedules"
	@echo "  compose-up     bring up the docker-compose Postgres-backed stack"
	@echo "  compose-down   tear down the compose stack and volumes"
	@echo "  clean          remove the SQLite incidents DB and the Temporal dev DB"

install:
	uv sync

dev:
	temporal server start-dev --db-filename ./temporal.db --ui-port 8233

worker:
	uv run python -m src.worker

schedule:
	uv run python -m src.schedules.create_interval

cron:
	uv run python -m src.schedules.create_cron

pause:
	uv run python -m src.schedules.pause_trigger pause

unpause:
	uv run python -m src.schedules.pause_trigger unpause

trigger:
	uv run python -m src.schedules.pause_trigger trigger

backfill:
	uv run python -m src.schedules.backfill

list:
	uv run python -m src.schedules.list_describe list

describe:
	uv run python -m src.schedules.list_describe describe

delete:
	uv run python -m src.schedules.delete_all

compose-up:
	docker compose up -d

compose-down:
	docker compose down -v

clean:
	rm -f incidents.db temporal.db

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A-share stock analysis service written in Chinese. FastAPI server that scrapes Tencent/Sina finance endpoints for K-line data, runs user-defined Python strategies against them, and serves three UIs: single-stock K-line viewer (`/`), screener (`/screener`), and strategy manager (`/strategies`).

All user-facing strings and comments are Simplified Chinese — preserve that when editing templates or log messages.

General project usage is documented in `USAGE.md`.
Project onboarding notes are summarized in `README.md`.

## Running locally

See `USAGE.md`.

## Database backend

The project is now **MySQL-only**. Database access still goes through `db/compat.py`, which wraps `pymysql` with a lightweight DB helper API and rewrites `?` placeholders / `INSERT OR IGNORE` on the fly.

Required env var:

- `STOCK_AI_DB_URL=mysql://user:pass@host:3306/stock_ai?charset=utf8mb4`

When writing new DB code:

- Import `db.compat as db` and use the same `conn.cursor() / ?` patterns as existing code — the wrapper handles the MySQL translation.
- Schema lives in `db/schema.py` with parallel `if is_mysql(...)` branches for every `CREATE TABLE`. New columns must go through `ensure_column()` so both backends stay in sync.
- MySQL-only SQL is now the default; no SQLite fallback remains.

## Screening architecture

The screener is the core feature; its flow spans several files:

1. **Strategies** are user-authored Python snippets stored in `strategy_definitions` (with optional `strategy_groups` + `strategy_group_items` for AND/OR combinations). Each must define `def run_strategy(context): -> dict` returning at least `{pass, reason}`.
2. `app/core/strategy_engine.py` builds a `context` dict (K-lines, normalized OHLCV arrays, MACD/MA indicators, snapshots) and executes user code inside a restricted `SAFE_GLOBALS` sandbox (`run_strategy_code`). The input/output contract is exposed at `GET /api/strategy/contract`.
3. `app/core/screening_tasks.py` runs strategies for a target (single strategy or group) against one stock; `app/services/screener_service.py` fans out over the full market via `TaskManager`, persists batches, and handles scheduled jobs.
4. Each run is tracked by a `run_token` in `screening_runs` with a `status` of `running/completed/failed`; results live in `screening_results`.
5. Scheduled screening, cleanup, and market cache sync are bootstrapped from `app/services/screener_service.py`.

The LLM-assisted strategy generator (`build_strategy_generation_context` / `generate_strategy_code`) supports MiniMax (`MINIMAX_API_KEY`) or OpenAI-compatible (`LLM_API_KEY` / `OPENAI_API_KEY`) endpoints and sends the contract + allowed-field list as part of the prompt.

## Data sources (no official API)

K-lines come from `web.ifzq.gtimg.cn` (Tencent) and quote/search from `hq.sinajs.cn` / `suggest3.sinajs.cn` / `vip.stock.finance.sina.com.cn` (Sina). Both require a `Referer: https://finance.sina.com.cn` header on some endpoints. Prefix rules for the `sh/sz/bj` symbol (see `stock_code_to_symbol` at `main.py:1060`):

- `6*` → `sh`
- `0*` / `3*` → `sz`
- `4*` / `8*` / `9*` → `bj`

These endpoints are unofficial and rate-limited; keep request volume bounded and reuse the existing retry/timeout patterns.

## Deployment

Tencent Cloud / Linux deployment is documented in `DEPLOY_TENCENT_CLOUD.md`.

## Conventions worth knowing

- The backend is now layered under `app/`: `routers/` for HTTP, `services/` for business logic, `repositories/` for DB access, `core/` for reusable engine/task modules.
- There is no linter/formatter config. Match the surrounding style (4-space indent, snake_case, Chinese user-facing text).
- `.venv/` is git-ignored — never commit it.

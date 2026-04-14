"""
股票K线+AI分析服务
基于 FastAPI + ECharts + 腾讯证券数据源
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from fastapi import FastAPI, Form, HTTPException, Cookie, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.responses import Response
from fastapi.staticfiles import StaticFiles
import httpx
import json
import asyncio
import threading
import time
from time import sleep
import sqlite3
import os
import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from strategy_engine import (
    DEFAULT_STRATEGY_CODE,
    DEFAULT_STRATEGY_DESCRIPTION,
    DEFAULT_STRATEGY_NAME,
    build_strategy_context,
    get_strategy_contract,
    run_strategy_code,
)

app = FastAPI(title="股票K线AI分析", description="A股实时数据 + K线图 + AI决策建议")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_DB_PATH = "/root/.openclaw/workspace/stock-ai/screening.db"
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "screening.db")
DB_PATH = os.getenv(
    "STOCK_AI_DB_PATH",
    LEGACY_DB_PATH if os.path.isdir(os.path.dirname(LEGACY_DB_PATH)) else DEFAULT_DB_PATH,
)
SCREENING_MAX_WORKERS = max(4, min(24, int(os.getenv("SCREENING_MAX_WORKERS", "12"))))
SCREENING_SUBMIT_BATCH = max(50, int(os.getenv("SCREENING_SUBMIT_BATCH", "200")))
SCREENING_SAVE_INTERVAL = max(10, int(os.getenv("SCREENING_SAVE_INTERVAL", "25")))


def ensure_column(cursor, table_name, column_name, column_sql):
    columns = [row[1] for row in cursor.execute(f"PRAGMA table_info({table_name})").fetchall()]
    if column_name not in columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

# 初始化数据库
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS screening_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT,
        run_time TEXT,
        stock_code TEXT,
        stock_name TEXT,
        daily_condition TEXT,
        weekly_condition TEXT,
        current_volume REAL,
        max_volume_3m REAL,
        dif REAL,
        dea REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS screening_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT,
        run_time TEXT,
        total_stocks INTEGER,
        matched_count INTEGER,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    ensure_column(c, "screening_results", "target_type", "TEXT")
    ensure_column(c, "screening_results", "target_id", "INTEGER")
    ensure_column(c, "screening_results", "target_name", "TEXT")
    ensure_column(c, "screening_results", "matched_strategies", "TEXT")
    ensure_column(c, "screening_results", "result_payload", "TEXT")
    ensure_column(c, "screening_results", "score", "REAL DEFAULT 0")
    ensure_column(c, "screening_runs", "target_type", "TEXT")
    ensure_column(c, "screening_runs", "target_id", "INTEGER")
    ensure_column(c, "screening_runs", "target_name", "TEXT")
    ensure_column(c, "screening_runs", "target_logic", "TEXT")
    c.execute('''CREATE TABLE IF NOT EXISTS strategy_definitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '',
        code TEXT NOT NULL,
        enabled INTEGER DEFAULT 1,
        create_mode TEXT DEFAULT 'direct',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS strategy_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '',
        match_mode TEXT DEFAULT 'AND',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS strategy_group_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        strategy_id INTEGER NOT NULL,
        sort_order INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, strategy_id),
        FOREIGN KEY (group_id) REFERENCES strategy_groups(id),
        FOREIGN KEY (strategy_id) REFERENCES strategy_definitions(id)
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_screening_runs_target ON screening_runs(target_type, target_id, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_screening_results_target ON screening_results(target_type, target_id, run_date, run_time)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_strategy_group_items_group ON strategy_group_items(group_id, sort_order)")
    c.execute("SELECT id FROM strategy_definitions WHERE name = ?", (DEFAULT_STRATEGY_NAME,))
    if not c.fetchone():
        c.execute(
            """INSERT INTO strategy_definitions (name, description, code, enabled, create_mode)
               VALUES (?, ?, ?, 1, 'builtin')""",
            (DEFAULT_STRATEGY_NAME, DEFAULT_STRATEGY_DESCRIPTION, DEFAULT_STRATEGY_CODE),
        )
    conn.commit()
    conn.close()

def save_screening_run(run_date, run_time, total_stocks, matched_count, status, results, target_info=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    target_info = target_info or {}
    
    # 先删除该 run_date+run_time 的旧数据（避免重复）
    if target_info.get("target_type") and target_info.get("target_id") is not None:
        c.execute(
            '''DELETE FROM screening_results
               WHERE run_date = ? AND run_time = ? AND target_type = ? AND target_id = ?''',
            (run_date, run_time, target_info.get("target_type"), target_info.get("target_id")),
        )
        c.execute(
            '''DELETE FROM screening_runs
               WHERE run_date = ? AND run_time = ? AND target_type = ? AND target_id = ?''',
            (run_date, run_time, target_info.get("target_type"), target_info.get("target_id")),
        )
    else:
        c.execute('DELETE FROM screening_results WHERE run_date = ? AND run_time = ?', (run_date, run_time))
        c.execute('DELETE FROM screening_runs WHERE run_date = ? AND run_time = ?', (run_date, run_time))
    
    # 保存运行记录
    c.execute('''INSERT INTO screening_runs (run_date, run_time, total_stocks, matched_count, status, target_type, target_id, target_name, target_logic)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (
                  run_date,
                  run_time,
                  total_stocks,
                  matched_count,
                  status,
                  target_info.get("target_type"),
                  target_info.get("target_id"),
                  target_info.get("target_name"),
                  target_info.get("target_logic"),
              ))
    
    # 保存选股结果（去重）
    seen = set()
    for r in results:
        code = r.get('code', '')
        if code in seen:
            continue
        seen.add(code)
        c.execute('''INSERT INTO screening_results 
                     (run_date, run_time, stock_code, stock_name, daily_condition, weekly_condition, current_volume, max_volume_3m, dif, dea, target_type, target_id, target_name, matched_strategies, result_payload, score)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (
                      run_date,
                      run_time,
                      code,
                      r.get('name', ''),
                      r.get('daily', ''),
                      r.get('weekly', ''),
                      r.get('current_vol', 0),
                      r.get('max_vol_3m', 0),
                      r.get('dif', 0),
                      r.get('dea', 0),
                      target_info.get("target_type"),
                      target_info.get("target_id"),
                      target_info.get("target_name"),
                      ", ".join(r.get("matched_strategies", [])),
                      json.dumps(r.get("payload", {}), ensure_ascii=False),
                      r.get("score", 0),
                  ))
    
    conn.commit()
    conn.close()

def get_screening_history(limit=30):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    runs = c.execute('''SELECT * FROM screening_runs ORDER BY created_at DESC LIMIT ?''', (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in runs]

def get_screening_results_by_run(run_date, run_time):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    results = c.execute('''SELECT * FROM screening_results 
                           WHERE run_date = ? AND run_time = ? 
                           ORDER BY current_volume DESC''', (run_date, run_time)).fetchall()
    conn.close()
    return [dict(r) for r in results]

def get_latest_screening_results():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 获取最新一次运行的日期时间（包括running状态的）
    latest = c.execute('''SELECT run_date, run_time, status FROM screening_runs 
                          ORDER BY created_at DESC LIMIT 1''').fetchone()
    
    if not latest:
        conn.close()
        return None, []
    
    results = c.execute('''SELECT * FROM screening_results 
                           WHERE run_date = ? AND run_time = ? 
                           ORDER BY current_volume DESC''', (latest['run_date'], latest['run_time'])).fetchall()
    
    run_info = dict(latest)
    conn.close()
    return run_info, [dict(r) for r in results]


def list_strategies(enabled_only=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    sql = "SELECT * FROM strategy_definitions"
    params = []
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id DESC"
    rows = c.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_strategy(strategy_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = c.execute("SELECT * FROM strategy_definitions WHERE id = ?", (strategy_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_strategy(name, description, code, create_mode="direct", enabled=1):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT INTO strategy_definitions (name, description, code, enabled, create_mode, updated_at)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (name.strip(), description.strip(), code, 1 if enabled else 0, create_mode),
    )
    conn.commit()
    strategy_id = c.lastrowid
    conn.close()
    return get_strategy(strategy_id)


def update_strategy(strategy_id, name, description, code, enabled=1):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """UPDATE strategy_definitions
           SET name = ?, description = ?, code = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (name.strip(), description.strip(), code, 1 if enabled else 0, strategy_id),
    )
    conn.commit()
    changed = c.rowcount
    conn.close()
    return changed > 0


def delete_strategy(strategy_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM strategy_group_items WHERE strategy_id = ?", (strategy_id,))
    c.execute("DELETE FROM strategy_definitions WHERE id = ?", (strategy_id,))
    conn.commit()
    deleted = c.rowcount
    conn.close()
    return deleted > 0


def list_strategy_groups():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    groups = c.execute("SELECT * FROM strategy_groups ORDER BY id DESC").fetchall()
    result = []
    for group in groups:
        items = c.execute(
            """SELECT s.id, s.name
               FROM strategy_group_items gi
               JOIN strategy_definitions s ON s.id = gi.strategy_id
               WHERE gi.group_id = ?
               ORDER BY gi.sort_order ASC, gi.id ASC""",
            (group["id"],),
        ).fetchall()
        payload = dict(group)
        payload["strategies"] = [dict(item) for item in items]
        payload["strategy_ids"] = [item["id"] for item in items]
        result.append(payload)
    conn.close()
    return result


def get_strategy_group(group_id):
    for group in list_strategy_groups():
        if group["id"] == group_id:
            return group
    return None


def create_strategy_group(name, description, match_mode, strategy_ids):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT INTO strategy_groups (name, description, match_mode, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
        (name.strip(), description.strip(), (match_mode or "AND").upper()),
    )
    group_id = c.lastrowid
    for index, strategy_id in enumerate(strategy_ids):
        c.execute(
            "INSERT OR IGNORE INTO strategy_group_items (group_id, strategy_id, sort_order) VALUES (?, ?, ?)",
            (group_id, strategy_id, index),
        )
    conn.commit()
    conn.close()
    return get_strategy_group(group_id)


def update_strategy_group(group_id, name, description, match_mode, strategy_ids):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """UPDATE strategy_groups
           SET name = ?, description = ?, match_mode = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (name.strip(), description.strip(), (match_mode or "AND").upper(), group_id),
    )
    c.execute("DELETE FROM strategy_group_items WHERE group_id = ?", (group_id,))
    for index, strategy_id in enumerate(strategy_ids):
        c.execute(
            "INSERT OR IGNORE INTO strategy_group_items (group_id, strategy_id, sort_order) VALUES (?, ?, ?)",
            (group_id, strategy_id, index),
        )
    conn.commit()
    updated = c.rowcount
    conn.close()
    return updated >= 0


def delete_strategy_group(group_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM strategy_group_items WHERE group_id = ?", (group_id,))
    c.execute("DELETE FROM strategy_groups WHERE id = ?", (group_id,))
    conn.commit()
    deleted = c.rowcount
    conn.close()
    return deleted > 0


def get_target_options():
    return {
        "strategies": [
            {
                "id": item["id"],
                "name": item["name"],
                "description": item.get("description", ""),
                "enabled": item.get("enabled", 1),
                "create_mode": item.get("create_mode", "direct"),
            }
            for item in list_strategies()
        ],
        "groups": [
            {
                "id": item["id"],
                "name": item["name"],
                "description": item.get("description", ""),
                "match_mode": item.get("match_mode", "AND"),
                "strategy_ids": item.get("strategy_ids", []),
                "strategy_names": [strategy["name"] for strategy in item.get("strategies", [])],
            }
            for item in list_strategy_groups()
        ],
    }


def is_mobile_user_agent(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    mobile_keywords = [
        "iphone",
        "android",
        "mobile",
        "ipad",
        "ipod",
        "windows phone",
        "opera mini",
        "blackberry",
    ]
    return any(keyword in ua for keyword in mobile_keywords)


def resolve_screening_target(target_type=None, target_id=None):
    target_type = target_type or "strategy"
    if target_type == "group":
        group = get_strategy_group(int(target_id)) if target_id else None
        if not group:
            groups = list_strategy_groups()
            group = groups[0] if groups else None
        if not group:
            return None
        strategies = [get_strategy(strategy_id) for strategy_id in group.get("strategy_ids", [])]
        strategies = [item for item in strategies if item and item.get("enabled", 1)]
        if not strategies:
            return None
        return {
            "target_type": "group",
            "target_id": group["id"],
            "target_name": group["name"],
            "target_logic": group.get("match_mode", "AND").upper(),
            "strategies": strategies,
        }
    strategy = get_strategy(int(target_id)) if target_id else None
    if strategy and not strategy.get("enabled", 1):
        strategy = None
    if not strategy:
        strategy_list = list_strategies(enabled_only=True)
        strategy = strategy_list[0] if strategy_list else None
    if not strategy:
        return None
    return {
        "target_type": "strategy",
        "target_id": strategy["id"],
        "target_name": strategy["name"],
        "target_logic": "SINGLE",
        "strategies": [strategy],
    }


def build_strategy_generation_context(prompt_text: str):
    minimax_api_key = os.getenv("MINIMAX_API_KEY")
    llm_api_key = os.getenv("LLM_API_KEY")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    api_key = minimax_api_key or llm_api_key or openai_api_key
    if not api_key:
        raise ValueError("未配置 MINIMAX_API_KEY、LLM_API_KEY 或 OPENAI_API_KEY")
    if minimax_api_key and api_key == minimax_api_key:
        base_url = (
            os.getenv("MINIMAX_API_BASE")
            or os.getenv("LLM_API_BASE")
            or "https://api.minimax.io/v1"
        ).rstrip("/")
        model = (
            os.getenv("MINIMAX_MODEL")
            or os.getenv("LLM_MODEL")
            or "MiniMax-M2.5"
        )
    else:
        base_url = (
            os.getenv("LLM_API_BASE")
            or os.getenv("OPENAI_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        model = (
            os.getenv("LLM_MODEL")
            or os.getenv("OPENAI_MODEL")
            or "gpt-4o-mini"
        )
    contract = get_strategy_contract()
    allowed_fields = [
        "context['stock']['code']",
        "context['stock']['name']",
        "context['stock']['symbol']",
        "context['data']['daily']['dates']",
        "context['data']['daily']['open']",
        "context['data']['daily']['close']",
        "context['data']['daily']['high']",
        "context['data']['daily']['low']",
        "context['data']['daily']['volume']",
        "context['data']['weekly']['dates']",
        "context['data']['weekly']['open']",
        "context['data']['weekly']['close']",
        "context['data']['weekly']['high']",
        "context['data']['weekly']['low']",
        "context['data']['weekly']['volume']",
        "context['snapshots']['daily']['enough_data']",
        "context['snapshots']['daily']['rows']",
        "context['snapshots']['daily']['latest_open']",
        "context['snapshots']['daily']['latest_close']",
        "context['snapshots']['daily']['latest_high']",
        "context['snapshots']['daily']['latest_low']",
        "context['snapshots']['daily']['current_volume']",
        "context['snapshots']['daily']['max_volume_3m']",
        "context['snapshots']['daily']['latest_dif']",
        "context['snapshots']['daily']['latest_dea']",
        "context['snapshots']['daily']['latest_macd_bar']",
        "context['snapshots']['weekly']['enough_data']",
        "context['snapshots']['weekly']['rows']",
        "context['snapshots']['weekly']['latest_open']",
        "context['snapshots']['weekly']['latest_close']",
        "context['snapshots']['weekly']['consecutive_red']",
        "context['snapshots']['weekly']['recent_red_bars']",
        "context['indicators']['daily']['ma5']",
        "context['indicators']['daily']['ma10']",
        "context['indicators']['daily']['ma20']",
        "context['indicators']['daily']['ma60']",
        "context['indicators']['daily']['macd']['dif']",
        "context['indicators']['daily']['macd']['dea']",
        "context['indicators']['daily']['macd']['bar']",
        "context['indicators']['weekly']['ma5']",
        "context['indicators']['weekly']['ma10']",
        "context['indicators']['weekly']['ma20']",
        "context['indicators']['weekly']['macd']['dif']",
        "context['indicators']['weekly']['macd']['dea']",
        "context['indicators']['weekly']['macd']['bar']",
    ]
    forbidden_fields = [
        "context['snapshots']['daily']['prev_close']",
        "context['snapshots']['daily']['change']",
        "context.symbol",
        "context.get_close(...)",
        "context.close",
        "backtrader",
        "talib / ta-lib",
    ]
    allowed_fields_text = "\n".join(f"- {item}" for item in allowed_fields)
    forbidden_fields_text = "\n".join(f"- {item}" for item in forbidden_fields)
    system_prompt = (
        "你是资深量化工程师。请根据用户要求输出可直接执行的 Python 策略代码。"
        "只能返回代码，不要解释，不要 Markdown，不要输出 <think>。"
        "必须定义 run_strategy(context) 函数，且只能使用项目已有的 context 字典结构。"
        "不要使用 backtrader、talib、context.symbol、context.get_close 之类项目中不存在的 API。"
    )
    user_prompt = (
        f"需求：{prompt_text}\n"
        "必须遵守这些约束：\n"
        "1. 只定义 run_strategy(context)。\n"
        "2. 只能使用下面这些已存在字段，不能猜测或发明新字段：\n"
        f"{allowed_fields_text}\n"
        "3. 必须使用 context['a']['b'] 这种字典访问方式。\n"
        "4. 返回 dict，至少包含 pass(bool) 和 reason(str)，可选 score、metrics。\n"
        "5. 数据不足时直接返回 pass=False 和明确原因。\n"
        "6. 严禁使用下面这些不存在或不允许的字段/库：\n"
        f"{forbidden_fields_text}\n"
        "7. 参考模板结构如下：\n"
        f"{contract['template']}\n"
        "现在只返回符合这些约束的完整 Python 代码。"
    )
    return {
        "model": model,
        "base_url": base_url,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
    }


def generate_strategy_code(prompt_text: str):
    minimax_api_key = os.getenv("MINIMAX_API_KEY")
    llm_api_key = os.getenv("LLM_API_KEY")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    api_key = minimax_api_key or llm_api_key or openai_api_key
    if not api_key:
        raise ValueError("未配置 MINIMAX_API_KEY、LLM_API_KEY 或 OPENAI_API_KEY")

    def extract_code(data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise ValueError(f"策略生成返回异常，缺少 choices: {data}")
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise ValueError(f"策略生成返回空内容: {data}")
        if "<think>" in content and "</think>" in content:
            content = content.split("</think>", 1)[1].strip()
        if content.startswith("```"):
            content = content.strip("`").strip()
            if content.startswith("python"):
                content = content[6:].lstrip()
        return content.strip()

    def call_llm(model_name, base_url, messages, temperature):
        payloads = [
            {
                "model": model_name,
                "messages": messages,
                "temperature": temperature,
            },
            {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": "只返回 Python 代码，定义 run_strategy(context)，不要解释。"},
                    {"role": "user", "content": messages[-1]["content"]},
                ],
                "temperature": 0.1,
            },
        ]

        last_error = None
        with httpx.Client(timeout=90) as client:
            for index, payload in enumerate(payloads):
                try:
                    response = client.post(
                        f"{base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    response.raise_for_status()
                    return response.json()
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if exc.response.status_code == 529 and index < len(payloads) - 1:
                        sleep(1)
                        continue
                    raise
        raise last_error or ValueError("策略生成失败")

    llm_context = build_strategy_generation_context(prompt_text)
    test_context = build_strategy_context({"code": "000001", "name": "平安银行", "symbol": "sz000001"}, [], [])
    contract = get_strategy_contract()

    generated = extract_code(
        call_llm(
            llm_context["model"],
            llm_context["base_url"],
            [
                {"role": "system", "content": llm_context["system_prompt"]},
                {"role": "user", "content": llm_context["user_prompt"]},
            ],
            0.2,
        )
    )

    validation = run_strategy_code(generated, test_context)
    if validation.get("error"):
        repair_prompt = (
            "下面这段策略代码不符合项目约定，请修复后只返回完整 Python 代码。\n"
            f"错误：{validation.get('reason', '未知错误')}\n"
            "项目约定：只能访问 context['stock']、context['snapshots']、context['indicators']；"
            "必须定义 run_strategy(context)；返回 dict。\n"
            f"参考模板：\n{contract['template']}\n"
            f"待修复代码：\n{generated}"
        )
        generated = extract_code(
            call_llm(
                llm_context["model"],
                llm_context["base_url"],
                [
                    {"role": "system", "content": llm_context["system_prompt"]},
                    {"role": "user", "content": repair_prompt},
                ],
                0.1,
            )
        )
        validation = run_strategy_code(generated, test_context)
        if validation.get("error"):
            raise ValueError(validation.get("reason", "策略代码校验失败"))

    return generated

# ========== 虚拟炒股模块 ==========

VT_DB_PATH = DB_PATH  # 共用同一个数据库
SESSION_COOKIE_NAME = "vt_session"
sessions: dict = {}  # session_token -> {user_id, username}

def hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()

def make_session_token() -> str:
    return secrets.token_hex(32)

def init_vt_db():
    """初始化虚拟炒股数据库"""
    conn = sqlite3.connect(VT_DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS vt_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS vt_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        account_name TEXT NOT NULL,
        balance REAL DEFAULT 500000.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES vt_users(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS vt_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        stock_code TEXT NOT NULL,
        stock_name TEXT NOT NULL,
        shares INTEGER NOT NULL,
        avg_cost REAL NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(account_id, stock_code),
        FOREIGN KEY (account_id) REFERENCES vt_accounts(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS vt_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        stock_code TEXT NOT NULL,
        stock_name TEXT NOT NULL,
        trade_type TEXT NOT NULL,
        shares INTEGER NOT NULL,
        price REAL NOT NULL,
        total_amount REAL NOT NULL,
        commission REAL DEFAULT 0,
        traded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES vt_accounts(id)
    )''')
    # 创建默认管理员
    c.execute("SELECT id FROM vt_users WHERE username='admin' AND is_admin=1")
    if not c.fetchone():
        c.execute("INSERT INTO vt_users (username, password, is_admin) VALUES ('admin', ?, 1)", (hash_password("admin"),))
    conn.commit()
    conn.close()

init_vt_db()

# ---- 用户认证 ----
def create_user(username: str, password: str) -> dict:
    """注册新用户，自动创建一个同名虚拟账户"""
    conn = sqlite3.connect(VT_DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO vt_users (username, password) VALUES (?, ?)", (username, hash_password(password)))
        user_id = c.lastrowid
        # 自动创建第一个虚拟账户
        c.execute("INSERT INTO vt_accounts (user_id, account_name, balance) VALUES (?, ?, 500000.0)", (user_id, f"账户A"))
        conn.commit()
        account_id = c.lastrowid
        conn.close()
        return {"ok": True, "user_id": user_id, "account_id": account_id}
    except sqlite3.IntegrityError:
        conn.close()
        return {"ok": False, "error": "用户名已存在"}

def login_user(username: str, password: str) -> dict:
    """登录"""
    conn = sqlite3.connect(VT_DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM vt_users WHERE username=? AND password=?", (username, hash_password(password)))
    user = c.fetchone()
    conn.close()
    if not user:
        return {"ok": False, "error": "用户名或密码错误"}
    token = make_session_token()
    sessions[token] = {"user_id": user["id"], "username": user["username"], "is_admin": user["is_admin"]}
    return {"ok": True, "token": token, "username": user["username"], "is_admin": user["is_admin"]}

def get_session(token: str) -> Optional[dict]:
    return sessions.get(token)

def get_user_accounts(user_id: int) -> list:
    conn = sqlite3.connect(VT_DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    accounts = c.execute("SELECT * FROM vt_accounts WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    conn.close()
    return [dict(a) for a in accounts]

def create_account(user_id: int, account_name: str) -> dict:
    conn = sqlite3.connect(VT_DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO vt_accounts (user_id, account_name, balance) VALUES (?, ?, 500000.0)", (user_id, account_name))
        conn.commit()
        aid = c.lastrowid
        conn.close()
        return {"ok": True, "account_id": aid}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}

def buy_stock(account_id: int, stock_code: str, stock_name: str, shares: int, price: float) -> dict:
    """买入股票"""
    total = price * shares
    commission = total * 0.0003  # 万三佣金，最低5元
    if commission < 5:
        commission = 5
    total_cost = total + commission
    
    conn = sqlite3.connect(VT_DB_PATH)
    c = conn.cursor()
    
    # 检查余额
    c.execute("SELECT balance FROM vt_accounts WHERE id=?", (account_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "账户不存在"}
    
    balance = row[0]
    if total_cost > balance:
        conn.close()
        return {"ok": False, "error": f"余额不足，所需 {total_cost:.2f}，账户余额 {balance:.2f}"}
    
    # 扣除余额
    new_balance = balance - total_cost
    c.execute("UPDATE vt_accounts SET balance=? WHERE id=?", (new_balance, account_id))
    
    # 更新持仓
    c.execute("SELECT * FROM vt_positions WHERE account_id=? AND stock_code=?", (account_id, stock_code))
    pos = c.fetchone()
    
    if pos:
        old_shares = pos[3]
        old_avg = pos[4]
        new_shares = old_shares + shares
        new_avg = (old_shares * old_avg + shares * price) / new_shares
        c.execute("UPDATE vt_positions SET shares=?, avg_cost=? WHERE id=?", (new_shares, new_avg, pos[0]))
    else:
        c.execute("INSERT INTO vt_positions (account_id, stock_code, stock_name, shares, avg_cost) VALUES (?, ?, ?, ?, ?)",
                  (account_id, stock_code, stock_name, shares, price))
    
    # 记录交易
    c.execute("INSERT INTO vt_trades (account_id, stock_code, stock_name, trade_type, shares, price, total_amount, commission) VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?)",
              (account_id, stock_code, stock_name, shares, price, total, commission))
    
    conn.commit()
    conn.close()
    return {"ok": True, "balance": new_balance, "commission": commission}

def sell_stock(account_id: int, stock_code: str, shares: int, price: float) -> dict:
    """卖出股票"""
    total = price * shares
    commission = total * 0.0003
    if commission < 5:
        commission = 5
    stamp_tax = total * 0.001  # 印花税
    net = total - commission - stamp_tax
    
    conn = sqlite3.connect(VT_DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT * FROM vt_positions WHERE account_id=? AND stock_code=?", (account_id, stock_code))
    pos = c.fetchone()
    if not pos:
        conn.close()
        return {"ok": False, "error": "无持仓"}
    
    if pos[3] < shares:
        conn.close()
        return {"ok": False, "error": f"持仓不足，当前持仓 {pos[3]} 股"}
    
    # 更新余额
    c.execute("SELECT balance FROM vt_accounts WHERE id=?", (account_id,))
    old_balance = c.fetchone()[0]
    new_balance = old_balance + net
    c.execute("UPDATE vt_accounts SET balance=? WHERE id=?", (new_balance, account_id))
    
    # 更新持仓
    new_shares = pos[3] - shares
    if new_shares == 0:
        c.execute("DELETE FROM vt_positions WHERE id=?", (pos[0],))
    else:
        c.execute("UPDATE vt_positions SET shares=? WHERE id=?", (new_shares, pos[0]))
    
    # 记录交易
    c.execute("INSERT INTO vt_trades (account_id, stock_code, stock_name, trade_type, shares, price, total_amount, commission) VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?)",
              (account_id, stock_code, pos[2], shares, price, total, commission + stamp_tax))
    
    conn.commit()
    conn.close()
    return {"ok": True, "balance": new_balance, "net": net, "commission": commission + stamp_tax}

def get_positions(account_id: int) -> list:
    conn = sqlite3.connect(VT_DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    positions = c.execute("SELECT * FROM vt_positions WHERE account_id=? ORDER BY id", (account_id,)).fetchall()
    conn.close()
    return [dict(p) for p in positions]

def get_trades(account_id: int, limit: int = 50) -> list:
    conn = sqlite3.connect(VT_DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    trades = c.execute("SELECT * FROM vt_trades WHERE account_id=? ORDER BY traded_at DESC LIMIT ?", (account_id, limit)).fetchall()
    conn.close()
    return [dict(t) for t in trades]

def get_account_summary(account_id: int) -> dict:
    conn = sqlite3.connect(VT_DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    acc = c.execute("SELECT * FROM vt_accounts WHERE id=?", (account_id,)).fetchone()
    if not acc:
        conn.close()
        return {"error": "账户不存在"}
    
    positions = c.execute("SELECT * FROM vt_positions WHERE account_id=?", (account_id,)).fetchall()
    
    # 计算持仓市值
    total_market_value = 0
    total_cost = 0
    pos_list = []
    
    for p in positions:
        shares = p["shares"]
        avg_cost = p["avg_cost"]
        cost = shares * avg_cost
        total_cost += cost
        # 获取最新价
        info = get_stock_info(p["stock_code"])
        current_price = info.get("price", avg_cost)
        market_value = shares * current_price
        total_market_value += market_value
        profit = market_value - cost
        profit_pct = (profit / cost * 100) if cost > 0 else 0
        pos_list.append({
            "stock_code": p["stock_code"],
            "stock_name": p["stock_name"],
            "shares": shares,
            "avg_cost": avg_cost,
            "current_price": current_price,
            "market_value": market_value,
            "profit": profit,
            "profit_pct": round(profit_pct, 2)
        })
    
    total_assets = acc["balance"] + total_market_value
    total_profit = total_assets - 500000
    total_profit_pct = (total_profit / 500000 * 100) if 500000 > 0 else 0
    
    conn.close()
    return {
        "account_id": account_id,
        "account_name": acc["account_name"],
        "balance": acc["balance"],
        "market_value": total_market_value,
        "total_assets": total_assets,
        "total_profit": total_profit,
        "total_profit_pct": round(total_profit_pct, 2),
        "positions": pos_list
    }

def get_all_users() -> list:
    conn = sqlite3.connect(VT_DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    users = c.execute("SELECT id, username, is_admin, created_at FROM vt_users ORDER BY id").fetchall()
    result = []
    for u in users:
        accounts = c.execute("SELECT id, account_name, balance, created_at FROM vt_accounts WHERE user_id=?", (u["id"],)).fetchall()
        result.append({
            "id": u["id"], "username": u["username"], "is_admin": u["is_admin"],
            "created_at": u["created_at"],
            "accounts": [dict(a) for a in accounts]
        })
    conn.close()
    return result

def reset_account(account_id: int) -> dict:
    conn = sqlite3.connect(VT_DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM vt_trades WHERE account_id=?", (account_id,))
    c.execute("DELETE FROM vt_positions WHERE account_id=?", (account_id,))
    c.execute("UPDATE vt_accounts SET balance=500000.0 WHERE id=?", (account_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


def change_user_password(user_id: int, old_pwd: str, new_pwd: str) -> dict:
    """用户修改自己的密码"""
    if len(new_pwd) < 4:
        return {"ok": False, "error": "新密码至少4字符"}
    conn = sqlite3.connect(VT_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT password FROM vt_users WHERE id=?", (user_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "用户不存在"}
    if row[0] != hash_password(old_pwd):
        conn.close()
        return {"ok": False, "error": "原密码错误"}
    c.execute("UPDATE vt_users SET password=? WHERE id=?", (hash_password(new_pwd), user_id))
    conn.commit()
    conn.close()
    return {"ok": True}


def admin_reset_user_password(user_id: int, new_pwd: str) -> dict:
    """管理员重置用户密码"""
    if len(new_pwd) < 4:
        return {"ok": False, "error": "密码至少4字符"}
    conn = sqlite3.connect(VT_DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE vt_users SET password=? WHERE id=? AND is_admin=0", (hash_password(new_pwd), user_id))
    if c.rowcount == 0:
        conn.close()
        return {"ok": False, "error": "用户不存在或无法修改管理员密码"}
    conn.commit()
    conn.close()
    return {"ok": True}


def delete_account(account_id: int) -> dict:
    """销户（删除账户及所有关联数据）"""
    conn = sqlite3.connect(VT_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM vt_accounts WHERE id=?", (account_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "账户不存在"}
    user_id = row[0]
    c.execute("DELETE FROM vt_trades WHERE account_id=?", (account_id,))
    c.execute("DELETE FROM vt_positions WHERE account_id=?", (account_id,))
    c.execute("DELETE FROM vt_accounts WHERE id=?", (account_id,))
    # 检查该用户是否还有其他账户
    c.execute("SELECT COUNT(*) FROM vt_accounts WHERE user_id=?", (user_id,))
    if c.fetchone()[0] == 0:
        # 没有其他账户了，一并删除用户
        c.execute("DELETE FROM vt_users WHERE id=? AND is_admin=0", (user_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


def adjust_balance(account_id: int, new_balance: float) -> dict:
    """管理员调整账户余额"""
    if new_balance < 0:
        return {"ok": False, "error": "余额不能为负"}
    conn = sqlite3.connect(VT_DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE vt_accounts SET balance=? WHERE id=?", (new_balance, account_id))
    if c.rowcount == 0:
        conn.close()
        return {"ok": False, "error": "账户不存在"}
    conn.commit()
    conn.close()
    return {"ok": True}


def get_account_detail(account_id: int) -> dict:
    """获取账户完整信息（持仓+交易，供管理员用）"""
    conn = sqlite3.connect(VT_DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    acc = c.execute("SELECT * FROM vt_accounts WHERE id=?", (account_id,)).fetchone()
    if not acc:
        conn.close()
        return {"error": "账户不存在"}
    positions = c.execute("SELECT * FROM vt_positions WHERE account_id=? ORDER BY id", (account_id,)).fetchall()
    trades = c.execute("SELECT * FROM vt_trades WHERE account_id=? ORDER BY traded_at DESC LIMIT 100", (account_id,)).fetchall()
    conn.close()
    return {
        "account": dict(acc),
        "positions": [dict(p) for p in positions],
        "trades": [dict(t) for t in trades]
    }


# 初始化数据库
init_db()

# 选股结果缓存（内存）
SCREENING_RESULT = {
    "time": "",
    "total_stocks": 0,
    "processed": 0,
    "matched_count": 0,
    "results": [],
    "running": False,
    "target_type": "",
    "target_id": None,
    "target_name": "",
    "error": "",
}

# ========== 选股模块 ==========

def get_all_stocks() -> list:
    """获取全量A股列表"""
    try:
        from urllib.request import Request, urlopen

        stocks = []
        seen = set()
        page = 1
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        }
        base_url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1"
            "&node=hs_a&symbol=&_s_r_a=page"
        )

        while True:
            last_error = None
            payload_text = None
            url = base_url.format(page=page)
            for _ in range(3):
                try:
                    req = Request(url, headers=headers)
                    with urlopen(req, timeout=20) as resp:
                        payload_text = resp.read().decode("utf-8")
                    break
                except Exception as exc:
                    last_error = exc
            if payload_text is None:
                raise last_error or RuntimeError("获取股票列表失败")

            data = json.loads(payload_text)
            if not data:
                break

            for item in data:
                symbol = (item.get("symbol") or "").strip()
                code = (item.get("code") or symbol[2:]).strip()
                name = (item.get("name") or "").strip()
                if symbol.startswith(("sh", "sz", "bj")) and len(code) == 6 and name and code not in seen:
                    seen.add(code)
                    stocks.append({"code": code, "name": name})

            page += 1
            if page > 200:
                break
        print(f"获取到 {len(stocks)} 只股票")
        return stocks
    except Exception as e:
        print(f"获取股票列表失败: {e}")
        return []


def stock_code_to_symbol(code: str) -> Optional[str]:
    """将A股代码转换为腾讯接口所需 symbol"""
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    return None

def get_kline_daily(symbol: str, days: int = 90) -> list:
    """获取日K线"""
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={symbol},day,,,{days},qfq&r=0.1"
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            text = resp.text
        json_start = text.find('=') + 1
        data = json.loads(text[json_start:])
        if data.get("code") != 0:
            return []
        stock_data = data.get("data", {}).get(symbol, {})
        return stock_data.get("qfqday", [])
    except:
        return []

def get_kline_weekly(symbol: str) -> list:
    """获取周K线"""
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_weekqfq&param={symbol},week,,,30,qfq&r=0.1"
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            text = resp.text
        json_start = text.find('=') + 1
        data = json.loads(text[json_start:])
        if data.get("code") != 0:
            return []
        stock_data = data.get("data", {}).get(symbol, {})
        return stock_data.get("qfqweek", [])
    except:
        return []

def check_daily_criteria(klines: list) -> dict:
    """检查日线: MACD零轴之上 + 成交额3月新高"""
    if not klines or len(klines) < 60:
        return {"pass": False, "reason": "数据不足"}
    
    closes = [float(k[2]) for k in klines[-90:] if len(k) >= 6]
    volumes = [float(k[5]) for k in klines[-90:] if len(k) >= 6]
    
    if len(closes) < 60:
        return {"pass": False, "reason": "数据不足"}
    
    close_series = pd.Series(closes)
    ema12 = close_series.ewm(span=12).mean()
    ema26 = close_series.ewm(span=26).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9).mean()
    
    latest_dif = dif.iloc[-1]
    latest_dea = dea.iloc[-1]
    macd_above_zero = latest_dif > latest_dea and latest_dif > 0
    
    recent_vol = volumes[-60:-1] if len(volumes) > 1 else volumes
    max_vol_3m = max(recent_vol) if recent_vol else 0
    current_vol = volumes[-1]
    volume_at_high = current_vol >= max_vol_3m if max_vol_3m > 0 else False
    
    result = {
        "pass": macd_above_zero and volume_at_high,
        "current_vol": current_vol,
        "max_vol_3m": max_vol_3m,
        "dif": round(latest_dif, 4),
        "dea": round(latest_dea, 4)
    }
    
    if macd_above_zero and volume_at_high:
        result["reason"] = "MACD零轴上+成交额3月新高"
    elif not macd_above_zero:
        result["reason"] = f"MACD未在零轴上(DIF={round(latest_dif,2)})"
    else:
        result["reason"] = "成交额未创新高"
    
    return result

def check_weekly_criteria(klines: list) -> dict:
    """检查周线: 2-3根连续红柱"""
    if not klines or len(klines) < 3:
        return {"pass": False, "reason": "数据不足", "count": 0}
    
    recent = klines[-5:]
    red_bars = []
    for k in recent:
        if len(k) >= 6:
            red_bars.append(float(k[2]) > float(k[1]))
    
    consecutive = 0
    for is_red in reversed(red_bars):
        if is_red:
            consecutive += 1
        else:
            break
    
    result = {
        "pass": 2 <= consecutive <= 3,
        "count": consecutive
    }
    
    if result["pass"]:
        result["reason"] = f"周线{consecutive}根红柱"
    elif consecutive < 2:
        result["reason"] = f"周线红柱不足({consecutive}根)"
    else:
        result["reason"] = f"周线红柱过多({consecutive}根)"
    
    return result

def screen_stock(code: str, name: str, target_info: dict) -> dict:
    """按策略或策略组筛选单只股票（同步版本）"""
    result = {
        "code": code,
        "name": name,
        "pass": False,
        "daily": "",
        "weekly": "",
        "reason": "",
        "matched_strategies": [],
        "current_vol": 0,
        "max_vol_3m": 0,
        "dif": 0,
        "dea": 0,
        "score": 0,
        "payload": {},
    }

    try:
        symbol = stock_code_to_symbol(code)
        if not symbol:
            result["reason"] = "不支持的股票代码"
            return result

        daily_klines = get_kline_daily(symbol, 180)
        weekly_klines = get_kline_weekly(symbol)
        context = build_strategy_context({"code": code, "name": name, "symbol": symbol}, daily_klines, weekly_klines)

        strategy_results = []
        for strategy in target_info.get("strategies", []):
            strategy_result = run_strategy_code(strategy["code"], context)
            strategy_result["strategy_id"] = strategy["id"]
            strategy_result["strategy_name"] = strategy["name"]
            strategy_results.append(strategy_result)

        if not strategy_results:
            result["reason"] = "没有可用策略"
            return result

        if target_info.get("target_type") == "group":
            match_mode = target_info.get("target_logic", "AND").upper()
            passed = all(item["pass"] for item in strategy_results) if match_mode == "AND" else any(item["pass"] for item in strategy_results)
        else:
            passed = strategy_results[0]["pass"]

        matched_names = [item["strategy_name"] for item in strategy_results if item["pass"]]
        failed_reasons = [f"{item['strategy_name']}: {item.get('reason', '')}" for item in strategy_results if not item["pass"]]
        pass_reasons = [f"{item['strategy_name']}: {item.get('reason', '')}" for item in strategy_results if item["pass"]]

        daily_snapshot = context["snapshots"]["daily"]
        result["current_vol"] = daily_snapshot.get("current_volume", 0)
        result["max_vol_3m"] = daily_snapshot.get("max_volume_3m", 0)
        result["dif"] = round(daily_snapshot.get("latest_dif", 0), 4)
        result["dea"] = round(daily_snapshot.get("latest_dea", 0), 4)
        result["pass"] = passed
        result["matched_strategies"] = matched_names
        result["daily"] = "、".join(matched_names) if matched_names else "未命中策略"
        result["weekly"] = " | ".join(pass_reasons if passed else failed_reasons[:3])
        result["reason"] = result["weekly"]
        result["score"] = max([item.get("score", 0) for item in strategy_results] + [0])
        result["payload"] = {
            "target": {
                "type": target_info.get("target_type"),
                "id": target_info.get("target_id"),
                "name": target_info.get("target_name"),
                "logic": target_info.get("target_logic"),
            },
            "strategy_results": strategy_results,
            "snapshots": context["snapshots"],
        }

    except Exception as e:
        result["error"] = str(e)
        result["reason"] = str(e)

    return result

def run_screening_sync(target_type="strategy", target_id=None):
    """执行完整选股筛选（同步版本，在后台线程运行）"""
    global SCREENING_RESULT
    SCREENING_RESULT["running"] = True
    SCREENING_RESULT["processed"] = 0
    target_info = resolve_screening_target(target_type, target_id)
    if not target_info:
        SCREENING_RESULT["running"] = False
        SCREENING_RESULT["error"] = "未找到可执行的策略或策略组"
        return

    now = datetime.now()
    run_date = now.strftime("%Y-%m-%d")
    run_time = now.strftime("%H:%M:%S")
    SCREENING_RESULT["time"] = f"{run_date} {run_time}"
    SCREENING_RESULT["target_type"] = target_info["target_type"]
    SCREENING_RESULT["target_id"] = target_info["target_id"]
    SCREENING_RESULT["target_name"] = target_info["target_name"]

    print(f"[{run_time}] 开始选股筛选（{target_info['target_name']}）...")
    
    stocks = get_all_stocks()
    SCREENING_RESULT["total_stocks"] = len(stocks)
    print(f"共 {len(stocks)} 只股票")
    
    results = []
    total = len(stocks)
    
    if total == 0:
        SCREENING_RESULT["running"] = False
        return

    save_counter = 0
    batch_size = min(SCREENING_SUBMIT_BATCH, total)
    max_workers = min(SCREENING_MAX_WORKERS, total)
    print(f"并发配置: workers={max_workers}, submit_batch={batch_size}, save_interval={SCREENING_SAVE_INTERVAL}")

    for start in range(0, total, batch_size):
        stock_batch = stocks[start:start + batch_size]
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(screen_stock, stock["code"], stock["name"], target_info): stock
                for stock in stock_batch
            }

            for future in as_completed(future_map):
                stock = future_map[future]
                try:
                    r = future.result()
                except Exception as exc:
                    r = {
                        "code": stock["code"],
                        "name": stock["name"],
                        "pass": False,
                        "reason": str(exc),
                        "error": str(exc),
                    }

                if r.get("pass"):
                    results.append(r)
                    save_counter += 1

                SCREENING_RESULT["processed"] += 1
                SCREENING_RESULT["matched_count"] = len(results)

                if save_counter >= SCREENING_SAVE_INTERVAL:
                    save_screening_run(run_date, run_time, total, len(results), "running", results, target_info=target_info)
                    save_counter = 0

                processed = SCREENING_RESULT["processed"]
                if processed % 100 == 0 or processed >= total:
                    print(f"  进度: {processed}/{total} ({processed*100//total}%)，符合条件: {len(results)} 只")
    
    results.sort(key=lambda x: x.get("current_vol", 0), reverse=True)
    
    # 保存最终结果
    save_screening_run(run_date, run_time, total, len(results), "completed", results, target_info=target_info)
    
    SCREENING_RESULT["matched_count"] = len(results)
    SCREENING_RESULT["results"] = results[:200]
    SCREENING_RESULT["running"] = False
    
    print(f"筛选完成！符合条件: {len(results)} 只")

def scheduled_screening():
    """定时选股任务（每天7:00执行）"""
    while True:
        now = datetime.now()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now.hour >= 7:
            target += timedelta(days=1)
        seconds = (target - now).total_seconds()
        
        print(f"[选股定时任务] 下次执行: {target.strftime('%Y-%m-%d %H:%M:%S')}, 等待 {int(seconds)} 秒")
        time.sleep(seconds)
        
        run_screening_sync()


def cleanup_old_data():
    """清理7天前的选股数据"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 删除7天前的运行记录
    c.execute("DELETE FROM screening_results WHERE run_date < date('now', '-7 days')")
    c.execute("DELETE FROM screening_runs WHERE run_date < date('now', '-7 days')")
    
    conn.commit()
    deleted_results = c.total_changes
    conn.close()
    
    print(f"[清理] 已删除 {deleted_results} 条7天前的选股记录")


def scheduled_cleanup():
    """定时清理任务（每天凌晨3:00执行）"""
    while True:
        now = datetime.now()
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now.hour >= 3:
            target += timedelta(days=1)
        seconds = (target - now).total_seconds()
        
        print(f"[清理任务] 下次执行: {target.strftime('%Y-%m-%d %H:%M:%S')}, 等待 {int(seconds)} 秒")
        time.sleep(seconds)
        
        cleanup_old_data()

threading.Thread(target=scheduled_screening, daemon=True).start()
threading.Thread(target=scheduled_cleanup, daemon=True).start()

# 静态文件和模板
app.mount("/static", StaticFiles(directory="static"), name="static")


# ========== 股票数据接口 ==========

def get_stock_info(stock_code: str) -> dict:
    """获取股票基本信息（新浪财经API）"""
    try:
        if stock_code.startswith("6"):
            symbol = f"sh{stock_code}"
        elif stock_code.startswith("0") or stock_code.startswith("3"):
            symbol = f"sz{stock_code}"
        else:
            symbol = f"bj{stock_code}"
        
        url = f"https://hq.sinajs.cn/list={symbol}"
        headers = {"Referer": "https://finance.sina.com.cn"}
        
        with httpx.Client(timeout=10) as client:
            resp = client.get(url, headers=headers)
            text = resp.text
        
        # 解析: var hq_str_sh600519="茅台,1459.54,1459.88,1460.00,..."
        start = text.find('"') + 1
        end = text.find('"', start)
        data = text[start:end].split(',')
        
        if len(data) < 32:
            return {"error": "股票代码不存在或已退市"}
        
        name = data[0]
        open_price = float(data[1]) if data[1] else 0
        close_prev = float(data[2]) if data[2] else 0
        current = float(data[3]) if data[3] else 0
        high = float(data[4]) if data[4] else 0
        low = float(data[5]) if data[5] else 0
        volume = float(data[8]) if data[8] else 0  # 成交量(股)
        amount = float(data[9]) if data[9] else 0  # 成交额(元)
        
        change = ((current - close_prev) / close_prev * 100) if close_prev else 0
        
        return {
            "code": stock_code,
            "name": name if name else "",
            "price": current,
            "change": round(change, 2),
            "change_amount": round(current - close_prev, 2),
            "open": open_price,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "close_prev": close_prev,
        }
    except Exception as e:
        return {"error": str(e)}


def get_kline_data(stock_code: str, period: str = "daily", adjust: str = "qfq") -> dict:
    """
    获取K线数据
    period: daily / weekly / monthly
    """
    try:
        if stock_code.startswith("6"):
            symbol = f"sh{stock_code}"
        elif stock_code.startswith("0") or stock_code.startswith("3"):
            symbol = f"sz{stock_code}"
        else:
            symbol = f"bj{stock_code}"
        
        # 腾讯证券 K线 API
        # period_key 映射到腾讯的 qfqxxx 格式
        period_map = {
            "daily": ("day", "qfqday"),
            "weekly": ("week", "qfqweek"),
            "monthly": ("month", "qfqmonth")
        }
        period_key, qfq_key = period_map.get(period, ("day", "qfqday"))
        
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_{period_key}qfq&param={symbol},{period_key},,,180,qfq&r=0.1"
        
        with httpx.Client(timeout=15) as client:
            resp = client.get(url)
            text = resp.text
        
        # 解析: kline_dayqfq={...}
        json_start = text.find('=') + 1
        data = json.loads(text[json_start:])
        
        if data.get("code") != 0:
            return {"error": "获取K线数据失败"}
        
        stock_data = data.get("data", {}).get(symbol, {})
        klines = stock_data.get(qfq_key, [])
        
        if not klines:
            return {"error": "暂无数据"}
        
        # 取最近180条
        klines = klines[-180:]
        
        dates, opens, closes, highs, lows, volumes = [], [], [], [], [], []
        
        for item in klines:
            if len(item) >= 6:
                dates.append(item[0])
                opens.append(float(item[1]))
                closes.append(float(item[2]))
                highs.append(float(item[3]))
                lows.append(float(item[4]))
                volumes.append(float(item[5]))
        
        return {
            "dates": dates,
            "open": opens,
            "close": closes,
            "high": highs,
            "low": lows,
            "volume": volumes,
            "amount": [0] * len(dates),
        }
    except Exception as e:
        return {"error": str(e)}


def calculate_indicators(kline_data: dict) -> dict:
    """计算技术指标"""
    close = np.array(kline_data['close'])
    high = np.array(kline_data['high'])
    low = np.array(kline_data['low'])
    volume = np.array(kline_data['volume'])
    
    # ========== MA 均线 ==========
    ma5 = pd.Series(close).rolling(window=5).mean().fillna(0).tolist()
    ma10 = pd.Series(close).rolling(window=10).mean().fillna(0).tolist()
    ma20 = pd.Series(close).rolling(window=20).mean().fillna(0).tolist()
    ma60 = pd.Series(close).rolling(window=60).mean().fillna(0).tolist()
    
    # ========== EMA ==========
    ema12 = pd.Series(close).ewm(span=12).mean().fillna(0).tolist()
    ema26 = pd.Series(close).ewm(span=26).mean().fillna(0).tolist()
    
    # ========== MACD ==========
    dif = pd.Series(close).ewm(span=12).mean() - pd.Series(close).ewm(span=26).mean()
    dea = dif.ewm(span=9).mean()
    macd_bar = (dif - dea) * 2
    macd = {
        "dif": dif.fillna(0).tolist(),
        "dea": dea.fillna(0).tolist(),
        "bar": macd_bar.fillna(0).tolist(),
    }
    
    # ========== KDJ ==========
    n = 9
    kdj_k = [50.0] * (n - 1)
    kdj_d = [50.0] * (n - 1)
    
    for i in range(n - 1, len(close)):
        pv = low[i-n+1:i+1].min()
        ph = high[i-n+1:i+1].max()
        if ph == pv:
            rsv = 50
        else:
            rsv = (close[i] - pv) / (ph - pv) * 100
        k_val = kdj_k[-1] * 2 / 3 + rsv / 3
        d_val = kdj_d[-1] * 2 / 3 + k_val / 3
        kdj_k.append(k_val)
        kdj_d.append(d_val)
    
    kdj_j = [k * 3 - d * 2 for k, d in zip(kdj_k, kdj_d)]
    kdj = {
        "k": kdj_k[-len(close):],
        "d": kdj_d[-len(close):],
        "j": kdj_j[-len(close):],
    }
    
    # ========== RSI ==========
    def calc_rsi(period):
        rsi = []
        for i in range(len(close)):
            if i < period:
                rsi.append(50)
            else:
                gains = []
                losses = []
                for j in range(i - period + 1, i + 1):
                    diff = close[j] - close[j - 1]
                    if diff > 0:
                        gains.append(diff)
                    else:
                        losses.append(abs(diff))
                avg_gain = sum(gains) / period if gains else 0
                avg_loss = sum(losses) / period if losses else 0
                if avg_loss == 0:
                    rsi.append(100)
                else:
                    rs = avg_gain / avg_loss
                    rsi.append(100 - (100 / (1 + rs)))
        return rsi
    
    rsi6 = calc_rsi(6)
    rsi12 = calc_rsi(12)
    rsi24 = calc_rsi(24)
    
    # ========== BOLL布林带 ==========
    boll_mid = pd.Series(close).rolling(window=20).mean().fillna(0)
    boll_std = pd.Series(close).rolling(window=20).std().fillna(0)
    boll_upper = (boll_mid + 2 * boll_std).tolist()
    boll_mid = boll_mid.tolist()
    boll_lower = (boll_mid - 2 * boll_std).tolist()
    
    return {
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "ema12": ema12, "ema26": ema26,
        "macd": macd,
        "kdj": kdj,
        "rsi6": rsi6, "rsi12": rsi12, "rsi24": rsi24,
        "boll_upper": boll_upper, "boll_mid": boll_mid, "boll_lower": boll_lower,
    }


def ai_analyze(stock_code: str, stock_name: str, kline_data: dict, indicators: dict) -> dict:
    """基于技术指标给出AI分析建议"""
    try:
        latest_close = kline_data['close'][-1]
        latest_ma5 = indicators['ma5'][-1]
        latest_ma10 = indicators['ma10'][-1]
        latest_ma20 = indicators['ma20'][-1]
        latest_ma60 = indicators['ma60'][-1]
        
        latest_macd = {
            "dif": indicators['macd']['dif'][-1],
            "dea": indicators['macd']['dea'][-1],
            "bar": indicators['macd']['bar'][-1],
        }
        
        latest_kdj = {
            "k": indicators['kdj']['k'][-1],
            "d": indicators['kdj']['d'][-1],
            "j": indicators['kdj']['j'][-1],
        }
        
        latest_rsi = {
            "rsi6": indicators['rsi6'][-1],
            "rsi12": indicators['rsi12'][-1],
            "rsi24": indicators['rsi24'][-1],
        }
        
        latest_boll = {
            "upper": indicators['boll_upper'][-1],
            "mid": indicators['boll_mid'][-1],
            "lower": indicators['boll_lower'][-1],
        }
        
        # 均线多空判断
        ma_trend = "多头排列" if latest_close > latest_ma5 > latest_ma10 > latest_ma20 else \
                   "空头排列" if latest_close < latest_ma5 < latest_ma10 < latest_ma20 else "震荡"
        
        # 基于指标计算评分 (1-10)
        score = 5
        
        # 均线多头给分
        if latest_close > latest_ma5:
            score += 0.5
        if latest_ma5 > latest_ma10:
            score += 0.5
        if latest_ma10 > latest_ma20:
            score += 0.5
        if latest_ma20 > latest_ma60:
            score += 0.5
        
        # MACD给分
        if latest_macd['dif'] > latest_macd['dea']:
            score += 1
        if latest_macd['bar'] > 0:
            score += 0.5
        
        # KDJ给分
        if 20 < latest_kdj['k'] < 80:
            score += 0.3
        if latest_kdj['k'] > latest_kdj['d'] and latest_kdj['d'] < 40:
            score += 0.5
        
        # RSI给分
        if 30 < latest_rsi['rsi6'] < 70:
            score += 0.3
        
        # 布林带给分
        if latest_boll['mid'] < latest_close < latest_boll['upper']:
            score += 0.4
        elif latest_close < latest_boll['mid']:
            score -= 0.3
        
        score = min(max(score, 1), 10)
        
        # 趋势判断
        medium_trend = "上涨" if latest_close > latest_ma60 else "下跌"
        
        if latest_ma5 > latest_ma10 and latest_macd['bar'] > 0:
            short_trend = "上涨"
        elif latest_ma5 < latest_ma10 and latest_macd['bar'] < 0:
            short_trend = "下跌"
        else:
            short_trend = "震荡"
        
        # 建议
        if score >= 7:
            advice = "强烈建议"
        elif score >= 6:
            advice = "建议"
        elif score >= 4:
            advice = "观望"
        else:
            advice = "不建议"
        
        # 生成理由
        macd_signal = "金叉" if latest_macd['dif'] > latest_macd['dea'] else "死叉"
        kdj_signal = "超买" if latest_kdj['k'] > 80 else "超卖" if latest_kdj['k'] < 20 else "正常"
        rsi_signal = "超买" if latest_rsi['rsi6'] > 70 else "超卖" if latest_rsi['rsi6'] < 30 else "正常"
        
        reason = f"均线{ma_trend}，MACD{latest_macd['dif']:.2f}{macd_signal}，KDJ {kdj_signal}，RSI {rsi_signal}，综合评分{score:.1f}分"
        
        return {
            "score": round(score, 1),
            "short_trend": short_trend,
            "medium_trend": medium_trend,
            "advice": advice,
            "reason": reason
        }
    except Exception as e:
        return {
            "score": 5,
            "short_trend": "震荡",
            "medium_trend": "震荡",
            "advice": "观望",
            "reason": f"分析异常: {str(e)[:50]}"
        }


# ========== API 路由 ==========

@app.get("/", response_class=HTMLResponse)
async def index():
    """主页"""
    with open("templates/index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, status_code=200)


@app.get("/api/stock/{stock_code}")
async def get_stock(stock_code: str):
    """获取股票基本信息"""
    return get_stock_info(stock_code)


@app.get("/api/kline/{stock_code}")
async def get_kline(stock_code: str, period: str = "daily"):
    """获取K线数据"""
    return get_kline_data(stock_code, period)


@app.get("/api/indicators/{stock_code}")
async def get_indicators(stock_code: str, period: str = "daily"):
    """获取技术指标"""
    kline = get_kline_data(stock_code, period)
    if "error" in kline:
        return kline
    return calculate_indicators(kline)


@app.get("/api/analyze/{stock_code}")
async def analyze_stock(stock_code: str, period: str = "daily"):
    """AI分析股票"""
    info = get_stock_info(stock_code)
    if "error" in info:
        return info
    
    kline = get_kline_data(stock_code, period)
    if "error" in kline:
        return kline
    
    indicators = calculate_indicators(kline)
    
    ai_result = ai_analyze(stock_code, info.get("name", ""), kline, indicators)
    
    return {
        "stock_info": info,
        "latest_price": kline['close'][-1] if kline.get('close') else 0,
        "indicators": indicators,
        "ai_analysis": ai_result,
    }


@app.get("/api/search")
async def search_stock(keyword: str):
    """搜索股票"""
    try:
        # 新浪搜索建议
        url = f"https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15,16,17,18,19,110&key={keyword}&limit=10"
        headers = {"Referer": "https://finance.sina.com.cn"}
        
        with httpx.Client(timeout=10) as client:
            resp = client.get(url, headers=headers)
            text = resp.text
        
        # 解析: var suggestresult_11_12_13_14_15_16_17_18_19_110="..."
        start = text.find('"') + 1
        end = text.find('"', start)
        data = text[start:end]
        
        results = []
        for item in data.split(';'):
            parts = item.split(',')
            if len(parts) >= 4:
                results.append({
                    "code": parts[1],
                    "name": parts[0],
                    "price": 0,
                    "change": 0
                })
        
        return {"results": results[:10]}
    except Exception as e:
        return {"error": str(e), "results": []}


# ========== 选股 API ==========

@app.get("/screener", response_class=HTMLResponse)
async def screener_page(request: Request):
    """选股结果页面"""
    user_agent = request.headers.get("user-agent", "")
    template_name = "templates/screener_mobile.html" if is_mobile_user_agent(user_agent) else "templates/screener.html"
    with open(template_name, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, status_code=200)


@app.get("/strategies", response_class=HTMLResponse)
async def strategies_page(request: Request):
    """策略管理页面"""
    user_agent = request.headers.get("user-agent", "")
    template_name = "templates/strategies_mobile.html" if is_mobile_user_agent(user_agent) else "templates/strategies.html"
    with open(template_name, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, status_code=200)


@app.get("/api/strategy/contract")
async def strategy_contract():
    return get_strategy_contract()


@app.get("/api/strategies")
async def get_strategy_list():
    return {
        "strategies": list_strategies(),
        "groups": list_strategy_groups(),
    }


@app.post("/api/strategies")
async def create_strategy_api(request: Request):
    payload = await request.json()
    name = (payload.get("name") or "").strip()
    description = (payload.get("description") or "").strip()
    code = (payload.get("code") or "").strip()
    create_mode = (payload.get("create_mode") or "direct").strip()
    enabled = bool(payload.get("enabled", True))
    if not name or not code:
        return JSONResponse({"ok": False, "error": "策略名称和代码不能为空"}, status_code=400)
    try:
        test_context = build_strategy_context({"code": "000001", "name": "平安银行", "symbol": "sz000001"}, [], [])
        validation = run_strategy_code(code, test_context)
        if validation.get("error"):
            return JSONResponse({"ok": False, "error": validation.get("reason", "策略代码校验失败")}, status_code=400)
        strategy = create_strategy(name, description, code, create_mode=create_mode, enabled=enabled)
        return {"ok": True, "strategy": strategy}
    except sqlite3.IntegrityError:
        return JSONResponse({"ok": False, "error": "策略名称已存在"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.put("/api/strategies/{strategy_id}")
async def update_strategy_api(strategy_id: int, request: Request):
    payload = await request.json()
    name = (payload.get("name") or "").strip()
    description = (payload.get("description") or "").strip()
    code = (payload.get("code") or "").strip()
    enabled = bool(payload.get("enabled", True))
    if not name or not code:
        return JSONResponse({"ok": False, "error": "策略名称和代码不能为空"}, status_code=400)
    try:
        test_context = build_strategy_context({"code": "000001", "name": "平安银行", "symbol": "sz000001"}, [], [])
        validation = run_strategy_code(code, test_context)
        if validation.get("error"):
            return JSONResponse({"ok": False, "error": validation.get("reason", "策略代码校验失败")}, status_code=400)
        ok = update_strategy(strategy_id, name, description, code, enabled=enabled)
        if not ok:
            return JSONResponse({"ok": False, "error": "策略不存在"}, status_code=404)
        return {"ok": True, "strategy": get_strategy(strategy_id)}
    except sqlite3.IntegrityError:
        return JSONResponse({"ok": False, "error": "策略名称已存在"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.delete("/api/strategies/{strategy_id}")
async def delete_strategy_api(strategy_id: int):
    if not delete_strategy(strategy_id):
        return JSONResponse({"ok": False, "error": "策略不存在"}, status_code=404)
    return {"ok": True}


@app.post("/api/strategy-groups")
async def create_strategy_group_api(request: Request):
    payload = await request.json()
    name = (payload.get("name") or "").strip()
    description = (payload.get("description") or "").strip()
    match_mode = (payload.get("match_mode") or "AND").upper()
    strategy_ids = [int(item) for item in payload.get("strategy_ids", [])]
    if not name:
        return JSONResponse({"ok": False, "error": "策略组名称不能为空"}, status_code=400)
    if not strategy_ids:
        return JSONResponse({"ok": False, "error": "策略组至少选择一个策略"}, status_code=400)
    try:
        group = create_strategy_group(name, description, match_mode, strategy_ids)
        return {"ok": True, "group": group}
    except sqlite3.IntegrityError:
        return JSONResponse({"ok": False, "error": "策略组名称已存在"}, status_code=400)


@app.put("/api/strategy-groups/{group_id}")
async def update_strategy_group_api(group_id: int, request: Request):
    payload = await request.json()
    name = (payload.get("name") or "").strip()
    description = (payload.get("description") or "").strip()
    match_mode = (payload.get("match_mode") or "AND").upper()
    strategy_ids = [int(item) for item in payload.get("strategy_ids", [])]
    if not name:
        return JSONResponse({"ok": False, "error": "策略组名称不能为空"}, status_code=400)
    if not strategy_ids:
        return JSONResponse({"ok": False, "error": "策略组至少选择一个策略"}, status_code=400)
    try:
        update_strategy_group(group_id, name, description, match_mode, strategy_ids)
        return {"ok": True, "group": get_strategy_group(group_id)}
    except sqlite3.IntegrityError:
        return JSONResponse({"ok": False, "error": "策略组名称已存在"}, status_code=400)


@app.delete("/api/strategy-groups/{group_id}")
async def delete_strategy_group_api(group_id: int):
    if not delete_strategy_group(group_id):
        return JSONResponse({"ok": False, "error": "策略组不存在"}, status_code=404)
    return {"ok": True}


@app.post("/api/strategies/generate-code")
async def generate_strategy_api(request: Request):
    payload = await request.json()
    prompt_text = (payload.get("prompt") or "").strip()
    if not prompt_text:
        return JSONResponse({"ok": False, "error": "请输入策略描述"}, status_code=400)
    try:
        code = generate_strategy_code(prompt_text)
        return {"ok": True, "code": code}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/strategies/generate-context")
async def generate_strategy_context_api(request: Request):
    payload = await request.json()
    prompt_text = (payload.get("prompt") or "").strip()
    if not prompt_text:
        return JSONResponse({"ok": False, "error": "请输入策略描述"}, status_code=400)
    try:
        llm_context = build_strategy_generation_context(prompt_text)
        copy_text = (
            "请根据下面要求生成符合项目约定的 Python 策略代码，只返回代码。\n\n"
            f"模型建议：{llm_context['model']}\n"
            f"接口地址参考：{llm_context['base_url']}/chat/completions\n\n"
            "System Prompt:\n"
            f"{llm_context['system_prompt']}\n\n"
            "User Prompt:\n"
            f"{llm_context['user_prompt']}\n"
        )
        return {"ok": True, "context": llm_context, "copy_text": copy_text}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/api/screener/targets")
async def screener_targets():
    return get_target_options()


@app.get("/api/screener/run")
async def run_screener(target_type: str = "strategy", target_id: Optional[int] = None):
    """手动触发选股（扫描全部A股）"""
    if SCREENING_RESULT["running"]:
        return {"status": "running", "message": "选股任务正在执行中..."}

    resolved = resolve_screening_target(target_type, target_id)
    if not resolved:
        return JSONResponse({"status": "error", "message": "没有可执行的策略或策略组"}, status_code=400)

    t = threading.Thread(target=run_screening_sync, kwargs={"target_type": target_type, "target_id": target_id}, daemon=True)
    t.start()
    return {
        "status": "started",
        "message": f"选股任务已启动，目标：{resolved['target_name']}",
        "target_type": resolved["target_type"],
        "target_id": resolved["target_id"],
        "target_name": resolved["target_name"],
    }


@app.get("/api/screener/status")
async def screener_status(target_type: Optional[str] = None, target_id: Optional[int] = None):
    """查询选股状态"""
    if SCREENING_RESULT["running"]:
        return {
            "running": True,
            "time": SCREENING_RESULT["time"],
            "total_stocks": SCREENING_RESULT["total_stocks"],
            "processed": SCREENING_RESULT["processed"],
            "matched_count": SCREENING_RESULT["matched_count"],
            "target_type": SCREENING_RESULT.get("target_type"),
            "target_id": SCREENING_RESULT.get("target_id"),
            "target_name": SCREENING_RESULT.get("target_name"),
            "error": SCREENING_RESULT.get("error", ""),
        }

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    sql = "SELECT * FROM screening_runs WHERE 1 = 1"
    params = []
    if target_type:
        sql += " AND target_type = ?"
        params.append(target_type)
    if target_id is not None:
        sql += " AND target_id = ?"
        params.append(target_id)
    sql += " ORDER BY created_at DESC LIMIT 1"
    run_info = c.execute(sql, params).fetchone()
    conn.close()
    if run_info:
        run_info = dict(run_info)
        return {
            "running": False,
            "time": f"{run_info['run_date']} {run_info['run_time']}",
            "total_stocks": run_info.get('total_stocks', 0),
            "processed": run_info.get('total_stocks', 0),
            "matched_count": run_info.get('matched_count', 0),
            "target_type": run_info.get("target_type"),
            "target_id": run_info.get("target_id"),
            "target_name": run_info.get("target_name"),
            "status": run_info.get("status"),
        }

    return {
        "running": False,
        "time": "",
        "total_stocks": 0,
        "processed": 0,
        "matched_count": 0,
        "target_type": target_type,
        "target_id": target_id,
        "target_name": "",
    }


@app.get("/api/screener/results")
async def get_screener_results(target_type: Optional[str] = None, target_id: Optional[int] = None):
    """获取选股结果"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    sql = "SELECT * FROM screening_runs WHERE status = 'completed'"
    params = []
    if target_type:
        sql += " AND target_type = ?"
        params.append(target_type)
    if target_id is not None:
        sql += " AND target_id = ?"
        params.append(target_id)
    sql += " ORDER BY created_at DESC LIMIT 1"
    latest = c.execute(sql, params).fetchone()

    if not latest:
        conn.close()
        return {"results": [], "time": "", "total": 0, "matched_count": 0}

    run_date = latest['run_date']
    run_time = latest['run_time']
    rows = c.execute(
        '''SELECT * FROM screening_results
           WHERE run_date = ? AND run_time = ?
           ORDER BY score DESC, current_volume DESC''',
        (run_date, run_time),
    ).fetchall()

    results = []
    for r in rows:
        payload = {}
        if r["result_payload"]:
            try:
                payload = json.loads(r["result_payload"])
            except Exception:
                payload = {}
        results.append({
            "code": r['stock_code'],
            "name": r['stock_name'],
            "daily": r['daily_condition'],
            "weekly": r['weekly_condition'],
            "reason": r['weekly_condition'],
            "current_vol": r['current_volume'],
            "dif": r['dif'],
            "dea": r['dea'],
            "score": r['score'] or 0,
            "matched_strategies": [item.strip() for item in (r["matched_strategies"] or "").split(",") if item.strip()],
            "payload": payload,
        })

    conn.close()

    return {
        "time": f"{run_date} {run_time}",
        "total": len(results),
        "matched_count": len(results),
        "target_type": latest["target_type"],
        "target_id": latest["target_id"],
        "target_name": latest["target_name"],
        "results": results,
    }


@app.get("/api/screener/history")
async def get_screener_history(target_type: Optional[str] = None, target_id: Optional[int] = None):
    """获取历史选股记录"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    sql = "SELECT * FROM screening_runs WHERE status = 'completed'"
    params = []
    if target_type:
        sql += " AND target_type = ?"
        params.append(target_type)
    if target_id is not None:
        sql += " AND target_id = ?"
        params.append(target_id)
    sql += " ORDER BY created_at DESC LIMIT 30"
    rows = c.execute(sql, params).fetchall()

    history = []
    for r in rows:
        history.append({
            "run_date": r['run_date'],
            "run_time": r['run_time'],
            "target_type": r['target_type'],
            "target_id": r['target_id'],
            "target_name": r['target_name'],
            "target_logic": r['target_logic'],
            "total_stocks": r['total_stocks'],
            "matched_count": r['matched_count'],
            "status": r['status'],
        })

    conn.close()
    return {"history": history}


@app.get("/api/screener/history/{run_date}/{run_time}")
async def get_history_detail(run_date: str, run_time: str, target_type: Optional[str] = None, target_id: Optional[int] = None):
    """获取某次选股的具体结果"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    sql = "SELECT * FROM screening_results WHERE run_date = ? AND run_time = ?"
    params = [run_date, run_time]
    if target_type:
        sql += " AND target_type = ?"
        params.append(target_type)
    if target_id is not None:
        sql += " AND target_id = ?"
        params.append(target_id)
    sql += " ORDER BY score DESC, current_volume DESC"
    rows = c.execute(sql, params).fetchall()
    conn.close()
    results = []
    for row in rows:
        item = dict(row)
        if item.get("result_payload"):
            try:
                item["result_payload"] = json.loads(item["result_payload"])
            except Exception:
                pass
        results.append(item)
    return {
        "run_date": run_date,
        "run_time": run_time,
        "total": len(results),
        "results": results,
    }


# ========== 虚拟炒股 API ==========

@app.get("/vt", response_class=HTMLResponse)
async def vt_page(request: Request):
    """虚拟炒股主页"""
    user_agent = request.headers.get("user-agent", "")
    template_name = "templates/vt_mobile.html" if is_mobile_user_agent(user_agent) else "templates/vt.html"
    with open(template_name, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, status_code=200)


@app.post("/api/vt/register")
async def vt_register(username: str = Form(...), password: str = Form(...)):
    """注册"""
    if len(username) < 2 or len(username) > 20:
        return {"ok": False, "error": "用户名2-20字符"}
    if len(password) < 4:
        return {"ok": False, "error": "密码至少4字符"}
    return create_user(username, password)


@app.post("/api/vt/login")
async def vt_login(username: str = Form(...), password: str = Form(...)):
    """登录"""
    result = login_user(username, password)
    if not result.get("ok"):
        return result
    resp = JSONResponse(content=result)
    resp.set_cookie(key="vt_session", value=result["token"], httponly=True, path="/", max_age=2592000, samesite="lax")
    return resp


@app.post("/api/vt/logout")
async def vt_logout(vt_session: str = Cookie(None)):
    """登出"""
    if vt_session and vt_session in sessions:
        del sessions[vt_session]
    return RedirectResponse(url="/vt", status_code=303)


@app.get("/api/vt/me")
async def vt_me(vt_session: str = Cookie(None)):
    """当前登录用户信息"""
    if not vt_session:
        return {"logged_in": False}
    s = get_session(vt_session)
    if not s:
        return {"logged_in": False}
    accounts = get_user_accounts(s["user_id"])
    return {
        "logged_in": True,
        "user_id": s["user_id"],
        "username": s["username"],
        "is_admin": s["is_admin"],
        "accounts": accounts
    }


@app.post("/api/vt/accounts")
async def vt_create_account(vt_session: str = Cookie(None), account_name: str = Form(...)):
    """新建虚拟账户"""
    s = get_session(vt_session) if vt_session else None
    if not s:
        raise HTTPException(status_code=401, detail="未登录")
    return create_account(s["user_id"], account_name)


@app.get("/api/vt/account/{account_id}")
async def vt_account_summary(account_id: int, vt_session: str = Cookie(None)):
    """账户概况"""
    s = get_session(vt_session) if vt_session else None
    if not s:
        raise HTTPException(status_code=401, detail="未登录")
    return get_account_summary(account_id)


@app.get("/api/vt/account/{account_id}/positions")
async def vt_positions(account_id: int, vt_session: str = Cookie(None)):
    s = get_session(vt_session) if vt_session else None
    if not s:
        raise HTTPException(status_code=401, detail="未登录")
    return {"positions": get_positions(account_id)}


@app.get("/api/vt/account/{account_id}/trades")
async def vt_trades(account_id: int, vt_session: str = Cookie(None)):
    s = get_session(vt_session) if vt_session else None
    if not s:
        raise HTTPException(status_code=401, detail="未登录")
    return {"trades": get_trades(account_id)}


@app.post("/api/vt/buy")
async def vt_buy(
    account_id: int = Form(...),
    stock_code: str = Form(...),
    stock_name: str = Form(...),
    shares: int = Form(...),
    price: float = Form(...),
    vt_session: str = Cookie(None)
):
    s = get_session(vt_session) if vt_session else None
    if not s:
        raise HTTPException(status_code=401, detail="未登录")
    return buy_stock(account_id, stock_code, stock_name, shares, price)


@app.post("/api/vt/sell")
async def vt_sell(
    account_id: int = Form(...),
    stock_code: str = Form(...),
    shares: int = Form(...),
    price: float = Form(...),
    vt_session: str = Cookie(None)
):
    s = get_session(vt_session) if vt_session else None
    if not s:
        raise HTTPException(status_code=401, detail="未登录")
    return sell_stock(account_id, stock_code, shares, price)


@app.get("/api/vt/admin/users")
async def vt_admin_users(vt_session: str = Cookie(None)):
    """管理员: 所有用户和账户"""
    s = get_session(vt_session) if vt_session else None
    if not s or not s.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return {"users": get_all_users()}


@app.post("/api/vt/admin/reset/{account_id}")
async def vt_admin_reset(account_id: int, vt_session: str = Cookie(None)):
    s = get_session(vt_session) if vt_session else None
    if not s or not s.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return reset_account(account_id)


@app.post("/api/vt/admin/password/{user_id}")
async def vt_admin_reset_password(user_id: int, new_password: str = Form(...), vt_session: str = Cookie(None)):
    """管理员重置用户密码"""
    s = get_session(vt_session) if vt_session else None
    if not s or not s.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return admin_reset_user_password(user_id, new_password)


@app.post("/api/vt/admin/delete/{account_id}")
async def vt_admin_delete_account(account_id: int, vt_session: str = Cookie(None)):
    """销户"""
    s = get_session(vt_session) if vt_session else None
    if not s or not s.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return delete_account(account_id)


@app.post("/api/vt/admin/balance/{account_id}")
async def vt_admin_adjust_balance(account_id: int, new_balance: float = Form(...), vt_session: str = Cookie(None)):
    """调整账户余额"""
    s = get_session(vt_session) if vt_session else None
    if not s or not s.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return adjust_balance(account_id, new_balance)


@app.get("/api/vt/admin/account/{account_id}")
async def vt_admin_account_detail(account_id: int, vt_session: str = Cookie(None)):
    """获取账户详情（持仓+交易）"""
    s = get_session(vt_session) if vt_session else None
    if not s or not s.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return get_account_detail(account_id)


@app.post("/api/vt/password")
async def vt_change_password(old_password: str = Form(...), new_password: str = Form(...), vt_session: str = Cookie(None)):
    """用户修改自己密码"""
    s = get_session(vt_session) if vt_session else None
    if not s:
        raise HTTPException(status_code=401, detail="未登录")
    return change_user_password(s["user_id"], old_password, new_password)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

from __future__ import annotations

import codecs
import json
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from db import compat as db
from app.core.strategy_engine import build_strategy_context, get_strategy_contract, run_strategy_code
from app.repositories.screening_repository import ScreeningRepository
from app.runtime import task_manager
from app.services.market_service import (
    ai_analyze,
    calculate_indicators,
    get_quote_bundle_async,
    get_kline_data_async,
    get_stock_info_async,
    search_stock_async,
)
from app.services.screener_service import enqueue_screening_task
from app.services.strategy_service import (
    build_strategy_generation_context,
    generate_strategy_code,
    get_target_options,
    strategy_repository,
)
from app.config import has_database_config, settings


router = APIRouter(prefix="/api")
screening_repository = ScreeningRepository(settings.db_path)


def _database_unavailable_response() -> JSONResponse:
    return JSONResponse(
        {"error": "数据库未配置，依赖数据库的功能暂不可用，请先设置 STOCK_AI_DB_URL"},
        status_code=503,
    )


def _task_list_item(task: dict) -> dict:
    result = task.get("result") or {}
    return {
        "id": task["id"],
        "task_type": task["task_type"],
        "queue_name": task.get("queue_name"),
        "status": task["status"],
        "priority": int(task.get("priority") or 0),
        "run_token": task.get("run_token", ""),
        "target_type": task.get("target_type"),
        "target_id": task.get("target_id"),
        "target_name": task.get("target_name"),
        "progress_current": task.get("progress_current", 0),
        "progress_total": task.get("progress_total", 0),
        "progress_message": task.get("progress_message", ""),
        "result_text": task.get("result_text", ""),
        "matched_count": result.get("matched_count", 0),
        "total_stocks": result.get("total_stocks", 0),
        "run_date": result.get("run_date", ""),
        "run_time": result.get("run_time", ""),
        "failure_summary": result.get("failure_summary", ""),
        "ai_summary": result.get("ai_summary", ""),
        "raw_miss_log_count": int(result.get("raw_miss_log_count") or 0),
        "has_miss_log": int(result.get("raw_miss_log_count") or 0) > 0,
        "error_text": task.get("error_text", ""),
        "created_at": task.get("created_at", ""),
        "started_at": task.get("started_at", ""),
        "completed_at": task.get("completed_at", ""),
    }


@router.get("/stock/{stock_code}")
async def get_stock(request: Request, stock_code: str):
    return await get_stock_info_async(stock_code, client=request.app.state.market_http_client)


@router.get("/kline/{stock_code}")
async def get_kline(stock_code: str, period: str = "daily", adjust: str = "qfq"):
    return await get_kline_data_async(stock_code, period, adjust)


@router.get("/indicators/{stock_code}")
async def get_indicators(stock_code: str, period: str = "daily", adjust: str = "qfq"):
    kline_data = await get_kline_data_async(stock_code, period, adjust)
    if kline_data.get("error"):
        return JSONResponse(kline_data, status_code=400)
    return {"code": stock_code, "period": period, "indicators": calculate_indicators(kline_data)}


@router.get("/analyze/{stock_code}")
async def analyze_stock(request: Request, stock_code: str, period: str = "daily", adjust: str = "qfq"):
    bundle = await get_quote_bundle_async(stock_code, period, adjust, request.app.state.market_http_client)
    if bundle.get("error"):
        return JSONResponse({"error": bundle["error"]}, status_code=400)
    return {
        "stock": bundle["stock"],
        "analysis": bundle["analysis"],
        "ai_analysis": bundle["analysis"],
        "indicators": bundle["indicators"],
    }


@router.get("/search")
async def search_stock(request: Request, q: Optional[str] = None, keyword: Optional[str] = None):
    search_text = (q or keyword or "").strip()
    if not search_text:
        return JSONResponse({"error": "请输入搜索关键词", "results": []}, status_code=400)
    return await search_stock_async(search_text, request.app.state.market_http_client)


@router.get("/quote/{stock_code}")
async def get_quote_bundle(request: Request, stock_code: str, period: str = "daily", adjust: str = "qfq"):
    bundle = await get_quote_bundle_async(stock_code, period, adjust, request.app.state.market_http_client)
    if bundle.get("error"):
        return JSONResponse({"error": bundle["error"]}, status_code=400)
    return bundle


@router.get("/strategy/contract")
async def strategy_contract():
    return get_strategy_contract()


@router.get("/strategies")
async def get_strategy_list():
    if not has_database_config():
        return _database_unavailable_response()
    return {
        "strategies": strategy_repository.list_strategies(),
        "groups": strategy_repository.list_strategy_groups(),
    }


@router.post("/strategies")
async def create_strategy_api(request: Request):
    if not has_database_config():
        return _database_unavailable_response()
    payload = await request.json()
    name = (payload.get("name") or "").strip()
    description = (payload.get("description") or "").strip()
    code = (payload.get("code") or "").strip()
    create_mode = (payload.get("create_mode") or "direct").strip() or "direct"
    enabled = 1 if payload.get("enabled", True) else 0
    if not name or not code:
        return JSONResponse({"ok": False, "error": "策略名称和代码不能为空"}, status_code=400)
    try:
        test_context = build_strategy_context({"code": "000001", "name": "平安银行", "symbol": "sz000001"}, [], [])
        validation = run_strategy_code(code, test_context)
        if validation.get("error"):
            return JSONResponse({"ok": False, "error": validation.get("reason", "策略代码校验失败")}, status_code=400)
        return {"ok": True, "strategy": strategy_repository.create_strategy(name, description, code, create_mode, enabled)}
    except db.IntegrityError:
        return JSONResponse({"ok": False, "error": "策略名称已存在"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.put("/strategies/{strategy_id}")
async def update_strategy_api(strategy_id: int, request: Request):
    if not has_database_config():
        return _database_unavailable_response()
    payload = await request.json()
    name = (payload.get("name") or "").strip()
    description = (payload.get("description") or "").strip()
    code = (payload.get("code") or "").strip()
    enabled = 1 if payload.get("enabled", True) else 0
    if not name or not code:
        return JSONResponse({"ok": False, "error": "策略名称和代码不能为空"}, status_code=400)
    try:
        test_context = build_strategy_context({"code": "000001", "name": "平安银行", "symbol": "sz000001"}, [], [])
        validation = run_strategy_code(code, test_context)
        if validation.get("error"):
            return JSONResponse({"ok": False, "error": validation.get("reason", "策略代码校验失败")}, status_code=400)
        ok = strategy_repository.update_strategy(strategy_id, name, description, code, enabled=enabled)
        if not ok:
            return JSONResponse({"ok": False, "error": "策略不存在"}, status_code=404)
        return {"ok": True, "strategy": strategy_repository.get_strategy(strategy_id)}
    except db.IntegrityError:
        return JSONResponse({"ok": False, "error": "策略名称已存在"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.delete("/strategies/{strategy_id}")
async def delete_strategy_api(strategy_id: int):
    if not has_database_config():
        return _database_unavailable_response()
    if not strategy_repository.delete_strategy(strategy_id):
        return JSONResponse({"ok": False, "error": "策略不存在"}, status_code=404)
    return {"ok": True}


@router.post("/strategy-groups")
async def create_strategy_group_api(request: Request):
    if not has_database_config():
        return _database_unavailable_response()
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
        return {"ok": True, "group": strategy_repository.create_strategy_group(name, description, match_mode, strategy_ids)}
    except db.IntegrityError:
        return JSONResponse({"ok": False, "error": "策略组名称已存在"}, status_code=400)


@router.put("/strategy-groups/{group_id}")
async def update_strategy_group_api(group_id: int, request: Request):
    if not has_database_config():
        return _database_unavailable_response()
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
        strategy_repository.update_strategy_group(group_id, name, description, match_mode, strategy_ids)
        return {"ok": True, "group": strategy_repository.get_strategy_group(group_id)}
    except db.IntegrityError:
        return JSONResponse({"ok": False, "error": "策略组名称已存在"}, status_code=400)


@router.delete("/strategy-groups/{group_id}")
async def delete_strategy_group_api(group_id: int):
    if not has_database_config():
        return _database_unavailable_response()
    if not strategy_repository.delete_strategy_group(group_id):
        return JSONResponse({"ok": False, "error": "策略组不存在"}, status_code=404)
    return {"ok": True}


@router.post("/strategies/generate-code")
async def generate_strategy_api(request: Request):
    payload = await request.json()
    prompt_text = (payload.get("prompt") or "").strip()
    if not prompt_text:
        return JSONResponse({"ok": False, "error": "请输入策略描述"}, status_code=400)
    try:
        return {"ok": True, "code": generate_strategy_code(prompt_text)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/strategies/generate-context")
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


@router.get("/screener/targets")
async def screener_targets():
    if not has_database_config():
        return _database_unavailable_response()
    return get_target_options()


@router.get("/screener/run")
async def run_screener(target_type: str = "strategy", target_id: Optional[int] = None):
    if not has_database_config():
        return _database_unavailable_response()
    try:
        task = enqueue_screening_task(target_type=target_type, target_id=target_id, source="manual")
    except ValueError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)
    return {
        "status": "queued",
        "message": f"选股任务已加入队列，目标：{task.get('target_name') or '-'}",
        "task_id": task["id"],
        "run_token": task.get("run_token", ""),
        "target_type": task.get("target_type"),
        "target_id": task.get("target_id"),
        "target_name": task.get("target_name"),
    }


@router.get("/screener/status")
async def screener_status(target_type: Optional[str] = None, target_id: Optional[int] = None):
    if not has_database_config():
        return _database_unavailable_response()
    task = task_manager.get_latest_task(task_type="screening", target_type=target_type, target_id=target_id)
    if task:
        result = task.get("result") or {}
        progress_total = int(task.get("progress_total") or 0)
        progress_current = int(task.get("progress_current") or 0)
        return {
            "running": task["status"] in ("queued", "running"),
            "queued": task["status"] == "queued",
            "task_id": task["id"],
            "run_token": task.get("run_token", ""),
            "time": task.get("started_at") or task.get("created_at") or "",
            "total_stocks": progress_total or int(result.get("total_stocks") or 0),
            "processed": progress_current if progress_total else int(result.get("total_stocks") or 0),
            "matched_count": int(result.get("matched_count") or 0),
            "target_type": task.get("target_type"),
            "target_id": task.get("target_id"),
            "target_name": task.get("target_name"),
            "status": task.get("status"),
            "progress_message": task.get("progress_message") or "",
            "failure_summary": result.get("failure_summary", ""),
            "ai_summary": result.get("ai_summary", ""),
            "raw_miss_log_count": int(result.get("raw_miss_log_count") or 0),
            "error": task.get("error_text", ""),
        }
    run_info = screening_repository.query_latest_run(target_type=target_type, target_id=target_id, completed_only=False)
    if run_info:
        return {
            "running": False,
            "queued": False,
            "task_id": None,
            "run_token": run_info.get("run_token", ""),
            "time": f"{run_info['run_date']} {run_info['run_time']}",
            "total_stocks": run_info.get("total_stocks", 0),
            "processed": run_info.get("total_stocks", 0),
            "matched_count": run_info.get("matched_count", 0),
            "target_type": run_info.get("target_type"),
            "target_id": run_info.get("target_id"),
            "target_name": run_info.get("target_name"),
            "status": run_info.get("status"),
            "progress_message": "",
            "failure_summary": run_info.get("failure_summary", ""),
            "ai_summary": "",
            "raw_miss_log_count": 0,
            "error": "",
        }
    return {
        "running": False,
        "queued": False,
        "task_id": None,
        "time": "",
        "total_stocks": 0,
        "processed": 0,
        "matched_count": 0,
        "target_type": target_type,
        "target_id": target_id,
        "target_name": "",
        "status": "",
        "progress_message": "",
        "failure_summary": "",
        "ai_summary": "",
        "raw_miss_log_count": 0,
        "error": "",
    }


@router.get("/tasks")
async def list_tasks_api(
    task_type: Optional[str] = None,
    status: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    limit: int = 20,
    sort: str = "recent",
):
    if not has_database_config():
        return _database_unavailable_response()
    tasks = task_manager.list_tasks(
        task_type=task_type,
        status=status,
        target_type=target_type,
        target_id=target_id,
        limit=max(1, min(limit, 100)),
        sort_mode="queue" if sort == "queue" else "recent",
    )
    items = [_task_list_item(task) for task in tasks]
    return {"tasks": items}


@router.get("/tasks/latest")
async def get_latest_task_api(task_type: Optional[str] = None):
    if not has_database_config():
        return _database_unavailable_response()
    task = task_manager.get_latest_task(task_type=task_type)
    return {"ok": True, "task": _task_list_item(task) if task else None}


@router.get("/tasks/{task_id}")
async def get_task_api(task_id: int):
    if not has_database_config():
        return _database_unavailable_response()
    task = task_manager.get_task(task_id)
    if not task:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    return {"ok": True, "task": task}


@router.delete("/tasks/{task_id}")
async def delete_task_api(task_id: int):
    if not has_database_config():
        return _database_unavailable_response()
    ok, message = task_manager.delete_task(task_id)
    if not ok:
        status_code = 404 if message == "任务不存在" else 400
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)
    return {"ok": True, "message": message}


@router.post("/tasks/{task_id}/move")
async def move_task_api(task_id: int, request: Request):
    if not has_database_config():
        return _database_unavailable_response()
    payload = await request.json()
    action = str(payload.get("action") or "").strip().lower()
    ok, message = task_manager.reorder_task(task_id, action)
    if not ok:
        status_code = 404 if message == "任务不存在" else 400
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)
    task = task_manager.get_task(task_id)
    return {"ok": True, "message": message, "task": _task_list_item(task) if task else None}


@router.get("/screener/results")
async def get_screener_results(target_type: Optional[str] = None, target_id: Optional[int] = None):
    if not has_database_config():
        return _database_unavailable_response()
    latest_task = task_manager.get_latest_task(
        task_type="screening",
        target_type=target_type,
        target_id=target_id,
        statuses=["completed", "failed"],
    )
    latest_task_result = (latest_task or {}).get("result") or {}
    latest = screening_repository.query_latest_run(target_type=target_type, target_id=target_id, completed_only=True)
    if not latest:
        return {"results": [], "time": "", "total": 0, "matched_count": 0, "failure_summary": ""}
    rows = screening_repository.query_results(
        run_token=latest.get("run_token") or None,
        run_date=latest["run_date"],
        run_time=latest["run_time"],
    )
    results = []
    for row in rows:
        payload = {}
        if row["result_payload"]:
            try:
                payload = json.loads(row["result_payload"])
            except Exception:
                payload = {}
        results.append(
            {
                "code": row["stock_code"],
                "name": row["stock_name"],
                "daily": row["daily_condition"],
                "weekly": row["weekly_condition"],
                "reason": row["weekly_condition"],
                "current_vol": row["current_volume"],
                "dif": row["dif"],
                "dea": row["dea"],
                "score": row["score"] or 0,
                "matched_strategies": [item.strip() for item in (row["matched_strategies"] or "").split(",") if item.strip()],
                "payload": payload,
            }
        )
    return {
        "time": f"{latest['run_date']} {latest['run_time']}",
        "run_token": latest.get("run_token", ""),
        "total": len(results),
        "matched_count": len(results),
        "target_type": latest["target_type"],
        "target_id": latest["target_id"],
        "target_name": latest["target_name"],
        "failure_summary": latest.get("failure_summary", ""),
        "ai_summary": latest_task_result.get("ai_summary", ""),
        "raw_miss_log_count": int(latest_task_result.get("raw_miss_log_count") or 0),
        "results": results,
    }


@router.get("/screener/history")
async def get_screener_history(target_type: Optional[str] = None, target_id: Optional[int] = None):
    if not has_database_config():
        return _database_unavailable_response()
    latest_task = task_manager.get_latest_task(
        task_type="screening",
        target_type=target_type,
        target_id=target_id,
        statuses=["completed", "failed"],
    )
    latest_task_result = (latest_task or {}).get("result") or {}
    rows = screening_repository.query_runs(target_type=target_type, target_id=target_id, limit=30, completed_only=True)
    history = []
    for item in rows:
        miss_log_text = str(item.get("miss_log_text") or "").strip()
        history.append(
            {
                "run_token": item.get("run_token", ""),
                "run_date": item["run_date"],
                "run_time": item["run_time"],
                "target_type": item["target_type"],
                "target_id": item["target_id"],
                "target_name": item["target_name"],
                "target_logic": item["target_logic"],
                "total_stocks": item["total_stocks"],
                "matched_count": item["matched_count"],
                "status": item["status"],
                "failure_summary": item.get("failure_summary", ""),
                "ai_summary": latest_task_result.get("ai_summary", "") if item.get("run_token", "") == (latest_task or {}).get("run_token", "") else "",
                "raw_miss_log_count": (
                    int(item.get("matched_count") or 0) < int(item.get("total_stocks") or 0)
                    if miss_log_text
                    else 0
                ),
                "has_miss_log": bool(miss_log_text),
            }
        )
    return {"history": history}


@router.get("/screener/history/{run_date}/{run_time}")
async def get_history_detail(
    run_date: str,
    run_time: str,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    run_token: Optional[str] = None,
):
    if not has_database_config():
        return _database_unavailable_response()
    rows = screening_repository.query_results(
        run_token=run_token,
        run_date=run_date,
        run_time=run_time,
        target_type=target_type,
        target_id=target_id,
    )
    results = []
    for item in rows:
        if item.get("result_payload"):
            try:
                item["result_payload"] = json.loads(item["result_payload"])
            except Exception:
                pass
        results.append(item)
    return {"run_date": run_date, "run_time": run_time, "total": len(results), "results": results}


@router.get("/screener/history/{run_date}/{run_time}/miss-log.txt")
async def download_history_miss_log(
    run_date: str,
    run_time: str,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    run_token: Optional[str] = None,
):
    if not has_database_config():
        return _database_unavailable_response()
    run_info = screening_repository.get_run(
        run_token=run_token,
        run_date=run_date,
        run_time=run_time,
        target_type=target_type,
        target_id=target_id,
    )
    if not run_info:
        return JSONResponse({"ok": False, "error": "运行记录不存在"}, status_code=404)
    miss_log_text = str(run_info.get("miss_log_text") or "").strip()
    if not miss_log_text:
        return JSONResponse({"ok": False, "error": "当前运行没有可下载的未命中日志"}, status_code=404)
    target_name = str(run_info.get("target_name") or "screening").strip() or "screening"
    safe_target_name = "".join(ch if ch.isascii() and (ch.isalnum() or ch in ("-", "_")) else "_" for ch in target_name)
    filename = f"{safe_target_name}_{run_date}_{run_time.replace(':', '-')}_miss_log.txt"
    utf8_filename = quote(f"{target_name}_{run_date}_{run_time.replace(':', '-')}_miss_log.txt")
    # Add UTF-8 BOM so local editors reliably detect Chinese text encoding.
    return Response(
        content=codecs.BOM_UTF8 + miss_log_text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={filename}; filename*=UTF-8''{utf8_filename}"
        },
    )

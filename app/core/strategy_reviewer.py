from __future__ import annotations

import ast
from typing import Any

from app.core.strategy_engine import build_strategy_context, run_strategy_code, validate_strategy_code


def _make_daily_rows(mode: str) -> list[list[Any]]:
    rows = []
    for index in range(80):
        if mode == "rising":
            close = 10 + index * 0.12
            open_price = close - 0.08
            volume = 1000 + index * 12
        else:
            close = 22 - index * 0.09
            open_price = close + 0.06
            volume = 1800 - min(index * 8, 500)
        rows.append(
            [
                f"2024-04-{(index % 28) + 1:02d}",
                round(open_price, 2),
                round(close, 2),
                round(max(open_price, close) + 0.15, 2),
                round(min(open_price, close) - 0.15, 2),
                float(volume),
            ]
        )
    if mode == "rising":
        rows[-1][5] = 2600.0
    return rows


def _make_weekly_rows(mode: str) -> list[list[Any]]:
    rows = []
    for index in range(12):
        close = 15 + index * 0.3 if mode == "rising" else 18 - index * 0.2
        open_price = close - 0.18 if mode == "rising" or index % 3 == 0 else close + 0.2
        rows.append(
            [
                f"2024-W{index + 1:02d}",
                round(open_price, 2),
                round(close, 2),
                round(max(open_price, close) + 0.25, 2),
                round(min(open_price, close) - 0.25, 2),
                float(5000 + index * 120),
            ]
        )
    return rows


def _review_cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "空数据保护",
            "description": "日线和周线为空，验证策略能安全返回数据不足原因。",
            "context": build_strategy_context(
                {"code": "000001", "name": "空数据样例", "symbol": "sz000001"},
                [],
                [],
            ),
        },
        {
            "name": "上升趋势样例",
            "description": "准备 80 根日线和 12 根周线，价格、成交量整体上行。",
            "context": build_strategy_context(
                {"code": "000002", "name": "上升趋势样例", "symbol": "sz000002"},
                _make_daily_rows("rising"),
                _make_weekly_rows("rising"),
            ),
        },
        {
            "name": "回落低量样例",
            "description": "准备 80 根日线和 12 根周线，价格回落、成交量偏弱。",
            "context": build_strategy_context(
                {"code": "000003", "name": "回落低量样例", "symbol": "sz000003"},
                _make_daily_rows("falling"),
                _make_weekly_rows("falling"),
            ),
        },
    ]


def _subscript_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Index):  # pragma: no cover - Python 3.8 compatibility
        return _subscript_key(node.value)
    return None


def _context_path(node: ast.AST) -> str | None:
    parts = []
    current = node
    while isinstance(current, ast.Subscript):
        key = _subscript_key(current.slice)
        if key is None:
            return None
        parts.append(key)
        current = current.value
    if not isinstance(current, ast.Name) or current.id != "context":
        return None
    if not parts:
        return None
    parts.reverse()
    return "context" + "".join(f"['{part}']" for part in parts)


def _collect_code_analysis(code: str) -> dict[str, Any]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {
            "context_paths": [],
            "return_keys": [],
            "explanation": ["代码存在语法错误，暂无法解读。"],
        }

    context_paths = set()
    return_keys = set()
    branches = 0
    for node in ast.walk(tree):
        path = _context_path(node)
        if path:
            context_paths.add(path)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for key_node in node.value.keys:
                if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                    return_keys.add(key_node.value)
        if isinstance(node, ast.If):
            branches += 1

    sorted_paths = sorted(
        path
        for path in context_paths
        if not any(other != path and other.startswith(path + "[") for other in context_paths)
    )
    sorted_keys = sorted(return_keys)
    explanation = []
    if sorted_paths:
        preview = "、".join(sorted_paths[:8])
        suffix = " 等字段" if len(sorted_paths) > 8 else ""
        explanation.append(f"主要读取 {preview}{suffix}。")
    else:
        explanation.append("未识别到对 context 字段的读取，需要重点检查策略是否真的使用行情数据。")
    if sorted_keys:
        explanation.append(f"返回结果包含 {', '.join(sorted_keys)}。")
    else:
        explanation.append("未识别到明确的 dict 返回字段。")
    explanation.append(f"代码里包含 {branches} 个条件分支，CR 时重点看数据不足和边界条件是否覆盖。")

    return {
        "context_paths": sorted_paths,
        "return_keys": sorted_keys,
        "explanation": explanation,
    }


def _build_findings(code: str, static_errors: list[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for message in static_errors:
        findings.append(
            {
                "severity": "error",
                "line": None,
                "title": "静态校验失败",
                "detail": message,
            }
        )

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [
            {
                "severity": "error",
                "line": exc.lineno,
                "title": "Python 语法错误",
                "detail": exc.msg,
            }
        ]

    has_data_guard = False
    has_reason = False
    has_metrics = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value == "enough_data":
                    has_data_guard = True
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            has_reason = has_reason or "reason" in keys
            has_metrics = has_metrics or "metrics" in keys

    if not static_errors:
        findings.append(
            {
                "severity": "ok",
                "line": None,
                "title": "静态结构通过",
                "detail": "已找到 run_strategy(context)，未发现 import、危险调用或顶层副作用。",
            }
        )
    if not has_data_guard:
        findings.append(
            {
                "severity": "warn",
                "line": None,
                "title": "建议补充数据不足保护",
                "detail": "没有识别到 enough_data 检查。真实行情缺少足够 K 线时，策略可能抛异常或误判。",
            }
        )
    if not has_reason:
        findings.append(
            {
                "severity": "warn",
                "line": None,
                "title": "建议返回 reason",
                "detail": "未识别到 reason 字段。保存结果和未命中日志会更难排查。",
            }
        )
    if not has_metrics:
        findings.append(
            {
                "severity": "info",
                "line": None,
                "title": "可选：返回 metrics",
                "detail": "metrics 可以记录关键阈值和最新指标，便于后续看结果时复盘。",
            }
        )
    return findings


def _trim_text(value: Any, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _run_cases(code: str, blocked: bool) -> list[dict[str, Any]]:
    results = []
    for case in _review_cases():
        if blocked:
            results.append(
                {
                    "name": case["name"],
                    "description": case["description"],
                    "status": "blocked",
                    "ok": False,
                    "passed": False,
                    "reason": "静态校验未通过，未执行样例。",
                    "score": None,
                    "metrics": {},
                }
            )
            continue

        result = run_strategy_code(code, case["context"])
        ok = not result.get("error")
        results.append(
            {
                "name": case["name"],
                "description": case["description"],
                "status": "passed" if ok else "failed",
                "ok": ok,
                "passed": bool(result.get("pass")),
                "reason": _trim_text(result.get("reason", "")),
                "score": result.get("score"),
                "metrics": result.get("metrics") if isinstance(result.get("metrics"), dict) else {},
                "traceback": result.get("traceback", ""),
            }
        )
    return results


def review_strategy_code(code: str) -> dict[str, Any]:
    code = (code or "").strip()
    if not code:
        return {
            "ok": False,
            "summary": {"severity": "error", "case_total": 0, "case_passed": 0},
            "findings": [
                {
                    "severity": "error",
                    "line": None,
                    "title": "策略代码为空",
                    "detail": "请先生成或填写 run_strategy(context) 代码。",
                }
            ],
            "tests": [],
            "analysis": {"context_paths": [], "return_keys": [], "explanation": []},
        }

    static_errors = validate_strategy_code(code)
    findings = _build_findings(code, static_errors)
    tests = _run_cases(code, blocked=bool(static_errors))
    analysis = _collect_code_analysis(code)
    signal_cases = [item for item in tests if item["name"] in {"上升趋势样例", "回落低量样例"} and item["ok"]]
    if len(signal_cases) == 2 and signal_cases[0]["passed"] == signal_cases[1]["passed"]:
        findings.append(
            {
                "severity": "warn",
                "line": None,
                "title": "样例区分度偏弱",
                "detail": "上升趋势和回落低量样例返回了相同命中结果，建议确认条件是否过宽或过窄。",
            }
        )

    error_count = sum(1 for item in findings if item["severity"] == "error")
    warn_count = sum(1 for item in findings if item["severity"] == "warn")
    case_passed = sum(1 for item in tests if item["ok"])
    case_total = len(tests)
    ok = error_count == 0 and case_passed == case_total
    severity = "warn" if ok and warn_count else ("ok" if ok else "error")

    return {
        "ok": ok,
        "summary": {
            "severity": severity,
            "error_count": error_count,
            "warn_count": warn_count,
            "case_total": case_total,
            "case_passed": case_passed,
        },
        "findings": findings,
        "tests": tests,
        "analysis": analysis,
    }

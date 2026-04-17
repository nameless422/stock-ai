from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import os

import httpx
from screening_core import StrategyScreeningFilter, SwitchingMarketDataSource, build_failure_summary


class ScreeningTaskHandler:
    def __init__(
        self,
        target_resolver,
        run_saver,
        max_workers: int,
        submit_batch: int,
        save_interval: int,
        data_source_factory=SwitchingMarketDataSource,
    ):
        self.target_resolver = target_resolver
        self.run_saver = run_saver
        self.max_workers = max_workers
        self.submit_batch = submit_batch
        self.save_interval = save_interval
        self.data_source_factory = data_source_factory

    def _build_ai_summary(self, target_info: dict, total: int, matched_count: int, failure_reason_counts: Counter, miss_log_samples: list[str]) -> str:
        api_key = os.getenv("MINIMAX_API_KEY") or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            return ""

        if os.getenv("MINIMAX_API_KEY"):
            base_url = (os.getenv("MINIMAX_API_BASE") or os.getenv("LLM_API_BASE") or "https://api.minimax.io/v1").rstrip("/")
            model = os.getenv("MINIMAX_MODEL") or os.getenv("LLM_MODEL") or "MiniMax-M2.5"
        else:
            base_url = (
                os.getenv("LLM_API_BASE")
                or os.getenv("OPENAI_BASE_URL")
                or os.getenv("OPENAI_API_BASE")
                or "https://api.openai.com/v1"
            ).rstrip("/")
            model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

        reasons_text = "\n".join(
            f"- {reason}: {count}"
            for reason, count in failure_reason_counts.most_common(20)
        ) or "- 无未命中原因"
        samples_text = "\n".join(f"- {item}" for item in miss_log_samples[:80]) or "- 无原始日志"
        messages = [
            {
                "role": "system",
                "content": "你是资深量化排障助手。请根据任务扫描日志总结最关键的未命中原因、异常模式、可能的修复方向。输出中文纯文本，控制在6行内。",
            },
            {
                "role": "user",
                "content": (
                    f"目标: {target_info.get('target_name')}\n"
                    f"扫描总数: {total}\n"
                    f"命中数: {matched_count}\n"
                    f"聚合原因:\n{reasons_text}\n"
                    f"原始未命中样本:\n{samples_text}\n"
                    "请总结：1. 最主要失败模式 2. 是否像策略代码错误/数据不足/数据源异常 3. 下一步建议。"
                ),
            },
        ]
        try:
            with httpx.Client(timeout=60) as client:
                response = client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": 0.1,
                    },
                )
                response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                return ""
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "").strip()
            if content.startswith("```"):
                content = content.strip("`").strip()
            return content
        except Exception:
            return ""

    def __call__(self, task: dict, context) -> dict:
        payload = task.get("payload") or {}
        requested_target_type = payload.get("target_type")
        requested_target_id = payload.get("target_id")
        target_info = self.target_resolver(requested_target_type, requested_target_id)
        if not target_info:
            raise ValueError("未找到可执行的策略或策略组")

        context.set_target(target_info["target_type"], target_info["target_id"], target_info["target_name"])
        context.log(f"开始扫描目标: {target_info['target_name']}")

        run_token = task.get("run_token") or ""
        run_started_at = datetime.now()
        run_date = run_started_at.strftime("%Y-%m-%d")
        run_time = run_started_at.strftime("%H:%M:%S")
        results = []
        total = 0

        try:
            data_source = self.data_source_factory()
            stocks = data_source.list_stocks()
            total = len(stocks)
            context.set_progress(0, total, "已加载股票池，等待开始")
            context.log(f"已加载股票池，共 {total} 只股票")

            if total == 0:
                failure_summary = "股票列表为空，未执行扫描"
                self.run_saver(
                    run_token,
                    run_date,
                    run_time,
                    0,
                    0,
                    "failed",
                    [],
                    target_info=target_info,
                    failure_summary=failure_summary,
                )
                return {
                    "summary": failure_summary,
                    "matched_count": 0,
                    "total_stocks": 0,
                    "run_date": run_date,
                    "run_time": run_time,
                    "failure_summary": failure_summary,
                }

            stock_filter = StrategyScreeningFilter(data_source, target_info)
            failure_reason_counts = Counter()
            miss_log_samples = []
            processed = 0
            save_counter = 0
            batch_size = min(self.submit_batch, total)
            max_workers = min(self.max_workers, total)
            context.log(f"执行参数: workers={max_workers}, batch={batch_size}, save_interval={self.save_interval}")

            for start in range(0, total, batch_size):
                stock_batch = stocks[start:start + batch_size]
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {
                        executor.submit(stock_filter.evaluate, stock["code"], stock["name"]): stock
                        for stock in stock_batch
                    }

                    for future in as_completed(future_map):
                        stock = future_map[future]
                        try:
                            item = future.result()
                        except Exception as exc:
                            item = {
                                "code": stock["code"],
                                "name": stock["name"],
                                "pass": False,
                                "reason": str(exc),
                                "error": str(exc),
                            }

                        processed += 1
                        if item.get("pass"):
                            results.append(item)
                            save_counter += 1
                        else:
                            reason = str(item.get("reason", "")).strip() or "未命中"
                            failure_reason_counts[reason] += 1
                            miss_log = f"{item.get('code', '-')}\t{item.get('name', '-')}\t{reason}"
                            context.log(miss_log, level="warn")
                            if len(miss_log_samples) < 120:
                                miss_log_samples.append(miss_log)
                            if item.get("error"):
                                error_detail = str(item.get("traceback") or item.get("error") or reason).strip()
                                context.log(f"ERROR\t{item.get('code', '-')}\t{item.get('name', '-')}\t{error_detail}", level="error")
                            for strategy_result in ((item.get("payload") or {}).get("strategy_results") or []):
                                if not strategy_result.get("error"):
                                    continue
                                strategy_error = str(
                                    strategy_result.get("traceback")
                                    or strategy_result.get("reason")
                                    or "策略执行异常"
                                ).strip()
                                context.log(
                                    f"ERROR\t{item.get('code', '-')}\t{item.get('name', '-')}\t{strategy_result.get('strategy_name', '-')}\t{strategy_error}",
                                    level="error",
                                )

                        if processed % 10 == 0 or processed >= total:
                            context.set_progress(
                                processed,
                                total,
                                f"已处理 {processed}/{total}，命中 {len(results)}，未命中原因 {build_failure_summary(failure_reason_counts)}",
                            )

                        if processed % 100 == 0 or processed >= total:
                            context.log(f"处理进度 {processed}/{total}，当前命中 {len(results)}")

                        if save_counter >= self.save_interval:
                            self.run_saver(
                                run_token,
                                run_date,
                                run_time,
                                total,
                                len(results),
                                "running",
                                results,
                                target_info=target_info,
                                failure_summary=build_failure_summary(failure_reason_counts),
                            )
                            save_counter = 0

            results.sort(key=lambda item: item.get("current_vol", 0), reverse=True)
            failure_summary = build_failure_summary(failure_reason_counts)
            ai_summary = self._build_ai_summary(
                target_info=target_info,
                total=total,
                matched_count=len(results),
                failure_reason_counts=failure_reason_counts,
                miss_log_samples=miss_log_samples,
            )
            self.run_saver(
                run_token,
                run_date,
                run_time,
                total,
                len(results),
                "completed",
                results,
                target_info=target_info,
                failure_summary=failure_summary,
            )

            summary = f"扫描完成，命中 {len(results)} / {total}"
            context.set_progress(total, total, summary)
            context.log(summary)
            if failure_summary:
                context.log(f"主要未命中原因: {failure_summary}")
            if ai_summary:
                context.log(f"AI总结: {ai_summary}")
            return {
                "summary": summary,
                "matched_count": len(results),
                "total_stocks": total,
                "run_date": run_date,
                "run_time": run_time,
                "failure_summary": failure_summary,
                "ai_summary": ai_summary,
                "raw_miss_log_count": sum(failure_reason_counts.values()),
                "target_name": target_info["target_name"],
                "target_type": target_info["target_type"],
                "target_id": target_info["target_id"],
            }
        except Exception as exc:
            self.run_saver(
                run_token,
                run_date,
                run_time,
                total,
                len(results),
                "failed",
                results,
                target_info=target_info,
                failure_summary=str(exc),
            )
            raise

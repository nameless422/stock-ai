from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from screening_core import StrategyScreeningFilter, TencentMarketDataSource, build_failure_summary


class ScreeningTaskHandler:
    def __init__(
        self,
        target_resolver,
        run_saver,
        max_workers: int,
        submit_batch: int,
        save_interval: int,
        data_source_factory=TencentMarketDataSource,
    ):
        self.target_resolver = target_resolver
        self.run_saver = run_saver
        self.max_workers = max_workers
        self.submit_batch = submit_batch
        self.save_interval = save_interval
        self.data_source_factory = data_source_factory

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
                            failure_reason_counts[str(item.get("reason", "")).strip() or "未命中"] += 1

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
            return {
                "summary": summary,
                "matched_count": len(results),
                "total_stocks": total,
                "run_date": run_date,
                "run_time": run_time,
                "failure_summary": failure_summary,
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

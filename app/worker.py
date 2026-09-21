"""Background worker: runs composition tasks in a daemon thread pool."""
from __future__ import annotations

import json
import threading
import traceback

from . import database
from .solver import solve_exam, solve_revise

# 组卷为 CPU 密集型任务，单线程串行执行即可保证结果确定性与 SQLite 写入安全。
_semaphore = threading.Semaphore(1)


def _run(task_id: int) -> None:
    with _semaphore:
        database.update_status(task_id, "running")
        row = database.get_task(task_id)
        try:
            req = json.loads(row["request"])
            if req.get("kind") == "revise":
                result = solve_revise(req)
            else:
                result = solve_exam(req)
        except Exception as exc:  # noqa: BLE001 - 任何异常都落库，任务可查询
            result = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
            # 改卷任务异常时也保留源任务溯源信息
            try:
                if req.get("kind") == "revise":
                    result["source_task_id"] = req["source_task_id"]
            except Exception:  # noqa: BLE001 - 溯源补充失败不应掩盖原异常
                pass
        database.finish_task(task_id, json.dumps(result, ensure_ascii=False))


def submit(task_id: int) -> None:
    threading.Thread(target=_run, args=(task_id,), daemon=True).start()

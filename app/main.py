"""FastAPI application: task creation, revision and result query."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import asynccontextmanager

from . import database
from .schemas import (
    ExamTaskIn,
    ReviseTaskIn,
    TaskCreated,
    TaskResult,
    TaskStatus,
)
from .worker import submit


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="智能组卷 API",
    description="面向教研组的题库快照约束组卷服务（FastAPI + SQLite + OR-Tools CP-SAT），支持 2~5 份平行卷联合组卷",
    version="1.2.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/tasks", response_model=TaskCreated, status_code=201)
def create_task(payload: ExamTaskIn) -> TaskCreated:
    """提交组卷任务：题库快照 + 总分/题型/知识点/难度/互斥题组约束。"""
    req = payload.model_dump()
    task_id = database.create_task(
        payload.name,
        json.dumps(req, ensure_ascii=False),
        payload.seed,
    )
    submit(task_id)
    return TaskCreated(task_id=task_id, status="pending")


def _source_paper_selection(
    source_req: Dict[str, Any], source_result: Dict[str, Any]
) -> List[List[Any]]:
    """从源任务结果中提取逐卷选中题号（单卷 -> 单元素列表）。"""
    paper_count = int(source_req.get("paper_count") or 1)
    if paper_count == 1:
        return [list(source_result.get("selected_ids") or [])]
    return [
        list(p.get("selected_ids") or [])
        for p in source_result.get("papers") or []
    ]


@app.post("/api/tasks/{task_id}/revise", response_model=TaskCreated, status_code=201)
def revise_task(task_id: int, payload: ReviseTaskIn) -> TaskCreated:
    """基于已完成且可行的源任务创建定向改卷任务（异步）：按卷指定必须保留/移除的题号。"""
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] != "done":
        raise HTTPException(status_code=422, detail="源任务尚未完成，无法改卷")

    source_req = json.loads(row["request"])
    # 改卷以最近一次组卷/改卷的原始请求与源卷选择为准（支持对改卷结果再次改卷）
    if source_req.get("kind") == "revise":
        source_req = source_req["source_request"]
    if not row["result"]:
        raise HTTPException(status_code=422, detail="源任务尚无结果，无法改卷")
    source_result = json.loads(row["result"])
    if source_result.get("status") != "feasible":
        raise HTTPException(status_code=422, detail="源任务结果不可行，无法改卷")

    paper_count = int(source_req.get("paper_count") or 1)
    id_set = {q["id"] for q in source_req["questions"]}

    # 卷序号不可重复；不在 [0, paper_count) 内或题号不存在均返回 422
    seen_papers = set()
    for e in payload.edits:
        if e.paper in seen_papers:
            raise HTTPException(status_code=422, detail=f"第 {e.paper} 卷的编辑条件重复指定")
        seen_papers.add(e.paper)
        if e.paper >= paper_count:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"卷序号 {e.paper} 不存在：源任务共 {paper_count} 份卷"
                    f"（卷序号 0~{paper_count - 1}）"
                ),
            )
        missing = [qid for qid in (e.retain_ids + e.remove_ids) if qid not in id_set]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"第 {e.paper} 卷引用了题库中不存在的题号: {missing}",
            )

    # 冻结源请求与逐卷人工编辑条件；源任务本身保持不变
    frozen = {
        "kind": "revise",
        "source_task_id": task_id,
        "source_request": source_req,
        "source_selection": _source_paper_selection(source_req, source_result),
        "edits": [e.model_dump() for e in payload.edits],
    }
    seed = int(source_req.get("seed") or 42)
    name = payload.name if payload.name is not None else row["name"]
    new_task_id = database.create_task(
        name,
        json.dumps(frozen, ensure_ascii=False),
        seed,
    )
    submit(new_task_id)
    return TaskCreated(task_id=new_task_id, status="pending")


@app.get("/api/tasks/{task_id}", response_model=TaskStatus)
def get_task(task_id: int) -> TaskStatus:
    """查询任务状态与组卷结果（可行卷或最小冲突约束集）。"""
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    result: Optional[TaskResult] = None
    if row["result"]:
        result = TaskResult(**json.loads(row["result"]))

    source_task_id: Optional[int] = None
    req = json.loads(row["request"])
    if req.get("kind") == "revise":
        source_task_id = req.get("source_task_id")

    return TaskStatus(
        task_id=row["id"],
        status=row["status"],
        name=row["name"],
        seed=row["seed"],
        created_at=row["created_at"],
        source_task_id=source_task_id,
        result=result,
    )

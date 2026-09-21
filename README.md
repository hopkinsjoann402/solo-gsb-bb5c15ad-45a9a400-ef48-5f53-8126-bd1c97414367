# 智能组卷 API

面向教研组的约束组卷服务：提交**题库快照**与**总分、题型数量、知识点覆盖、难度区间、互斥题组**等约束，自动生成分值精确、约束满足、结果可复现的试卷；多组可行解按 **难度偏差 → 重复知识点数 → 题目编号序列** 依次择优；无解时返回经逐项删减验证的**最小冲突约束集**。

支持一次请求生成 **2～5 份平行卷**（`paper_count`）：所有卷在同一个 CP-SAT 联合模型中分别满足全部单卷约束，并限制任意两卷共同题数不超过 `max_shared_questions`；多卷可行解按 **各卷难度偏差总和 → 跨卷重复题总数 → 各卷题号序列** 依次择优，无解时冲突集区分**单卷约束**（`per_paper`）与**跨卷重叠上限**（`cross_paper`）。不传 `paper_count` 时为单卷，请求与响应结构完全保持兼容。

还支持一次请求返回 **1～5 个候选卷组**（`alternative_count`，缺省 `1`）：在同一冻结题库与原约束下，按上述既有择优规则依次给出排名候选；后续候选与此前每个候选的**每张对应卷**至少有 `min_question_changes`（多候选缺省 `1`）道题不同，且多卷时**不能仅靠交换卷序**制造新候选。差异约束只增不删、绝不放宽原约束；候选不足时返回实际数量与 `exhausted: true`。`alternative_count` 缺省（或为 `1`）时响应结构与旧版完全一致。

此外支持对已完成且可行的任务**定向改卷**（`POST /api/tasks/{task_id}/revise`）：按卷指定必须保留/移除的题号，联合重求解全部卷并在满足原约束的前提下**最大化各卷保留原题总数**，详见下文[定向改卷](#定向改卷post-apitaskstask_idrevise)。

技术栈：Python · FastAPI · SQLite · OR-Tools CP-SAT · Docker / Compose。

## 启动

```bash
docker compose up --build
```

容器启动时自动安装依赖并创建 SQLite 数据库，无需任何手动初始化。

- API 地址：http://localhost:8000
- 端口：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 的 `ports` 修改）
- 必要配置（环境变量，均有默认值，一般无需修改）：
  - `EXAM_DB_PATH`：SQLite 数据库文件路径，默认 `/data/exam.db`（已挂载为持久卷 `exam_data`）
- 交互式文档（Swagger）：http://localhost:8000/docs

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/tasks` | 创建组卷任务（异步），返回 `task_id` |
| `POST` | `/api/tasks/{task_id}/revise` | 在已完成且可行的任务上创建**定向改卷**任务（异步） |
| `GET`  | `/api/tasks/{task_id}` | 查询任务状态与结果 |
| `GET`  | `/health` | 健康检查 |

任务状态：`pending` → `running` → `done`；结果在 `result` 字段中返回（`feasible` / `infeasible` / `error`）。

## 请求示例

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考",
    "questions": [
      {"id": 1, "score": 5,  "type": "single_choice", "difficulty": 0.2, "knowledge_points": ["集合"]},
      {"id": 2, "score": 5,  "type": "single_choice", "difficulty": 0.5, "knowledge_points": ["函数"]},
      {"id": 3, "score": 5,  "type": "single_choice", "difficulty": 0.8, "knowledge_points": ["导数"]},
      {"id": 4, "score": 10, "type": "essay",         "difficulty": 0.6, "knowledge_points": ["函数", "导数"]},
      {"id": 5, "score": 10, "type": "essay",         "difficulty": 0.7, "knowledge_points": ["数列"]}
    ],
    "total_score": 20,
    "type_counts": {"single_choice": 2, "essay": 1},
    "knowledge_coverage": {"函数": 1},
    "difficulty_range": [0.3, 0.7],
    "target_difficulty": 0.5,
    "mutex_groups": [[2, 4]],
    "seed": 42
  }'
```

字段说明：

- `questions`：题库快照，提交后冻结。每题含 `id`（数字或字符串，唯一）、`score`（正整数）、`type`、`difficulty`（0~1）、`knowledge_points`。
- `total_score`：试卷总分，**精确等于**。
- `type_counts`：各题型的精确数量。
- `knowledge_coverage`：知识点 → 试卷中至少出现的题目数。
- `difficulty_range`：试卷平均难度（按分值加权）闭区间 `[min, max]`。
- `target_difficulty`：期望平均难度，缺省取难度区间中点；难度偏差为 `|平均难度 - 目标难度|`。
- `mutex_groups`：互斥题组，每组至多选取 1 题。
- `paper_count`：平行卷份数，取 `2`～`5` 时一次生成多份卷；缺省（或 `1`）为单卷，响应结构与旧版一致。
- `max_shared_questions`：任意两卷**共同题数上限**，仅多卷时可设置；缺省 `0`（平行卷之间不重复）。
- `alternative_count`：候选卷组数量，取 `1`～`5`，缺省 `1`（单候选，响应结构与旧版完全一致）。
- `min_question_changes`：后续候选与此前每个候选的**每张对应卷**至少不同的题目数（按对称差计），多候选时缺省 `1`；多卷任务下仅交换卷序不算新候选。
- `seed`：随机种子（默认 42），相同请求 + 相同种子结果可复现。

## 多卷请求示例（2～5 份平行卷）

在单卷请求体上增加 `paper_count` 与 `max_shared_questions` 即可。各卷在**同一个联合 CP-SAT 模型**中分别满足总分、题型、知识点覆盖、难度区间、互斥题组约束，且任意两卷共同题数不超过上限：

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考·A/B/C 平行卷",
    "questions": [
      {"id": 1,  "score": 5,  "type": "single_choice", "difficulty": 0.3, "knowledge_points": ["集合"]}
      // ……题库快照（题量需足够分给各卷，允许部分题目各卷都不选）
    ],
    "total_score": 150,
    "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
    "knowledge_coverage": {"函数": 2, "导数": 1},
    "difficulty_range": [0.45, 0.6],
    "target_difficulty": 0.52,
    "mutex_groups": [[12, 13]],
    "paper_count": 3,
    "max_shared_questions": 1,
    "seed": 42
  }'
```

择优顺序（逐层最优后再优化下一层，保证确定性、与卷的排列无关）：

1. **各卷难度偏差总和**最小；
2. **跨卷重复题总数**（所有卷对共同题数求和）最少；
3. **各卷题号序列**按卷序依次最小（同时唯一确定卷的排列）。

### 多卷可行结果

结果在 `papers` 数组中逐卷返回（每卷字段与单卷结果一致），并附跨卷统计：

```json
{
  "status": "feasible",
  "paper_count": 3,
  "max_shared_questions": 1,
  "papers": [
    {
      "selected_ids": [1, 4, 9, "/* … */"],
      "total_score": 150,
      "average_difficulty": 0.52,
      "difficulty_deviation": 0.0,
      "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
      "knowledge_point_counts": {"函数": 3, "导数": 2}
    }
  ],
  "total_difficulty_deviation": 0.04,
  "total_shared_questions": 1,
  "shared_question_pairs": [
    {"papers": [0, 1], "count": 1, "question_ids": [9]},
    {"papers": [0, 2], "count": 0, "question_ids": []},
    {"papers": [1, 2], "count": 0, "question_ids": []}
  ],
  "seed": 42
}
```

- `papers[i].paper`：第 `i` 卷的完整题目对象（卷序号从 0 起）。
- `total_difficulty_deviation`：各卷 `difficulty_deviation` 之和（第 1 择优目标）。
- `total_shared_questions`：所有卷对共同题数之和（第 2 择优目标），`shared_question_pairs` 给出每对卷的共同题号，每对的 `count` 均不超过 `max_shared_questions`。

### 多卷无解结果（单卷约束 vs 跨卷上限）

多卷无解时，`conflict_constraints` 用 `scope` 区分两类来源：

- `per_paper`：某一份卷自身的约束，`detail.paper` 标明卷序号（从 0 起）；
- `cross_paper`：跨卷共同题上限（`kind` 为 `shared_questions`，`detail.papers` 标明卷对）。

```json
{
  "status": "infeasible",
  "message": "不存在满足全部约束的平行卷组",
  "conflict_constraints": [
    {"kind": "type", "scope": "per_paper",
     "detail": {"paper": 1, "type": "essay", "count": 2}},
    {"kind": "shared_questions", "scope": "cross_paper",
     "detail": {"papers": [1, 2], "max_shared_questions": 0}}
  ]
}
```

上例表示：第 2 卷（`paper: 1`）的题型数量约束与卷 2/卷 3 的零重叠上限相互冲突（题库中大题不足）；放宽其中任一条（例如允许两卷共用 1 题，或调大题量）后即可组卷。

## 多候选卷组（`alternative_count` 1～5）

在任意单卷/多卷请求上增加 `alternative_count`（及可选 `min_question_changes`），即可在**同一冻结题库、同一组原始约束**下一次取回多个候选卷组。求解过程：

1. 第 1 个候选按既有择优规则（单卷：难度偏差 → 重复知识点 → 题号序列；多卷：难度偏差总和 → 跨卷重复题 → 题号序列）求出；
2. 此后每轮**只追加、从不放宽**两类差异约束后重新分层择优：
   - **逐卷差异**：新候选的第 `p` 卷与此前**每个**候选的第 `p` 卷至少有 `min_question_changes` 道题不同（对单卷任务即整张卷的差异）；
   - **禁止卷序重排**（多卷）：不存在任何卷排列能让新候选与旧候选逐卷重合——不能只把 A/B 卷换个顺序充当新候选；
3. 全部候选共用同一 `seed`、单线程确定性求解，候选顺序可复现；
4. 某一轮在全部原约束 + 累计差异约束下无解（或限时内无法判定）时立即停止，返回已找到的实际数量并置 `exhausted: true`，**不会为凑数放宽任何约束**。

### 请求示例（单卷任务取 3 个候选）

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考·3 个候选卷",
    "questions": [ /* ……题库快照…… */ ],
    "total_score": 150,
    "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
    "knowledge_coverage": {"函数": 2, "导数": 1},
    "difficulty_range": [0.45, 0.6],
    "target_difficulty": 0.52,
    "alternative_count": 3,
    "min_question_changes": 2,
    "seed": 42
  }'
```

多卷任务同理，与 `paper_count` / `max_shared_questions` 同时给出即可，例如
`"paper_count": 3, "max_shared_questions": 0, "alternative_count": 4, "min_question_changes": 3`：
返回的每个候选都是一个完整的 3 卷平行卷组，候选间 3 张对应卷两两至少差 3 道题。

### 多候选响应

单候选（缺省 `alternative_count: 1`）响应与旧版**完全一致**；多候选时结果放在 `alternatives` 数组中，按既有择优规则从 `rank: 1` 起排序，**每组都带原有全部统计字段**（单卷为 `selected_ids` / `average_difficulty` / `difficulty_deviation` / `type_counts` / `knowledge_point_counts` / `repeated_knowledge_points`；多卷另含 `papers` 与 `total_difficulty_deviation` / `total_shared_questions` / `shared_question_pairs`）：

```json
{
  "status": "feasible",
  "alternative_count": 3,
  "returned_count": 3,
  "min_question_changes": 2,
  "exhausted": false,
  "alternatives": [
    {
      "rank": 1,
      "selected_ids": [1, 4, 9, "/* … */"],
      "total_score": 150,
      "average_difficulty": 0.52,
      "difficulty_deviation": 0.0,
      "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
      "knowledge_point_counts": {"函数": 3, "导数": 2},
      "repeated_knowledge_points": 1,
      "paper": [ /* 完整题目对象 */ ]
    },
    {
      "rank": 2,
      "selected_ids": ["/* 与 rank 1 至少 2 题不同…… */"],
      "total_score": 150,
      "average_difficulty": 0.51,
      "difficulty_deviation": 0.01,
      "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
      "knowledge_point_counts": {"/* …… */": 0},
      "repeated_knowledge_points": 1,
      "paper": [ /* …… */ ]
    },
    { "rank": 3 }
  ],
  "seed": 42
}
```

多卷任务的多候选响应顶层另有 `paper_count` 与 `max_shared_questions`，每个候选元素形如：

```json
{
  "rank": 2,
  "papers": [
    {"selected_ids": ["/* … */"], "total_score": 150, "paper": [ /* …… */ ]},
    {"selected_ids": ["/* … */"], "total_score": 150, "paper": [ /* …… */ ]}
  ],
  "total_difficulty_deviation": 0.05,
  "total_shared_questions": 0,
  "shared_question_pairs": [
    {"papers": [0, 1], "count": 0, "question_ids": []}
  ]
}
```

字段说明：

- `alternative_count`：请求的候选数量；`returned_count`：实际返回数量；
- `exhausted`：候选是否已穷尽——`true` 表示在不放宽任何原约束的前提下无法再给出满足差异要求的候选（`returned_count < alternative_count` 时恒为 `true`）；
- `min_question_changes`：本次实际采用的逐卷最少差异题数（回显缺省值）；
- `alternatives[i].rank`：从 1 起的择优排名；排名仅依据既有择优规则，候选间的差异约束不参与排序。

> 多候选任务同样可以发起[定向改卷](#定向改卷post-apitaskstask_idrevise)：以排名第 1（择优最优）的候选为基准卷冻结；改卷任务始终只返回一个新卷组（差异最大化择优），不再产生多候选。

## 定向改卷（`POST /api/tasks/{task_id}/revise`）

在一份**已完成且可行**的组卷结果上按卷施加人工编辑：指定每份卷**必须保留**与**必须移除**的题号，服务在**联合模型中对全部卷重新求解**，得到满足编辑条件且尽量少改的新平行卷组。

- 源任务仅允许状态为 `done` 且结果为 `feasible`：任务不存在返回 `404`；未完成或不可行返回 `422`。源任务为多候选结果时，以 `rank: 1`（择优最优）候选的各卷作为改卷基准。
- 请求体逐卷给出编辑（卷序号 `paper` 从 0 起）：`retain_ids`（必须选入）、`remove_ids`（不得选入）。
  - 两者题号重叠、引用题库中不存在的题号、卷序号越界、同一卷重复指定，均返回 `422`。
- 创建后返回一个**新的异步任务**：源任务请求、题库快照与编辑条件一并冻结，**源任务保持不变**；新任务沿用源任务的 `seed`，也可再次作为源任务继续改卷。
- 求解顺序在原有三层择优之前新增第 0 层：

  1. **各卷保留源卷原题的总数最大化**（先满足全部原始约束与人工编辑，再尽量少改）；
  2. 各卷难度偏差总和（单卷为自身偏差）最小；
  3. 跨卷重复题总数（单卷为重复知识点数）最少；
  4. 各卷题号序列最小。

```bash
curl -X POST http://localhost:8000/api/tasks/42/revise \
  -H "Content-Type: application/json" \
  -d '{
    "name": "A/B 平行卷·人工微调",
    "edits": [
      {"paper": 0, "retain_ids": [9],            "remove_ids": [12, 13]},
      {"paper": 1, "retain_ids": [4, 7],         "remove_ids": []}
    ]
  }'
# -> {"task_id": 43, "status": "pending"}
```

字段说明：

- `name`：新任务名称，缺省沿用源任务名称。
- `edits`：非空数组，每份卷至多一条；未出现的卷序表示该卷无人工编辑条件（仍参与联合重求解与保留最大化）。

新任务通过 `GET /api/tasks/43` 查询，顶层与 `result` 均带 `source_task_id`（上例为 `42`）；可行时逐卷给出与源卷的差异：

- `retained_ids`：新卷选入、且源卷也有的题号（保留的原题）；
- `added_ids`：源卷没有、新卷新增的题号；
- `removed_ids`：源卷有、新卷被移走的题号（按题库规范顺序输出）。

多卷时差异位于 `papers[i]` 内：

```json
{
  "status": "feasible",
  "source_task_id": 42,
  "paper_count": 2,
  "papers": [
    {
      "selected_ids": [1, 4, 8, 9, "/* … */"],
      "retained_ids": [1, 4, 9],
      "added_ids": [8],
      "removed_ids": [12, 13],
      "total_score": 150
    },
    {
      "selected_ids": [4, 7, "/* … */"],
      "retained_ids": [7],
      "added_ids": [4],
      "removed_ids": ["/* … */"],
      "total_score": 150
    }
  ],
  "seed": 42
}
```

单卷改卷时差异字段与 `source_task_id` 直接位于结果顶层（不返回 `papers` 数组）：

```json
{
  "status": "feasible",
  "source_task_id": 7,
  "selected_ids": [1, 2, 5],
  "retained_ids": [1, 5],
  "added_ids": [2],
  "removed_ids": [3]
}
```

### 定向改卷无解

编辑条件与原有约束冲突时，`conflict_constraints` 同样返回逐项删减验证的删除极小冲突集，其中人工编辑条件为：

- `kind: "question_retain"`（必须保留）/ `"question_remove"`（必须移除）；
- `scope: "per_paper"`，`detail.paper` 为卷序号、`detail.question_id` 为题号、`detail.edit` 为“保留”/“移除”，可直接定位到“哪份卷的哪道题”。

```json
{
  "status": "infeasible",
  "source_task_id": 42,
  "message": "不存在满足全部约束及人工编辑条件的平行卷组",
  "conflict_constraints": [
    {"kind": "type", "scope": "per_paper",
     "detail": {"paper": 0, "type": "essay", "count": 4}},
    {"kind": "question_retain", "scope": "per_paper",
     "detail": {"paper": 0, "question_id": 21, "edit": "保留"}},
    {"kind": "shared_questions", "scope": "cross_paper",
     "detail": {"papers": [0, 1], "max_shared_questions": 0}}
  ]
}
```

## 可行结果

```json
{
  "status": "feasible",
  "selected_ids": [1, 2, 5],
  "total_score": 20,
  "average_difficulty": 0.525,
  "difficulty_deviation": 0.025,
  "type_counts": {"single_choice": 2, "essay": 1},
  "knowledge_point_counts": {"集合": 1, "函数": 1, "数列": 1},
  "repeated_knowledge_points": 0,
  "paper": [ /* 完整题目对象 */ ]
}
```

## 无解结果（最小冲突约束集）

当约束无解时，`status` 为 `infeasible`，`conflict_constraints` 给出经**逐项删减验证**的删除极小冲突集——即其中任何一条被放宽/删除后，其余约束即可满足：

```json
{
  "status": "infeasible",
  "message": "不存在满足全部约束的试卷",
  "conflict_constraints": [
    {"kind": "total_score",      "detail": {"total_score": 20}},
    {"kind": "difficulty_range", "detail": {"range": [0.9, 1.0]}}
  ]
}
```

单卷冲突项的 `kind` 取值：`total_score` / `type` / `coverage` / `difficulty_range` / `mutex`；多卷时另有跨卷项 `shared_questions`（`scope: "cross_paper"`），单卷项带 `scope: "per_paper"`。定向改卷任务的人工编辑条件为 `question_retain` / `question_remove`（均带 `scope: "per_paper"`，可定位到 `detail.paper` 与 `detail.question_id`）。

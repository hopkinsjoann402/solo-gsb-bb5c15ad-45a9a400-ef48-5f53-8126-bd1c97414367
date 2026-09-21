"""Exam-paper composition solver built on OR-Tools CP-SAT.

支持一次请求生成 1～5 份平行卷（paper_count）。多卷时所有卷建在**同一个联合
CP-SAT 模型**中：每份卷各自满足总分、题型、知识点覆盖、难度区间、互斥题组约束，
任意两卷的共同题数不超过 max_shared_questions（缺省 0，即平行卷互不重复）。

择优规则（字典序，逐层最优后固定再优化下一层）：
  单卷：
    1. 难度偏差 |平均难度 - 目标难度| 最小；
    2. 重复知识点数（命中 >=2 道题的知识点个数）最少；
    3. 题目编号序列最小（规范化排序后的选择编码，分块最小化，见 _optimize）。
  多卷：
    1. 各卷难度偏差之和最小；
    2. 跨卷重复题总数（所有卷对共同题数之和）最少，且不超过上限；
    3. 各卷题号序列最小（各卷按同一选择编码、按卷序逐卷分块最小化；
       同时完成卷间对称破缺，结果与卷的排列无关、可复现）。

无解时通过“逐项删减验证”求删除极小的冲突约束集（deletion-based MUS）：
逐条尝试移除约束，若移除后仍无解则永久删除，最终保留的即为最小冲突集。
冲突项通过 scope 区分：per_paper 为单卷约束（detail.paper 为从 0 起的卷序号），
cross_paper 为跨卷重叠上限（kind=shared_questions）。

定向改卷（revise）：在冻结的源请求之上，按卷追加人工编辑条件
（question_retain：该卷必须选入；question_remove：该卷不得选入）。
择优在原三层之前新增第 0 层“各卷保留源卷原题总数最大化”，无解时人工编辑条件
同样可进入最小冲突集，其 detail 可定位到卷与题号（paper + question_id）。

多候选（alternative_count 为 2~5）：在同一冻结题库与原约束下迭代求解，按既有
择优规则逐组返回候选。求得一个候选后，后续模型对**此前每个**候选追加差异硬约束：
每份“对应卷”（按既有卷序，卷序由第 3 层对称破缺唯一确定）与之至少有
min_question_changes 道题不同（对称差下界，缺省 1）；多卷时再枚举所有非恒等卷
排列并要求至少一份卷不同于该排列，避免仅交换卷序制造新候选。差异约束只增不松，
迭代被证明无解时返回实际数量与 exhausted=true。整个过程同种子、单线程，结果可复现。
"""
from __future__ import annotations

from itertools import permutations
from typing import Any, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

SCALE = 1000  # 难度浮点 -> 整数的放大倍数
CHUNK_BITS = 20  # 编号序列分块编码的块大小，2^20 远在 int64 安全范围内
STAGE_TIME_LIMIT = 20.0  # 每个择优阶段的求解上限（秒）
MUS_TIME_LIMIT = 15.0  # 冲突集逐项验证的单次求解上限（秒）

MAX_PAPER_COUNT = 5  # 平行卷数量上限（下限 2，缺省/1 为单卷）

# 定向改卷的人工编辑条件（可参与冲突集删减验证，定位到卷与题号）
EDIT_KINDS = ("question_retain", "question_remove")


# --------------------------------------------------------------------------- #
# 数据整理
# --------------------------------------------------------------------------- #
def _id_sort_key(qid: Any):
    """编号规范化排序：整数型编号在前按数值排，其余按字符串排。"""
    if isinstance(qid, bool):  # guard: bool 是 int 的子类
        return (1, str(qid))
    if isinstance(qid, int):
        return (0, qid)
    s = str(qid)
    if s.lstrip("-").isdigit():
        return (0, int(s))
    return (1, s)


def _prepare(
    req: Dict[str, Any],
    source_selection: Optional[List[List[Any]]] = None,
    edits: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    questions = sorted(req["questions"], key=lambda q: _id_sort_key(q["id"]))
    ids = [q["id"] for q in questions]
    id_index = {qid: i for i, qid in enumerate(ids)}

    type_members: Dict[str, List[int]] = {}
    kp_members: Dict[str, List[int]] = {}
    for i, q in enumerate(questions):
        type_members.setdefault(q["type"], []).append(i)
        for kp in set(q["knowledge_points"]):
            kp_members.setdefault(kp, []).append(i)

    lo, hi = req["difficulty_range"]
    target = req["target_difficulty"]
    if target is None:
        target = (lo + hi) / 2.0

    # 只保留非平凡的互斥题组（成员均存在且 >=2）
    mutex_groups = [g for g in req.get("mutex_groups", []) if len(set(g)) >= 2]

    # 平行卷：缺省（或显式 1）为单卷，保持响应兼容；多卷允许 2~5
    paper_count = int(req.get("paper_count") or 1)
    is_multi = paper_count >= 2
    # 多卷时缺省共同题上限为 0（平行卷互不重复）
    max_shared = req.get("max_shared_questions")
    if max_shared is None:
        max_shared = 0 if is_multi else None
    else:
        max_shared = int(max_shared)

    # 多候选：缺省（或显式 1）为单候选，响应与旧版完全一致
    alternative_count = int(req.get("alternative_count") or 1)
    if alternative_count < 2:
        alternative_count = 1
    # 多候选时逐对应卷至少不同的题目数，缺省 1
    min_changes = req.get("min_question_changes")
    min_changes = int(min_changes) if min_changes is not None else 1

    d: Dict[str, Any] = {
        "questions": questions,
        "ids": ids,
        "id_index": id_index,
        "type_members": type_members,
        "kp_members": kp_members,
        "T": int(req["total_score"]),
        "type_counts": dict(req.get("type_counts", {})),
        "coverage": dict(req.get("knowledge_coverage", {})),
        "diff_lo": float(lo),
        "diff_hi": float(hi),
        "target": float(target),
        "mutex_groups": mutex_groups,
        "seed": int(req.get("seed", 42)),
        "P": paper_count,
        "is_multi": is_multi,
        "max_shared": max_shared,
        "A": alternative_count,
        "min_changes": min_changes,
    }
    # 改卷任务始终在单候选模式下重求解（多候选源任务在接口层拒绝改卷）
    if source_selection is not None:
        d["A"] = 1
    d["specs"] = _build_constraint_specs(d)

    # 定向改卷：冻结源卷选择（题号 -> 题库下标集合）与逐卷人工编辑条件
    d["revise"] = source_selection is not None
    if d["revise"]:
        source_sets: List[set] = []
        for p in range(paper_count):
            ids_p = source_selection[p] if p < len(source_selection) else []
            source_sets.append({id_index[qid] for qid in ids_p if qid in id_index})
        d["source_sel"] = source_sets
        if edits:
            for e in edits:
                p = int(e["paper"])
                for qid in e.get("retain_ids", []):
                    d["specs"].append(
                        {
                            "kind": "question_retain",
                            "paper": p,
                            "detail": {"question_id": qid},
                        }
                    )
                for qid in e.get("remove_ids", []):
                    d["specs"].append(
                        {
                            "kind": "question_remove",
                            "paper": p,
                            "detail": {"question_id": qid},
                        }
                    )
    return d


# --------------------------------------------------------------------------- #
# 约束清单
# --------------------------------------------------------------------------- #
def _build_constraint_specs(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    """将用户请求展开为带编号、保留原始顺序的约束项。

    每份卷各自挂一组单卷约束（spec["paper"] 为卷序号）；多卷时在其后追加
    跨卷重叠上限（kind=shared_questions，按卷对展开）。
    """
    P = d["P"]
    specs: List[Dict[str, Any]] = []
    for p in range(P):
        specs.append(
            {
                "kind": "total_score",
                "paper": p,
                "detail": {"total_score": d["T"]},
            }
        )
        for t, c in d["type_counts"].items():
            specs.append(
                {"kind": "type", "paper": p, "detail": {"type": t, "count": int(c)}}
            )
        for kp, c in d["coverage"].items():
            specs.append(
                {
                    "kind": "coverage",
                    "paper": p,
                    "detail": {"knowledge_point": kp, "min_count": int(c)},
                }
            )
        specs.append(
            {
                "kind": "difficulty_range",
                "paper": p,
                "detail": {"range": [d["diff_lo"], d["diff_hi"]]},
            }
        )
        for g in d["mutex_groups"]:
            specs.append({"kind": "mutex", "paper": p, "detail": {"group": list(g)}})

    if d["is_multi"]:
        for a in range(P):
            for b in range(a + 1, P):
                specs.append(
                    {
                        "kind": "shared_questions",
                        "detail": {
                            "papers": [a, b],
                            "max_shared_questions": d["max_shared"],
                        },
                    }
                )
    return specs


# --------------------------------------------------------------------------- #
# 建模
# --------------------------------------------------------------------------- #
def _build_model(
    d: Dict[str, Any],
    active: set,
    priors: Optional[List[List[List[bool]]]] = None,
) -> Tuple[cp_model.CpModel, List[List[cp_model.IntVar]], Dict[str, Any]]:
    """联合模型：x[p][i] 表示第 p 卷是否选第 i 题。

    priors 为多候选迭代中此前已求得候选的选择向量；非空时追加候选间差异硬约束
    （逐对应卷至少不同 min_changes 题，且多卷时不得为已有候选的卷序排列）。
    """
    m = cp_model.CpModel()
    n = len(d["questions"])
    P = d["P"]
    x = [[m.NewBoolVar(f"x_{p}_{i}") for i in range(n)] for p in range(P)]

    # 放大后的难度加权和 SD_p = Σ round(diff*1000) * score * x，平均难度 = SD/(1000*T)
    target_scaled = int(round(d["target"] * SCALE * d["T"]))
    max_dev = max(
        abs(int(round(d["diff_hi"] * SCALE * d["T"])) - target_scaled),
        abs(target_scaled - int(round(d["diff_lo"] * SCALE * d["T"]))),
        SCALE * d["T"],
    )

    score_sums: List[cp_model.LinearExpr] = []
    sds: List[cp_model.LinearExpr] = []
    devs: List[cp_model.IntVar] = []
    for p in range(P):
        score_sums.append(
            sum(int(q["score"]) * x[p][i] for i, q in enumerate(d["questions"]))
        )
        sd_terms = [
            int(round(q["difficulty"] * SCALE)) * int(q["score"]) * x[p][i]
            for i, q in enumerate(d["questions"])
        ]
        sds.append(sum(sd_terms) if sd_terms else 0)

        dev = m.NewIntVar(0, max_dev, f"dev_{p}")
        m.Add(dev >= sds[p] - target_scaled)
        m.Add(dev >= target_scaled - sds[p])
        devs.append(dev)

    # 跨卷共同题：y[(a,b)][i] = 1  iff  两卷同时选第 i 题（定义为完整恒等式，
    # 与 active 无关；仅当上界约束被停用时，求解器可自由忽略）
    share_sums: Dict[Tuple[int, int], cp_model.LinearExpr] = {}
    for a in range(P):
        for b in range(a + 1, P):
            ys = [m.NewBoolVar(f"y_{a}_{b}_{i}") for i in range(n)]
            for i in range(n):
                m.AddBoolAnd([x[a][i], x[b][i]]).OnlyEnforceIf(ys[i])
                m.AddBoolOr([x[a][i].Not(), x[b][i].Not()]).OnlyEnforceIf(ys[i].Not())
            share_sums[(a, b)] = sum(ys)

    total_dev = sum(devs)
    shared_total = (
        sum(share_sums.values()) if share_sums else 0
    )
    # 定向改卷第 0 择优目标：各卷保留源卷原题的总数（Σ_p Σ_{i∈源卷p} x[p][i]）
    retained_total = 0
    if d.get("revise"):
        retained_total = sum(
            sum(x[p][i] for i in d["source_sel"][p]) for p in range(P)
        )
    extra = {
        "score_sum": score_sums,
        "sd": sds,
        "dev": devs,
        "total_dev": total_dev,
        "shared_total": shared_total,
        "share_sums": share_sums,
        "retained_total": retained_total,
    }

    specs = d["specs"]
    for cid in active:
        spec = specs[cid]
        kind = spec["kind"]

        if kind == "shared_questions":
            a, b = spec["detail"]["papers"]
            cap = spec["detail"]["max_shared_questions"]
            m.Add(share_sums[(a, b)] <= cap)
            continue

        p = spec["paper"]
        xp = x[p]

        if kind == "total_score":
            m.Add(score_sums[p] == d["T"])

        elif kind == "type":
            t = spec["detail"]["type"]
            c = spec["detail"]["count"]
            members = d["type_members"].get(t, [])
            m.Add(sum(xp[i] for i in members) == c)

        elif kind == "coverage":
            kp = spec["detail"]["knowledge_point"]
            c = spec["detail"]["min_count"]
            members = d["kp_members"].get(kp, [])
            if not members:  # 知识点在题库快照中没有任何题目
                z = m.NewBoolVar(f"impossible_cov_p{p}_{cid}")
                m.Add(z == 0)
                m.Add(z == 1)
            else:
                m.Add(sum(xp[i] for i in members) >= c)

        elif kind == "difficulty_range":
            lb = int(round(d["diff_lo"] * SCALE * d["T"]))
            ub = int(round(d["diff_hi"] * SCALE * d["T"]))
            m.Add(sds[p] >= lb)
            m.Add(sds[p] <= ub)

        elif kind == "mutex":
            group = spec["detail"]["group"]
            m.Add(sum(xp[d["id_index"][qid]] for qid in group) <= 1)

        elif kind == "question_retain":
            # 人工编辑：该卷必须选入此题
            m.Add(xp[d["id_index"][spec["detail"]["question_id"]]] == 1)

        elif kind == "question_remove":
            # 人工编辑：该卷不得选入此题
            m.Add(xp[d["id_index"][spec["detail"]["question_id"]]] == 0)

    # 多候选：相对此前每个候选的差异硬约束（不放宽任何原有约束）
    if priors:
        _add_diversity_constraints(m, d, x, priors)

    return m, x, extra


def _add_diversity_constraints(
    m: cp_model.CpModel,
    d: Dict[str, Any],
    x: List[List[cp_model.IntVar]],
    priors: List[List[List[bool]]],
) -> None:
    """对每个此前候选 q 追加两类约束：

    1. 逐对应卷对称差下界：每份卷 p 的选择与候选 q 的卷 p 至少有
       min_changes 道题不同——即 Σ_i (x[p][i] XOR q[p][i]) >= min_changes。
       旧有新无 Σ_{i∈q}(1-x) 与旧无新有 Σ_{i∉q} x 均可直接线性表达。
    2. 卷序排列排斥（仅多卷，防止仅交换卷序制造新候选）：对每个非恒等排列 π
       直接施加 Σ_p (x[p] 与 q[π(p)] 的对称差) >= 1，即至少一份卷不同于
       q 按 π 重排后的卷。恒等排列被第 1 类约束（min_changes>=1）天然排除。
    """
    n = len(d["questions"])
    P = d["P"]
    min_changes = d["min_changes"]

    for q in priors:
        # 第 1 类：逐对应卷至少 min_changes 道题不同
        # 对称差 = 旧有新无（Σ_{i∈q}(1-x)）+ 旧无新有（Σ_{i∉q} x）
        for p in range(P):
            removed = sum((1 - x[p][i]) for i in range(n) if q[p][i])
            gained = sum(x[p][i] for i in range(n) if not q[p][i])
            m.Add(removed + gained >= min_changes)

        if P < 2:
            continue

        # 第 2 类：不得等于该候选的任何非恒等卷序排列。
        # 对每个排列 π 直接要求 Σ_p diff(当前卷 p, 旧卷 π(p)) >= 1，
        # 即至少一份卷不同于该排列（恒等排列已被第 1 类约束排除）。
        for perm in permutations(range(P)):
            if perm == tuple(range(P)):
                continue
            total_diff = []
            for p in range(P):
                removed = sum(
                    (1 - x[p][i]) for i in range(n) if q[perm[p]][i]
                )
                gained = sum(x[p][i] for i in range(n) if not q[perm[p]][i])
                total_diff.append(removed + gained)
            m.Add(sum(total_diff) >= 1)


def _solver(d: Dict[str, Any]) -> cp_model.CpSolver:
    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 1  # 单线程保证确定性
    s.parameters.random_seed = d["seed"]
    return s


def _solve(
    d: Dict[str, Any],
    active: set,
    *,
    time_limit: float,
    priors: Optional[List[List[List[bool]]]] = None,
) -> Tuple[str, Optional[List[List[bool]]]]:
    """在给定约束子集上求解，返回 (状态, 各卷选择向量)。状态 feasible/infeasible/unknown。"""
    m, x, _ = _build_model(d, active, priors)
    s = _solver(d)
    s.parameters.max_time_in_seconds = time_limit
    status = s.Solve(m)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        n = len(d["questions"])
        P = d["P"]
        chosen = [[bool(s.Value(x[p][i])) for i in range(n)] for p in range(P)]
        return "feasible", chosen
    if status == cp_model.INFEASIBLE:
        return "infeasible", None
    return "unknown", None


# --------------------------------------------------------------------------- #
# 快速无解预判（仅用于立即给出结论；冲突集仍以求解器逐项验证为准）
# --------------------------------------------------------------------------- #
def _quick_infeasible(d: Dict[str, Any], active: set) -> Optional[str]:
    """仅做单卷充分性预判（与具体哪些约束被停用无关地保持可靠）。

    跨卷重叠上限引发的无解统一交给求解器判定，避免在 MUS 删减过程中
    误用“总量不足”这类与被停用约束相关的启发式结论。
    """
    specs = d["specs"]
    for cid in active:
        spec = specs[cid]
        kind, detail = spec["kind"], spec["detail"]
        if kind == "shared_questions":
            continue
        p = spec["paper"]
        prefix = f"第 {p + 1} 卷：" if d["is_multi"] else ""
        if kind == "total_score":
            if sum(int(q["score"]) for q in d["questions"]) < d["T"]:
                return f"{prefix}题库总分不足，无法达到试卷总分"
        elif kind == "type":
            if len(d["type_members"].get(detail["type"], [])) < detail["count"]:
                return f"{prefix}题型 {detail['type']} 的题量不足"
        elif kind == "coverage":
            if len(d["kp_members"].get(detail["knowledge_point"], [])) < detail["min_count"]:
                return f"{prefix}知识点 {detail['knowledge_point']} 的题量不足"
    return None


# --------------------------------------------------------------------------- #
# 分层择优
# --------------------------------------------------------------------------- #
def _repeat_expr(
    m: cp_model.CpModel, d: Dict[str, Any], xp: List[cp_model.IntVar], tag: str
) -> cp_model.LinearExpr:
    """单卷重复知识点数：命中 >=2 道题的知识点个数。"""
    repeat_vars = []
    for kp, members in d["kp_members"].items():
        if len(members) < 2:
            continue
        r = m.NewBoolVar(f"repeat_{tag}_{kp}")
        cnt = sum(xp[i] for i in members)
        m.Add(cnt >= 2).OnlyEnforceIf(r)
        m.Add(cnt <= 1).OnlyEnforceIf(r.Not())
        repeat_vars.append(r)
    return sum(repeat_vars) if repeat_vars else 0


def _optimize(
    d: Dict[str, Any],
    priors: Optional[List[List[List[bool]]]] = None,
) -> Tuple[str, Optional[List[List[bool]]], Optional[str]]:
    """字典序分层择优，逐阶段固定已得到的最优值。"""
    n = len(d["questions"])
    P = d["P"]
    active = set(range(len(d["specs"])))
    revise = d.get("revise", False)

    # 定向改卷阶段 0：最大化各卷保留源卷原题的总数（先满足全部约束，再谈保留）
    best_retained: Optional[int] = None
    if revise:
        m, x, extra = _build_model(d, active, priors)
        m.Maximize(extra["retained_total"])
        s = _solver(d)
        s.parameters.max_time_in_seconds = STAGE_TIME_LIMIT
        status = s.Solve(m)
        if status == cp_model.INFEASIBLE:
            return "infeasible", None, None
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return "unknown", None, "求解器在规定时间内无法判定可行性"
        best_retained = int(s.Value(extra["retained_total"]))
        chosen = [[bool(s.Value(x[p][i])) for i in range(n)] for p in range(P)]

    # 阶段 1：各卷难度偏差（单卷即其自身偏差）最小
    m, x, extra = _build_model(d, active, priors)
    if revise:
        m.Add(extra["retained_total"] == best_retained)
    m.Minimize(extra["total_dev"])
    s = _solver(d)
    s.parameters.max_time_in_seconds = STAGE_TIME_LIMIT
    status = s.Solve(m)
    if status == cp_model.INFEASIBLE:
        return "infeasible", None, None
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return "unknown", None, "求解器在规定时间内无法判定可行性"
    best_dev = int(s.Value(extra["total_dev"]))
    chosen = [[bool(s.Value(x[p][i])) for i in range(n)] for p in range(P)]

    # 阶段 2：单卷最小化重复知识点数；多卷最小化跨卷重复题总数
    m, x, extra = _build_model(d, active, priors)
    if revise:
        m.Add(extra["retained_total"] == best_retained)
    m.Add(extra["total_dev"] == best_dev)
    if P == 1:
        objective_2 = _repeat_expr(m, d, x[0], "p0")
    else:
        objective_2 = extra["shared_total"]
    m.Minimize(objective_2)
    s = _solver(d)
    s.parameters.max_time_in_seconds = STAGE_TIME_LIMIT
    status = s.Solve(m)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        best_second = int(s.Value(objective_2))
        chosen = [[bool(s.Value(x[p][i])) for i in range(n)] for p in range(P)]
    else:
        # 退化：直接使用阶段 1 的解
        best_second = 0

    # 阶段 3：题目编号序列最小（沿用单卷既定编码，保证单卷结果不变）。
    # 规范化排序后的 0/1 选择向量按 2^q（q 为规范题号序）分块，逐块最小化；
    # 多卷时分块严格落在单卷内部（不跨卷边界），按 (卷序, 块序) 逐块最小化并
    # 固定此前各块。由此卷间对称被唯一破缺（各卷按同一确定性顺序择优），结果可复现。
    n_chunks_paper = (n + CHUNK_BITS - 1) // CHUNK_BITS
    fixed_chunks: List[Tuple[int, int, int]] = []  # (卷 p, 块 k, 块值)
    total_steps = P * n_chunks_paper
    for step in range(total_steps):
        p, k = divmod(step, n_chunks_paper)
        m, x, extra = _build_model(d, active, priors)
        if revise:
            m.Add(extra["retained_total"] == best_retained)
        m.Add(extra["total_dev"] == best_dev)
        if P == 1:
            m.Add(_repeat_expr(m, d, x[0], "p0") == best_second)
        else:
            m.Add(extra["shared_total"] == best_second)

        for fp, fk, fv in fixed_chunks:
            flo = fk * CHUNK_BITS
            fhi = min((fk + 1) * CHUNK_BITS, n)
            m.Add(sum(x[fp][flo + q] * (1 << q) for q in range(fhi - flo)) == fv)

        lo, hi = k * CHUNK_BITS, min((k + 1) * CHUNK_BITS, n)
        e_k = sum(x[p][lo + q] * (1 << q) for q in range(hi - lo))
        m.Minimize(e_k)

        s = _solver(d)
        s.parameters.max_time_in_seconds = STAGE_TIME_LIMIT
        status = s.Solve(m)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break  # 保留上一阶段已得的最优解
        fixed_chunks.append((p, k, int(s.Value(e_k))))
        chosen = [[bool(s.Value(x[pp][i])) for i in range(n)] for pp in range(P)]

    return "feasible", chosen, None


# --------------------------------------------------------------------------- #
# 最小冲突集（逐项删减验证）
# --------------------------------------------------------------------------- #
def _minimal_conflicts(d: Dict[str, Any]) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """Deletion-based MUS：逐条尝试移除，移除后仍无解则永久删除。

    最终保留下来的每条约束都是“必要”的（删去它其余约束即可满足），
    即关于删除操作极小的冲突约束集。

    单次判定若超时（unknown），保守视为“可能可行”，即不把该约束计入冲突集，
    避免输出未经严格证明的冲突项；此时整体结果标记为 incomplete。
    """
    active = set(range(len(d["specs"])))

    # 以完整约束集无解为前提
    note = "ok"
    reason = _quick_infeasible(d, active)
    if reason is None:
        status, _ = _solve(d, active, time_limit=MUS_TIME_LIMIT)
        if status == "unknown":
            return None, "求解器无法在限定时间内判定约束是否无解，未能给出冲突集"
        if status != "infeasible":
            return [], "ok"  # 实际上可行，不存在冲突集

    kept: List[int] = []
    for cid in range(len(d["specs"])):
        if cid not in active:
            continue
        trial = active - {cid}
        reason = _quick_infeasible(d, trial)
        if reason is None:
            status, _ = _solve(d, trial, time_limit=MUS_TIME_LIMIT)
            infeasible = status == "infeasible"
            if status == "unknown":
                note = "incomplete"  # 无法证明该约束必要，保守跳过
        else:
            infeasible = True  # 快速判定已证明移除后仍无解
        if infeasible:
            active = trial  # 该约束与冲突无关，永久删除
        else:
            kept.append(cid)  # 删去它即可行 -> 属于最小冲突集

    return [d["specs"][cid] for cid in kept], note


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def solve_exam(req: Dict[str, Any]) -> Dict[str, Any]:
    return _solve_prepared(_prepare(req))


def solve_revise(payload: Dict[str, Any]) -> Dict[str, Any]:
    """定向改卷：payload 为创建改卷任务时冻结的完整数据（源请求 + 编辑条件）。

    payload["source_request"] 为源组卷请求，payload["source_selection"] 为源任务
    各卷选中的题号（逐卷冻结），payload["edits"] 为逐卷人工编辑条件。
    """
    base_req = payload["source_request"]
    d = _prepare(
        base_req,
        source_selection=payload.get("source_selection"),
        edits=payload.get("edits"),
    )
    result = _solve_prepared(d)
    _attach_revision(d, payload, result)
    return result


def _solve_prepared(d: Dict[str, Any]) -> Dict[str, Any]:
    active = set(range(len(d["specs"])))

    # 快速无解预判
    reason = _quick_infeasible(d, active)
    if reason is not None:
        conflicts, note = _minimal_conflicts(d)
        result = _infeasible_result(d, conflicts, note, reason)
        return _with_alternative_meta(d, result)

    if d["A"] >= 2:
        return _solve_alternatives(d)

    status, chosen, note = _optimize(d)
    if status == "feasible":
        return _feasible_result(d, chosen)
    if status == "unknown":
        return {"status": "error", "error": note or "求解超时，无法判定可行性"}

    # 无解：逐项删减验证最小冲突集
    conflicts, note = _minimal_conflicts(d)
    return _infeasible_result(d, conflicts, note)


def _solve_alternatives(d: Dict[str, Any]) -> Dict[str, Any]:
    """多候选迭代：差异约束只增不松，按既有择优规则依次求得候选卷组。

    每个候选都在“原约束 ∪ 对全部此前候选的差异约束”下重新走完整分层择优，
    因此排名与求解顺序一致（后续候选不可能在更紧的可行域中反超此前最优值层级）。
    """
    requested = d["A"]
    priors: List[List[List[bool]]] = []
    groups: List[Dict[str, Any]] = []
    exhausted = False

    for _ in range(requested):
        status, chosen, note = _optimize(d, priors)
        if status == "feasible":
            groups.append(_feasible_result(d, chosen))
            priors.append(chosen)
            continue

        if status == "unknown":
            # 超时无法证明后续是否还有候选：不谎报 exhausted，返回已求得的候选
            result = _alternatives_result(d, groups, exhausted=False)
            result["message"] = note or "后续候选求解超时，未能继续枚举"
            return result

        # 被证明无解：仅当发生在第 0 个候选时属于“原约束本身无解”，
        # 给出最小冲突集；否则只是差异约束下候选耗尽。
        if not priors:
            conflicts, cnote = _minimal_conflicts(d)
            infeasible = _infeasible_result(d, conflicts, cnote)
            return _with_alternative_meta(d, infeasible)
        exhausted = True
        break

    return _alternatives_result(d, groups, exhausted=exhausted)


def _with_alternative_meta(d: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """多候选请求在“一个候选都求不出”的无解/错误结果上附带请求参数，便于客户端核对。"""
    if d["A"] >= 2:
        result["alternative_count"] = d["A"]
        result["returned_alternatives"] = 0
        result["exhausted"] = True
        result["min_question_changes"] = d["min_changes"]
    return result


def _alternatives_result(
    d: Dict[str, Any], groups: List[Dict[str, Any]], *, exhausted: bool
) -> Dict[str, Any]:
    """构造多候选响应：每组沿用其原有统计并附排名，响应信封与单候选结构互不干扰。"""
    candidates: List[Dict[str, Any]] = []
    for rank, g in enumerate(groups):
        item = {k: v for k, v in g.items() if k not in ("status", "seed")}
        ordered = {"rank": rank}
        ordered.update(item)
        candidates.append(ordered)

    return {
        "status": "feasible",
        "alternative_count": d["A"],
        "returned_alternatives": len(candidates),
        "exhausted": exhausted,
        "min_question_changes": d["min_changes"],
        "alternatives": candidates,
        "seed": d["seed"],
    }


def _attach_revision(
    d: Dict[str, Any], payload: Dict[str, Any], result: Dict[str, Any]
) -> None:
    """为改卷结果补充 source_task_id 及逐卷 retained/added/removed_ids（原地写入）。"""
    result["source_task_id"] = payload["source_task_id"]
    if result.get("status") != "feasible":
        return

    def diff(selected_ids: List[Any], p: int) -> Tuple[List[Any], List[Any], List[Any]]:
        # 以源任务冻结的源卷题号集合为准，按新卷题库规范化顺序输出
        source_ids = set(
            payload["source_selection"][p]
            if p < len(payload["source_selection"])
            else []
        )
        selected = set(selected_ids)
        retained = [qid for qid in selected_ids if qid in source_ids]
        added = [qid for qid in selected_ids if qid not in source_ids]
        removed = [
            qid
            for qid in d["ids"]
            if qid in source_ids and qid not in selected
        ]
        return retained, added, removed

    if not d["is_multi"]:
        retained, added, removed = diff(result["selected_ids"], 0)
        result["retained_ids"] = retained
        result["added_ids"] = added
        result["removed_ids"] = removed
        return

    for p, paper in enumerate(result["papers"]):
        retained, added, removed = diff(paper["selected_ids"], p)
        paper["retained_ids"] = retained
        paper["added_ids"] = added
        paper["removed_ids"] = removed


def _infeasible_result(
    d: Dict[str, Any],
    conflicts: Optional[List[Dict[str, Any]]],
    note: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    if conflicts is None:
        return {
            "status": "error",
            "error": note or "无法在限定时间内验证冲突约束集",
        }

    def render(c: Dict[str, Any]) -> Dict[str, Any]:
        if c["kind"] == "shared_questions":
            return {
                "kind": "shared_questions",
                "scope": "cross_paper",
                "detail": c["detail"],
            }
        if c["kind"] in EDIT_KINDS:
            # 人工编辑条件：始终定位到卷与题号（单卷改卷同样带 paper 以便定位）
            label = "保留" if c["kind"] == "question_retain" else "移除"
            return {
                "kind": c["kind"],
                "scope": "per_paper",
                "detail": {
                    "paper": c["paper"],
                    "question_id": c["detail"]["question_id"],
                    "edit": label,
                },
            }
        if d["is_multi"]:
            # 单卷约束标注卷序号（从 0 起），与跨卷重叠上限区分
            return {
                "kind": c["kind"],
                "scope": "per_paper",
                "detail": {"paper": c["paper"], **c["detail"]},
            }
        # 单卷模式：冲突项结构与旧版完全一致（不新增 scope/paper 字段）
        return {"kind": c["kind"], "detail": c["detail"]}

    default_msg = (
        "不存在满足全部约束的平行卷组" if d["is_multi"] else "不存在满足全部约束的试卷"
    )
    if d.get("revise"):
        default_msg = (
            "不存在满足全部约束及人工编辑条件的平行卷组"
            if d["is_multi"]
            else "不存在满足全部约束及人工编辑条件的试卷"
        )
    result: Dict[str, Any] = {
        "status": "infeasible",
        "message": reason or default_msg,
        "conflict_constraints": [render(c) for c in conflicts],
    }
    if note == "incomplete":
        result["message"] += "（部分约束判定超时，冲突集可能不完整）"
    return result


def _paper_result(d: Dict[str, Any], chosen: List[bool]) -> Dict[str, Any]:
    questions = d["questions"]
    selected = [questions[i] for i, ok in enumerate(chosen) if ok]
    selected_ids = [q["id"] for q in selected]

    total = sum(int(q["score"]) for q in selected)
    sd = sum(int(round(q["difficulty"] * SCALE)) * int(q["score"]) for q in selected)
    avg = sd / (SCALE * total) if total else 0.0
    deviation = abs(avg - d["target"])

    type_counts: Dict[str, int] = {}
    for q in selected:
        type_counts[q["type"]] = type_counts.get(q["type"], 0) + 1

    kp_counts: Dict[str, int] = {}
    for q in selected:
        for kp in q["knowledge_points"]:
            kp_counts[kp] = kp_counts.get(kp, 0) + 1
    repeated = sum(1 for c in kp_counts.values() if c >= 2)

    return {
        "paper": selected,
        "selected_ids": selected_ids,
        "total_score": total,
        "average_difficulty": round(avg, 6),
        "difficulty_deviation": round(deviation, 6),
        "type_counts": type_counts,
        "knowledge_point_counts": kp_counts,
        "repeated_knowledge_points": repeated,
    }


def _feasible_result(d: Dict[str, Any], chosen: List[List[bool]]) -> Dict[str, Any]:
    if not d["is_multi"]:
        # 单卷：保持既有响应结构不变
        result = _paper_result(d, chosen[0])
        result["status"] = "feasible"
        result["seed"] = d["seed"]
        return result

    P = d["P"]
    papers = [_paper_result(d, chosen[p]) for p in range(P)]

    # 跨卷共同题（按题库规范化顺序），逐卷对统计
    selected_sets = [
        {i for i, ok in enumerate(chosen[p]) if ok} for p in range(P)
    ]
    shared_pairs = []
    shared_total = 0
    for a in range(P):
        for b in range(a + 1, P):
            common_idx = sorted(selected_sets[a] & selected_sets[b])
            count = len(common_idx)
            shared_total += count
            shared_pairs.append(
                {
                    "papers": [a, b],
                    "count": count,
                    "question_ids": [d["ids"][i] for i in common_idx],
                }
            )

    return {
        "status": "feasible",
        "paper_count": P,
        "max_shared_questions": d["max_shared"],
        "papers": papers,
        "total_difficulty_deviation": round(
            sum(p["difficulty_deviation"] for p in papers), 6
        ),
        "total_shared_questions": shared_total,
        "shared_question_pairs": shared_pairs,
        "seed": d["seed"],
    }

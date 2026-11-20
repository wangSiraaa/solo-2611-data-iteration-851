"""End-to-end stage restoration and dresser-planning demonstration.

Run:  python3 -m solo.stagedemo

It builds *real* SOLO bodies for the blocking sheets described in the
task, keeps every version in the byte-faithful :class:`RevisionLibrary`,
restores the occupancy score of each version, and lets the costume
department's own casting/worktime/shift data cross-check the dresser
headcount.  Everything is written under ``artifacts/stage/``.

Scenarios
---------
1. the worked example (six one-minute columns):
   v0  P=A...B.  Q=C...D.   -> two full-day dressers required;
   v1  Q revised to C....D  -> one dresser does both jobs in sequence;
   the original sheet is retained, and with C->D reduced to one minute
   v0 itself also needs only one dresser.
2. double-byte PAIR costume labels and dangling/continued ``~`` cells,
   including a revision that changes the costume at the head of a run so
   that the following ``~`` cells inherit the new costume while the old
   version still reopens.
3. one actor playing two roles, a dresser whose shift is broken in the
   middle, and non-uniform column widths; plus the deliberately
   conflicting casting and a missing-worktime case.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass

from .blocking import BlockingError, parse_score
from .codec import encode_text
from .revision import RevisionLibrary, replace
from .wardrobe import (
    PlanningInput,
    PlanningResult,
    Worker,
    build_input,
    solve,
    verify_plan,
)

STAGE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts", "stage"
)


# ---------------------------------------------------------------------------
# Helpers


def encode_sheet(rows: tuple[str, ...]) -> bytes:
    """Encode a blocking grid as a real SOLO body (newlines separate rows)."""
    return encode_text("\n".join(rows)).data


def add_sheet(lib: RevisionLibrary, rows: tuple[str, ...]) -> int:
    version = lib.add_original(encode_sheet(rows))
    return version.version_id


def revise_row(
    lib: RevisionLibrary,
    base_id: int,
    rows: tuple[str, ...],
    row_index: int,
    new_line: str,
) -> int:
    """Replace one whole role line by its decoded character interval."""
    base = lib.get(base_id)
    ncols = len(rows[0])
    start = sum(len(r) + 1 for r in rows[:row_index])
    end = start + ncols
    version = lib.apply(base_id, [replace(start, end, new_line)])
    return version.version_id


def _jsonable(value):
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if is_dataclass(value):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    return value


def score_dump(score) -> dict:
    return {
        "version_id": score.version_id,
        "widths": list(score.widths),
        "boundaries": list(score.boundaries),
        "horizon": score.horizon,
        "roles": list(score.roles),
        "grid": [list(row) for row in score.grid],
        "spans": [
            {
                "role": span.role,
                "costume": span.costume,
                "interval": [span.start, span.end],
                "columns": [span.column_start, span.column_end],
                "cells": [
                    {
                        "column": cell.column,
                        "char": cell.char,
                        "byte_range": [cell.byte_start, cell.byte_end],
                        "byte_count": cell.byte_count,
                    }
                    for cell in span.cells
                ],
            }
            for span in score.all_spans()
        ],
    }


def result_dump(result: PlanningResult) -> dict:
    return {
        "feasible": result.feasible,
        "minimum_workers": result.minimum_workers,
        "used_worker_ids": list(result.used_worker_ids),
        "assignments": [
            {
                "change_id": a.change_id,
                "worker_id": a.worker_id,
                "interval": [a.start, a.end],
            }
            for a in sorted(result.assignments, key=lambda a: a.change_id)
        ],
        "blockers": [
            {"kind": b.kind, "message": b.message, "change_id": b.change_id, "details": b.details}
            for b in result.blockers
        ],
    }


def input_dump(data: PlanningInput) -> dict:
    return {
        "score_version_id": data.score_version_id,
        "horizon": data.horizon,
        "casting": {actor: list(roles) for actor, roles in data.casting.items()},
        "timelines": {
            actor: [
                {
                    "role": seg.role,
                    "costume": seg.costume,
                    "interval": [seg.start, seg.end],
                    "columns": [seg.column_start, seg.column_end],
                }
                for seg in segments
            ]
            for actor, segments in data.timelines.items()
        },
        "changes": [
            {
                "change_id": c.change_id,
                "actor": c.actor,
                "from_role": c.from_role,
                "to_role": c.to_role,
                "from_costume": c.from_costume,
                "to_costume": c.to_costume,
                "window": [c.release, c.deadline],
                "duration": c.duration,
            }
            for c in data.changes
        ],
    }


def run_plan(score, casting, worktimes, workers) -> PlanningResult:
    data = build_input(score, casting, worktimes, workers)
    return solve(data)


# ---------------------------------------------------------------------------
# Scenario 1: the worked example


def scenario_worked_example(log) -> dict:
    log("== scenario 1: worked example, six one-minute columns ==")
    lib = RevisionLibrary()
    rows0 = ("A...B.", "C...D.")
    v0 = add_sheet(lib, rows0)
    rows1 = ("A...B.", "C....D")
    v1 = revise_row(lib, v0, rows0, row_index=1, new_line=rows1[1])

    widths = (1, 1, 1, 1, 1, 1)
    roles = ("P", "Q")
    score0 = parse_score(lib.get(v0), widths, roles)
    score1 = parse_score(lib.get(v1), widths, roles)
    for score in (score0, score1):
        for span in score.all_spans():
            log("  " + span.describe())
            for cell in span.cells:
                log("    " + cell.location())

    full_day = (Worker.create("W1", ((0, 60),)), Worker.create("W2", ((0, 60),)))
    one_full = (Worker.create("W1", ((0, 60),)),)
    casting = {"PA": ("P",), "QB": ("Q",)}
    worktimes = {("A", "B"): 2, ("C", "D"): 2}

    result0 = run_plan(score0, casting, worktimes, full_day)
    result1 = run_plan(score1, casting, worktimes, full_day)
    result0_fast_cd = run_plan(
        score0, casting, {("A", "B"): 2, ("C", "D"): 1}, one_full
    )

    log(f"  v0 two full-day dressers -> minimum={result0.minimum_workers} feasible={result0.feasible}")
    for wid, jobs in result0.schedule_per_worker().items():
        for job in jobs:
            log(f"    {wid}: {job.describe()}")
    log(f"  v1 revised Q             -> minimum={result1.minimum_workers} feasible={result1.feasible}")
    for wid, jobs in result1.schedule_per_worker().items():
        for job in jobs:
            log(f"    {wid}: {job.describe()}")
    log(
        "  v0 kept, C->D worktime 1  -> minimum="
        f"{result0_fast_cd.minimum_workers} feasible={result0_fast_cd.feasible}"
    )
    assert result0.minimum_workers == 2
    assert result1.minimum_workers == 1
    assert result0_fast_cd.minimum_workers == 1
    assert not verify_plan(build_input(score0, casting, worktimes, full_day), result0.assignments)
    # Every published version still reopens and decodes.
    assert all(lib.verify(v) for v in (v0, v1))
    return {
        "library_versions": [
            {"version_id": vid, "text": lib.get(vid).text} for vid in (v0, v1)
        ],
        "scores": [score_dump(score0), score_dump(score1)],
        "plans": {
            "v0_original_two_min_changes": result_dump(result0),
            "v1_revised_q": result_dump(result1),
            "v0_original_cd_one_minute": result_dump(result0_fast_cd),
        },
        "inputs": {
            "v0": input_dump(build_input(score0, casting, worktimes, full_day)),
            "v1": input_dump(build_input(score1, casting, worktimes, full_day)),
        },
    }


# ---------------------------------------------------------------------------
# Scenario 2: double-byte costumes and tilde inheritance across revisions


def scenario_tilde_and_pair(log) -> dict:
    log("== scenario 2: PAIR costume numbers and '~' continuation ==")
    pair_costume = chr(0x4E00)      # two SOLO bytes, one column
    other_pair = chr(0x4E01)
    lib = RevisionLibrary()
    # Row R (7 cols): costume, three continuations, empty, a PAIR costume, empty.
    rows0 = (f"A~~~.{pair_costume}.", f".{pair_costume}~~...")
    v0 = add_sheet(lib, rows0)
    # Change only the head costume of the first run; the three '~' follow
    # the new costume in the new version, while v0 stays openable.
    rows1 = (f"B~~~.{pair_costume}.", f".{pair_costume}~~...")
    v1 = revise_row(lib, v0, rows0, 0, rows1[0])
    # A second revision replaces the head with another double-byte number.
    rows2 = (f"{other_pair}~~~.{pair_costume}.", f".{pair_costume}~~...")
    v2 = revise_row(lib, v1, rows1, 0, rows2[0])

    widths = (1, 1, 1, 1, 1, 1, 1)
    score0 = parse_score(lib.get(v0), widths, ("R", "S"))
    score1 = parse_score(lib.get(v1), widths, ("R", "S"))
    score2 = parse_score(lib.get(v2), widths, ("R", "S"))
    for score in (score0, score1, score2):
        for span in score.all_spans():
            log("  " + span.describe())
            for cell in span.cells:
                log("    " + cell.location())

    # The double-byte costume occupies one column but two original bytes;
    # the tilde cells inherit the head costume and merge into one span.
    r0 = score0.role_spans("R")[0]
    assert r0.costume == "A" and (r0.column_start, r0.column_end) == (0, 4)
    r0_pair = score0.role_spans("R")[1]
    assert r0_pair.cells[0].byte_count == 2 and r0_pair.column_start == 5
    s0 = score0.role_spans("S")[0]
    assert s0.costume == pair_costume and (s0.column_start, s0.column_end) == (1, 4)
    assert score1.role_spans("R")[0].costume == "B"
    assert score2.role_spans("R")[0].costume == other_pair
    assert score2.role_spans("R")[0].cells[0].byte_count == 2
    assert all(lib.verify(v) for v in (v0, v1, v2))

    # A dangling tilde must be rejected with its true original position.
    bad_lib = RevisionLibrary()
    bad_v = bad_lib.add_original(encode_sheet(("~A.....", "......A")))
    try:
        parse_score(bad_v, widths, ("R", "S"))
    except BlockingError as exc:
        log(f"  dangling '~' rejected: {exc}")
        assert exc.offset == 0 and exc.row == 0 and exc.column == 0
        dangling = {"offset": exc.offset, "end_offset": exc.end_offset, "row": exc.row, "column": exc.column}
    else:  # pragma: no cover - defensive
        raise AssertionError("dangling tilde was not rejected")

    # A tilde after an empty cell is equally dangling.
    bad2 = RevisionLibrary()
    bad2_v = bad2.add_original(encode_sheet((".A~....", ".....~A")))
    try:
        parse_score(bad2_v, widths, ("R", "S"))
    except BlockingError as exc:
        log(f"  post-empty '~' rejected: {exc}")
        assert exc.row == 1 and exc.column == 5
        dangling2 = {"offset": exc.offset, "end_offset": exc.end_offset, "row": exc.row, "column": exc.column}
    else:  # pragma: no cover
        raise AssertionError("post-empty tilde was not rejected")

    return {
        "library_versions": [
            {"version_id": vid, "text": lib.get(vid).text} for vid in (v0, v1, v2)
        ],
        "scores": [score_dump(s) for s in (score0, score1, score2)],
        "dangling_tilde": [dangling, dangling2],
    }


# ---------------------------------------------------------------------------
# Scenario 3: double casting, broken shift, non-uniform widths


def scenario_double_cast(log) -> dict:
    log("== scenario 3: one actor, two roles; broken shift; uneven columns ==")
    # Seventeen columns with uneven minute widths; boundaries
    # 0,2,3,4,5,6,7,9,10,11,12,13,14,15,16,17,18,19.  Empty columns always
    # separate the double-cast actor's consecutive segments, so every
    # costume change has a real, non-zero window.
    widths = (2, 1, 1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1)  # horizon 19
    lib = RevisionLibrary()
    #            cols 01234567890123456
    rows = ("A....B....C....D.", "......E..F.......", "...X........Y....")
    assert all(len(r) == 17 for r in rows)
    v0 = add_sheet(lib, rows)
    score = parse_score(lib.get(v0), widths, ("P", "Q", "R"))
    for span in score.all_spans():
        log("  " + span.describe())
        for cell in span.cells:
            log("    " + cell.location())

    # Actor 甲 plays P and R (two roles, no onstage overlap); actor 乙 plays Q.
    casting = {"甲": ("P", "R"), "乙": ("Q",)}
    worktimes = {
        ("A", "X"): 1,
        ("X", "B"): 1,
        ("B", "C"): 4,
        ("C", "Y"): 1,
        ("Y", "D"): 1,
        ("E", "F"): 2,
    }
    # Two full-day dressers.  E->F has its only slot [9,11) while B->C's
    # 4-minute job must run [7,11) or [8,12); the two always overlap, so no
    # single dresser can serve both and the minimum headcount is two.
    workers = (
        Worker.create("W1", ((0, 19),)),
        Worker.create("W2", ((0, 19),)),
    )
    data = build_input(score, casting, worktimes, workers)
    log("  actor timelines:")
    for actor, segments in data.timelines.items():
        for seg in segments:
            log(f"    {actor}: {seg.describe()}")
    for change in data.changes:
        log("  " + change.describe())
    result = solve(data)
    log(f"  minimum dressers={result.minimum_workers} feasible={result.feasible}")
    for wid, jobs in result.schedule_per_worker().items():
        for job in jobs:
            log(f"    {wid}: {job.describe()}")
    assert result.feasible and result.minimum_workers == 2
    assert not verify_plan(data, result.assignments)

    # A dresser whose shift is broken in the middle cannot cover the two
    # jobs whose windows cross the [9,13) gap, so that one-person roster is
    # infeasible even though the two-person full-day roster works.
    only_w1 = (Worker.create("W1", ((0, 9), (13, 19))),)
    result_w1 = solve(build_input(score, casting, worktimes, only_w1))
    log(f"  only a broken-shift dresser on duty -> feasible={result_w1.feasible}")
    for b in result_w1.blockers:
        log("    " + b.describe())
    assert not result_w1.feasible
    assert any(b.kind == "no_worker_covers_window" for b in result_w1.blockers)

    # The same show with one full-day dresser alone still cannot work,
    # because the two overlapping changes need two people at once.
    one_worker = (Worker.create("W2", ((0, 19),)),)
    result_one = solve(build_input(score, casting, worktimes, one_worker))
    assert not result_one.feasible
    assert any(b.kind == "single_dresser_contention" for b in result_one.blockers)

    # A conflicting casting needs two roles that are onstage at the same
    # time.  On this sheet P's A [0,2) and Q's E [7,9) do not overlap, so
    # build a small dedicated sheet where one actor is double-booked.
    conflict_lib = RevisionLibrary()
    conflict_rows = ("AA..", ".BB.")
    conflict_v = add_sheet(conflict_lib, conflict_rows)
    conflict_score = parse_score(conflict_lib.get(conflict_v), (1, 1, 1, 1), ("P", "Q"))
    bad_data = build_input(
        conflict_score,
        {"甲": ("P", "Q")},
        {("A", "B"): 1},
        (Worker.create("W1", ((0, 10),)),),
    )
    bad_result = solve(bad_data)
    log(f"  conflicting casting -> feasible={bad_result.feasible}")
    for b in bad_result.blockers:
        log("    " + b.describe())
    assert not bad_result.feasible
    assert any(b.kind == "casting_conflict" for b in bad_result.blockers)

    # A missing worktime is flagged on its own, before scheduling.
    partial_worktimes = dict(worktimes)
    del partial_worktimes[("B", "C")]
    missing = solve(build_input(score, casting, partial_worktimes, workers))
    assert not missing.feasible
    kinds = {b.kind for b in missing.blockers}
    assert "missing_worktime" in kinds
    log(f"  missing worktime blockers: {sorted(kinds)}")

    return {
        "library_versions": [{"version_id": v0, "text": lib.get(v0).text}],
        "widths": list(widths),
        "score": score_dump(score),
        "input": input_dump(data),
        "plans": {
            "broken_shift_two_workers": result_dump(result),
            "only_broken_shift_worker": result_dump(result_w1),
            "one_full_day_worker": result_dump(result_one),
            "conflicting_casting": result_dump(bad_result),
            "missing_worktime": result_dump(missing),
        },
    }


# ---------------------------------------------------------------------------
# Entry point


def demo(log=print) -> dict:
    os.makedirs(STAGE_DIR, exist_ok=True)
    report = {
        "scenario_1_worked_example": scenario_worked_example(log),
        "scenario_2_tilde_and_pair": scenario_tilde_and_pair(log),
        "scenario_3_double_cast": scenario_double_cast(log),
    }
    path = os.path.join(STAGE_DIR, "stage_report.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    log(f"== stage report written to {path} ==")
    return report


def main() -> int:
    demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

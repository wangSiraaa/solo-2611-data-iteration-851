"""Costume-department cross-check and dresser scheduling.

The blocking sheet never says who plays whom.  The costume department
keeps its own independent casting sheet mapping roles to actors, the
worktime (whole minutes) of every required costume conversion, and the
actual shift intervals of the dressers who showed up.

Per actor
---------
* the actor is already dressed in the first segment's costume before the
  show starts, so the first segment never needs a change;
* two consecutive segments on the actor's *whole* timeline (different
  roles count, because one person may play several parts) need one
  costume change when their costumes differ;
* the change must fit inside the half-open window between the earlier
  segment's exit and the later segment's entrance;
* two segments overlapping in time are a direct casting conflict.

Scheduling
----------
A change cannot be paused or handed over: one dresser performs the whole
job, helps one actor at a time, and the whole ``[start, start+duration)``
interval must lie inside one of that dresser's actual on-duty intervals.
All times are integer minutes.  The problem size is bounded by twelve
changes, six dressers and a sixty-minute show, so the production solver
uses an exhaustive backtracking search over dresser subsets; the tests
carry a second, independently written enumerator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from .blocking import BlockingScore, OccupancySpan

__all__ = [
    "WardrobeError",
    "Segment",
    "CastingConflict",
    "CostumeChange",
    "Worker",
    "Assignment",
    "Blocker",
    "PlanningInput",
    "PlanningResult",
    "build_input",
    "solve",
    "verify_plan",
    "MAX_CHANGES",
    "MAX_WORKERS",
    "MAX_HORIZON",
]

MAX_CHANGES = 12
MAX_WORKERS = 6
MAX_HORIZON = 60


class WardrobeError(Exception):
    """Invalid input data rather than an unsatisfiable schedule."""


# ---------------------------------------------------------------------------
# Value types


@dataclass(frozen=True)
class Segment:
    """One onstage span placed on an actor's personal timeline."""

    role: str
    costume: str
    start: int
    end: int
    column_start: int
    column_end: int

    @classmethod
    def from_span(cls, span: OccupancySpan) -> "Segment":
        return cls(
            role=span.role,
            costume=span.costume,
            start=span.start,
            end=span.end,
            column_start=span.column_start,
            column_end=span.column_end,
        )

    def describe(self) -> str:
        return (
            f"{self.role}/{self.costume!r} [{self.start},{self.end}) "
            f"cols [{self.column_start},{self.column_end})"
        )


@dataclass(frozen=True)
class CastingConflict:
    """Two parts of one actor are onstage at the same time."""

    actor: str
    first: Segment
    second: Segment
    overlap_start: int
    overlap_end: int

    def describe(self) -> str:
        return (
            f"actor {self.actor!r} double-booked on {self.first.describe()} and "
            f"{self.second.describe()}; overlap [{self.overlap_start},{self.overlap_end})"
        )


@dataclass(frozen=True)
class CostumeChange:
    """One required conversion on one actor's timeline."""

    change_id: int
    actor: str
    from_role: str
    to_role: str
    from_costume: str
    to_costume: str
    release: int  # earlier segment exit; earliest job start
    deadline: int  # later segment entrance; job must end by then
    duration: int | None  # None means the costume department gave no worktime

    @property
    def window(self) -> tuple[int, int]:
        return (self.release, self.deadline)

    @property
    def key(self) -> tuple[str, str]:
        return (self.from_costume, self.to_costume)

    def describe(self) -> str:
        dur = "missing worktime" if self.duration is None else f"{self.duration} min"
        return (
            f"#{self.change_id} {self.actor!r} {self.from_costume!r}->"
            f"{self.to_costume!r} ({self.from_role}->{self.to_role}) "
            f"window [{self.release},{self.deadline}), {dur}"
        )


@dataclass(frozen=True)
class Worker:
    """A dresser who actually showed up, with merged on-duty intervals."""

    worker_id: str
    intervals: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        if not self.intervals:
            raise WardrobeError(f"worker {self.worker_id!r} has no duty interval")
        for a, b in self.intervals:
            if not isinstance(a, int) or not isinstance(b, int) or a < 0 or b <= a:
                raise WardrobeError(
                    f"worker {self.worker_id!r} has invalid duty interval [{a},{b})"
                )

    @classmethod
    def create(cls, worker_id: str, intervals) -> "Worker":
        ordered = sorted((int(a), int(b)) for a, b in intervals)
        merged: list[list[int]] = []
        for a, b in ordered:
            if merged and a <= merged[-1][1]:
                if b > merged[-1][1]:
                    merged[-1][1] = b
            else:
                merged.append([a, b])
        return cls(worker_id, tuple((a, b) for a, b in merged))

    def covers(self, start: int, end: int) -> bool:
        return any(a <= start and end <= b for a, b in self.intervals)

    def describe(self) -> str:
        return f"{self.worker_id}: " + ", ".join(f"[{a},{b})" for a, b in self.intervals)


@dataclass(frozen=True)
class Assignment:
    """One dresser job: a whole change performed without interruption."""

    change_id: int
    worker_id: str
    start: int
    end: int

    def describe(self) -> str:
        return f"change #{self.change_id} on {self.worker_id} [{self.start},{self.end})"


@dataclass(frozen=True)
class Blocker:
    """A checkable reason why no schedule exists (or input is unusable)."""

    kind: str
    message: str
    change_id: int | None = None
    details: dict = field(default_factory=dict)

    def describe(self) -> str:
        tail = f" (change #{self.change_id})" if self.change_id is not None else ""
        return f"[{self.kind}]{tail} {self.message}"


# ---------------------------------------------------------------------------
# Input construction


@dataclass(frozen=True)
class PlanningInput:
    score_version_id: int
    horizon: int
    casting: dict[str, tuple[str, ...]]
    timelines: dict[str, tuple[Segment, ...]]
    changes: tuple[CostumeChange, ...]
    workers: tuple[Worker, ...]
    conflicts: tuple[CastingConflict, ...]
    missing_worktime: tuple[CostumeChange, ...]

    def change(self, change_id: int) -> CostumeChange:
        return self.changes[change_id]


def _normalise_casting(
    score: BlockingScore, casting: dict[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    known = set(score.roles)
    seen: dict[str, str] = {}
    out: dict[str, tuple[str, ...]] = {}
    for actor, roles in casting.items():
        norm = tuple(roles)
        if not norm:
            raise WardrobeError(f"actor {actor!r} is cast in no role")
        for role in norm:
            if role not in known:
                raise WardrobeError(
                    f"casting sheet assigns unknown role {role!r} to actor {actor!r}"
                )
            if role in seen:
                raise WardrobeError(
                    f"role {role!r} is cast to both {seen[role]!r} and {actor!r}"
                )
            seen[role] = actor
        out[actor] = norm
    missing = known - set(seen)
    if missing:
        raise WardrobeError(f"roles not covered by the casting sheet: {sorted(missing)}")
    return out


def build_input(
    score: BlockingScore,
    casting: dict[str, tuple[str, ...]],
    worktimes: dict[tuple[str, str], int],
    workers,
) -> PlanningInput:
    """Assemble the costume department's independent view of one version.

    ``casting`` maps actor -> tuple of roles.  ``worktimes`` maps
    ``(from_costume, to_costume)`` -> positive integer minutes.
    """
    casting = _normalise_casting(score, casting)
    if score.horizon > MAX_HORIZON:
        raise WardrobeError(
            f"show horizon {score.horizon} exceeds the {MAX_HORIZON}-minute bound"
        )

    worker_tuple = tuple(workers)
    if len(worker_tuple) > MAX_WORKERS:
        raise WardrobeError(f"at most {MAX_WORKERS} dressers are supported")
    if len({w.worker_id for w in worker_tuple}) != len(worker_tuple):
        raise WardrobeError("worker ids must be unique")

    timelines: dict[str, tuple[Segment, ...]] = {}
    conflicts: list[CastingConflict] = []
    for actor, roles in casting.items():
        segments: list[Segment] = []
        for role in roles:
            segments.extend(Segment.from_span(span) for span in score.role_spans(role))
        segments.sort(key=lambda s: (s.start, s.end, s.role))
        for earlier, later in zip(segments, segments[1:]):
            if earlier.end > later.start and earlier.start < later.end:
                conflicts.append(
                    CastingConflict(
                        actor=actor,
                        first=earlier,
                        second=later,
                        overlap_start=max(earlier.start, later.start),
                        overlap_end=min(earlier.end, later.end),
                    )
                )
        timelines[actor] = tuple(segments)

    changes: list[CostumeChange] = []
    missing: list[CostumeChange] = []
    for actor in sorted(timelines):
        segments = timelines[actor]
        for earlier, later in zip(segments, segments[1:]):
            if earlier.costume == later.costume:
                continue  # already dressed correctly
            key = (earlier.costume, later.costume)
            duration = worktimes.get(key)
            if duration is not None:
                if not isinstance(duration, int) or duration <= 0:
                    raise WardrobeError(
                        f"worktime for {key[0]!r}->{key[1]!r} must be a positive integer"
                    )
            change = CostumeChange(
                change_id=len(changes),
                actor=actor,
                from_role=earlier.role,
                to_role=later.role,
                from_costume=earlier.costume,
                to_costume=later.costume,
                release=earlier.end,
                deadline=later.start,
                duration=duration,
            )
            changes.append(change)
            if duration is None:
                missing.append(change)

    if len(changes) > MAX_CHANGES:
        raise WardrobeError(
            f"{len(changes)} costume changes exceed the {MAX_CHANGES}-change bound"
        )

    return PlanningInput(
        score_version_id=score.version_id,
        horizon=score.horizon,
        casting=casting,
        timelines=timelines,
        changes=tuple(changes),
        workers=tuple(worker_tuple),
        conflicts=tuple(conflicts),
        missing_worktime=tuple(missing),
    )


# ---------------------------------------------------------------------------
# Verification (shared by the solver and the tests)


def verify_plan(
    data: PlanningInput, assignments
) -> list[Blocker]:
    """Return every rule violation; an empty list means the plan is legal."""
    blockers: list[Blocker] = []
    by_id = {a.change_id: a for a in assignments}
    if set(by_id) != {c.change_id for c in data.changes if c.duration is not None}:
        blockers.append(
            Blocker("assignment_coverage", "every known-worktime change needs exactly one job")
        )
    if len(by_id) != len(assignments):
        blockers.append(Blocker("duplicate_assignment", "a change was assigned more than once"))

    worker_index = {w.worker_id: w for w in data.workers}
    per_worker: dict[str, list[Assignment]] = {}
    for asm in assignments:
        change = data.change(asm.change_id)
        if asm.end - asm.start != change.duration:
            blockers.append(
                Blocker(
                    "wrong_duration",
                    f"job length {asm.end - asm.start} != worktime {change.duration}",
                    asm.change_id,
                )
            )
        worker = worker_index.get(asm.worker_id)
        if worker is None:
            blockers.append(
                Blocker("unknown_worker", f"no dresser {asm.worker_id!r} on the shift", asm.change_id)
            )
            continue
        if not (change.release <= asm.start and asm.end <= change.deadline):
            blockers.append(
                Blocker(
                    "outside_window",
                    f"job {asm.describe()} not inside change window "
                    f"[{change.release},{change.deadline})",
                    asm.change_id,
                )
            )
        if not worker.covers(asm.start, asm.end):
            blockers.append(
                Blocker(
                    "outside_shift",
                    f"job {asm.describe()} is not wholly inside a duty interval of "
                    f"{worker.describe()}",
                    asm.change_id,
                )
            )
        per_worker.setdefault(asm.worker_id, []).append(asm)

    for worker_id, jobs in per_worker.items():
        jobs.sort(key=lambda a: a.start)
        for earlier, later in zip(jobs, jobs[1:]):
            if earlier.end > later.start:
                blockers.append(
                    Blocker(
                        "dresser_overlap",
                        f"dresser {worker_id!r} would help two actors at once: "
                        f"{earlier.describe()} vs {later.describe()}",
                    )
                )
    return blockers


# ---------------------------------------------------------------------------
# Solver


def _earliest_slot(worker: Worker, change: CostumeChange, busy) -> int | None:
    """Earliest legal start on ``worker`` given its existing assignments."""
    if change.duration is None:
        return None
    duration = change.duration
    candidates = [change.release]
    candidates.extend(a.end for a in busy if change.release <= a.end <= change.deadline)
    for start in sorted(set(candidates)):
        end = start + duration
        if end > change.deadline or start < change.release:
            continue
        if not worker.covers(start, end):
            continue
        if any(not (end <= a.start or a.end <= start) for a in busy):
            continue
        return start
    return None


def _covering_workers(data: PlanningInput, change: CostumeChange) -> list[Worker]:
    if change.duration is None:
        return []
    return [
        w
        for w in data.workers
        if any(
            a <= change.release and change.deadline <= b
            and (b - a) >= change.duration
            and (change.deadline - change.release) >= change.duration
            for a, b in w.intervals
        )
    ]


def _precheck_blockers(data: PlanningInput) -> list[Blocker]:
    blockers: list[Blocker] = []
    for conflict in data.conflicts:
        blockers.append(
            Blocker(
                "casting_conflict",
                conflict.describe(),
                details={
                    "actor": conflict.actor,
                    "overlap": [conflict.overlap_start, conflict.overlap_end],
                },
            )
        )
    for change in data.missing_worktime:
        blockers.append(
            Blocker(
                "missing_worktime",
                f"no worktime on file for {change.from_costume!r}->"
                f"{change.to_costume!r} needed by {change.actor!r}",
                change.change_id,
                details={"window": list(change.window)},
            )
        )
    for change in data.changes:
        if change.duration is None:
            continue
        window_len = change.deadline - change.release
        if change.duration > window_len:
            blockers.append(
                Blocker(
                    "window_too_short",
                    f"{change.describe()}: window is only {window_len} min",
                    change.change_id,
                    details={
                        "window": list(change.window),
                        "duration": change.duration,
                    },
                )
            )
            continue
        covers = _covering_workers(data, change)
        if not covers:
            blockers.append(
                Blocker(
                    "no_worker_covers_window",
                    f"{change.describe()}: no on-duty interval covers the whole job; "
                    f"shifts are "
                    + "; ".join(w.describe() for w in data.workers),
                    change.change_id,
                    details={
                        "window": list(change.window),
                        "duration": change.duration,
                        "workers": [w.describe() for w in data.workers],
                    },
                )
            )
    return blockers


def _search(data: PlanningInput, subset) -> tuple[Assignment, ...] | None:
    ordered = sorted(
        (c for c in data.changes if c.duration is not None),
        key=lambda c: (c.deadline, c.release, -c.duration, c.change_id),
    )
    busy: dict[str, list[Assignment]] = {w.worker_id: [] for w in subset}
    chosen: list[Assignment] = []

    def backtrack(index: int):
        if index == len(ordered):
            return True
        change = ordered[index]
        for worker in subset:
            start = _earliest_slot(worker, change, busy[worker.worker_id])
            if start is None:
                continue
            asm = Assignment(change.change_id, worker.worker_id, start, start + change.duration)
            busy[worker.worker_id].append(asm)
            chosen.append(asm)
            if backtrack(index + 1):
                return True
            chosen.pop()
            busy[worker.worker_id].remove(asm)
        return False

    return tuple(chosen) if backtrack(0) else None


def _failure_blockers(data: PlanningInput) -> list[Blocker]:
    """Structured, independently checkable evidence for an exhausted search."""
    blockers: list[Blocker] = []
    coverage: dict[int, list[str]] = {}
    for change in data.changes:
        if change.duration is None:
            continue
        covers = [w.worker_id for w in _covering_workers(data, change)]
        coverage[change.change_id] = covers
        blockers.append(
            Blocker(
                "candidate_dressers",
                f"{change.describe()}: candidate dressers {covers}",
                change.change_id,
                details={
                    "window": list(change.window),
                    "duration": change.duration,
                    "candidate_worker_ids": covers,
                },
            )
        )
    # Pairwise pressure: two changes whose windows overlap and that share
    # only one dresser cannot both be served by that dresser.
    timed = [c for c in data.changes if c.duration is not None]
    for i, first in enumerate(timed):
        for second in timed[i + 1 :]:
            overlap = min(first.deadline, second.deadline) - max(first.release, second.release)
            if overlap <= 0:
                continue
            shared = sorted(set(coverage[first.change_id]) & set(coverage[second.change_id]))
            if len(shared) == 1:
                blockers.append(
                    Blocker(
                        "single_dresser_contention",
                        f"changes #{first.change_id} and #{second.change_id} have overlapping "
                        f"windows and only dresser {shared[0]!r} can cover either",
                        details={
                            "changes": [first.change_id, second.change_id],
                            "windows": [list(first.window), list(second.window)],
                            "worker": shared[0],
                        },
                    )
                )
    blockers.append(
        Blocker(
            "no_schedule_found",
            "exhaustive search over every dresser subset found no collision-free placement; "
            "the candidate_dressers/single_dresser_contention records above are the binding "
            "window and shift constraints",
        )
    )
    return blockers


@dataclass(frozen=True)
class PlanningResult:
    feasible: bool
    input: PlanningInput
    minimum_workers: int | None
    assignments: tuple[Assignment, ...]
    blockers: tuple[Blocker, ...]

    @property
    def used_worker_ids(self) -> tuple[str, ...]:
        return tuple(sorted({a.worker_id for a in self.assignments}))

    def schedule_per_worker(self) -> dict[str, tuple[Assignment, ...]]:
        out: dict[str, list[Assignment]] = {}
        for asm in sorted(self.assignments, key=lambda a: (a.worker_id, a.start, a.end)):
            out.setdefault(asm.worker_id, []).append(asm)
        return {k: tuple(v) for k, v in out.items()}


def solve(data: PlanningInput) -> PlanningResult:
    """Find the minimum number of on-duty dressers for the whole run."""
    blockers = _precheck_blockers(data)
    if blockers:
        return PlanningResult(False, data, None, (), tuple(blockers))

    timed = [c for c in data.changes if c.duration is not None]
    if not timed:
        # Nobody needs a dresser: the show runs with zero dressers.
        return PlanningResult(True, data, 0, (), ())

    workers = list(data.workers)
    for count in range(1, len(workers) + 1):
        for subset in combinations(workers, count):
            assignments = _search(data, subset)
            if assignments is not None:
                violations = verify_plan(data, assignments)
                if violations:
                    raise WardrobeError(
                        "internal solver produced an illegal plan: "
                        + "; ".join(b.describe() for b in violations)
                    )
                return PlanningResult(True, data, count, tuple(sorted(assignments, key=lambda a: a.change_id)), ())

    return PlanningResult(False, data, None, (), tuple(_failure_blockers(data)))

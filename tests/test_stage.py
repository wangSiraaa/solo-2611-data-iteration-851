"""Tests for blocking-sheet restoration and dresser planning.

The planner in ``solo.wardrobe`` is a backtracking search.  These tests
cross-check it against a deliberately naive, independently written
enumerator that never imports the solver: it enumerates every legal
integer start of every costume change and brute-forces every dresser
assignment, so agreement on feasibility and minimum headcount is strong
evidence rather than a self-consistent tautology.
"""

from __future__ import annotations

import itertools
import unittest

from solo.blocking import BlockingError, parse_score
from solo.codec import decode_document, encode_text
from solo.revision import RevisionLibrary, replace
from solo.wardrobe import (
    MAX_CHANGES,
    MAX_HORIZON,
    MAX_WORKERS,
    WardrobeError,
    Worker,
    build_input,
    solve,
    verify_plan,
)


# ---------------------------------------------------------------------------
# Independent brute-force enumerator (no use of solo.wardrobe.solve)


def naive_feasible(data, worker_subset) -> bool:
    """True when every known-worktime change admits a legal assignment.

    Enumerates each change's legal integer starts independently, then every
    combination across changes, checking window, shift, duration and
    no-overlap rules directly.
    """
    timed = [c for c in data.changes if c.duration is not None]
    shift_index = {w.worker_id: w.intervals for w in worker_subset}

    def legal_starts(change, intervals):
        out = []
        for start in range(change.release, change.deadline - change.duration + 1):
            end = start + change.duration
            if any(a <= start and end <= b for a, b in intervals):
                out.append(start)
        return out

    options = []
    for change in timed:
        per_worker = {}
        for worker in worker_subset:
            starts = legal_starts(change, shift_index[worker.worker_id])
            if starts:
                per_worker[worker.worker_id] = starts
        if not per_worker:
            return False
        options.append((change, per_worker))

    # Cartesian product over (worker, start) choices for every change.
    domains = [
        [(wid, start) for wid, starts in per_worker.items() for start in starts]
        for _change, per_worker in options
    ]
    for combo in itertools.product(*domains):
        busy: dict[str, list[tuple[int, int]]] = {}
        ok = True
        for (change, _pw), (wid, start) in zip(options, combo):
            end = start + change.duration
            for a, b in busy.get(wid, ()):
                if not (end <= a or b <= start):
                    ok = False
                    break
            if not ok:
                break
            busy.setdefault(wid, []).append((start, end))
        if ok:
            return True
    return False


def naive_minimum(data) -> int | None:
    """Independently brute-forced minimum dresser count, or None."""
    # Mirror the production pre-checks that make scheduling impossible
    # before any assignment is tried.
    if data.conflicts or data.missing_worktime:
        return None
    for c in data.changes:
        if c.duration is None:
            continue
        if c.duration > c.deadline - c.release:
            return None
        # No on-duty interval can contain the whole job.
        if not any(
            any(a <= c.release and c.deadline <= b and (b - a) >= c.duration
                for a, b in w.intervals)
            for w in data.workers
        ):
            return None
    if not any(c.duration is not None for c in data.changes):
        return 0
    workers = list(data.workers)
    for count in range(1, len(workers) + 1):
        for subset in itertools.combinations(workers, count):
            if naive_feasible(data, subset):
                return count
    return None


def make_score(rows, widths, roles):
    lib = RevisionLibrary()
    version = lib.add_original(encode_text("\n".join(rows)).data)
    return parse_score(version, widths, roles), lib


# ---------------------------------------------------------------------------
# Blocking restoration


class BlockingParseTests(unittest.TestCase):
    def test_intervals_cells_and_byte_locations(self):
        rows = ("A...B.", "C...D.")
        score, _ = make_score(rows, (1, 1, 1, 1, 1, 1), ("P", "Q"))
        p_spans = score.role_spans("P")
        self.assertEqual([(s.costume, s.start, s.end) for s in p_spans], [("A", 0, 1), ("B", 4, 5)])
        # The second role line lives after the newline; Q's first cell is
        # at byte 7 (6 chars + newline).
        self.assertEqual(score.role_spans("Q")[0].cells[0].byte_start, 7)

    def test_non_uniform_widths_boundaries(self):
        rows = ("A.B",)
        score, _ = make_score(rows, (2, 3, 4), ("P",))
        self.assertEqual(score.boundaries, (0, 2, 5, 9))
        spans = score.role_spans("P")
        self.assertEqual((spans[0].start, spans[0].end), (0, 2))
        self.assertEqual((spans[1].start, spans[1].end), (5, 9))

    def test_tilde_merges_same_costume_run(self):
        rows = ("A~~.A",)
        score, _ = make_score(rows, (1, 1, 1, 1, 1), ("P",))
        spans = score.role_spans("P")
        self.assertEqual(len(spans), 2)
        merged = spans[0]
        self.assertEqual((merged.costume, merged.column_start, merged.column_end), ("A", 0, 3))
        # The run keeps every covered column's original byte location.
        self.assertEqual([c.byte_start for c in merged.cells], [0, 1, 2])
        # The empty column breaks continuation; the second A is a new span.
        self.assertEqual((spans[1].column_start, spans[1].column_end), (4, 5))

    def test_dangling_tilde_first_column_reports_real_offset(self):
        rows = ("~A...", ".....")
        with self.assertRaises(BlockingError) as ctx:
            make_score(rows, (1,) * 5, ("P", "Q"))
        self.assertEqual(ctx.exception.offset, 0)
        self.assertEqual((ctx.exception.row, ctx.exception.column), (0, 0))

    def test_dangling_tilde_after_empty_cell(self):
        rows = (".A~..", "..~..")
        with self.assertRaises(BlockingError) as ctx:
            make_score(rows, (1,) * 5, ("P", "Q"))
        self.assertEqual((ctx.exception.row, ctx.exception.column), (1, 2))
        # Real byte offset: row 1 starts after 5 chars + newline = 6.
        self.assertEqual(ctx.exception.offset, 8)

    def test_double_byte_costume_is_one_column_two_bytes(self):
        pair = chr(0x4E07)
        rows = (f"{pair}....",)
        score, _ = make_score(rows, (1,) * 5, ("P",))
        cell = score.role_spans("P")[0].cells[0]
        self.assertEqual(cell.byte_count, 2)
        self.assertEqual(cell.column, 0)
        self.assertEqual(score.column_count, 5)

    def test_row_width_mismatch_rejected(self):
        rows = ("A.B",)
        with self.assertRaises(BlockingError):
            make_score(rows, (1, 1), ("P",))

    def test_non_positive_width_rejected(self):
        rows = ("AB",)
        with self.assertRaises(BlockingError):
            make_score(rows, (1, 0), ("P",))

    def test_default_role_names(self):
        score, _ = make_score(("A..", ".B."), (1, 1, 1), None)
        self.assertEqual(score.roles, ("role1", "role2"))


class BlockingRevisionTests(unittest.TestCase):
    def test_change_head_costume_tilde_follows_new_version_old_still_opens(self):
        pair = chr(0x4E00)
        rows0 = (f"A~~~.{pair}.", f".{pair}~~...")
        lib = RevisionLibrary()
        v0 = lib.add_original(encode_text("\n".join(rows0)).data)
        ncols = 7
        start = 0
        v1 = lib.apply(v0.version_id, [replace(start, start + ncols, f"B~~~.{pair}.")])
        score0 = parse_score(lib.get(v0.version_id), (1,) * ncols, ("R", "S"))
        score1 = parse_score(v1, (1,) * ncols, ("R", "S"))
        self.assertEqual(score0.role_spans("R")[0].costume, "A")
        self.assertEqual(score1.role_spans("R")[0].costume, "B")
        # Tilde cells inherit the new head costume and still merge.
        self.assertEqual(score1.role_spans("R")[0].column_end, 4)
        # Both versions still reopen and decode cleanly.
        self.assertEqual(decode_document(v0.body).text, v0.text)
        self.assertEqual(decode_document(v1.body).text, v1.text)
        self.assertTrue(lib.verify(v0.version_id) and lib.verify(v1.version_id))

    def test_revision_shifts_byte_locations_but_locations_stay_accurate(self):
        # Replacing a one-byte head with a two-byte PAIR costume shifts the
        # following tilde cells; the reported byte ranges must match a fresh
        # decode of the new body exactly.
        pair = chr(0x4E10)
        lib = RevisionLibrary()
        v0 = lib.add_original(encode_text("A~~..").data)
        v1 = lib.apply(0, [replace(0, 1, pair)])
        score = parse_score(v1, (1,) * 5, ("P",))
        doc = decode_document(v1.body)
        for span in score.role_spans("P"):
            for cell in span.cells:
                self.assertEqual(doc.byte_range(cell.column), (cell.byte_start, cell.byte_end))


# ---------------------------------------------------------------------------
# Timeline construction and conflicts


class TimelineTests(unittest.TestCase):
    def setUp(self):
        rows = ("A.B..", "..C.D", ".E...")
        self.score, _ = make_score(rows, (1,) * 5, ("P", "Q", "R"))

    def test_double_cast_timeline_merges_roles_in_time_order(self):
        data = build_input(
            self.score,
            {"甲": ("P", "R"), "乙": ("Q",)},
            {("A", "B"): 1, ("A", "E"): 1, ("E", "B"): 1, ("C", "D"): 1},
            (Worker.create("W", ((0, 10),)),),
        )
        timeline = [(s.role, s.costume, s.start) for s in data.timelines["甲"]]
        self.assertEqual(timeline, [("P", "A", 0), ("R", "E", 1), ("P", "B", 2)])

    def test_double_booked_roles_report_conflict(self):
        data = build_input(
            self.score,
            {"甲": ("P", "Q"), "乙": ("R",)},
            {},
            (Worker.create("W", ((0, 10),)),),
        )
        self.assertTrue(data.conflicts)
        result = solve(data)
        self.assertFalse(result.feasible)
        self.assertTrue(any(b.kind == "casting_conflict" for b in result.blockers))

    def test_missing_worktime_flagged_separately(self):
        data = build_input(
            self.score,
            {"甲": ("P",), "乙": ("Q",), "丙": ("R",)},
            {},  # no worktimes at all
            (Worker.create("W", ((0, 10),)),),
        )
        self.assertTrue(data.missing_worktime)
        result = solve(data)
        self.assertTrue(any(b.kind == "missing_worktime" for b in result.blockers))

    def test_same_costume_consecutive_needs_no_change(self):
        score, _ = make_score(("A.A..",), (1,) * 5, ("P",))
        data = build_input(
            score, {"甲": ("P",)}, {}, (Worker.create("W", ((0, 10),)),)
        )
        self.assertEqual(data.changes, ())
        result = solve(data)
        self.assertTrue(result.feasible)
        self.assertEqual(result.minimum_workers, 0)

    def test_zero_length_window_is_too_short(self):
        # Two segments back to back leave no time to change.
        score, _ = make_score(("AB...",), (1,) * 5, ("P",))
        data = build_input(
            score, {"甲": ("P",)}, {("A", "B"): 1}, (Worker.create("W", ((0, 10),)),)
        )
        result = solve(data)
        self.assertFalse(result.feasible)
        self.assertTrue(any(b.kind == "window_too_short" for b in result.blockers))


# ---------------------------------------------------------------------------
# Scheduling cross-checked against the independent enumerator


class SchedulingTruthTests(unittest.TestCase):
    def _check(self, data):
        result = solve(data)
        if result.feasible:
            self.assertEqual(result.minimum_workers, naive_minimum(data))
            self.assertEqual(verify_plan(data, result.assignments), [])
        else:
            self.assertIsNone(naive_minimum(data))

    def test_worked_example_two_then_one(self):
        widths = (1,) * 6
        score0, _ = make_score(("A...B.", "C...D."), widths, ("P", "Q"))
        score1, _ = make_score(("A...B.", "C....D"), widths, ("P", "Q"))
        casting = {"PA": ("P",), "QB": ("Q",)}
        work = {("A", "B"): 2, ("C", "D"): 2}
        two = (Worker.create("W1", ((0, 60),)), Worker.create("W2", ((0, 60),)))
        one = (Worker.create("W1", ((0, 60),)),)
        self.assertEqual(solve(build_input(score0, casting, work, two)).minimum_workers, 2)
        self.assertEqual(solve(build_input(score1, casting, work, two)).minimum_workers, 1)
        # v0 with C->D shortened to one minute also needs just one dresser.
        fast = {("A", "B"): 2, ("C", "D"): 1}
        self.assertEqual(solve(build_input(score0, casting, fast, one)).minimum_workers, 1)
        for score in (score0, score1):
            self._check(build_input(score, casting, work, two))

    def test_broken_shift_blocks_jobs_crossing_gap(self):
        score, _ = make_score(("A...B.",), (1,) * 6, ("P",))
        work = {("A", "B"): 2}
        broken = (Worker.create("W1", ((0, 2), (4, 6))),)
        full = (Worker.create("W2", ((0, 6),)),)
        # The only window [1,4) is not wholly inside either broken interval.
        r_broken = solve(build_input(score, {"甲": ("P",)}, work, broken))
        self.assertFalse(r_broken.feasible)
        self.assertTrue(any(b.kind == "no_worker_covers_window" for b in r_broken.blockers))
        r_full = solve(build_input(score, {"甲": ("P",)}, work, full))
        self.assertTrue(r_full.feasible)
        self._check(build_input(score, {"甲": ("P",)}, work, broken + full))

    def test_overlapping_changes_require_two_dressers(self):
        # Both roles have the identical A->B change whose only 2-minute slot
        # is [2,4); the two jobs must run simultaneously, so one dresser
        # cannot serve both actors.
        widths = (2, 2, 2, 2)
        score, _ = make_score(("A.B.", "A.B."), widths, ("P", "Q"))
        work = {("A", "B"): 2}
        workers = (Worker.create("W1", ((0, 20),)), Worker.create("W2", ((0, 20),)))
        data = build_input(score, {"甲": ("P",), "乙": ("Q",)}, work, workers)
        result = solve(data)
        self.assertTrue(result.feasible)
        self.assertEqual(result.minimum_workers, 2)
        self._check(data)

    def test_random_small_instances_match_enumerator(self):
        import random

        rnd = random.Random(20261120)
        for trial in range(60):
            ncols = rnd.randrange(4, 9)
            widths = tuple(rnd.randrange(1, 4) for _ in range(ncols))
            n_roles = rnd.randrange(1, 4)
            costume_pool = "ABCDE"
            rows = []
            for _ in range(n_roles):
                line = []
                for _ in range(ncols):
                    line.append("." if rnd.random() < 0.5 else rnd.choice(costume_pool))
                rows.append("".join(line))
            roles = tuple(f"R{i}" for i in range(n_roles))
            try:
                score, _ = make_score(rows, widths, roles)
            except BlockingError:
                continue
            casting = {f"a{i}": (role,) for i, role in enumerate(roles)}
            conversions = {
                (x, y) for role in roles for x in costume_pool for y in costume_pool if x != y
            }
            worktimes = {k: rnd.randrange(1, 4) for k in conversions if rnd.random() < 0.8}
            n_workers = rnd.randrange(1, 4)
            workers = []
            for i in range(n_workers):
                if rnd.random() < 0.5:
                    intervals = ((0, score.horizon + 5),)
                else:
                    cut = rnd.randrange(1, max(2, score.horizon))
                    intervals = ((0, cut), (cut + 1, score.horizon + 5))
                workers.append(Worker.create(f"W{i}", intervals))
            try:
                data = build_input(score, casting, worktimes, tuple(workers))
            except WardrobeError:
                continue
            if len(data.changes) > MAX_CHANGES or len(workers) > MAX_WORKERS:
                continue
            self._check(data)


class BoundTests(unittest.TestCase):
    def test_horizon_limit_enforced(self):
        widths = (10,) * 7  # horizon 70 > 60
        score, _ = make_score(("A......",), widths, ("P",))
        with self.assertRaises(WardrobeError):
            build_input(score, {"甲": ("P",)}, {}, (Worker.create("W", ((0, 100),)),))

    def test_worker_limit_enforced(self):
        score, _ = make_score(("A.B...",), (1,) * 6, ("P",))
        workers = tuple(Worker.create(f"W{i}", ((0, 10),)) for i in range(MAX_WORKERS + 1))
        with self.assertRaises(WardrobeError):
            build_input(score, {"甲": ("P",)}, {("A", "B"): 1}, workers)

    def test_unknown_role_in_casting_rejected(self):
        score, _ = make_score(("A..",), (1, 1, 1), ("P",))
        with self.assertRaises(WardrobeError):
            build_input(score, {"甲": ("Z",)}, {}, (Worker.create("W", ((0, 10),)),))

    def test_horizon_constant_matches_task_bounds(self):
        self.assertEqual((MAX_CHANGES, MAX_WORKERS, MAX_HORIZON), (12, 6, 60))


if __name__ == "__main__":
    unittest.main(verbosity=2)

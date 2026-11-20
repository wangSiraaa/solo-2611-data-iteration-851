"""Restore role occupancy scores from SOLO-encoded blocking sheets.

A blocking sheet is a SOLO document whose decoded text is a rectangular
grid of rows separated by ``"\\n"``.  Every row is one role line and every
column is one show column; column *j* lasts ``widths[j]`` minutes and
adjacent columns are contiguous in time.

Cell vocabulary
---------------
``.``
    the role is offstage in this column.
``~``
    the role stays onstage in the same costume as in the immediately
    preceding occupied cell.  A ``~`` in the first column or after an
    empty stretch has nothing to attach to and is a hard error reported
    at the cell's real byte offsets in the version body.
any other character
    a costume label.  A double-byte PAIR-mode costume still occupies
    exactly one column; mode-switch bytes occupy no column at all.

Consecutive occupied columns carrying the same costume are merged into
one half-open onstage interval.  The output keeps the costume and, for
every covered column, the original byte range of the cell inside the
concrete revision version the score was derived from.
"""

from __future__ import annotations

from dataclasses import dataclass

from .codec import MODE_NAMES, DecodeError, decode_document

__all__ = [
    "EMPTY",
    "CONTINUE",
    "BlockingError",
    "ColumnCell",
    "OccupancySpan",
    "BlockingScore",
    "parse_score",
    "grid_rows",
]

EMPTY = "."
CONTINUE = "~"


class BlockingError(Exception):
    """The blocking sheet is malformed.

    ``offset``/``end_offset`` are the real byte range of the offending
    cell inside the version body; ``row``/``column`` are its grid
    coordinates (0-based).
    """

    def __init__(
        self,
        reason: str,
        *,
        offset: int | None = None,
        end_offset: int | None = None,
        row: int | None = None,
        column: int | None = None,
    ) -> None:
        self.reason = reason
        self.offset = offset
        self.end_offset = end_offset
        self.row = row
        self.column = column
        where = []
        if row is not None and column is not None:
            where.append(f"row {row} col {column}")
        if offset is not None:
            where.append(
                f"bytes [{offset},{end_offset})" if end_offset is not None else f"byte {offset}"
            )
        head = "blocking sheet error"
        if where:
            head += " at " + ", ".join(where)
        super().__init__(f"{head}: {reason}")


@dataclass(frozen=True)
class ColumnCell:
    """One decoded grid cell and its original byte location."""

    column: int
    char: str
    byte_start: int
    byte_end: int
    mode: int

    @property
    def byte_count(self) -> int:
        return self.byte_end - self.byte_start

    def location(self) -> str:
        return (
            f"col {self.column} char {self.char!r} "
            f"bytes [{self.byte_start},{self.byte_end}) via {MODE_NAMES[self.mode]}"
        )


@dataclass(frozen=True)
class OccupancySpan:
    """A maximal run of consecutive columns in one costume."""

    role: str
    costume: str
    start: int  # half-open stage time in minutes
    end: int
    column_start: int
    column_end: int
    cells: tuple[ColumnCell, ...]

    @property
    def columns(self) -> tuple[int, ...]:
        return tuple(c.column for c in self.cells)

    def describe(self) -> str:
        return (
            f"{self.role} costume {self.costume!r} onstage [{self.start},{self.end}) "
            f"cols [{self.column_start},{self.column_end})"
        )


@dataclass(frozen=True)
class BlockingScore:
    """The full occupancy score derived from one revision version."""

    version_id: int
    widths: tuple[int, ...]
    boundaries: tuple[int, ...]
    roles: tuple[str, ...]
    grid: tuple[tuple[str, ...], ...]
    spans: dict[str, tuple[OccupancySpan, ...]]

    @property
    def column_count(self) -> int:
        return len(self.widths)

    @property
    def horizon(self) -> int:
        return self.boundaries[-1]

    def role_spans(self, role: str) -> tuple[OccupancySpan, ...]:
        return self.spans[role]

    def all_spans(self) -> tuple[OccupancySpan, ...]:
        out: list[OccupancySpan] = []
        for role in self.roles:
            out.extend(self.spans[role])
        return tuple(out)


def grid_rows(text: str) -> tuple[list[str], list[int]]:
    """Split a decoded sheet into row texts and their starting char indices.

    A single trailing empty line (text ending in ``"\\n"``) is accepted;
    any other empty line is rejected.
    """
    pieces = text.split("\n")
    if pieces and pieces[-1] == "":
        pieces.pop()
    rows: list[str] = []
    bases: list[int] = []
    cursor = 0
    for row_index, piece in enumerate(pieces):
        if piece == "":
            raise BlockingError("empty row is not a role line", row=row_index)
        rows.append(piece)
        bases.append(cursor)
        cursor += len(piece) + 1  # +1 for the newline
    return rows, bases


def parse_score(
    version: Version,
    widths: tuple[int, ...] | list[int],
    roles: tuple[str, ...] | list[str] | None = None,
) -> BlockingScore:
    """Decode ``version`` into a :class:`BlockingScore`.

    ``widths`` are the positive integer minute counts of the columns.
    ``roles`` names the rows top to bottom; when omitted they are called
    ``role1``, ``role2`` ...
    """
    width_tuple = tuple(int(w) for w in widths)
    if not width_tuple:
        raise BlockingError("at least one column width is required")
    if any(w <= 0 for w in width_tuple):
        raise BlockingError("column widths must be positive integers")
    boundaries = [0]
    for w in width_tuple:
        boundaries.append(boundaries[-1] + w)
    boundaries_tuple = tuple(boundaries)
    ncols = len(width_tuple)

    try:
        doc = decode_document(version.body)
    except DecodeError as exc:
        raise BlockingError(f"version body does not decode: {exc}") from exc

    rows, bases = grid_rows(doc.text)
    if not rows:
        raise BlockingError("the sheet contains no role lines")
    if roles is None:
        role_names = tuple(f"role{i + 1}" for i in range(len(rows)))
    else:
        role_names = tuple(roles)
        if len(role_names) != len(rows):
            raise BlockingError(
                f"{len(role_names)} role names supplied for {len(rows)} role lines"
            )
    if len(set(role_names)) != len(role_names):
        raise BlockingError("role names must be unique")

    grid: list[tuple[str, ...]] = []
    spans_by_role: dict[str, tuple[OccupancySpan, ...]] = {}

    for row_index, (role, line, base) in enumerate(zip(role_names, rows, bases)):
        if len(line) != ncols:
            raise BlockingError(
                f"row has {len(line)} cells but {ncols} column widths were given",
                row=row_index,
            )
        raw_cells = tuple(line)
        grid.append(raw_cells)

        # Resolve "~" first; a dangling one is reported at its real bytes.
        resolved: list[str | None] = []
        last_costume: str | None = None
        for col, ch in enumerate(raw_cells):
            if ch == EMPTY:
                resolved.append(None)
                last_costume = None
            elif ch == CONTINUE:
                char_index = base + col
                start, end = doc.byte_range(char_index)
                if last_costume is None:
                    raise BlockingError(
                        "'~' has no preceding occupied cell in the same role line "
                        "(first column or only empty cells before it)",
                        offset=start,
                        end_offset=end,
                        row=row_index,
                        column=col,
                    )
                resolved.append(last_costume)
            else:
                last_costume = ch
                resolved.append(ch)

        # Merge consecutive columns that share one resolved costume.
        spans: list[OccupancySpan] = []
        col = 0
        while col < ncols:
            costume = resolved[col]
            if costume is None:
                col += 1
                continue
            end_col = col
            cells: list[ColumnCell] = []
            while end_col < ncols and resolved[end_col] == costume:
                char_index = base + end_col
                byte_start, byte_end = doc.byte_range(char_index)
                cells.append(
                    ColumnCell(
                        column=end_col,
                        char=raw_cells[end_col],
                        byte_start=byte_start,
                        byte_end=byte_end,
                        mode=doc.char_mode(char_index),
                    )
                )
                end_col += 1
            spans.append(
                OccupancySpan(
                    role=role,
                    costume=costume,
                    start=boundaries_tuple[col],
                    end=boundaries_tuple[end_col],
                    column_start=col,
                    column_end=end_col,
                    cells=tuple(cells),
                )
            )
            col = end_col
        spans_by_role[role] = tuple(spans)

    return BlockingScore(
        version_id=version.version_id,
        widths=width_tuple,
        boundaries=boundaries_tuple,
        roles=role_names,
        grid=tuple(grid),
        spans=spans_by_role,
    )

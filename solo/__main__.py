"""Command line entry points.

    python3 -m solo decode FILE [--chunk N] [--checkpoint OFFSET]
    python3 -m solo encode TEXT
    python3 -m solo revise ORIGINAL PATCHES.json
    python3 -m solo demo
    python3 -m solo check FILE
    python3 -m solo stage FILE WIDTHS [--roles R1,R2]
    python3 -m solo plan FILE PLAN.json
    python3 -m solo stagedemo

``revise`` reads a JSON batch:

    {"patches": [
        {"replace": [start_char, end_char, "text"]},
        {"preserve": [start_byte, end_byte]}
    ]}

``plan`` reads the costume department's own independent sheet:

    {"widths": [1, 1, ...],
     "roles": ["P", "Q"],
     "casting": {"actor": ["role", ...]},
     "worktimes": {"A->B": 2},
     "workers": {"W1": [[0, 60]], "W2": [[0, 30], [40, 60]]}}
"""

from __future__ import annotations

import argparse
import json
import sys

from .blocking import BlockingError, parse_score
from .codec import CheckpointError, DecodeError, EncodeError, decode_document, encode_text
from .revision import RevisionError, RevisionLibrary, preserve, replace
from .wardrobe import Worker, build_input, solve, verify_plan


def _load(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def cmd_decode(args: argparse.Namespace) -> int:
    body = _load(args.file)
    try:
        if args.checkpoint is not None:
            lib = RevisionLibrary()
            version = lib.add_original(body)
            cp = version.checkpoint_at(min(args.checkpoint, len(body)))
            dec = cp.resume(body)
            chars = dec.feed(body[cp.position :])
            final_mode = dec.finish()
            print("".join(c.char for c in chars))
            print(f"# resumed at {cp.position}, final mode {final_mode}", file=sys.stderr)
        else:
            doc = decode_document(body, chunk_size=args.chunk)
            print(doc.text)
            print(
                f"# {len(doc.text)} chars, final mode {doc.final_mode}", file=sys.stderr
            )
    except DecodeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except CheckpointError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def cmd_encode(args: argparse.Namespace) -> int:
    try:
        encoded = encode_text(args.text)
    except EncodeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(encoded.data)
    return 0


def cmd_revise(args: argparse.Namespace) -> int:
    batch = json.loads(_load(args.patches).decode("utf-8"))
    patches = []
    for item in batch["patches"]:
        if "replace" in item:
            start, end, text = item["replace"]
            patches.append(replace(start, end, text))
        elif "preserve" in item:
            start, end = item["preserve"]
            patches.append(preserve(start, end))
        else:
            print(f"unknown patch {item}", file=sys.stderr)
            return 2
    lib = RevisionLibrary()
    try:
        original = lib.add_original(_load(args.original))
        version = lib.apply(0, patches)
    except (DecodeError, RevisionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(version.body)
    print(f"# {original.summary()}", file=sys.stderr)
    print(f"# {version.summary()}", file=sys.stderr)
    for rec in version.records:
        print(f"# {rec.summary()}", file=sys.stderr)
    for adj in version.boundary_adjustments:
        print(f"# boundary {adj.summary()}", file=sys.stderr)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import demo

    demo()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    body = _load(args.file)
    try:
        doc = decode_document(body)
    except DecodeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        f"ok chars={len(doc.text)} bytes={len(body)} final={doc.final_mode}",
        file=sys.stderr,
    )
    return 0


def _open_version(file: str, version: int):
    lib = RevisionLibrary()
    v = lib.add_original(_load(file))
    if version:
        raise RevisionError(
            "a single file holds one version; use the revision library API for multi-version stores"
        )
    return v


def cmd_stage(args: argparse.Namespace) -> int:
    try:
        version = _open_version(args.file, args.version)
        roles = tuple(args.roles.split(",")) if args.roles else None
        score = parse_score(version, tuple(args.widths), roles)
    except (BlockingError, RevisionError, DecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out = {
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
                    }
                    for cell in span.cells
                ],
            }
            for span in score.all_spans()
        ],
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        config = json.loads(_load(args.plan).decode("utf-8"))
        version = _open_version(args.file, int(config.get("version", 0)))
        roles = config.get("roles")
        score = parse_score(version, tuple(config["widths"]), tuple(roles) if roles else None)
        worktimes = {
            tuple(key.split("->", 1)): int(value)
            for key, value in config.get("worktimes", {}).items()
        }
        workers = [
            Worker.create(worker_id, [tuple(iv) for iv in intervals])
            for worker_id, intervals in config["workers"].items()
        ]
        data = build_input(score, dict(config["casting"]), worktimes, workers)
        result = solve(data)
    except (BlockingError, RevisionError, DecodeError, KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    out = {
        "feasible": result.feasible,
        "minimum_workers": result.minimum_workers,
        "used_worker_ids": list(result.used_worker_ids),
        "changes": [
            {
                "change_id": c.change_id,
                "actor": c.actor,
                "conversion": f"{c.from_costume}->{c.to_costume}",
                "window": [c.release, c.deadline],
                "duration": c.duration,
            }
            for c in data.changes
        ],
        "schedule": [
            {
                "worker_id": worker_id,
                "jobs": [
                    {"change_id": job.change_id, "interval": [job.start, job.end]}
                    for job in jobs
                ],
            }
            for worker_id, jobs in result.schedule_per_worker().items()
        ],
        "blockers": [b.describe() for b in result.blockers],
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    if not result.feasible:
        return 1
    if verify_plan(data, result.assignments):
        print("produced plan failed verification", file=sys.stderr)
        return 2
    return 0


def cmd_stagedemo(args: argparse.Namespace) -> int:
    from .stagedemo import demo

    demo()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="solo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("decode", help="decode a SOLO body")
    p.add_argument("file")
    p.add_argument("--chunk", type=int, default=131072)
    p.add_argument("--checkpoint", type=int, default=None)
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser("encode", help="encode representable text")
    p.add_argument("text")
    p.set_defaults(func=cmd_encode)

    p = sub.add_parser("revise", help="apply a JSON patch batch")
    p.add_argument("original")
    p.add_argument("patches")
    p.set_defaults(func=cmd_revise)

    p = sub.add_parser("demo", help="write a demo and artifacts")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("check", help="validate a body")
    p.add_argument("file")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("stage", help="restore a blocking-sheet occupancy score")
    p.add_argument("file")
    p.add_argument("widths", type=int, nargs="+", help="positive minute count per column")
    p.add_argument("--roles", default=None, help="comma-separated role names, top to bottom")
    p.add_argument("--version", type=int, default=0)
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("plan", help="cross-check casting and schedule dressers")
    p.add_argument("file")
    p.add_argument("plan", help="JSON casting/worktime/worker sheet")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("stagedemo", help="run the stage restoration demo")
    p.set_defaults(func=cmd_stagedemo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

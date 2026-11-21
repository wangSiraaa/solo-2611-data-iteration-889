"""Command line interface for the citation catalog / repository.

Typical use::

    python -m citation_system init --db work/catalog.db \\
        --pages materials/726/pages.json

    python -m citation_system add-references --db work/catalog.db \\
        --references materials/726/references.jsonl

    python -m citation_system apply-operations --db work/catalog.db \\
        --operations materials/726/operations.jsonl

    python -m citation_system export --db work/catalog.db \\
        --usable exports/current_usable.jsonl \\
        --needs-attention exports/current_needs_attention.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import export
from .catalog import Catalog
from .proof import ProofDesk, open_handover_bundle
from .repository import CitationRepository


def _read_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            rows.append(json.loads(line))
    return rows


def _open_existing(path: str) -> Catalog:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"database {path} does not exist; run 'init' first")
    return Catalog.open(path)


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not args.force:
        raise SystemExit(f"{path} exists; use --force to reset it")
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            extra.unlink()
    catalog = Catalog.create(str(path))
    document = _read_json(args.pages)
    catalog.load_pages_document(document)
    print(
        json.dumps(
            {
                "db": str(path),
                "catalog_revision": catalog.revision(),
                "volumes": len(catalog.volume_ids()),
                "pages": sum(len(catalog.pages(v)) for v in catalog.volume_ids()),
            },
            ensure_ascii=False,
        )
    )
    catalog.close()
    return 0


def cmd_add_references(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    repo = CitationRepository(catalog)
    created: list[str] = []
    failures: list[dict[str, Any]] = []
    for reference in _read_jsonl(args.references):
        try:
            citation = repo.create_from_reference(reference)
        except Exception as exc:  # noqa: BLE001 - report, then fail the run
            failures.append(
                {
                    "citation_id": (reference or {}).get("citation_id"),
                    "error": _error_dict(exc),
                }
            )
            continue
        created.append(citation.citation_id)
    catalog.close()
    print(
        json.dumps(
            {"created": created, "failures": failures},
            ensure_ascii=False,
        )
    )
    return 1 if failures else 0


def cmd_apply_operations(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    operations = _read_jsonl(args.operations)
    try:
        results = catalog.apply_operations(operations)
        log = catalog.operation_log()
        final_revision = catalog.revision()
    finally:
        catalog.close()
    print(
        json.dumps(
            {
                "results": [
                    {
                        "request_index": r.get("request_index"),
                        "status": r.get("status"),
                        "error_code": r.get("error_code"),
                        "catalog_revision": r.get("catalog_revision"),
                    }
                    for r in results
                ],
                "operation_log": [
                    {
                        "request_index": row["request_index"],
                        "status": row["status"],
                        "error_code": row["error_code"],
                        "catalog_revision": row["catalog_revision"],
                        "expectation": row["expectation"],
                        "expectation_met": row["expectation_met"],
                    }
                    for row in log
                ],
                "final_catalog_revision": final_revision,
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        summary = export.write_products(
            catalog.conn, args.usable, args.needs_attention
        )
    finally:
        catalog.close()
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        repo = CitationRepository(catalog)
        data = {
            "catalog_revision": catalog.revision(),
            "volumes": {
                v: [
                    {
                        "page_id": p.page_id,
                        "section_id": p.section_id,
                        "label": p.label,
                        "active": p.active,
                    }
                    for p in catalog.pages(v)
                ]
                for v in catalog.volume_ids()
            },
            "citations": [
                {
                    "citation_id": c.citation_id,
                    "kind": c.kind,
                    "volume_id": c.volume_id,
                    "created_revision": c.created_revision,
                }
                for c in repo.all()
            ],
        }
    finally:
        catalog.close()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_show_citation(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        repo = CitationRepository(catalog)
        citation = repo.get(args.citation_id)
        out: dict[str, Any] = citation.as_dict()
        if citation.kind == "PINNED":
            out["members"] = [
                p.as_dict() for p in repo.pinned_members(args.citation_id)
            ]
        else:
            start, end = repo.range_endpoints(args.citation_id)
            out["start"] = start.as_dict()
            out["end"] = end.as_dict()
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_lookup(args: argparse.Namespace) -> int:
    """Resolve a (volume, section, label) to its single active page."""

    catalog = _open_existing(args.db)
    try:
        page = catalog.resolve(args.volume_id, args.section_id, args.label)
        out = {"page": page.as_dict(include_seq=True)}
    except Exception as exc:  # noqa: BLE001 - structural errors are the output
        out = {"error": _error_dict(exc)}
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if "page" in out else 2


def cmd_referrers(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        repo = CitationRepository(catalog)
        found = repo.citations_referencing(args.volume_id, args.page_id)
    finally:
        catalog.close()
    print(json.dumps(found, ensure_ascii=False, indent=2))
    return 0


def _error_dict(exc: BaseException) -> dict[str, Any]:
    if hasattr(exc, "as_dict"):
        return exc.as_dict()  # type: ignore[no-any-return]
    return {"code": exc.__class__.__name__, "message": str(exc)}


# ----------------------------------------------------------------------
# proof / handover commands
# ----------------------------------------------------------------------
def cmd_volume_names(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        desk = ProofDesk(catalog.conn)
        if args.volume and args.full_name is not None and args.short_name is not None:
            desk.set_volume_names(args.volume, args.full_name, args.short_name)
        names = desk.volume_names()
        out = {
            vid: {"full_name": n.full_name, "short_name": n.short_name}
            for vid, n in sorted(names.items())
        }
    finally:
        catalog.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_create_chapter(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        chapter = ProofDesk(catalog.conn).create_chapter(args.chapter_id, args.title)
    finally:
        catalog.close()
    print(json.dumps(
        {"chapter_id": chapter.chapter_id, "chapter_order": chapter.chapter_order,
         "title": chapter.title}, ensure_ascii=False))
    return 0


def cmd_add_group(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        record = ProofDesk(catalog.conn).add_group(
            args.chapter_id, args.group_id, args.excerpt
        )
    finally:
        catalog.close()
    print(json.dumps(
        {"group_id": record.group_id, "chapter_id": record.chapter_id,
         "position": record.position}, ensure_ascii=False))
    return 0


def cmd_submit_claim(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        claim = ProofDesk(catalog.conn).submit_claim(
            args.chapter_id, args.before, args.after, args.desk,
            claim_id=args.claim_id,
        )
    finally:
        catalog.close()
    print(json.dumps(
        {"claim_id": claim.claim_id, "chapter_id": claim.chapter_id,
         "from_group": claim.from_group, "to_group": claim.to_group,
         "desk_id": claim.desk_id, "status": claim.status}, ensure_ascii=False))
    return 0


def cmd_retract_claim(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        claim = ProofDesk(catalog.conn).retract_claim(args.claim_id)
    finally:
        catalog.close()
    print(json.dumps(
        {"claim_id": claim.claim_id, "status": claim.status,
         "from_group": claim.from_group, "to_group": claim.to_group},
        ensure_ascii=False))
    return 0


def cmd_correct_claims(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        corrections = _read_json(args.corrections)
        if isinstance(corrections, dict):
            corrections = corrections.get("corrections", [corrections])
        claims = ProofDesk(catalog.conn).apply_corrections(corrections)
    finally:
        catalog.close()
    print(json.dumps(
        {"corrections": [
            {"claim_id": c.claim_id, "chapter_id": c.chapter_id,
             "from_group": c.from_group, "to_group": c.to_group, "status": c.status}
            for c in claims
        ]}, ensure_ascii=False))
    return 0


def cmd_correct_excerpt(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        result = ProofDesk(catalog.conn).correct_excerpt(
            args.group_id, args.seq, args.citation_id, args.desk,
            correction_id=args.correction_id,
        )
    finally:
        catalog.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_prereview(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        report = ProofDesk(catalog.conn).chapter_report(args.chapter_id)
    finally:
        catalog.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_issue_handover(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        payload = ProofDesk(catalog.conn).issue_handover(args.chapter_id, args.batch_id)
    finally:
        catalog.close()
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(
        {"batch_id": payload["batch_id"],
         "catalog_revision": payload["catalog_revision"],
         "groups": len(payload["groups"]),
         "out": args.out}, ensure_ascii=False))
    return 0


def cmd_reopen_handover(args: argparse.Namespace) -> int:
    payload = _read_json(args.bundle)
    analysis = open_handover_bundle(payload)
    report = analysis.to_dict()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_list_handovers(args: argparse.Namespace) -> int:
    catalog = _open_existing(args.db)
    try:
        batches = ProofDesk(catalog.conn).handover_batches()
    finally:
        catalog.close()
    print(json.dumps(batches, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="citation_system")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a catalog from volumes pages JSON")
    p.add_argument("--db", required=True)
    p.add_argument("--pages", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add-references", help="create citations from JSONL")
    p.add_argument("--db", required=True)
    p.add_argument("--references", required=True)
    p.set_defaults(func=cmd_add_references)

    p = sub.add_parser("apply-operations", help="apply operation JSONL batches")
    p.add_argument("--db", required=True)
    p.add_argument("--operations", required=True)
    p.set_defaults(func=cmd_apply_operations)

    p = sub.add_parser("export", help="write citation-product/1 JSONL files")
    p.add_argument("--db", required=True)
    p.add_argument("--usable", required=True)
    p.add_argument("--needs-attention", required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("status", help="dump catalog and citation summary")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("show-citation", help="show a stored citation")
    p.add_argument("--db", required=True)
    p.add_argument("citation_id")
    p.set_defaults(func=cmd_show_citation)

    p = sub.add_parser("lookup", help="resolve volume/section/label")
    p.add_argument("--db", required=True)
    p.add_argument("--volume", dest="volume_id", required=True)
    p.add_argument("--section", dest="section_id", required=True)
    p.add_argument("--label", required=True)
    p.set_defaults(func=cmd_lookup)

    p = sub.add_parser("referrers", help="citations referencing a real page")
    p.add_argument("--db", required=True)
    p.add_argument("--volume", dest="volume_id", required=True)
    p.add_argument("--page", dest="page_id", required=True)
    p.set_defaults(func=cmd_referrers)

    # ---- proof / handover flow ----------------------------------------
    p = sub.add_parser("volume-names", help="set or list printer volume names")
    p.add_argument("--db", required=True)
    p.add_argument("--volume")
    p.add_argument("--full-name", dest="full_name")
    p.add_argument("--short-name", dest="short_name")
    p.set_defaults(func=cmd_volume_names)

    p = sub.add_parser("create-chapter", help="open an empty proof chapter")
    p.add_argument("--db", required=True)
    p.add_argument("--chapter-id", dest="chapter_id", required=True)
    p.add_argument("--title")
    p.set_defaults(func=cmd_create_chapter)

    p = sub.add_parser("add-group", help="add a stable footnote group")
    p.add_argument("--db", required=True)
    p.add_argument("--chapter-id", dest="chapter_id", required=True)
    p.add_argument("--group-id", dest="group_id", required=True)
    p.add_argument("--excerpt", action="append", required=True,
                   help="existing citation_id (repeatable, in order)")
    p.set_defaults(func=cmd_add_group)

    p = sub.add_parser("submit-claim", help="sign 'before prints before after'")
    p.add_argument("--db", required=True)
    p.add_argument("--chapter-id", dest="chapter_id", required=True)
    p.add_argument("--before", required=True, dest="before")
    p.add_argument("--after", required=True, dest="after")
    p.add_argument("--desk", required=True)
    p.add_argument("--claim-id", dest="claim_id")
    p.set_defaults(func=cmd_submit_claim)

    p = sub.add_parser("retract-claim", help="retract one desk's signed relation")
    p.add_argument("--db", required=True)
    p.add_argument("claim_id")
    p.set_defaults(func=cmd_retract_claim)

    p = sub.add_parser("correct-claims", help="apply a JSON correction batch atomically")
    p.add_argument("--db", required=True)
    p.add_argument("--corrections", required=True)
    p.set_defaults(func=cmd_correct_claims)

    p = sub.add_parser("correct-excerpt", help="repoint one excerpt binding")
    p.add_argument("--db", required=True)
    p.add_argument("--group-id", dest="group_id", required=True)
    p.add_argument("--seq", type=int, required=True)
    p.add_argument("--citation-id", dest="citation_id", required=True)
    p.add_argument("--desk", required=True)
    p.add_argument("--correction-id", dest="correction_id")
    p.set_defaults(func=cmd_correct_excerpt)

    p = sub.add_parser("prereview", help="per-group print determinism for a chapter")
    p.add_argument("--db", required=True)
    p.add_argument("--chapter-id", dest="chapter_id", required=True)
    p.set_defaults(func=cmd_prereview)

    p = sub.add_parser("issue-handover", help="issue an immutable handover batch")
    p.add_argument("--db", required=True)
    p.add_argument("--chapter-id", dest="chapter_id", required=True)
    p.add_argument("--batch-id", dest="batch_id", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_issue_handover)

    p = sub.add_parser("reopen-handover", help="re-review a batch offline (no DB)")
    p.add_argument("bundle")
    p.set_defaults(func=cmd_reopen_handover)

    p = sub.add_parser("list-handovers", help="list issued handover batches")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_list_handovers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - single, structured CLI failure
        print(json.dumps({"error": _error_dict(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

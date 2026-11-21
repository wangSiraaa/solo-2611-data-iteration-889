"""End-to-end proofing / handover demonstration against the real materials.

Uses the *real* catalog and references from ``materials/726/`` plus the
worked example from the task statement, and runs the whole flow on a real
SQLite database:

  temporary return to the desk -> correction -> issue for print -> re-open old
  package offline, then the three edge demonstrations:
  * independent retraction of one of two duplicate relations,
  * a range endpoint deactivated,
  * a short name colliding with another volume.

Every export directory under ``exports/proofing/`` is kept; nothing is
rewritten.  ``reopen`` is done from a saved JSON bundle only -- no database.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from citation_system.catalog import Catalog  # noqa: E402
from citation_system.proof import ProofDesk, open_handover_bundle  # noqa: E402
from citation_system.repository import CitationRepository  # noqa: E402

MATERIALS = ROOT / "materials" / "726"
WORK = ROOT / "work" / "proofing"
EXPORTS = ROOT / "exports" / "proofing"
STEPS: list[dict] = []


def reset(path: Path) -> None:
    for p in [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]:
        if p.exists():
            p.unlink()


def step(name: str, title: str, payload) -> None:
    out = EXPORTS / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    STEPS.append({"step": name, "title": title, "dir": str(out.relative_to(ROOT))})
    print(f"\n=== {name}: {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def group_lines(report: dict) -> list[str]:
    lines = []
    for g in report["groups"]:
        if g["deterministic"]:
            lines.append(f"{g['group_id']}: {g['print']['render']}  [可制版]")
        else:
            forms = ", ".join(
                f"{w['order']} -> {w['this_group']['render']}" for w in g["witnesses"]
            )
            lines.append(f"{g['group_id']}: 不确定；{forms}")
    return lines


def brief(report: dict) -> dict:
    return {
        "chapter": report["chapter_id"],
        "all_deterministic": report["all_deterministic"],
        "claims": report["claims"],
        "groups": [
            {
                "group_id": g["group_id"],
                "usable": g["usable"],
                "deterministic": g["deterministic"],
                "print": g.get("print", {}).get("render"),
                "order": None if g["deterministic"] else None,
                "witnesses": [
                    {"order": w["order"], "render": w["this_group"]["render"]}
                    for w in g.get("witnesses", [])
                ],
            }
            for g in report["groups"]
        ],
        "lines": group_lines(report),
    }


def main() -> int:
    if EXPORTS.exists():
        shutil.rmtree(EXPORTS)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------
    # Worked example, on a fresh real database.
    # --------------------------------------------------------------
    db = WORK / "worked.db"
    reset(db)
    catalog = Catalog.create(str(db))
    catalog.load_pages_document(
        {
            "format": "volume-pages/1",
            "revision": 1,
            "volumes": [
                {
                    "volume_id": "卷甲",
                    "pages": [
                        {"page_id": "p", "section_id": "body", "label": "3", "active": True},
                        {"page_id": "q", "section_id": "appendix", "label": "3", "active": True},
                    ],
                }
            ],
        }
    )
    repo = CitationRepository(catalog)
    repo.create_pinned("F", "固定引 p", "卷甲", ["p"])
    repo.create_pinned("Q", "引 q", "卷甲", ["q"])
    repo.create_pinned("C", "引 p", "卷甲", ["p"])

    desk = ProofDesk(catalog.conn)
    desk.set_volume_names("卷甲", "卷甲全称", "卷甲")
    desk.create_chapter("ch", "示例章")
    for gid in ("F", "Q", "C"):
        desk.add_group("ch", gid, [gid])
    desk.submit_claim("ch", "F", "Q", "甲组")
    desk.submit_claim("ch", "F", "C", "甲组")

    # 1. Before the desk answers: C is undetermined, two valid orders.
    rep = desk.chapter_report("ch")
    step("00_before_correction", "Q/C 无签关系；C 待定", brief(rep))

    # 2. Temporary return to the desk, then correct Q's binding to body p.
    desk.correct_excerpt("Q", 0, "C", "乙组", reason="Q 应绑正文 p")
    rep = desk.chapter_report("ch")
    step("01_corrected_binding", "把 Q 改绑 p；C 始终 同上", brief(rep))
    excerpt_history = desk.excerpt_corrections("Q")
    step(
        "01b_correction_trail",
        "更正指到原绑定，旧绑定保留",
        excerpt_history,
    )

    # 3. Undo the rebinding by keeping the old binding, and instead sign Q<C.
    desk.correct_excerpt("Q", 0, "Q", "乙组", reason="恢复旧绑定")
    claim = desk.submit_claim("ch", "Q", "C", "乙组")
    rep = desk.chapter_report("ch")
    step("02_signed_q_before_c", "恢复旧绑定 + 补签 Q<C；C 始终短名",
         {"report": brief(rep), "new_claim": claim.claim_id})

    # 4. Issue for print, then re-open the saved bundle offline (no DB).
    bundle = desk.issue_handover("ch", "batch-worked-1")
    bundle_path = EXPORTS / "03_handover" / "bundle.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    step("03_handover", "送印批次（固定内容）", {
        "batch_id": bundle["batch_id"],
        "catalog_revision": bundle["catalog_revision"],
        "all_deterministic": bundle["analysis"]["all_deterministic"],
        "lines": group_lines(bundle["analysis"]),
    })
    reopened = open_handover_bundle(json.loads(bundle_path.read_text(encoding="utf-8")))
    step("04_offline_reopen", "断网重开旧包，结果一致", {
        "matches_issued_analysis": reopened.to_dict() == bundle["analysis"],
        "lines": group_lines(reopened.to_dict()),
    })

    # Change the live DB after issuing: the old bundle must not move.
    desk.correct_excerpt("Q", 0, "C", "乙组", reason="送印后另改")
    live = desk.chapter_report("ch")
    old = desk.handover_batch("batch-worked-1")
    step("05_after_live_change", "送印后改动只影响现行版，旧批次不变", {
        "live_lines": group_lines(live),
        "old_batch_lines": group_lines(old["analysis"]),
        "old_batch_unchanged": old == bundle,
    })
    catalog.close()

    # --------------------------------------------------------------
    # Edge 1: two desks sign the same relation; retract one independently.
    # --------------------------------------------------------------
    db = WORK / "duplicates.db"
    reset(db)
    cat = Catalog.create(str(db))
    cat.load_pages_document(
        {
            "format": "volume-pages/1",
            "revision": 1,
            "volumes": [
                {
                    "volume_id": "W",
                    "pages": [
                        {"page_id": "a", "section_id": "body", "label": "1", "active": True},
                        {"page_id": "b", "section_id": "body", "label": "2", "active": True},
                    ],
                }
            ],
        }
    )
    rep_ = CitationRepository(cat)
    rep_.create_pinned("A", "A", "W", ["a"])
    rep_.create_pinned("B", "B", "W", ["b"])
    d = ProofDesk(cat.conn)
    d.set_volume_names("W", "W 全称", "W")
    d.create_chapter("ch")
    d.add_group("ch", "A", ["A"])
    d.add_group("ch", "B", ["B"])
    c1 = d.submit_claim("ch", "A", "B", "甲组")
    c2 = d.submit_claim("ch", "A", "B", "乙组")
    before = d.chapter_report("ch")
    d.retract_claim(c1.claim_id)
    after = d.chapter_report("ch")
    step("06_duplicate_retraction", "同一条关系两台各签，撤回甲台，乙台继续生效", {
        "claims_before": [(c.claim_id, c.desk_id) for c in d.all_claims("ch")],
        "active_after": [
            {"claim_id": c.claim_id, "desk_id": c.desk_id}
            for c in d.active_claims("ch")
        ],
        "before_lines": group_lines(before),
        "after_lines": group_lines(after),
        "B_form_kept": (
            {g["group_id"]: g["forms"] for g in before["groups"]}
            == {g["group_id"]: g["forms"] for g in after["groups"]}
        ),
    })

    # --------------------------------------------------------------
    # Edge 2: range endpoint deactivated -> group pending.
    # --------------------------------------------------------------
    cat.apply_operations(
        [
            {
                "operation": "insert_many",
                "volume_id": "W",
                "after_page_id": "b",
                "pages": [
                    {"page_id": "c", "section_id": "body", "label": "3"},
                    {"page_id": "d", "section_id": "body", "label": "4"},
                ],
            }
        ]
    )
    rep_.create_range("R", "R", "W", "b", "c")
    d.add_group("ch", "R", ["R"])
    d.submit_claim("ch", "B", "R", "甲组")
    rep_before = d.chapter_report("ch")
    cat.apply_operations(
        [{"operation": "deactivate", "volume_id": "W", "page_id": "c"}]
    )
    rep_after = d.chapter_report("ch")
    step("07_range_endpoint", "范围端停用 -> 组待核；相邻关系被打断", {
        "before": {g["group_id"]: (g["usable"], g["forms"]) for g in rep_before["groups"]},
        "after": {g["group_id"]: (g["usable"], g["forms"]) for g in rep_after["groups"]},
        "range_group_after": next(
            g for g in rep_after["groups"] if g["group_id"] == "R"
        )["print"]["render"],
    })

    # --------------------------------------------------------------
    # Edge 3: short-name collision -> full names only.
    # --------------------------------------------------------------
    cat.apply_operations(
        [
            {
                "operation": "activate",
                "volume_id": "W",
                "page_id": "c",
            }
        ]
    )
    cat.add_volume("X", "另一卷")
    cat.apply_operations(
        [
            {
                "operation": "insert",
                "volume_id": "X",
                "page_id": "z",
                "at_end": True,
                "page": {"page_id": "z", "section_id": "body", "label": "9", "active": True},
            }
        ]
    )
    rep_.create_pinned("Z", "Z", "X", ["z"])
    d.set_volume_names("X", "X 全称", "W")  # same short name as W
    d.add_group("ch", "Z", ["Z"])
    d.submit_claim("ch", "R", "Z", "甲组")
    rep_collide = d.chapter_report("ch")
    step("08_short_collision", "短名重名 -> 该卷一律印全称", {
        "names": {
            vid: {"full": n.full_name, "short": n.short_name}
            for vid, n in d.volume_names().items()
        },
        "Z": next(g for g in rep_collide["groups"] if g["group_id"] == "Z")["print"],
        "R": next(g for g in rep_collide["groups"] if g["group_id"] == "R")["print"],
    })
    cat.close()

    # --------------------------------------------------------------
    # Evidence that the original citation products are untouched.
    # --------------------------------------------------------------
    db = WORK / "materials.db"
    reset(db)
    from citation_system import export

    mat = Catalog.create(str(db))
    mat.load_pages_document(
        json.loads((MATERIALS / "pages.json").read_text(encoding="utf-8"))
    )
    mrepo = CitationRepository(mat)
    for line in (MATERIALS / "references.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            mrepo.create_from_reference(json.loads(line))
    out_dir = EXPORTS / "09_legacy_export"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = export.write_products(
        mat.conn, out_dir / "usable.jsonl", out_dir / "needs_attention.jsonl"
    )
    expected = (MATERIALS / "example_usable.jsonl").read_text(encoding="utf-8")
    actual = (out_dir / "usable.jsonl").read_text(encoding="utf-8")
    step("09_legacy_export", "旧 citation-product/1 导出逐字节不变", {
        "summary": summary,
        "byte_identical": actual == expected,
    })
    mat.close()

    index = {
        "exports_root": str(EXPORTS.relative_to(ROOT)),
        "steps": STEPS,
    }
    (EXPORTS / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {EXPORTS / 'index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

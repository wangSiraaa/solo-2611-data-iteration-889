"""CLI-level integration for the proof / handover flow."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATERIALS = ROOT / "materials" / "726"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "citation_system", *args],
        cwd=ROOT, capture_output=True, text=True,
    )


def run_ok(*args: str) -> dict:
    proc = run(*args)
    if proc.returncode != 0:
        raise AssertionError(f"{args} failed: {proc.stderr}")
    return json.loads(proc.stdout)


class CliProofFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "cli.db")
        pages = self.tmp / "pages.json"
        pages.write_text(
            json.dumps(
                {
                    "format": "volume-pages/1",
                    "revision": 1,
                    "volumes": [
                        {
                            "volume_id": "J1",
                            "pages": [
                                {"page_id": "p", "section_id": "body", "label": "3", "active": True},
                                {"page_id": "q", "section_id": "appendix", "label": "3", "active": True},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        refs = self.tmp / "refs.jsonl"
        refs.write_text(
            "\n".join(
                [
                    json.dumps({"citation_id": "F", "text": "f", "kind": "PINNED",
                                "volume_id": "J1", "page_ids": ["p"]}),
                    json.dumps({"citation_id": "Q", "text": "q", "kind": "PINNED",
                                "volume_id": "J1", "page_ids": ["q"]}),
                    json.dumps({"citation_id": "C", "text": "c", "kind": "PINNED",
                                "volume_id": "J1", "page_ids": ["p"]}),
                ]
            ),
            encoding="utf-8",
        )
        run_ok("init", "--db", self.db, "--pages", str(pages), "--force")
        run_ok("add-references", "--db", self.db, "--references", str(refs))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_full_flow_and_offline_reopen(self):
        run_ok("volume-names", "--db", self.db, "--volume", "J1",
               "--full-name", "卷甲全称", "--short-name", "卷甲")
        run_ok("create-chapter", "--db", self.db, "--chapter-id", "ch")
        for gid in ("F", "Q", "C"):
            run_ok("add-group", "--db", self.db, "--chapter-id", "ch",
                   "--group-id", gid, "--excerpt", gid)
        run_ok("submit-claim", "--db", self.db, "--chapter-id", "ch",
               "--before", "F", "--after", "Q", "--desk", "甲组")
        run_ok("submit-claim", "--db", self.db, "--chapter-id", "ch",
               "--before", "F", "--after", "C", "--desk", "甲组")

        report = run_ok("prereview", "--db", self.db, "--chapter-id", "ch")
        c = next(g for g in report["groups"] if g["group_id"] == "C")
        self.assertFalse(c["deterministic"])
        self.assertEqual(
            {w["this_group"]["render"] for w in c["witnesses"]},
            {"同上", "卷甲：3"},
        )

        # Correct Q's binding so C always prints 同上.
        run_ok("correct-excerpt", "--db", self.db, "--group-id", "Q",
               "--seq", "0", "--citation-id", "C", "--desk", "乙组")
        report = run_ok("prereview", "--db", self.db, "--chapter-id", "ch")
        c = next(g for g in report["groups"] if g["group_id"] == "C")
        self.assertTrue(c["deterministic"])
        self.assertEqual(c["print"]["render"], "同上")

        # Issue, then reopen offline with no --db argument.
        bundle = str(self.tmp / "bundle.json")
        run_ok("issue-handover", "--db", self.db, "--chapter-id", "ch",
               "--batch-id", "b1", "--out", bundle)
        reopened = run_ok("reopen-handover", bundle)
        self.assertEqual(reopened["all_deterministic"], True)
        self.assertEqual(
            next(g for g in reopened["groups"] if g["group_id"] == "C")["print"]["render"],
            "同上",
        )
        batches = run_ok("list-handovers", "--db", self.db)
        self.assertEqual([b["batch_id"] for b in batches], ["b1"])

    def test_cycle_correction_exits_nonzero_with_chain(self):
        run_ok("volume-names", "--db", self.db, "--volume", "J1",
               "--full-name", "卷甲全称", "--short-name", "卷甲")
        run_ok("create-chapter", "--db", self.db, "--chapter-id", "ch")
        for gid in ("F", "Q", "C"):
            run_ok("add-group", "--db", self.db, "--chapter-id", "ch",
                   "--group-id", gid, "--excerpt", gid)
        run_ok("submit-claim", "--db", self.db, "--chapter-id", "ch",
               "--before", "F", "--after", "Q", "--desk", "d", "--claim-id", "c1")
        run_ok("submit-claim", "--db", self.db, "--chapter-id", "ch",
               "--before", "Q", "--after", "C", "--desk", "d", "--claim-id", "c2")
        corrections = self.tmp / "corr.json"
        corrections.write_text(
            json.dumps(
                {
                    "corrections": [
                        {"original_claim_id": "c1", "from_group": "C", "to_group": "F",
                         "desk_id": "d"},
                        {"original_claim_id": "c2", "from_group": "F", "to_group": "C",
                         "desk_id": "d"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        proc = run("correct-claims", "--db", self.db, "--corrections", str(corrections))
        self.assertNotEqual(proc.returncode, 0)
        err = json.loads(proc.stderr)["error"]
        self.assertEqual(err["code"], "order_cycle")
        self.assertEqual(err["details"]["cycle"][0], err["details"]["cycle"][-1])

    def test_legacy_export_still_matches_materials(self):
        out = self.tmp / "out"
        out.mkdir()
        mats = str(self.tmp / "m.db")
        run_ok("init", "--db", mats, "--pages", str(MATERIALS / "pages.json"), "--force")
        run_ok("add-references", "--db", mats,
               "--references", str(MATERIALS / "references.jsonl"))
        run_ok("export", "--db", mats, "--usable", str(out / "u.jsonl"),
               "--needs-attention", str(out / "a.jsonl"))
        self.assertEqual(
            (out / "u.jsonl").read_text(encoding="utf-8"),
            (MATERIALS / "example_usable.jsonl").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()

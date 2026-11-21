"""Tests for the proofing layer: groups, two-desk claims, print pre-review."""

from __future__ import annotations

import itertools
import json
import random
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from citation_system import export
from citation_system.catalog import Catalog
from citation_system.errors import OperationRejected
from citation_system.proof import (
    FULL,
    IBID,
    PENDING,
    SHORT,
    Chapter,
    ChapterAnalysis,
    Claim,
    GroupState,
    ProofDesk,
    VolumeName,
    open_handover_bundle,
)
from citation_system.repository import CitationRepository


def load_doc(volumes):
    return {
        "format": "volume-pages/1",
        "revision": 1,
        "volumes": [
            {
                "volume_id": vid,
                "pages": [
                    {
                        "page_id": p["page_id"],
                        "section_id": p.get("section_id", "body"),
                        "label": p.get("label", p["page_id"]),
                        "active": p.get("active", True),
                    }
                    for p in pages
                ],
            }
            for vid, pages in volumes.items()
        ],
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cat = Catalog.create(str(self.tmp / "proof.db"))
        self.repo = CitationRepository(self.cat)
        self.desk = ProofDesk(self.cat.conn)

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp)

    def load(self, volumes):
        self.cat.load_pages_document(load_doc(volumes))

    def report(self, chapter="ch"):
        return self.desk.chapter_report(chapter)

    def group_result(self, chapter, group_id):
        return next(g for g in self.report(chapter)["groups"] if g["group_id"] == group_id)


class WorkedExampleTests(Base):
    """The exact hand-worked scenario from the task statement."""

    def setUp(self):
        super().setUp()
        self.load(
            {
                "J1": [
                    {"page_id": "p", "section_id": "body", "label": "3"},
                    {"page_id": "q", "section_id": "appendix", "label": "3"},
                ]
            }
        )
        self.repo.create_pinned("F", "F", "J1", ["p"])
        self.repo.create_pinned("Q", "Q", "J1", ["q"])
        self.repo.create_pinned("C", "C", "J1", ["p"])
        self.desk.set_volume_names("J1", "卷甲全称", "卷甲")
        self.desk.create_chapter("ch", "示例章")
        self.desk.add_group("ch", "F", ["F"])
        self.desk.add_group("ch", "Q", ["Q"])
        self.desk.add_group("ch", "C", ["C"])
        # Signed: F before Q, F before C.  No relation between Q and C.
        self.desk.submit_claim("ch", "F", "Q", "deskA")
        self.desk.submit_claim("ch", "F", "C", "deskA")

    def test_without_q_to_c_relation_c_is_undetermined_with_two_orders(self):
        c = self.group_result("ch", "C")
        self.assertFalse(c["deterministic"])
        self.assertEqual(set(c["forms"]), {IBID, SHORT})
        renders = {w["this_group"]["render"] for w in c["witnesses"]}
        # F,Q,C -> short name; F,C,Q -> 同上.
        self.assertIn("卷甲：3", renders)
        self.assertIn("同上", renders)
        orders = [w["order"] for w in c["witnesses"]]
        self.assertIn(["F", "Q", "C"], orders)
        self.assertIn(["F", "C", "Q"], orders)
        for w in c["witnesses"]:
            self.assertEqual(w["prints"]["C"], w["this_group"]["form"])
        # F and Q themselves are always determined.
        self.assertEqual(self.group_result("ch", "F")["print"]["render"], "卷甲全称：3")
        self.assertEqual(self.group_result("ch", "Q")["print"]["render"], "卷甲：3")

    def test_rebinding_q_to_p_makes_c_always_ibid(self):
        self.desk.correct_excerpt("Q", 0, "C", "deskA")  # Q now binds body p
        c = self.group_result("ch", "C")
        self.assertTrue(c["deterministic"])
        self.assertEqual(c["print"]["form"], IBID)
        q = self.group_result("ch", "Q")
        self.assertTrue(q["deterministic"])
        self.assertEqual(q["print"]["form"], IBID)

    def test_extra_signed_relation_q_before_c_makes_c_always_short(self):
        self.desk.submit_claim("ch", "Q", "C", "deskB")
        c = self.group_result("ch", "C")
        self.assertTrue(c["deterministic"])
        self.assertEqual(c["print"]["form"], SHORT)
        self.assertEqual(c["print"]["render"], "卷甲：3")

    def test_rebinding_keeps_old_binding_recorded(self):
        self.desk.correct_excerpt("Q", 0, "C", "deskA")
        corrections = self.desk.excerpt_corrections("Q")
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["old_citation_id"], "Q")
        self.assertEqual(corrections[0]["new_citation_id"], "C")
        # The catalog citation Q itself was not modified.
        q_members = self.repo.pinned_members("Q")
        self.assertEqual([p.page_id for p in q_members], ["q"])


class GroupCompositionTests(Base):
    def test_dedup_by_complete_volume_page_in_first_appearance_order(self):
        self.load(
            {
                "V1": [
                    {"page_id": "a", "label": "1"},
                    {"page_id": "b", "label": "2"},
                    {"page_id": "c", "label": "3"},
                ],
                "V2": [
                    {"page_id": "a", "label": "1"},
                    {"page_id": "b", "label": "2"},
                ],
            }
        )
        self.repo.create_pinned("P1", "p1", "V1", ["a", "b"])
        self.repo.create_pinned("P2", "p2", "V2", ["a"])   # V2/a is NOT V1/a
        self.repo.create_pinned("P3", "p3", "V1", ["a", "b"])  # duplicate sequence
        self.desk.set_volume_names("V1", "卷一", "V1")
        self.desk.set_volume_names("V2", "卷二", "V2")
        self.desk.create_chapter("ch")
        self.desk.add_group("ch", "G", ["P1", "P2", "P3"])
        g = self.group_result("ch", "G")
        self.assertEqual(
            [tuple(x) for x in g["sequence"]],
            [("V1", "a"), ("V1", "b"), ("V2", "a")],
        )
        self.assertEqual(g["volumes"], ["V1", "V2"])

    def test_any_pending_excerpt_makes_whole_group_pending(self):
        self.load({"V1": [{"page_id": "a", "label": "1"}, {"page_id": "b", "label": "2"}]})
        self.repo.create_pinned("OK", "ok", "V1", ["a"])
        self.repo.create_pinned("BAD", "bad", "V1", ["a", "b"])
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "b"}]
        )
        self.desk.set_volume_names("V1", "卷一", "V1")
        self.desk.create_chapter("ch")
        self.desk.add_group("ch", "G", ["OK", "BAD"])
        g = self.group_result("ch", "G")
        self.assertFalse(g["usable"])
        self.assertEqual(g["print"]["render"], "待核")

    def test_chapters_are_independent_and_open_empty(self):
        self.load({"V1": [{"page_id": "a", "label": "1"}]})
        self.repo.create_pinned("P", "p", "V1", ["a"])
        self.desk.set_volume_names("V1", "卷一", "V1")
        self.desk.create_chapter("one")
        self.desk.create_chapter("two")
        self.desk.add_group("one", "G1", ["P"])
        self.assertEqual(self.desk.groups("two"), [])
        r = self.report("two")
        self.assertEqual(r["groups"], [])
        self.assertTrue(r["all_deterministic"])

    def test_pending_group_breaks_ibid_but_volumes_still_appeared(self):
        self.load(
            {
                "V1": [{"page_id": "a", "label": "1"}],
                "V2": [{"page_id": "b", "label": "2"}],
            }
        )
        self.repo.create_pinned("A", "a", "V1", ["a"])
        self.repo.create_pinned("A2", "a2", "V1", ["a"])
        self.repo.create_pinned("B", "b", "V2", ["b"])
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "a"}]
        )
        self.desk.set_volume_names("V1", "卷一", "V1")
        self.desk.set_volume_names("V2", "卷二", "V2")
        self.desk.create_chapter("ch")
        self.desk.add_group("ch", "G1", ["A", "A2"])  # pending (a inactive)
        self.desk.add_group("ch", "G2", ["B"])        # V2
        self.desk.submit_claim("ch", "G1", "G2", "d")
        rep = self.report("ch")
        g1 = next(g for g in rep["groups"] if g["group_id"] == "G1")
        g2 = next(g for g in rep["groups"] if g["group_id"] == "G2")
        self.assertEqual(g1["print"]["render"], "待核")
        # G2's V2 never appeared in a *usable* group before, so full name.
        self.assertEqual(g2["print"]["form"], FULL)


class ClaimTests(Base):
    def setUp(self):
        super().setUp()
        self.load({"V1": [{"page_id": "a", "label": "1"}, {"page_id": "b", "label": "2"}]})
        self.repo.create_pinned("A", "a", "V1", ["a"])
        self.repo.create_pinned("B", "b", "V1", ["b"])
        self.desk.set_volume_names("V1", "卷一", "V1")
        self.desk.create_chapter("ch")
        self.desk.add_group("ch", "A", ["A"])
        self.desk.add_group("ch", "B", ["B"])

    def test_two_desks_same_relation_retract_one_keeps_other(self):
        c1 = self.desk.submit_claim("ch", "A", "B", "desk1")
        c2 = self.desk.submit_claim("ch", "A", "B", "desk2")
        self.assertNotEqual(c1.claim_id, c2.claim_id)
        self.desk.retract_claim(c1.claim_id)
        active = self.desk.active_claims("ch")
        self.assertEqual([c.claim_id for c in active], [c2.claim_id])
        # The surviving relation still orders A before B.
        rep = self.report("ch")
        b = next(g for g in rep["groups"] if g["group_id"] == "B")
        self.assertTrue(b["deterministic"])
        self.assertEqual(b["print"]["form"], SHORT)  # V1 appeared in A

    def test_same_desk_duplicate_rejected(self):
        self.desk.submit_claim("ch", "A", "B", "desk1")
        with self.assertRaises(OperationRejected) as ctx:
            self.desk.submit_claim("ch", "A", "B", "desk1")
        self.assertEqual(ctx.exception.code, "duplicate_claim")

    def test_direct_cycle_rejected_with_chain(self):
        self.desk.submit_claim("ch", "A", "B", "d")
        with self.assertRaises(OperationRejected) as ctx:
            self.desk.submit_claim("ch", "B", "A", "d")
        self.assertEqual(ctx.exception.code, "order_cycle")
        chain = ctx.exception.details["cycle"]
        self.assertEqual(chain[0], chain[-1])
        self.assertGreaterEqual(len(chain), 3)

    def test_claim_across_chapters_rejected(self):
        self.desk.create_chapter("other")
        self.repo.create_pinned("Z", "z", "V1", ["a"])
        self.desk.add_group("other", "Z", ["Z"])
        with self.assertRaises(OperationRejected) as ctx:
            self.desk.submit_claim("ch", "A", "Z", "d")
        self.assertEqual(ctx.exception.code, "invalid_claim")

    def test_retracted_claim_cannot_be_corrected(self):
        c = self.desk.submit_claim("ch", "A", "B", "d")
        self.desk.retract_claim(c.claim_id)
        with self.assertRaises(OperationRejected) as ctx:
            self.desk.correct_claim(c.claim_id, "A", "B", "d")
        self.assertEqual(ctx.exception.code, "claim_already_retracted")

    def test_correction_points_at_original_claim(self):
        c = self.desk.submit_claim("ch", "A", "B", "d")
        new = self.desk.correct_claim(c.claim_id, "A", "B", "d")
        corr = self.desk.corrections("ch")
        self.assertEqual(corr[0]["claim_id"], c.claim_id)
        self.assertEqual(corr[0]["new_claim_id"], new.claim_id)
        self.assertEqual(self.desk.claim(c.claim_id).status, "retracted")
        self.assertEqual(self.desk.claim(new.claim_id).status, "active")


class CycleBatchTests(Base):
    """A batch of corrections that would form a cycle is rejected whole."""

    def setUp(self):
        super().setUp()
        self.load(
            {
                "V1": [
                    {"page_id": "a", "label": "1"},
                    {"page_id": "b", "label": "2"},
                    {"page_id": "c", "label": "3"},
                ]
            }
        )
        for cid, pid in (("A", "a"), ("B", "b"), ("C", "c")):
            self.repo.create_pinned(cid, cid, "V1", [pid])
        self.desk.set_volume_names("V1", "卷一", "V1")
        self.desk.create_chapter("ch")
        for cid in ("A", "B", "C"):
            self.desk.add_group("ch", cid, [cid])
        self.ca = self.desk.submit_claim("ch", "A", "B", "d")
        self.cb = self.desk.submit_claim("ch", "B", "C", "d")

    def test_batch_cycle_rejected_whole_and_points_chain(self):
        # Replace A->B with C->A AND B->C with A->C.  Either alone is acyclic,
        # but together they give C->A->C.
        with self.assertRaises(OperationRejected) as ctx:
            self.desk.apply_corrections(
                [
                    {
                        "original_claim_id": self.ca.claim_id,
                        "from_group": "C",
                        "to_group": "A",
                        "desk_id": "d",
                    },
                    {
                        "original_claim_id": self.cb.claim_id,
                        "from_group": "A",
                        "to_group": "C",
                        "desk_id": "d",
                    },
                ]
            )
        self.assertEqual(ctx.exception.code, "order_cycle")
        chain = ctx.exception.details["cycle"]
        self.assertEqual(chain[0], chain[-1])
        self.assertIn("A", chain)
        self.assertIn("C", chain)
        # Nothing changed: both originals still active, no correction written.
        self.assertEqual(self.desk.claim(self.ca.claim_id).status, "active")
        self.assertEqual(self.desk.claim(self.cb.claim_id).status, "active")
        self.assertFalse(self.desk.corrections("ch"))

    def test_valid_batch_applies_all_atomically(self):
        # A->B and B->C are replaced by A->C and C->B (still acyclic).
        new_claims = self.desk.apply_corrections(
            [
                {
                    "original_claim_id": self.ca.claim_id,
                    "from_group": "A",
                    "to_group": "C",
                    "desk_id": "d",
                },
                {
                    "original_claim_id": self.cb.claim_id,
                    "from_group": "C",
                    "to_group": "B",
                    "desk_id": "d",
                },
            ]
        )
        self.assertEqual(len(new_claims), 2)
        self.assertEqual(self.desk.claim(self.ca.claim_id).status, "retracted")
        self.assertEqual(self.desk.claim(self.cb.claim_id).status, "retracted")
        active = {(c.from_group, c.to_group) for c in self.desk.active_claims("ch")}
        self.assertEqual(active, {("A", "C"), ("C", "B")})

    def test_failed_batch_does_not_retract_any_original(self):
        with self.assertRaises(OperationRejected):
            self.desk.apply_corrections(
                [
                    {
                        "original_claim_id": self.ca.claim_id,
                        "from_group": "A",
                        "to_group": "C",
                        "desk_id": "d",
                    },
                    {
                        "original_claim_id": self.cb.claim_id,
                        "from_group": "C",
                        "to_group": "A",
                        "desk_id": "d",
                    },
                ]
            )
        self.assertEqual(self.desk.claim(self.ca.claim_id).status, "active")
        self.assertEqual(self.desk.claim(self.cb.claim_id).status, "active")


class RangeAndNameTests(Base):
    def test_deactivated_range_endpoint_group_is_pending(self):
        self.load(
            {
                "V1": [
                    {"page_id": "a", "label": "1"},
                    {"page_id": "b", "label": "2"},
                    {"page_id": "c", "label": "3"},
                    {"page_id": "d", "label": "4"},
                ]
            }
        )
        self.repo.create_range("R", "range", "V1", "a", "c")
        self.desk.set_volume_names("V1", "卷一", "V1")
        self.desk.create_chapter("ch")
        self.desk.add_group("ch", "G", ["R"])
        self.assertTrue(self.group_result("ch", "G")["usable"])
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "c"}]
        )
        g = self.group_result("ch", "G")
        self.assertFalse(g["usable"])
        self.assertEqual(g["print"]["form"], PENDING)

    def test_short_name_collision_forces_full_name(self):
        self.load(
            {
                "V1": [{"page_id": "a", "label": "1"}],
                "V2": [{"page_id": "b", "label": "2"}],
            }
        )
        self.repo.create_pinned("A", "a", "V1", ["a"])
        self.repo.create_pinned("B", "b", "V2", ["b"])
        # Two different volumes share a short name.
        self.desk.set_volume_names("V1", "卷一", "同名")
        self.desk.set_volume_names("V2", "卷二", "同名")
        self.desk.create_chapter("ch")
        self.desk.add_group("ch", "A", ["A"])
        self.desk.add_group("ch", "B", ["B"])
        self.desk.submit_claim("ch", "A", "B", "d")
        b = self.group_result("ch", "B")
        self.assertEqual(b["print"]["form"], FULL)
        self.assertEqual(b["print"]["render"], "卷二：2")

    def test_missing_volume_name_is_reported(self):
        self.load({"V1": [{"page_id": "a", "label": "1"}]})
        self.repo.create_pinned("A", "a", "V1", ["a"])
        self.desk.create_chapter("ch")
        self.desk.add_group("ch", "A", ["A"])
        with self.assertRaises(OperationRejected) as ctx:
            self.report("ch")
        self.assertEqual(ctx.exception.code, "missing_volume_name")


class HandoverTests(Base):
    def setUp(self):
        super().setUp()
        self.load(
            {
                "V1": [
                    {"page_id": "a", "label": "1"},
                    {"page_id": "b", "label": "2"},
                ]
            }
        )
        self.repo.create_pinned("A", "a", "V1", ["a"])
        self.repo.create_pinned("B", "b", "V1", ["b"])
        self.desk.set_volume_names("V1", "卷一", "V1")
        self.desk.create_chapter("ch", "交印章")
        self.desk.add_group("ch", "A", ["A"])
        self.desk.add_group("ch", "B", ["B"])

    def test_issued_batch_is_immutable_and_reopen_matches(self):
        batch = self.desk.issue_handover("ch", "b1")
        self.assertEqual(batch["format"], "proof-handover/1")
        # With no signed relation either group could precede the other, so the
        # first group's form is genuinely undetermined until a desk signs.
        self.assertFalse(batch["analysis"]["all_deterministic"])

        # Change the live state well after issuing.
        self.desk.submit_claim("ch", "A", "B", "d")
        self.repo.create_pinned("C", "c", "V1", ["a"])
        self.desk.add_group("ch", "C", ["C"])
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "V1", "page_id": "b"}]
        )
        live = self.report("ch")
        self.assertNotEqual(len(live["groups"]), len(batch["analysis"]["groups"]))

        # Stored batch and an offline re-open are byte-for-byte equal in result.
        stored = self.desk.handover_batch("b1")
        self.assertEqual(stored, batch)
        offline = open_handover_bundle(stored).to_dict()
        self.assertEqual(offline, batch["analysis"])

    def test_duplicate_batch_id_rejected(self):
        self.desk.issue_handover("ch", "b1")
        with self.assertRaises(OperationRejected) as ctx:
            self.desk.issue_handover("ch", "b1")
        self.assertEqual(ctx.exception.code, "batch_exists")

    def test_handover_survives_db_round_trip(self):
        self.desk.issue_handover("ch", "b1")
        path = str(self.cat.conn.execute("PRAGMA database_list").fetchone()["file"])
        self.cat.close()
        reopened = Catalog.open(path)
        try:
            payload = ProofDesk(reopened.conn).handover_batch("b1")
            self.assertEqual(payload["chapter"]["chapter_id"], "ch")
            self.assertEqual(
                open_handover_bundle(payload).to_dict(), payload["analysis"]
            )
        finally:
            reopened.close()
        # Keep Base.tearDown happy (it closes again).
        self.cat = Catalog.open(path)


class ManyGroupsTests(Base):
    def test_ten_plus_mutually_unordered_groups_all_reported(self):
        pages = [{"page_id": f"p{i}", "label": str(i)} for i in range(1, 15)]
        self.load({"V1": pages})
        self.desk.set_volume_names("V1", "卷一", "V1")
        self.desk.create_chapter("ch")
        for i in range(1, 14):
            self.repo.create_pinned(f"P{i}", f"p{i}", "V1", [f"p{i}"])
            self.desk.add_group("ch", f"G{i}", [f"P{i}"])
        rep = self.report("ch")
        self.assertEqual(len(rep["groups"]), 13)
        # Every group is usable and every group's form is reported.
        for g in rep["groups"]:
            self.assertTrue(g["usable"])
            self.assertIn("forms", g)
            self.assertTrue(g["forms"])
        # With no signed relations any group can be first, so no group's form
        # is fixed; the printer still receives a result for every one of them.
        self.assertFalse(any(g["deterministic"] for g in rep["groups"]))
        self.assertFalse(rep["all_deterministic"])
        # Each group holds a distinct page, so 同上 is impossible; but any group
        # can be first (full name) or preceded (short name).
        for g in rep["groups"]:
            self.assertEqual(len(g["witnesses"]), 2)
            orders = [tuple(w["order"]) for w in g["witnesses"]]
            self.assertEqual(len(set(orders)), 2)
            self.assertEqual(
                {w["this_group"]["form"] for w in g["witnesses"]},
                {"full_name", "short_name"},
            )


class BruteForceAgreementTests(unittest.TestCase):
    """Lock the reachability analysis against exhaustive enumeration."""

    def _brute(self, states, claims, names):
        by = {s.group_id: s for s in states}
        nodes = [s.group_id for s in states]
        counts = Counter(names.values())
        collide = {v: counts[n] > 1 for v, n in names.items()}
        result = {g: set() for g in nodes}
        for perm in itertools.permutations(nodes):
            pos = {n: i for i, n in enumerate(perm)}
            if any(pos[c.from_group] >= pos[c.to_group] for c in claims):
                continue
            appeared: set[str] = set()
            prev = None
            for gid in perm:
                s = by[gid]
                if not s.usable:
                    form = PENDING
                elif prev is not None and by[prev].usable and by[prev].seq == s.seq:
                    form = IBID
                else:
                    v = s.single_volume
                    if v is not None and v in appeared and not collide.get(v, True):
                        form = SHORT
                    else:
                        form = FULL
                result[gid].add(form)
                appeared.update(s.volumes)
                prev = gid
        return result

    def _rand(self, rng):
        n = rng.randint(2, 5)
        vols = rng.choice([["J1"], ["J1", "J2"]])
        states = []
        pool = [(v, p) for v in vols for p in range(1, 4)]
        for i in range(n):
            usable = rng.random() > 0.2
            seq = [rng.choice(pool) for _ in range(rng.randint(1, 2))]
            seen: set = set()
            sq = []
            for x in seq:
                if x not in seen:
                    seen.add(x)
                    sq.append(x)
            pages = tuple(
                {"volume_id": v, "page_id": f"p{p}", "section_id": "body", "label": str(p)}
                for v, p in sq
            )
            vlist: list[str] = []
            for v, _ in sq:
                if v not in vlist:
                    vlist.append(v)
            states.append(
                GroupState(f"G{i}", i, (), usable, tuple(sq), pages, tuple(vlist))
            )
        order = list(range(n))
        rng.shuffle(order)
        claims = [
            Claim(f"c{a}{b}", "ch", f"G{order[a]}", f"G{order[b]}", "d", "active", None)
            for a in range(n) for b in range(a + 1, n) if rng.random() < 0.15
        ]
        return states, claims

    def test_agrees_with_exhaustive_enumeration(self):
        rng = random.Random(11)
        for _ in range(800):
            states, claims = self._rand(rng)
            vols = {v for s in states for v in s.volumes}
            short = {v: ("同名" if rng.random() < 0.2 else v + "短") for v in vols}
            names = {v: VolumeName(v, v + "全", s) for v, s in short.items()}
            analysis = ChapterAnalysis(Chapter("ch", 0, "t"), states, claims, names)
            expected = self._brute(states, claims, short)
            for s in states:
                self.assertEqual(
                    analysis.forms(s.group_id), expected[s.group_id],
                    f"{s.group_id}: {s.usable} {s.seq}",
                )


if __name__ == "__main__":
    unittest.main()

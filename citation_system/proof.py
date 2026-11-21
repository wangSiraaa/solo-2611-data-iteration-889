"""Proofing desk: stable footnote groups, two-desk ordering, print pre-review.

This module adds an independent *proofing layer* on top of the existing
catalog / citation repository.  It does not change fixed (PINNED) or range
(RANGE) references and does not change the ``citation-product/1`` export; it
only reads them.

Domain model
------------
* A **volume name** record holds the printer's full name and short name.  The
  printer owns these names; the editor does not pick them.
* A **chapter** opens empty and never inherits groups from another chapter.
* A **group** is a stable footnote group inside one chapter.  It holds a
  sequence of existing excerpts (catalog citations).  On evaluation each
  excerpt is expanded under the current catalog revision; page identities are
  de-duplicated by the complete ``(volume, stable page)`` pair in
  first-appearance order.  If *any* excerpt needs attention the whole group
  needs attention.
* A **claim** is one desk's signed statement "from_group prints before
  to_group", inside one chapter.  Claims are per desk: two desks may sign the
  same pair independently, and retracting one leaves the other in force.  Only
  signed claims constrain the order; the printer is free to arrange everything
  else.
* A **correction** must point at the claim (or excerpt binding) it replaces.

Printing rule (per chapter, for one concrete total order)
---------------------------------------------------------
* needs-attention group -> prints ``待核``.  It breaks the *immediately
  preceding* relationship (the next group can never be ``同上`` against it) but
  the volumes it references still count as having appeared.
* usable group, whose immediate predecessor is usable and has exactly the same
  actual volume/page sequence -> ``同上``.
* otherwise, if the group spans exactly one volume that appeared earlier and
  whose short name does not collide with another volume -> short name + pages.
* every other usable group -> full name + pages.

Determinism
-----------
For each group the pre-review asks: over **all** total orders that satisfy the
signed claims, is the printed form always the same?  If yes the group can be
plated.  If not, the editor is handed back two valid full-chapter orders and
what the group prints under each.

The analysis does not enumerate linear extensions.  It uses the reachability
relation of the claim DAG.  A group ``p`` can be the *immediate* predecessor of
``G`` in some linear extension iff there is no path ``G -> p`` and no node
``x`` with ``p -> x -> G`` (``x`` would have to sit between them).  For the
short-name rule the "volume appeared earlier" condition is likewise decided
from forced/forbidden inclusion in the down-set placed before ``G``.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from . import db, export
from .errors import (
    BATCH_EXISTS,
    CHAPTER_EXISTS,
    CHAPTER_NOT_FOUND,
    CITATION_NOT_FOUND,
    CLAIM_ALREADY_RETRACTED,
    CLAIM_NOT_FOUND,
    DUPLICATE_CLAIM,
    GROUP_NOT_FOUND,
    HANDOVER_INVALID,
    INVALID_CLAIM,
    INVALID_CORRECTION,
    INVALID_GROUP,
    MISSING_VOLUME_NAME,
    ORDER_CYCLE,
    VOLUME_NAME_INVALID,
    VOLUME_NOT_FOUND,
    OperationRejected,
)

FORMAT_HANDOVER = "proof-handover/1"

PENDING = "pending"
IBID = "ibid"
SHORT = "short_name"
FULL = "full_name"

RENDER = {PENDING: "待核", IBID: "同上"}


# ----------------------------------------------------------------------
# data records
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class VolumeName:
    volume_id: str
    full_name: str
    short_name: str


@dataclass(frozen=True)
class Chapter:
    chapter_id: str
    chapter_order: int
    title: str | None


@dataclass(frozen=True)
class GroupRecord:
    group_id: str
    chapter_id: str
    position: int


@dataclass(frozen=True)
class Claim:
    claim_id: str
    chapter_id: str
    from_group: str
    to_group: str
    desk_id: str
    status: str
    retracted_at: str | None


@dataclass(frozen=True)
class GroupState:
    """A group expanded under one catalog snapshot."""

    group_id: str
    position: int
    excerpt_ids: tuple[str, ...]
    usable: bool
    seq: tuple[tuple[str, str], ...]
    pages: tuple[dict[str, Any], ...]
    volumes: tuple[str, ...]

    @property
    def single_volume(self) -> str | None:
        distinct = set(self.volumes)
        return next(iter(distinct)) if len(distinct) == 1 else None


# ----------------------------------------------------------------------
# graph helpers
# ----------------------------------------------------------------------
def _transitive_closure(nodes: list[str], edges: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {n: set() for n in nodes}
    for a, b in edges:
        if a in adj and b in adj and a != b:
            adj[a].add(b)
    reach: dict[str, set[str]] = {}
    for start in nodes:
        seen: set[str] = set()
        stack = list(adj[start])
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(adj[n])
        reach[start] = seen
    return reach


def _find_cycle(nodes: list[str], edges: Iterable[tuple[str, str]]) -> list[str] | None:
    """Return one concrete cycle as ``[a, b, ..., a]`` or ``None``."""

    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        if a in adj and b in adj and a != b:
            adj[a].append(b)
    state: dict[str, int] = {n: 0 for n in nodes}  # 0 unseen, 1 on stack, 2 done
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        state[n] = 1
        stack.append(n)
        for m in adj[n]:
            if state[m] == 1:
                idx = stack.index(m)
                return stack[idx:] + [m]
            if state[m] == 0:
                found = visit(m)
                if found:
                    return found
        stack.pop()
        state[n] = 2
        return None

    for n in nodes:
        if state[n] == 0:
            found = visit(n)
            if found:
                return found
    return None


def _short_collisions(names: dict[str, VolumeName]) -> dict[str, bool]:
    counts: dict[str, int] = {}
    for name in names.values():
        counts[name.short_name] = counts.get(name.short_name, 0) + 1
    return {vid: counts[n.short_name] > 1 for vid, n in names.items()}


def _topo_sort(
    nodes: set[str],
    edges: Iterable[tuple[str, str]],
    position: dict[str, int],
) -> list[str]:
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    indeg: dict[str, int] = {n: 0 for n in nodes}
    for a, b in edges:
        if a in nodes and b in nodes and a != b:
            adj[a].append(b)
            indeg[b] += 1
    ready = sorted([n for n in nodes if indeg[n] == 0], key=lambda n: position[n])
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
                ready.sort(key=lambda x: position[x])
    if len(order) != len(nodes):  # pragma: no cover - caller guarantees a DAG
        raise OperationRejected(ORDER_CYCLE, "internal: residual graph has a cycle")
    return order


# ----------------------------------------------------------------------
# analysis core (shared by live pre-review and offline handover re-open)
# ----------------------------------------------------------------------
class ChapterAnalysis:
    """Per-group determinism analysis for one chapter."""

    def __init__(
        self,
        chapter: Chapter,
        groups: list[GroupState],
        active_claims: list[Claim],
        names: dict[str, VolumeName],
    ) -> None:
        self.chapter = chapter
        self.groups = groups
        self.claims = active_claims
        self.names = names
        self.collide = _short_collisions(names)
        self.by_id = {g.group_id: g for g in groups}
        self.positions = {g.group_id: g.position for g in groups}
        self.nodes = [g.group_id for g in groups]
        self.edges = sorted({(c.from_group, c.to_group) for c in active_claims})
        self.reach = _transitive_closure(self.nodes, self.edges)
        # ancestors(G) = nodes forced strictly before G
        self.ancestors: dict[str, set[str]] = {g: set() for g in self.nodes}
        for a in self.nodes:
            for b in self.reach[a]:
                self.ancestors[b].add(a)
        self._forms: dict[str, set[str]] = {}
        self._print_cache: dict[tuple[str, ...], dict[str, str]] = {}

    # -- graph predicates ------------------------------------------------
    def _path(self, a: str, b: str) -> bool:
        return b in self.reach.get(a, set())

    def _can_be_immediate_pred(self, g: str, p: str) -> bool:
        """Can ``p`` sit immediately before ``g`` in some linear extension?"""

        if p == g or self._path(g, p):
            return False
        # No node may be forced strictly between p and g.
        return not (self.reach.get(p, set()) & self.ancestors[g])

    def _possible_predecessors(self, g: str) -> list[str]:
        return [
            p for p in self.nodes
            if p != g and self._can_be_immediate_pred(g, p)
        ]

    def _same_seq_usable(self, g: str) -> list[str]:
        target = self.by_id[g].seq
        return [
            h.group_id for h in self.groups
            if h.group_id != g and h.usable and h.seq == target
        ]

    # -- appeared-volume feasibility ------------------------------------
    def _ref_groups(self, volume_id: str) -> list[str]:
        return [g.group_id for g in self.groups if volume_id in g.volumes]

    def _appeared_true_feasible(self, g: str, p: str, volume_id: str) -> bool:
        """With predecessor ``p``, can ``volume_id`` have appeared earlier?"""

        for a in self._ref_groups(volume_id):
            if a == g:
                continue
            if a == p:
                return True          # p is before g and references the volume
            if self._path(g, a):
                continue             # forced after g
            if self._path(p, a):
                continue             # would be forced between p and g
            return True              # can be placed before p
        return False

    def _appeared_false_feasible(self, g: str, p: str | None, volume_id: str) -> bool:
        """Can no earlier group reference ``volume_id`` (with predecessor p)?"""

        if p is None:
            return not self.ancestors[g]
        for a in self._ref_groups(volume_id):
            if a == g:
                continue
            # Forced before g (and therefore before the print of g):
            if a == p or self._path(a, p) or self._path(a, g):
                return False
        return True

    # -- possible forms --------------------------------------------------
    def forms(self, g: str) -> set[str]:
        if g in self._forms:
            return self._forms[g]
        state = self.by_id[g]
        result: set[str] = set()
        if not state.usable:
            result.add(PENDING)
            self._forms[g] = result
            return result

        same = set(self._same_seq_usable(g))
        if any(self._can_be_immediate_pred(g, p) for p in same):
            result.add(IBID)

        preds: list[str | None] = list(self._possible_predecessors(g))
        if not self.ancestors[g]:
            preds.append(None)  # g can be first

        for p in preds:
            if p is not None and p in same:
                continue  # that predecessor yields 同上
            v = state.single_volume
            if v is None or self.collide.get(v, True):
                result.add(FULL)
                continue
            if p is None:
                result.add(FULL)  # nothing before g yet
                continue
            if self._appeared_true_feasible(g, p, v):
                result.add(SHORT)
            if self._appeared_false_feasible(g, p, v):
                result.add(FULL)

        if not result:  # pragma: no cover - a usable group always has a form
            result.add(FULL)
        self._forms[g] = result
        return result

    # -- concrete orders -------------------------------------------------
    def _check_order(self, order: Sequence[str]) -> bool:
        if len(order) != len(self.nodes) or set(order) != set(self.nodes):
            return False
        pos = {n: i for i, n in enumerate(order)}
        for a, b in self.edges:
            if pos[a] >= pos[b]:
                return False
        return True

    def _try_build(self, g: str, p: str | None, a: str | None) -> list[str] | None:
        """Build a full linear extension with ``p`` immediately before ``g``
        and ``a`` appearing somewhere earlier than ``g``."""

        if p is not None:
            if not self._can_be_immediate_pred(g, p):
                return None
        else:
            if self.ancestors[g]:
                return None  # g cannot be first

        if a is not None:
            if a == g or self._path(g, a):
                return None
            if p is None:
                return None  # g is first, nothing can appear earlier
            if a != p and self._path(p, a):
                return None  # a forced after p but must precede g

        base: set[str] = set(self.ancestors[g])
        if p is not None:
            base |= self.ancestors[p]
            base.add(p)
        if a is not None:
            base |= self.ancestors[a]
            base.add(a)
        base.discard(g)

        rest = set(base)
        if p is not None:
            rest.discard(p)
            # p must be the last element before g; it cannot force any node
            # that is placed before it.
            if self.reach.get(p, set()) & rest:
                return None

        ordered = _topo_sort(rest, self.edges, self.positions)
        if p is not None:
            ordered.append(p)
        ordered.append(g)
        remaining = set(self.nodes) - set(ordered)
        ordered.extend(_topo_sort(remaining, self.edges, self.positions))
        return ordered if self._check_order(ordered) else None

    def print_order(self, order: Sequence[str]) -> dict[str, str]:
        key = tuple(order)
        cached = self._print_cache.get(key)
        if cached is not None:
            return dict(cached)
        appeared: set[str] = set()
        prev: str | None = None
        out: dict[str, str] = {}
        for gid in order:
            state = self.by_id[gid]
            if not state.usable:
                out[gid] = PENDING
                prev = gid  # breaks 同上 adjacency; volumes still appeared
                appeared.update(state.volumes)
                continue
            if (
                prev is not None
                and self.by_id[prev].usable
                and self.by_id[prev].seq == state.seq
            ):
                form = IBID
            else:
                v = state.single_volume
                if v is not None and v in appeared and not self.collide.get(v, True):
                    form = SHORT
                else:
                    form = FULL
            out[gid] = form
            appeared.update(state.volumes)
            prev = gid
        self._print_cache[key] = dict(out)
        return out

    def witness(self, g: str, form: str) -> list[str]:
        """Build a full-chapter order that makes ``g`` print ``form``."""

        if form == PENDING:
            order = _topo_sort(set(self.nodes), self.edges, self.positions)
            if self.print_order(order)[g] == form:
                return order

        same = set(self._same_seq_usable(g))
        candidates: list[tuple[str | None, str | None]] = []

        if form == IBID:
            for h in sorted(same, key=lambda x: self.positions[x]):
                candidates.append((h, None))
        elif form == SHORT:
            v = self.by_id[g].single_volume
            refs = self._ref_groups(v) if v is not None else []
            for p in self._possible_predecessors(g):
                if p in same:
                    continue
                for a in refs:
                    if a == g:
                        continue
                    candidates.append((p, a))
        elif form == FULL:
            preds: list[str | None] = list(self._possible_predecessors(g))
            if not self.ancestors[g]:
                preds.append(None)
            for p in preds:
                if p is not None and p in same:
                    continue
                candidates.append((p, None))

        for p, a in candidates:
            order = self._try_build(g, p, a)
            if order is not None and self.print_order(order)[g] == form:
                return order
        raise OperationRejected(
            ORDER_CYCLE,  # pragma: no cover - a witnessed form must be realisable
            f"internal: could not construct a witness for {g} -> {form}",
            details={"group_id": g, "form": form},
        )

    # -- rendering -------------------------------------------------------
    def render(self, g: str, form: str) -> dict[str, Any]:
        state = self.by_id[g]
        if form in RENDER:
            return {"form": form, "render": RENDER[form], "entries": []}
        entries = []
        for page in state.pages:
            vid = page["volume_id"]
            name = self.names.get(vid)
            if name is None:
                raise OperationRejected(
                    MISSING_VOLUME_NAME,
                    f"volume {vid!r} has no full/short print name",
                    details={"volume_id": vid},
                )
            entries.append(
                {
                    "volume_id": vid,
                    "page_id": page["page_id"],
                    "section_id": page["section_id"],
                    "label": page["label"],
                    "name": name.short_name if form == SHORT else name.full_name,
                    "name_kind": "short" if form == SHORT else "full",
                }
            )
        if form == SHORT:
            render = f"{entries[0]['name']}：" + ",".join(e["label"] for e in entries)
        else:
            by_volume: dict[str, list[str]] = {}
            order: list[str] = []
            for e in entries:
                if e["volume_id"] not in by_volume:
                    by_volume[e["volume_id"]] = []
                    order.append(e["volume_id"])
                by_volume[e["volume_id"]].append(e["label"])
            render = "；".join(
                f"{next(x['name'] for x in entries if x['volume_id'] == vid)}："
                + ",".join(by_volume[vid])
                for vid in order
            )
        return {"form": form, "render": render, "entries": entries}

    # -- public result ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        groups_out = []
        for state in sorted(self.groups, key=lambda g: g.position):
            g = state.group_id
            forms = sorted(self.forms(g))
            deterministic = len(forms) == 1
            item: dict[str, Any] = {
                "group_id": g,
                "position": state.position,
                "usable": state.usable,
                "sequence": [list(x) for x in state.seq],
                "pages": list(state.pages),
                "volumes": list(state.volumes),
                "forms": forms,
                "deterministic": deterministic,
                "can_plate": deterministic,
            }
            if deterministic:
                item["print"] = self.render(g, forms[0])
            else:
                witnesses = []
                for form in forms[:2]:
                    order = self.witness(g, form)
                    witnesses.append(
                        {
                            "order": order,
                            "prints": self.print_order(order),
                            "this_group": self.render(g, form),
                        }
                    )
                item["witnesses"] = witnesses
            groups_out.append(item)
        return {
            "chapter_id": self.chapter.chapter_id,
            "chapter_order": self.chapter.chapter_order,
            "title": self.chapter.title,
            "claims": [
                {
                    "claim_id": c.claim_id,
                    "from_group": c.from_group,
                    "to_group": c.to_group,
                    "desk_id": c.desk_id,
                }
                for c in sorted(self.claims, key=lambda c: c.claim_id)
            ],
            "groups": groups_out,
            "all_deterministic": all(item["deterministic"] for item in groups_out),
        }


# ----------------------------------------------------------------------
# persistence / writer
# ----------------------------------------------------------------------
class ProofDesk:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        db.ensure_proof_schema(conn)

    # -- volume names ----------------------------------------------------
    def set_volume_names(self, volume_id: str, full_name: str, short_name: str) -> None:
        if not isinstance(full_name, str) or not full_name.strip():
            raise OperationRejected(VOLUME_NAME_INVALID, "full_name must be a non-empty string")
        if not isinstance(short_name, str) or not short_name.strip():
            raise OperationRejected(VOLUME_NAME_INVALID, "short_name must be a non-empty string")
        row = self.conn.execute(
            "SELECT 1 FROM volumes WHERE volume_id = ?", (volume_id,)
        ).fetchone()
        if row is None:
            raise OperationRejected(
                VOLUME_NOT_FOUND, f"volume {volume_id!r} does not exist",
                details={"volume_id": volume_id},
            )
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO volume_names (volume_id, full_name, short_name) "
                "VALUES (?, ?, ?) ON CONFLICT(volume_id) DO UPDATE SET "
                "full_name = excluded.full_name, short_name = excluded.short_name",
                (volume_id, full_name, short_name),
            )
            db.bump_proof_revision(self.conn)

    def volume_names(self) -> dict[str, VolumeName]:
        rows = self.conn.execute("SELECT * FROM volume_names ORDER BY volume_id").fetchall()
        return {
            r["volume_id"]: VolumeName(r["volume_id"], r["full_name"], r["short_name"])
            for r in rows
        }

    # -- chapters --------------------------------------------------------
    def create_chapter(self, chapter_id: str, title: str | None = None) -> Chapter:
        if not isinstance(chapter_id, str) or not chapter_id:
            raise OperationRejected(INVALID_GROUP, "chapter_id must be a non-empty string")
        if self._chapter_row(chapter_id) is not None:
            raise OperationRejected(CHAPTER_EXISTS, f"chapter {chapter_id!r} already exists")
        row = self.conn.execute(
            "SELECT COALESCE(MAX(chapter_order), -1) + 1 AS n FROM proof_chapters"
        ).fetchone()
        order = int(row["n"])
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO proof_chapters (chapter_id, chapter_order, title, created_revision) "
                "VALUES (?, ?, ?, ?)",
                (chapter_id, order, title, db.catalog_revision(self.conn)),
            )
            db.bump_proof_revision(self.conn)
        return Chapter(chapter_id, order, title)

    def _chapter_row(self, chapter_id: str):
        return self.conn.execute(
            "SELECT * FROM proof_chapters WHERE chapter_id = ?", (chapter_id,)
        ).fetchone()

    def chapter(self, chapter_id: str) -> Chapter:
        row = self._chapter_row(chapter_id)
        if row is None:
            raise OperationRejected(
                CHAPTER_NOT_FOUND, f"chapter {chapter_id!r} not found",
                details={"chapter_id": chapter_id},
            )
        return Chapter(row["chapter_id"], int(row["chapter_order"]), row["title"])

    def chapters(self) -> list[Chapter]:
        rows = self.conn.execute(
            "SELECT * FROM proof_chapters ORDER BY chapter_order"
        ).fetchall()
        return [Chapter(r["chapter_id"], int(r["chapter_order"]), r["title"]) for r in rows]

    # -- groups ----------------------------------------------------------
    def add_group(
        self,
        chapter_id: str,
        group_id: str,
        excerpt_ids: Sequence[str],
    ) -> GroupRecord:
        self.chapter(chapter_id)
        if not isinstance(group_id, str) or not group_id:
            raise OperationRejected(INVALID_GROUP, "group_id must be a non-empty string")
        if self._group_row(group_id) is not None:
            raise OperationRejected(INVALID_GROUP, f"group {group_id!r} already exists")
        if not isinstance(excerpt_ids, (list, tuple)) or not excerpt_ids:
            raise OperationRejected(INVALID_GROUP, "a group needs at least one excerpt")
        for cid in excerpt_ids:
            if not isinstance(cid, str) or not cid:
                raise OperationRejected(INVALID_GROUP, "excerpt citation_id must be a string")
            if self.conn.execute(
                "SELECT 1 FROM citations WHERE citation_id = ?", (cid,)
            ).fetchone() is None:
                raise OperationRejected(
                    CITATION_NOT_FOUND, f"citation {cid!r} does not exist",
                    details={"citation_id": cid},
                )
        row = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS n FROM proof_groups WHERE chapter_id = ?",
            (chapter_id,),
        ).fetchone()
        position = int(row["n"])
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO proof_groups (group_id, chapter_id, position, created_revision) "
                "VALUES (?, ?, ?, ?)",
                (group_id, chapter_id, position, db.catalog_revision(self.conn)),
            )
            for seq, cid in enumerate(excerpt_ids):
                self.conn.execute(
                    "INSERT INTO proof_excerpts (group_id, seq, citation_id) VALUES (?, ?, ?)",
                    (group_id, seq, cid),
                )
            db.bump_proof_revision(self.conn)
        return GroupRecord(group_id, chapter_id, position)

    def _group_row(self, group_id: str):
        return self.conn.execute(
            "SELECT * FROM proof_groups WHERE group_id = ?", (group_id,)
        ).fetchone()

    def group(self, group_id: str) -> GroupRecord:
        row = self._group_row(group_id)
        if row is None:
            raise OperationRejected(
                GROUP_NOT_FOUND, f"group {group_id!r} not found",
                details={"group_id": group_id},
            )
        return GroupRecord(row["group_id"], row["chapter_id"], int(row["position"]))

    def groups(self, chapter_id: str) -> list[GroupRecord]:
        rows = self.conn.execute(
            "SELECT * FROM proof_groups WHERE chapter_id = ? ORDER BY position",
            (chapter_id,),
        ).fetchall()
        return [GroupRecord(r["group_id"], r["chapter_id"], int(r["position"])) for r in rows]

    def excerpt_ids(self, group_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT citation_id FROM proof_excerpts WHERE group_id = ? ORDER BY seq",
            (group_id,),
        ).fetchall()
        return [r["citation_id"] for r in rows]

    # -- claims ----------------------------------------------------------
    def _claim_row(self, claim_id: str):
        return self.conn.execute(
            "SELECT * FROM proof_claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()

    def claim(self, claim_id: str) -> Claim:
        row = self._claim_row(claim_id)
        if row is None:
            raise OperationRejected(
                CLAIM_NOT_FOUND, f"claim {claim_id!r} not found",
                details={"claim_id": claim_id},
            )
        return self._row_to_claim(row)

    @staticmethod
    def _row_to_claim(row: sqlite3.Row) -> Claim:
        return Claim(
            row["claim_id"], row["chapter_id"], row["from_group"],
            row["to_group"], row["desk_id"], row["status"], row["retracted_at"],
        )

    def active_claims(self, chapter_id: str) -> list[Claim]:
        rows = self.conn.execute(
            "SELECT * FROM proof_claims WHERE chapter_id = ? AND status = 'active' "
            "ORDER BY claim_id",
            (chapter_id,),
        ).fetchall()
        return [self._row_to_claim(r) for r in rows]

    def all_claims(self, chapter_id: str) -> list[Claim]:
        rows = self.conn.execute(
            "SELECT * FROM proof_claims WHERE chapter_id = ? ORDER BY claim_id",
            (chapter_id,),
        ).fetchall()
        return [self._row_to_claim(r) for r in rows]

    def _validate_edge(self, chapter_id: str, from_group: str, to_group: str) -> None:
        for gid in (from_group, to_group):
            rec = self.group(gid)
            if rec.chapter_id != chapter_id:
                raise OperationRejected(
                    INVALID_CLAIM,
                    f"group {gid!r} is not in chapter {chapter_id!r}",
                    details={"group_id": gid, "chapter_id": chapter_id},
                )
        if from_group == to_group:
            raise OperationRejected(
                INVALID_CLAIM, "a group cannot be ordered before itself",
                details={"group_id": from_group},
            )

    def _insert_claim(
        self, claim_id: str, chapter_id: str, from_group: str, to_group: str, desk_id: str
    ) -> None:
        row = self.conn.execute(
            "SELECT status FROM proof_claims WHERE chapter_id = ? AND from_group = ? "
            "AND to_group = ? AND desk_id = ? AND status = 'active'",
            (chapter_id, from_group, to_group, desk_id),
        ).fetchone()
        if row is not None:
            raise OperationRejected(
                DUPLICATE_CLAIM,
                "this desk already signed this ordering relation",
                details={
                    "chapter_id": chapter_id, "from_group": from_group,
                    "to_group": to_group, "desk_id": desk_id,
                },
            )
        self.conn.execute(
            "INSERT INTO proof_claims "
            "(claim_id, chapter_id, from_group, to_group, desk_id, status, created_revision) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (claim_id, chapter_id, from_group, to_group, desk_id,
             db.catalog_revision(self.conn)),
        )

    def submit_claim(
        self,
        chapter_id: str,
        from_group: str,
        to_group: str,
        desk_id: str,
        *,
        claim_id: str | None = None,
    ) -> Claim:
        self.chapter(chapter_id)
        if not isinstance(desk_id, str) or not desk_id:
            raise OperationRejected(INVALID_CLAIM, "desk_id must be a non-empty string")
        self._validate_edge(chapter_id, from_group, to_group)
        cid = claim_id or f"{chapter_id}:{desk_id}:{from_group}->{to_group}"
        if self._claim_row(cid) is not None:
            raise OperationRejected(
                DUPLICATE_CLAIM, f"claim {cid!r} already exists",
                details={"claim_id": cid},
            )
        existing = [(c.from_group, c.to_group) for c in self.active_claims(chapter_id)]
        cycle = _find_cycle(
            [g.group_id for g in self.groups(chapter_id)],
            existing + [(from_group, to_group)],
        )
        if cycle:
            raise OperationRejected(
                ORDER_CYCLE, "signed relation would create a cycle",
                details={"cycle": cycle},
            )
        with db.write_transaction(self.conn):
            self._insert_claim(cid, chapter_id, from_group, to_group, desk_id)
            db.bump_proof_revision(self.conn)
        return self.claim(cid)

    def retract_claim(self, claim_id: str) -> Claim:
        row = self._claim_row(claim_id)
        if row is None:
            raise OperationRejected(
                CLAIM_NOT_FOUND, f"claim {claim_id!r} not found",
                details={"claim_id": claim_id},
            )
        if row["status"] == "retracted":
            raise OperationRejected(
                CLAIM_ALREADY_RETRACTED, f"claim {claim_id!r} is already retracted",
                details={"claim_id": claim_id},
            )
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with db.write_transaction(self.conn):
            self.conn.execute(
                "UPDATE proof_claims SET status = 'retracted', retracted_at = ? "
                "WHERE claim_id = ?",
                (now, claim_id),
            )
            db.bump_proof_revision(self.conn)
        return self.claim(claim_id)

    def correct_claim(
        self,
        original_claim_id: str,
        new_from: str,
        new_to: str,
        desk_id: str,
        *,
        correction_id: str | None = None,
        reason: str | None = None,
    ) -> Claim:
        return self.apply_corrections(
            [
                {
                    "correction_id": correction_id,
                    "original_claim_id": original_claim_id,
                    "from_group": new_from,
                    "to_group": new_to,
                    "desk_id": desk_id,
                    "reason": reason,
                }
            ]
        )[0]

    def apply_corrections(self, corrections: Sequence[dict[str, Any]]) -> list[Claim]:
        """Correct several relations as one atomic batch.

        Every correction must point at the original claim it replaces.  The
        batch is rejected entirely if the resulting active graph would contain
        a cycle, and one concrete contradiction chain is reported.
        """

        if not corrections:
            raise OperationRejected(INVALID_CORRECTION, "no corrections supplied")
        prepared: list[tuple[str, Claim, str, str, str, dict[str, Any]]] = []
        seen_old: set[str] = set()
        for c in corrections:
            old_id = c.get("original_claim_id")
            if not isinstance(old_id, str) or not old_id:
                raise OperationRejected(
                    INVALID_CORRECTION, "a correction must name original_claim_id"
                )
            old = self.claim(old_id)
            if old.status != "active":
                raise OperationRejected(
                    CLAIM_ALREADY_RETRACTED,
                    f"claim {old_id!r} is already retracted",
                    details={"claim_id": old_id},
                )
            if old_id in seen_old:
                raise OperationRejected(
                    INVALID_CORRECTION,
                    f"claim {old_id!r} corrected twice in one batch",
                    details={"claim_id": old_id},
                )
            seen_old.add(old_id)
            new_from = c.get("from_group", old.from_group)
            new_to = c.get("to_group", old.to_group)
            desk_id = c.get("desk_id", old.desk_id)
            if not isinstance(desk_id, str) or not desk_id:
                raise OperationRejected(INVALID_CORRECTION, "desk_id must be a string")
            self._validate_edge(old.chapter_id, new_from, new_to)
            corr_id = c.get("correction_id") or f"corr:{old_id}:{new_from}->{new_to}"
            if self.conn.execute(
                "SELECT 1 FROM proof_corrections WHERE correction_id = ?", (corr_id,)
            ).fetchone():
                raise OperationRejected(
                    INVALID_CORRECTION, f"correction {corr_id!r} already exists",
                    details={"correction_id": corr_id},
                )
            prepared.append((old_id, old, new_from, new_to, desk_id, c))

        # Simulate the final active edge set before writing anything.
        removed = {old_id for old_id, *_ in prepared}
        chapters = sorted({old.chapter_id for _, old, *_ in prepared})
        for chapter_id in chapters:
            survivors = [
                (c.from_group, c.to_group)
                for c in self.active_claims(chapter_id)
                if c.claim_id not in removed
            ]
            added = [
                (nf, nt) for _, old, nf, nt, _, _ in prepared
                if old.chapter_id == chapter_id
            ]
            cycle = _find_cycle(
                [g.group_id for g in self.groups(chapter_id)],
                survivors + added,
            )
            if cycle:
                raise OperationRejected(
                    ORDER_CYCLE,
                    "this correction batch would create a cycle; rejected as a whole",
                    details={
                        "chapter_id": chapter_id,
                        "cycle": cycle,
                        "rejected_corrections": sorted(removed),
                    },
                )

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        new_claims: list[Claim] = []
        with db.write_transaction(self.conn):
            for old_id, old, new_from, new_to, desk_id, raw in prepared:
                self.conn.execute(
                    "UPDATE proof_claims SET status = 'retracted', retracted_at = ? "
                    "WHERE claim_id = ?",
                    (now, old_id),
                )
                new_cid = f"{old_id}~new"
                suffix = 1
                while self._claim_row(new_cid) is not None:
                    suffix += 1
                    new_cid = f"{old_id}~new{suffix}"
                self._insert_claim(new_cid, old.chapter_id, new_from, new_to, desk_id)
                corr_id = raw.get("correction_id") or (
                    f"corr:{old_id}:{new_from}->{new_to}"
                )
                self.conn.execute(
                    "INSERT INTO proof_corrections "
                    "(correction_id, claim_id, new_claim_id, desk_id, reason, created_revision) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (corr_id, old_id, new_cid, desk_id, raw.get("reason"),
                     db.catalog_revision(self.conn)),
                )
                new_claims.append(
                    Claim(new_cid, old.chapter_id, new_from, new_to, desk_id, "active", None)
                )
            db.bump_proof_revision(self.conn)
        return new_claims

    def corrections(self, chapter_id: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT c.* FROM proof_corrections c "
            "JOIN proof_claims p ON p.claim_id = c.claim_id "
        )
        params: tuple[Any, ...] = ()
        if chapter_id is not None:
            sql += "WHERE p.chapter_id = ? "
            params = (chapter_id,)
        sql += "ORDER BY c.correction_id"
        return [dict(r) for r in self.conn.execute(sql, params)]

    # -- excerpt binding correction -------------------------------------
    def correct_excerpt(
        self,
        group_id: str,
        seq: int,
        new_citation_id: str,
        desk_id: str,
        *,
        correction_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.group(group_id)
        row = self.conn.execute(
            "SELECT citation_id FROM proof_excerpts WHERE group_id = ? AND seq = ?",
            (group_id, seq),
        ).fetchone()
        if row is None:
            raise OperationRejected(
                INVALID_CORRECTION,
                f"group {group_id!r} has no excerpt at position {seq}",
                details={"group_id": group_id, "seq": seq},
            )
        old = row["citation_id"]
        if self.conn.execute(
            "SELECT 1 FROM citations WHERE citation_id = ?", (new_citation_id,)
        ).fetchone() is None:
            raise OperationRejected(
                CITATION_NOT_FOUND,
                f"citation {new_citation_id!r} does not exist",
                details={"citation_id": new_citation_id},
            )
        cid = correction_id or f"corr-excerpt:{group_id}:{seq}:{old}->{new_citation_id}"
        if correction_id is None:
            base, suffix = cid, 1
            while self.conn.execute(
                "SELECT 1 FROM proof_excerpt_corrections WHERE correction_id = ?", (cid,)
            ).fetchone():
                suffix += 1
                cid = f"{base}#{suffix}"
        elif self.conn.execute(
            "SELECT 1 FROM proof_excerpt_corrections WHERE correction_id = ?", (cid,)
        ).fetchone():
            raise OperationRejected(
                INVALID_CORRECTION, f"correction {cid!r} already exists",
                details={"correction_id": cid},
            )
        with db.write_transaction(self.conn):
            self.conn.execute(
                "UPDATE proof_excerpts SET citation_id = ? WHERE group_id = ? AND seq = ?",
                (new_citation_id, group_id, seq),
            )
            self.conn.execute(
                "INSERT INTO proof_excerpt_corrections "
                "(correction_id, group_id, seq, old_citation_id, new_citation_id, "
                " desk_id, reason, created_revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, group_id, seq, old, new_citation_id, desk_id, reason,
                 db.catalog_revision(self.conn)),
            )
            db.bump_proof_revision(self.conn)
        return {
            "correction_id": cid, "group_id": group_id, "seq": seq,
            "old_citation_id": old, "new_citation_id": new_citation_id,
        }

    def excerpt_corrections(self, group_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM proof_excerpt_corrections WHERE group_id = ? "
            "ORDER BY correction_id",
            (group_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- expansion and analysis -----------------------------------------
    def _expand_group(
        self,
        group_id: str,
        chapter_position: int,
        evaluate: dict[str, dict[str, Any]],
    ) -> GroupState:
        excerpt_ids = self.excerpt_ids(group_id)
        seen: set[tuple[str, str]] = set()
        seq: list[tuple[str, str]] = []
        pages: list[dict[str, Any]] = []
        usable = True
        for cid in excerpt_ids:
            product = evaluate[cid]
            if product["status"] != "usable":
                usable = False
            for page in product["pages"]:
                key = (page["volume_id"], page["page_id"])
                # De-duplicate by complete (volume, stable page), preserving
                # first appearance across the whole group.
                if key in seen:
                    continue
                seen.add(key)
                seq.append(key)
                pages.append(dict(page))
        volumes: list[str] = []
        for vid, _pid in seq:
            if vid not in volumes:
                volumes.append(vid)
        return GroupState(group_id, chapter_position, tuple(excerpt_ids),
                          usable, tuple(seq), tuple(pages), tuple(volumes))

    def _states_from_snapshot(
        self, chapter_id: str, snap: export.Snapshot
    ) -> list[GroupState]:
        evaluate = {
            c["citation_id"]: export.evaluate_citation(snap, c)
            for c in snap.citations
        }
        return [
            self._expand_group(rec.group_id, rec.position, evaluate)
            for rec in self.groups(chapter_id)
        ]

    def analyze_chapter(self, chapter_id: str) -> ChapterAnalysis:
        chapter = self.chapter(chapter_id)
        with db.read_snapshot_transaction(self.conn):
            snap = export.load_snapshot(self.conn)
            states = self._states_from_snapshot(chapter_id, snap)
        return ChapterAnalysis(chapter, states, self.active_claims(chapter_id),
                               self.volume_names())

    def chapter_report(self, chapter_id: str) -> dict[str, Any]:
        return self.analyze_chapter(chapter_id).to_dict()

    # -- handover --------------------------------------------------------
    def issue_handover(self, chapter_id: str, batch_id: str) -> dict[str, Any]:
        chapter = self.chapter(chapter_id)
        if not isinstance(batch_id, str) or not batch_id:
            raise OperationRejected(HANDOVER_INVALID, "batch_id must be a non-empty string")
        if self.conn.execute(
            "SELECT 1 FROM proof_handover_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone():
            raise OperationRejected(
                BATCH_EXISTS, f"handover batch {batch_id!r} already exists",
                details={"batch_id": batch_id},
            )
        with db.read_snapshot_transaction(self.conn):
            snap = export.load_snapshot(self.conn)
            states = self._states_from_snapshot(chapter_id, snap)
            catalog_revision = snap.revision
            active = self.active_claims(chapter_id)
            analysis = ChapterAnalysis(
                chapter, states, active, self.volume_names()
            ).to_dict()
            payload = {
                "format": FORMAT_HANDOVER,
                "batch_id": batch_id,
                "issued_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "proof_revision": db.proof_revision(self.conn),
                "catalog_revision": catalog_revision,
                "chapter": {
                    "chapter_id": chapter.chapter_id,
                    "chapter_order": chapter.chapter_order,
                    "title": chapter.title,
                },
                "volume_names": {
                    vid: {"full_name": n.full_name, "short_name": n.short_name}
                    for vid, n in sorted(self.volume_names().items())
                },
                "groups": [
                    {
                        "group_id": st.group_id,
                        "position": st.position,
                        "excerpt_ids": list(st.excerpt_ids),
                        "usable": st.usable,
                        "seq": [list(x) for x in st.seq],
                        "pages": list(st.pages),
                        "volumes": list(st.volumes),
                    }
                    for st in sorted(states, key=lambda s: s.position)
                ],
                "claims": [
                    {
                        "claim_id": c.claim_id, "from_group": c.from_group,
                        "to_group": c.to_group, "desk_id": c.desk_id,
                        "status": c.status,
                    }
                    for c in active
                ],
                "analysis": analysis,
            }
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO proof_handover_batches "
                "(batch_id, catalog_revision, issued_at, content_json) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, catalog_revision, payload["issued_at"], content),
            )
            db.bump_proof_revision(self.conn)
        return payload

    def handover_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM proof_handover_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise OperationRejected(
                HANDOVER_INVALID, f"handover batch {batch_id!r} not found",
                details={"batch_id": batch_id},
            )
        return json.loads(row["content_json"])

    def handover_batches(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM proof_handover_batches ORDER BY issued_at, batch_id"
        ).fetchall()
        return [
            {
                "batch_id": r["batch_id"],
                "catalog_revision": r["catalog_revision"],
                "issued_at": r["issued_at"],
            }
            for r in rows
        ]


# ----------------------------------------------------------------------
# offline re-open of an issued handover package
# ----------------------------------------------------------------------
def open_handover_bundle(payload: dict[str, Any]) -> ChapterAnalysis:
    """Recompute a chapter pre-review from a bundle, without a database.

    The bundle is fully self-contained (expanded pages, usability, signed
    claims and volume names), so an old package can be re-opened offline and
    reproduces the same per-group result.
    """

    if not isinstance(payload, dict) or payload.get("format") != FORMAT_HANDOVER:
        raise OperationRejected(
            HANDOVER_INVALID, "unsupported handover package format",
            details={"format": (payload or {}).get("format")},
        )
    try:
        ch = payload["chapter"]
        names = {
            vid: VolumeName(vid, n["full_name"], n["short_name"])
            for vid, n in payload["volume_names"].items()
        }
        chapter = Chapter(ch["chapter_id"], int(ch.get("chapter_order", 0)), ch.get("title"))
        states = [
            GroupState(
                group_id=g["group_id"],
                position=int(g["position"]),
                excerpt_ids=tuple(g.get("excerpt_ids", [])),
                usable=bool(g["usable"]),
                seq=tuple((v, p) for v, p in g.get("seq", [])),
                pages=tuple(dict(p) for p in g.get("pages", [])),
                volumes=tuple(g.get("volumes", [])),
            )
            for g in payload["groups"]
        ]
        claims = [
            Claim(
                c["claim_id"], chapter.chapter_id, c["from_group"], c["to_group"],
                c["desk_id"], c.get("status", "active"), None,
            )
            for c in payload.get("claims", [])
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise OperationRejected(
            HANDOVER_INVALID, f"malformed handover package: {exc}"
        ) from None
    return ChapterAnalysis(chapter, states, claims, names)

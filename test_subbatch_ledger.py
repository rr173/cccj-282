from __future__ import annotations

import unittest

from subbatch_ledger import (
    INSUFFICIENT_EVIDENCE,
    REJECTED,
    RELEASED,
    ReleaseLedger,
    RevisionRejected,
    run_request,
)


def spec(sid: str, packages: list[str] | frozenset[str], **overrides):
    base = {
        "subbatch_id": sid,
        "sampling_frame": list(packages),
        "propagation_threshold": 1,
        "rejection_threshold": 1,
        "required_coverage": len(packages),
        "max_ordinary_defects": 0,
        "frame_complete": True,
    }
    base.update(overrides)
    return base


def item(iid: str, package: str, result: str):
    return {"id": iid, "package_id": package, "result": result}


class LedgerRuleTests(unittest.TestCase):
    def test_coverage_and_ordinary_defect_deduplicate_by_package(self):
        ledger = ReleaseLedger(
            subbatches=[spec("A", ["p1", "p2"], required_coverage=2, max_ordinary_defects=1)],
            initial_items=[
                item("i1", "p1", "normal"),
                item("i2", "p2", "ordinary_defect"),
                item("i3", "p2", "ordinary_defect"),
            ],
        )
        view = ledger.query(1, "A")
        self.assertEqual(view.decision, RELEASED)
        self.assertEqual(view.coverage, 2)
        self.assertEqual(view.ordinary_defects, 1)
        self.assertEqual(view.ordinary_defect_packages[0].package_id, "p2")
        self.assertEqual(
            {x.item_id for x in view.ordinary_defect_packages[0].ordinary_items},
            {"i2", "i3"},
        )

        strict = ReleaseLedger(
            subbatches=[spec("A", ["p1", "p2"], required_coverage=2, max_ordinary_defects=0)],
            initial_items=[item("i1", "p1", "normal"), item("i2", "p2", "ordinary_defect")],
        )
        self.assertEqual(strict.query(1, "A").decision, INSUFFICIENT_EVIDENCE)

    def test_rejection_threshold_uses_held_deduplicated_severe_packages(self):
        ledger = ReleaseLedger(
            subbatches=[spec("A", ["p1"], rejection_threshold=1)],
            initial_items=[item("i1", "p1", "severe_defect"), item("i2", "p1", "severe_defect")],
        )
        view = ledger.query(1, "A")
        self.assertEqual(view.decision, REJECTED)
        self.assertEqual(view.severe_evidence_count, 1)
        self.assertEqual(len(view.severe_packages), 1)
        self.assertEqual({x.item_id for x in view.severe_packages[0].severe_items}, {"i1", "i2"})

    def test_propagation_audit_and_withdrawal_changes_downstream(self):
        ledger = ReleaseLedger(
            subbatches=[
                spec("A", ["pA"]),
                spec("B", ["pB"]),
            ],
            edges=[("A", "B")],
            initial_items=[item("iA", "pA", "severe_defect"), item("iB", "pB", "normal")],
        )
        old_b = ledger.query_dict(1, "B")
        self.assertEqual(old_b["decision"], REJECTED)
        self.assertEqual(old_b["severe_evidence_count"], 1)
        evidence = old_b["severe_packages"][0]
        self.assertEqual(evidence["package_id"], "pA")
        self.assertEqual(evidence["origin_subbatch_id"], "A")
        self.assertEqual(evidence["severe_items"][0]["item_id"], "iA")
        self.assertEqual(evidence["propagation_edges"], [
            {
                "from_subbatch_id": "A",
                "to_subbatch_id": "B",
                "from_severe_evidence_count": 1,
                "propagation_threshold": 1,
                "threshold_met": True,
            }
        ])

        result = ledger.publish_revision(withdraw=["iA"])
        self.assertEqual(ledger.current_version, 2)
        self.assertEqual(result.affected_subbatches, ("A", "B"))
        self.assertEqual(set(result.decision_changed_subbatches), {"A", "B"})
        self.assertEqual(set(result.severe_basis_changed_subbatches), {"A", "B"})

        new_b = ledger.query(2, "B")
        self.assertEqual(new_b.decision, RELEASED)
        self.assertEqual(new_b.severe_evidence_count, 0)

        # 旧版结果保持不变。
        self.assertEqual(ledger.query_dict(1, "B"), old_b)

    def test_same_package_over_multiple_paths_counts_once(self):
        ledger = ReleaseLedger(
            subbatches=[
                spec("A", ["pA"], required_coverage=0),
                spec("B", [], required_coverage=0),
                spec("C", [], required_coverage=0),
                spec("D", [], required_coverage=0),
            ],
            edges=[("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")],
            initial_items=[item("iA", "pA", "severe_defect")],
        )
        view = ledger.query(1, "D")
        self.assertEqual(view.severe_evidence_count, 1)
        ev = view.severe_packages[0]
        self.assertEqual(ev.direct_predecessors, ("B", "C"))
        self.assertEqual(
            {(e.from_subbatch_id, e.to_subbatch_id) for e in ev.propagation_edges},
            {("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")},
        )

        change = ledger.publish_revision(withdraw=["iA"])
        self.assertEqual(change.version, 2)
        d_change = next(c for c in change.changes if c.subbatch_id == "D")
        self.assertEqual(d_change.severe_basis_before, frozenset({"iA"}))
        self.assertEqual(d_change.severe_basis_after, frozenset())

    def test_cycle_cannot_sustain_withdrawn_evidence(self):
        ledger = ReleaseLedger(
            subbatches=[spec("A", ["pA"], required_coverage=0), spec("B", [], required_coverage=0)],
            edges=[("A", "B"), ("B", "A")],
            initial_items=[item("iA", "pA", "severe_defect")],
        )
        self.assertEqual(ledger.query(1, "A").severe_evidence_count, 1)
        self.assertEqual(ledger.query(1, "B").severe_evidence_count, 1)

        ledger.publish_revision(withdraw=["iA"])
        self.assertEqual(ledger.query(2, "A").severe_evidence_count, 0)
        self.assertEqual(ledger.query(2, "B").severe_evidence_count, 0)
        self.assertEqual(ledger.history["A"][-1].support, {})
        self.assertEqual(ledger.history["B"][-1].support, {})

        empty_cycle = ReleaseLedger(
            subbatches=[spec("A", [], required_coverage=0), spec("B", [], required_coverage=0)],
            edges=[("A", "B"), ("B", "A")],
        )
        self.assertEqual(empty_cycle.query(1, "A").severe_evidence_count, 0)
        self.assertEqual(empty_cycle.query(1, "B").severe_evidence_count, 0)

    def test_revision_only_appends_records_to_affected_nodes(self):
        ledger = ReleaseLedger(
            subbatches=[
                spec("A", ["pA"]),
                spec("B", ["pB"]),
                spec("C", ["pC"]),
            ],
            edges=[("A", "B")],
            initial_items=[item("iC", "pC", "normal")],
        )
        result = ledger.publish_revision(add=[item("iA", "pA", "normal")])
        self.assertEqual(result.affected_subbatches, ("A", "B"))
        self.assertEqual([r.version for r in ledger.history["A"]], [1, 2])
        self.assertEqual([r.version for r in ledger.history["B"]], [1, 2])
        self.assertEqual([r.version for r in ledger.history["C"]], [1])

    def test_affected_region_keeps_boundary_evidence_from_outside(self):
        ledger = ReleaseLedger(
            subbatches=[
                spec("E", ["pE"], required_coverage=1, rejection_threshold=2),
                spec("X", ["pX"], required_coverage=1, rejection_threshold=2, max_ordinary_defects=0),
                spec("Y", [], required_coverage=0, rejection_threshold=2),
            ],
            edges=[("E", "X"), ("X", "Y")],
            initial_items=[
                item("iE", "pE", "severe_defect"),
                item("iX", "pX", "normal"),
            ],
        )
        self.assertEqual(ledger.query(1, "X").decision, RELEASED)
        self.assertEqual(ledger.query(1, "X").severe_evidence_count, 1)

        result = ledger.publish_revision(add=[item("iX2", "pX", "ordinary_defect")])
        self.assertEqual(result.affected_subbatches, ("X", "Y"))
        x_change = next(c for c in result.changes if c.subbatch_id == "X")
        self.assertTrue(x_change.decision_changed)
        self.assertFalse(x_change.severe_basis_changed)

        x_view = ledger.query(2, "X")
        self.assertEqual(x_view.decision, INSUFFICIENT_EVIDENCE)
        self.assertEqual(x_view.severe_evidence_count, 1)
        self.assertEqual(x_view.severe_packages[0].origin_subbatch_id, "E")
        self.assertEqual(
            x_view.severe_packages[0].propagation_edges[0].from_subbatch_id, "E"
        )
        self.assertEqual([r.version for r in ledger.history["E"]], [1])

    def test_withdraw_one_item_keeps_package_basis_from_other_item(self):
        ledger = ReleaseLedger(
            subbatches=[
                spec("A", ["p1"], required_coverage=1, rejection_threshold=2),
                spec("B", [], required_coverage=0, rejection_threshold=2),
            ],
            edges=[("A", "B")],
            initial_items=[
                item("i1", "p1", "severe_defect"),
                item("i2", "p1", "severe_defect"),
            ],
        )
        result = ledger.publish_revision(withdraw=["i1"])
        a_change = next(c for c in result.changes if c.subbatch_id == "A")
        self.assertFalse(a_change.decision_changed)
        self.assertTrue(a_change.severe_basis_changed)
        self.assertEqual(a_change.severe_basis_before, frozenset({"i1", "i2"}))
        self.assertEqual(a_change.severe_basis_after, frozenset({"i2"}))
        self.assertEqual(ledger.query(2, "A").severe_evidence_count, 1)
        self.assertEqual(
            {i.item_id for i in ledger.query(2, "B").severe_packages[0].severe_items},
            {"i2"},
        )

    def test_edge_order_does_not_change_basis_or_audit(self):
        edges = [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")]
        kwargs = dict(
            subbatches=[
                spec("A", ["pA"], required_coverage=0),
                spec("B", [], required_coverage=0),
                spec("C", [], required_coverage=0),
                spec("D", [], required_coverage=0),
            ],
            initial_items=[item("iA", "pA", "severe_defect")],
        )
        first = ReleaseLedger(edges=edges, **kwargs).query_dict(1, "D")
        second = ReleaseLedger(edges=list(reversed(edges)), **kwargs).query_dict(1, "D")
        self.assertEqual(first, second)

    def test_invalid_revision_is_atomic_and_previous_version_queryable(self):
        ledger = ReleaseLedger(
            subbatches=[spec("A", ["p1"])],
            initial_items=[item("i1", "p1", "normal")],
        )
        bad = ledger.try_publish_revision(
            add=[item("i2", "p1", "normal")],
            withdraw=["missing"],
        )
        self.assertFalse(bad.accepted)
        self.assertEqual(bad.version, 1)
        self.assertEqual(ledger.current_version, 1)
        self.assertNotIn("i2", ledger.items)
        self.assertEqual(ledger.query(1, "A").coverage, 1)

        with self.assertRaises(RevisionRejected):
            ledger.publish_revision(withdraw=["also-missing"])

        ok = ledger.publish_revision(withdraw=["i1"])
        self.assertTrue(ok.published)
        self.assertEqual(ledger.current_version, 2)
        bad_again = ledger.try_publish_revision(withdraw=["i1"])
        self.assertFalse(bad_again.accepted)
        self.assertEqual(ledger.current_version, 2)

    def test_frame_incomplete_blocks_release(self):
        ledger = ReleaseLedger(
            subbatches=[spec("A", ["p1"], frame_complete=False)],
            initial_items=[item("i1", "p1", "normal")],
        )
        view = ledger.query(1, "A")
        self.assertFalse(view.frame_complete)
        self.assertEqual(view.decision, INSUFFICIENT_EVIDENCE)

    def test_run_request_end_to_end(self):
        request = {
            "subbatches": [
                spec("A", ["pA"]),
                spec("B", ["pB"]),
            ],
            "edges": [{"from": "A", "to": "B"}],
            "initial_items": [item("iA", "pA", "severe_defect"), item("iB", "pB", "normal")],
            "revisions": [
                {"withdraw": ["iA"]},
                {"add": [item("iC", "pA", "normal")], "withdraw": ["missing"]},
            ],
            "queries": [
                {"version": 1, "subbatch_id": "B"},
                {"version": 2, "subbatch_id": "B"},
            ],
        }
        output = run_request(request)
        self.assertEqual(output["current_version"], 2)
        self.assertTrue(output["publish_results"][1]["accepted"])
        self.assertFalse(output["publish_results"][2]["accepted"])
        self.assertTrue(output["query_results"][0]["ok"])
        self.assertEqual(output["query_results"][0]["result"]["decision"], REJECTED)
        self.assertEqual(output["query_results"][1]["result"]["decision"], RELEASED)


if __name__ == "__main__":
    unittest.main()

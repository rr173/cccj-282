from __future__ import annotations

import random

from subbatch_ledger import (
    NewInspection,
    ReleaseLedger,
    SamplingFrame,
    SamplingPlan,
    SubbatchSpec,
)


def make_specs(rng: random.Random, n: int):
    specs = []
    packages = {}
    for i in range(n):
        sid = f"S{i}"
        pkgs = [f"{sid}-P{j}" for j in range(rng.randrange(0, 4))]
        packages[sid] = pkgs
        frame = SamplingFrame(frozenset(pkgs), complete=rng.random() > 0.15)
        plan = SamplingPlan(
            propagation_threshold=rng.randrange(1, 4),
            rejection_threshold=rng.randrange(1, 4),
            required_coverage=rng.randrange(0, max(1, len(pkgs)) + 1),
            max_ordinary_defects=rng.randrange(0, 3),
        )
        specs.append(SubbatchSpec(sid, plan, frame))
    return specs, packages


def main(seed: int = 20261125, trials: int = 200) -> None:
    rng = random.Random(seed)
    results = ["normal", "ordinary_defect", "severe_defect"]
    for trial in range(trials):
        n = rng.randrange(1, 7)
        specs, packages = make_specs(rng, n)
        ids = [s.subbatch_id for s in specs]
        edges = []
        for a in ids:
            for b in ids:
                if rng.random() < 0.18:
                    edges.append((a, b))

        item_seq = 0
        initial = []
        for sid, pkgs in packages.items():
            for pkg in pkgs:
                for _ in range(rng.randrange(0, 4)):
                    item_seq += 1
                    initial.append(NewInspection(f"I{item_seq}", pkg, rng.choice(results)))

        ledger = ReleaseLedger(specs, edges, initial)

        for step in range(25):
            add = []
            for _ in range(rng.randrange(0, 4)):
                if not any(packages.values()):
                    continue
                sid = rng.choice(ids)
                if not packages[sid]:
                    continue
                item_seq += 1
                add.append(NewInspection(f"I{item_seq}", rng.choice(packages[sid]), rng.choice(results)))

            valid_ids = [iid for iid in ledger.items if iid not in ledger.withdrawn_at]
            withdraw = rng.sample(valid_ids, k=min(len(valid_ids), rng.randrange(0, 4)))
            if not add and not withdraw:
                continue

            pub = ledger.publish_revision(add=add, withdraw=withdraw)
            assert pub.accepted

            valid_items = [
                NewInspection(item.item_id, item.package_id, item.result)
                for item in ledger.items.values()
                if item.item_id not in ledger.withdrawn_at
            ]
            fresh = ReleaseLedger(specs, edges, valid_items)
            assert fresh.current_version == 1

            version = ledger.current_version
            for sid in ids:
                inc = ledger._record_at(sid, version)
                ref = fresh._record_at(sid, 1)
                fields = [
                    "decision",
                    "coverage",
                    "ordinary_defects",
                    "severe_packages",
                    "covered_packages",
                    "local_severe_items",
                    "local_ordinary_items",
                    "support",
                ]
                for field in fields:
                    a = getattr(inc, field)
                    b = getattr(ref, field)
                    if a != b:
                        raise AssertionError(
                            f"trial={trial} step={step} sid={sid} field={field}\n"
                            f"incremental={a!r}\nfresh={b!r}\n"
                            f"publish={pub}\nedges={edges}"
                        )

    print(f"differential check passed: {trials} random ledgers x 25 revisions")


if __name__ == "__main__":
    main()

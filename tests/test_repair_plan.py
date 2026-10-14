import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import repair_contaminated_events as repair
from src.storage import EventStore


class RepairPlanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store_path = self.root / "events.jsonl"
        self.plan_path = self.root / "repair-plan.json"
        self.old = [{"event_uid": uid, "cable_name": "WACS", "original_text": "you might also like WACS outage", "evidence_urls": ["https://example.com/old/" + uid]} for uid in ("old-a", "old-b")]
        self.normal = {"event_uid": "normal-wacs", "cable_name": "WACS", "occurrence_date": "2026-02-01", "original_text": "A confirmed WACS cable fault affected South Africa.", "evidence_urls": ["https://example.com/wacs-b", "https://example.com/wacs-a"]}
        self.normal_line = (json.dumps(self.normal, ensure_ascii=False, separators=(", ", " : ")) + "\n").encode()
        self.store_path.write_bytes(repair.jsonl_bytes(self.old) + self.normal_line)
        self.candidate = {"cable_name": "C2C", "occurrence_date": "2026-10-02", "repair_date": "2026-10-03", "title": "C2C submarine cable outage restored", "url": "https://example.com/c2c", "source": "fixture", "original_text": "The C2C submarine cable outage affected Philippines-Singapore services.", "affected_area": "Philippines-Singapore", "reason": "unknown", "verification_status": "source_reviewed"}

    def write_plan(self, candidates=None):
        candidates = candidates if candidates is not None else [[self.candidate], []]
        plan = {"decisions": [{"old_uid": old["event_uid"], "old_sha256": repair.fingerprint(old), "action": "replace" if new else "quarantine", "new_events": new, "reason": "confirmed_contamination", "evidence_urls": old["evidence_urls"]} for old, new in zip(self.old, candidates)]}
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")
        return plan

    def apply(self, store=None):
        store = store or EventStore(self.store_path)
        with contextlib.redirect_stdout(io.StringIO()):
            repair.apply_plan(store, self.plan_path)
        return store

    def test_every_original_fingerprint_is_checked_before_mutation(self):
        self.write_plan()
        changed = copy.deepcopy(self.old)
        changed[1]["original_text"] += " changed after review"
        before = repair.jsonl_bytes(changed) + self.normal_line
        self.store_path.write_bytes(before)
        with self.assertRaisesRegex(RuntimeError, "Original records changed"):
            self.apply()
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertFalse(self.plan_path.with_name("repair-plan.before.jsonl").exists())

    def test_later_invalid_candidate_does_not_remove_any_originals(self):
        invalid = {**self.candidate, "url": "https://example.com/another", "occurrence_date": "October 2026"}
        self.write_plan([[self.candidate], [invalid]])
        before = self.store_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "complete ISO dates"):
            self.apply()
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertFalse(self.plan_path.with_name("repair-plan.before.jsonl").exists())

    def test_duplicate_candidates_are_added_once_and_normal_wacs_bytes_survive(self):
        self.write_plan([[self.candidate], [copy.deepcopy(self.candidate)]])
        before = self.store_path.read_bytes()
        with patch.object(EventStore, "upsert", side_effect=AssertionError("fuzzy upsert must not run")):
            store = self.apply()
        self.assertEqual(len(store.events), 2)
        self.assertEqual(store.events[0], self.normal)
        self.assertTrue(self.store_path.read_bytes().startswith(self.normal_line))
        self.assertEqual(self.plan_path.with_name("repair-plan.before.jsonl").read_bytes(), before)
        mappings = [json.loads(line) for line in self.plan_path.with_name("repair-plan.applied.jsonl").read_text().splitlines()]
        self.assertEqual(mappings[0]["new_uids"], mappings[1]["new_uids"])
        self.assertEqual(len(mappings[0]["new_uids"]), 1)

    def test_same_uid_candidates_with_different_facts_are_rejected(self):
        conflicting = {**self.candidate, "reason": "unsupported new cause"}
        self.write_plan([[self.candidate], [conflicting]])
        before = self.store_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Conflicting candidates"):
            self.apply()
        self.assertEqual(self.store_path.read_bytes(), before)

    def test_existing_same_source_cable_requires_explicit_review(self):
        self.write_plan()
        store = EventStore(self.store_path)
        existing = store.normalize_event({**self.candidate, "occurrence_date": "2026-10-01"})
        self.store_path.write_bytes(self.store_path.read_bytes() + repair.jsonl_bytes([existing]))
        before = self.store_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "same-source"):
            self.apply()
        self.assertEqual(self.store_path.read_bytes(), before)

    def test_idempotence_requires_transaction_proof(self):
        self.write_plan()
        self.apply()
        after = self.store_path.read_bytes()
        self.apply()
        self.assertEqual(self.store_path.read_bytes(), after)
        self.plan_path.with_name("repair-plan.transaction.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "Original records changed"):
            self.apply()
        self.assertEqual(self.store_path.read_bytes(), after)

    def test_commit_interruption_recovers_mapping_without_reapplying(self):
        self.write_plan()
        actual_write = repair.write_bytes_atomic

        def interrupted_write(path, payload):
            if str(path).endswith(".applied.jsonl"):
                raise OSError("simulated audit-write interruption")
            return actual_write(path, payload)

        with patch.object(repair, "write_bytes_atomic", side_effect=interrupted_write):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.apply()
        after = self.store_path.read_bytes()
        self.apply()
        self.assertEqual(self.store_path.read_bytes(), after)
        transaction = json.loads(self.plan_path.with_name("repair-plan.transaction.json").read_text())
        self.assertEqual(transaction["state"], "committed")
        self.assertTrue(self.plan_path.with_name("repair-plan.applied.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

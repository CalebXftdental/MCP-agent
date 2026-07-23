"""FilePolicyStore tests: seed, CRUD, persistence, serialization roundtrip.

Run:  python _smoke/test_file_store.py    (writes a temp file under scratch)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "governance_core"))

os.environ["GOVERNANCE_KEY_SEED_AGENT"] = "seed-key-1"

from store.file_store import FilePolicyStore
from store.keys import hash_api_key
from store.models import ConsumerRecord
from policy.categories import Category

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {name}")
    else:
        FAIL += 1; print(f"  FAIL {name}")


tmp = Path(tempfile.mkdtemp()) / "policy.json"

print("seed on first run + persist")
s = FilePolicyStore(str(tmp))
check("file created", tmp.exists())
check("seeded env consumer present", s.get_by_api_key("seed-key-1") is not None)
check("seeded categories present", s.get_category("finance") is not None)
check("writable flag", s.writable is True)

print("create + persist + reload")
rec = ConsumerRecord(consumer_id="c_new", name="new_bot", key_hash=hash_api_key("brand-new-key"),
                     categories=["orders", "accounts"], type="agent", rate_limit_per_hour=42)
s.upsert_consumer(rec)
s2 = FilePolicyStore(str(tmp))  # reload from disk
check("consumer persisted across reload", s2.get_consumer("c_new") is not None)
check("auth works after reload", s2.get_by_api_key("brand-new-key").name == "new_bot")
check("fields survive roundtrip", s2.get_consumer("c_new").rate_limit_per_hour == 42
      and s2.get_consumer("c_new").categories == ["orders", "accounts"])

print("update + delete")
from dataclasses import replace
s2.upsert_consumer(replace(s2.get_consumer("c_new"), status="disabled"))
check("update persisted", FilePolicyStore(str(tmp)).get_consumer("c_new").status == "disabled")
check("disabled consumer not auth'd", FilePolicyStore(str(tmp)).get_by_api_key("brand-new-key") is None)
s2.delete_consumer("c_new")
check("delete persisted", FilePolicyStore(str(tmp)).get_consumer("c_new") is None)

print("category upsert + delete roundtrip")
cat = Category(id="lab", display_name="Lab", backend="minierp_orders",
               tools=frozenset({"get_order_details"}), levels=frozenset({"PUBLIC", "INTERNAL"}),
               data_domains=["X"])
s2.upsert_category(cat)
r = FilePolicyStore(str(tmp)).get_category("lab")
check("category persisted", r is not None and r.tools == frozenset({"get_order_details"}))
check("category levels roundtrip", r.levels == frozenset({"PUBLIC", "INTERNAL"}) and r.backend == "minierp_orders")
s2.delete_category("lab")
check("category delete persisted", FilePolicyStore(str(tmp)).get_category("lab") is None)

print("whitelist roundtrip")
s2.set_whitelist(["10.0.0.0/8", "192.168.1.0/24"])
check("whitelist persisted", FilePolicyStore(str(tmp)).get_whitelist() == ["10.0.0.0/8", "192.168.1.0/24"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

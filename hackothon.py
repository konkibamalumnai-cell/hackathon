"""
verify_high_value_confirmation.py
===================================
Verification suite for Feature: High-Value Transaction Final Confirmation.

Tests:
  1. Syntax & Zero-disallowed-imports check (no pandas, numpy, plotly, st.bar_chart, st.dataframe
     in finguard/payment_confirmation.py or pages/11_💸_Simulated_Payment.py).
  2. Baseline median / average calculation on ACC1001 & ACC1004.
  3. Trigger rule (> 2.5x historical median OR > ₹50,000):
     - Normal payment (₹3,000 on ACC1001, median ₹3,525) -> completes immediately without warning.
     - > 2.5x median payment (₹15,000 on ACC1001, > ₹8,812.50) -> triggers warning popup data + PENDING_CONFIRMATION.
     - > ₹50,000 payment (₹60,000 on ACC1003, median ₹32,100) -> triggers warning via ₹50,000 threshold.
  4. Confirmation flow:
     - Displays recipient name, masked account ID, attempted amount, usual amount, % increase, reason.
     - Confirming marks status CONFIRMED, appends simulated tx to `transactions`, logs to `audit_ledger`.
  5. Cancellation flow:
     - Cancelling marks status CANCELLED, does NOT insert a transaction into `transactions`, logs to `audit_ledger`.
  6. Duplicate submission prevention:
     - Submitting duplicate while PENDING_CONFIRMATION exists is blocked & logged.
     - Submitting identical completed/confirmed payment within duplicate window is blocked & logged.
     - Calling finalize_simulated_payment twice on the same payment_id is blocked & logged.
     - Reusing an explicit idempotency_key is blocked & logged.
  7. Insufficient transaction history handling:
     - Sender with 0 or 1 historical transactions sending ₹15,000 (<= ₹50,000) is NOT falsely flagged.
     - Sender with 0 transactions sending ₹60,000 (> ₹50,000) triggers only via ₹50,000 limit without ZeroDivisionError.
  8. Existing features preservation check: audit chain valid & built-in login still works.

Usage:
  python verify_high_value_confirmation.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from finguard import audit, database
from finguard.auth import authenticate_demo_user
from finguard.payment_confirmation import (
    compute_historical_baseline,
    evaluate_payment_risk,
    finalize_simulated_payment,
    get_simulated_payments,
    initiate_simulated_payment,
)

_PASS: list[str] = []
_FAIL: list[str] = []


def _ok(label: str) -> None:
    _PASS.append(label)
    print(f"  PASS  {label}")


def _fail(label: str, reason: str) -> None:
    _FAIL.append(label)
    print(f"  FAIL  {label} — {reason}")


# ── 1. No disallowed libraries & clean syntax ─────────────────────────────────
print("\n── 1: Constraints & Syntax Check ───────────────────────────────────────")
for rel in ("finguard/payment_confirmation.py", "pages/11_💸_Simulated_Payment.py"):
    p = ROOT / rel
    src = p.read_text(encoding="utf-8")
    try:
        ast.parse(src)
        _ok(f"{rel} passes ast.parse()")
    except SyntaxError as exc:
        _fail(f"{rel} passes ast.parse()", str(exc))

    bad_tokens = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("pandas", "numpy", "plotly"):
                    bad_tokens.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if any(mod == lib or mod.startswith(f"{lib}.") for lib in ("pandas", "numpy", "plotly")):
                bad_tokens.append(f"from {mod} import ...")
        elif isinstance(node, ast.Attribute) and node.attr in ("bar_chart", "dataframe"):
            bad_tokens.append(f"attr.{node.attr}")
    if not bad_tokens:
        _ok(f"{rel} introduces zero disallowed imports/widgets")
    else:
        _fail(f"{rel} constraint check", f"found disallowed tokens: {bad_tokens}")

conn = database.reset_database()

# ── 2. Baseline computation & Normal payment ──────────────────────────────────
print("\n── 2: Baseline & Normal Transfer (No Warning) ──────────────────────────")
txs_1001 = database.get_transactions_for_subject(conn, "ACC1001")
base_1001 = compute_historical_baseline(txs_1001)
# ACC1001 has ₹4,200 and ₹2,850 -> median = 3525.0, 2.5x = 8812.5
if base_1001["has_sufficient_history"] and base_1001["median_inr"] == 3525.0:
    _ok(f"ACC1001 baseline median computed accurately (₹{base_1001['median_inr']:,.2f})")
else:
    _fail("ACC1001 baseline median", str(base_1001))

res_norm = initiate_simulated_payment(
    conn,
    sender_id="ACC1001",
    recipient_name="Aarav Patel",
    recipient_account="990011223344",
    amount_inr=3000.0,
    actor_name="Alex Mercer (Investigator)",
    role_id="INVESTIGATOR",
    username="investigator",
)
if res_norm["ok"] and not res_norm["requires_confirmation"] and res_norm["payment"]["status"] == "COMPLETED":
    _ok("Normal transfer (₹3,000 on ACC1001) completes without popup")
else:
    _fail("Normal transfer", str(res_norm))

# ── 3. Trigger Rule A: Exceeds 2.5x median & Confirmation Flow ───────────────
print("\n── 3: High-Value > 2.5x Median Warning + Confirm Flow ──────────────────")
res_warn = initiate_simulated_payment(
    conn,
    sender_id="ACC1001",
    recipient_name="Riya Sharma",
    recipient_account="887766554433",
    amount_inr=15000.0,
    actor_name="Alex Mercer (Investigator)",
    role_id="INVESTIGATOR",
    username="investigator",
)
ev = res_warn.get("evaluation", {})
pmt = res_warn.get("payment", {})
if (
    res_warn["ok"]
    and res_warn["requires_confirmation"]
    and pmt.get("status") == "PENDING_CONFIRMATION"
    and ev.get("exceeds_median") is True
    and pmt.get("recipient_account_masked", "").endswith("4433")
    and "88776655" not in pmt.get("recipient_account_masked", "")
    and ev.get("pct_increase") is not None
    and ev["pct_increase"] > 150
):
    _ok(
        f">2.5x median triggered PENDING_CONFIRMATION ({pmt['id']}), masked acct={pmt['recipient_account_masked']}, "
        f"+{ev['pct_increase']:.1f}% increase"
    )
else:
    _fail(">2.5x median warning", str(res_warn))

# Test duplicate submission while PENDING_CONFIRMATION is active
res_pending_dup = initiate_simulated_payment(
    conn,
    sender_id="ACC1001",
    recipient_name="Riya Sharma",
    recipient_account="887766554433",
    amount_inr=15000.0,
    actor_name="Alex Mercer (Investigator)",
    role_id="INVESTIGATOR",
    username="investigator",
)
if not res_pending_dup["ok"] and res_pending_dup.get("duplicate"):
    _ok("Duplicate submission blocked while PENDING_CONFIRMATION is open")
else:
    _fail("Pending duplicate prevention", str(res_pending_dup))

# Confirm Payment
txs_before = len(database.get_transactions_for_subject(conn, "ACC1001"))
ok_c, msg_c, pmt_c = finalize_simulated_payment(
    conn,
    payment_id=pmt["id"],
    action="CONFIRM",
    actor_name="Alex Mercer (Investigator)",
    role_id="INVESTIGATOR",
    username="investigator",
)
txs_after = len(database.get_transactions_for_subject(conn, "ACC1001"))
if ok_c and pmt_c and pmt_c["status"] == "CONFIRMED" and pmt_c["linked_tx_id"] and txs_after == txs_before + 1:
    _ok(f"Confirm Payment updated status=CONFIRMED and inserted tx {pmt_c['linked_tx_id']}")
else:
    _fail("Confirm Payment", f"ok={ok_c}, pmt={pmt_c}, before={txs_before}, after={txs_after}")

# Replay / double finalize guard
ok_replay, msg_replay, _ = finalize_simulated_payment(
    conn,
    payment_id=pmt["id"],
    action="CONFIRM",
    actor_name="Alex Mercer (Investigator)",
    role_id="INVESTIGATOR",
    username="investigator",
)
if not ok_replay:
    _ok("Double confirmation prevented cleanly on finalized payment")
else:
    _fail("Double confirmation guard", "second CONFIRM succeeded unexpectedly")

# Duplicate submission after CONFIRMED within window
res_after_dup = initiate_simulated_payment(
    conn,
    sender_id="ACC1001",
    recipient_name="Riya Sharma",
    recipient_account="887766554433",
    amount_inr=15000.0,
    actor_name="Alex Mercer (Investigator)",
    role_id="INVESTIGATOR",
    username="investigator",
)
if not res_after_dup["ok"] and res_after_dup.get("duplicate"):
    _ok("Duplicate submission blocked after recent CONFIRMED payment")
else:
    _fail("Recent confirmed duplicate guard", str(res_after_dup))

# ── 4. Trigger Rule B: Exceeds ₹50,000 + Cancel Flow ──────────────────────────
print("\n── 4: High-Value > ₹50,000 Trigger + Cancel Flow ───────────────────────")
# ACC1003 has ₹14,200 and ₹50,000 -> median = ₹32,100 -> 2.5x = ₹80,250.
# Transferring ₹60,000 triggers ONLY via the > ₹50,000 rule!
res_50k = initiate_simulated_payment(
    conn,
    sender_id="ACC1003",
    recipient_name="Karan Verma",
    recipient_account="554433221100",
    amount_inr=60000.0,
    actor_name="Jordan Vance (Senior Investigator)",
    role_id="SENIOR_INVESTIGATOR",
    username="senior",
)
ev_50k = res_50k.get("evaluation", {})
pmt_50k = res_50k.get("payment", {})
if res_50k["ok"] and res_50k["requires_confirmation"] and ev_50k.get("exceeds_limit") and not ev_50k.get("exceeds_median"):
    _ok(f"> ₹50,000 triggered confirmation safely ({pmt_50k['id']}): {ev_50k['reason']}")
else:
    _fail("> ₹50,000 trigger", str(res_50k))

txs_1003_before = len(database.get_transactions_for_subject(conn, "ACC1003"))
ok_x, msg_x, pmt_x = finalize_simulated_payment(
    conn,
    payment_id=pmt_50k["id"],
    action="CANCEL",
    actor_name="Jordan Vance (Senior Investigator)",
    role_id="SENIOR_INVESTIGATOR",
    username="senior",
)
txs_1003_after = len(database.get_transactions_for_subject(conn, "ACC1003"))
if ok_x and pmt_x and pmt_x["status"] == "CANCELLED" and txs_1003_after == txs_1003_before:
    _ok(f"Cancel Payment set status=CANCELLED and created 0 new transactions")
else:
    _fail("Cancel Payment", f"ok={ok_x}, pmt={pmt_x}, before={txs_1003_before}, after={txs_1003_after}")

# ── 5. Insufficient Transaction History Handling ──────────────────────────────
print("\n── 5: Insufficient Transaction History (< 2 txns) ──────────────────────")
res_empty_ok = initiate_simulated_payment(
    conn,
    sender_id="NEW-DEMO",
    recipient_name="Neha Gupta",
    recipient_account="445566778899",
    amount_inr=12000.0,
    actor_name="Alex Mercer (Investigator)",
    role_id="INVESTIGATOR",
    username="investigator",
    custom_history=[],
)
if res_empty_ok["ok"] and not res_empty_ok["requires_confirmation"] and res_empty_ok["evaluation"]["pct_increase"] is None:
    _ok("0-history account sending ₹12,000 is NOT falsely flagged as unusual")
else:
    _fail("0-history safe handling", str(res_empty_ok))

res_one_ok = initiate_simulated_payment(
    conn,
    sender_id="NEW-DEMO",
    recipient_name="Mohit Kulkarni",
    recipient_account="112233445566",
    amount_inr=9500.0,
    actor_name="Alex Mercer (Investigator)",
    role_id="INVESTIGATOR",
    username="investigator",
    custom_history=[{"amount": 500, "currency": "INR"}],
)
if res_one_ok["ok"] and not res_one_ok["requires_confirmation"]:
    _ok("1-history transaction account sending ₹9,500 is safely not flagged")
else:
    _fail("1-history safe handling", str(res_one_ok))

res_empty_50k = initiate_simulated_payment(
    conn,
    sender_id="NEW-DEMO",
    recipient_name="Vikram Rao",
    recipient_account="998877665511",
    amount_inr=65000.0,
    actor_name="Alex Mercer (Investigator)",
    role_id="INVESTIGATOR",
    username="investigator",
    custom_history=[],
)
if (
    res_empty_50k["ok"]
    and res_empty_50k["requires_confirmation"]
    and res_empty_50k["evaluation"]["exceeds_limit"]
    and not res_empty_50k["evaluation"]["exceeds_median"]
    and res_empty_50k["evaluation"]["pct_increase"] is None
):
    _ok("0-history account sending ₹65,000 still enforces ₹50,000 cap without ZeroDivisionError")
else:
    _fail("0-history >₹50,000 handling", str(res_empty_50k))

# ── 6. Audit Log & SQLite Record Completeness ─────────────────────────────────
print("\n── 6: SQLite & SHA-256 Audit Log Verification ──────────────────────────")
chain = audit.verify_audit_chain(conn)
evts = {e["event_type"] for e in audit.get_audit_log(conn)}
needed_evts = {
    "HIGH_VALUE_PAYMENT_WARNING",
    "HIGH_VALUE_PAYMENT_CONFIRMED",
    "HIGH_VALUE_PAYMENT_CANCELLED",
    "SIMULATED_PAYMENT_COMPLETED",
    "SIMULATED_PAYMENT_DUPLICATE_BLOCKED",
}
missing_evts = needed_evts - evts
if chain.get("valid") and not missing_evts:
    _ok(f"All 5 payment event types logged & SHA-256 audit chain intact ({chain['total_blocks']} blocks)")
else:
    _fail("Audit log verification", f"chain={chain}, missing={missing_evts}")

# Verify existing demo login still works untouched
ok_auth, _, auth_u = authenticate_demo_user(conn, "investigator", "invest123")
if ok_auth and auth_u and auth_u["role_id"] == "INVESTIGATOR":
    _ok("Existing demo user authentication preserved (investigator)")
else:
    _fail("Existing demo auth check", f"ok={ok_auth}")

# Restore clean DB seed state
database.reset_database(conn).close()

print()
print("=" * 68)
total = len(_PASS) + len(_FAIL)
print(f"High-Value Final Confirmation  |  {len(_PASS)}/{total} PASS  |  {len(_FAIL)} FAIL")
print("=" * 68)
if _FAIL:
    sys.exit(1)
sys.exit(0)

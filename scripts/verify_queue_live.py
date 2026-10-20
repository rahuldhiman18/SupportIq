"""LIVE queue verification against real Postgres -- run manually, never in CI.

Phase 1: seeds a realistic priority/age mix, proves the claim SQL (FOR UPDATE
SKIP LOCKED, ordered by priority - wait/30) returns exactly the predicted
order, then repeats under 6 concurrent sessions and asserts exactly-once.

Phase 3: drives the REAL app.worker -- claim_next ordering, a 6-thread
exactly-once hammer, and Option-A retry bookkeeping through process_one with
a fake AI (zero Gemini quota).

Usage: python scripts/verify_queue_live.py   (exit 0 = all checks passed)
"""
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv
from sqlalchemy import text

# Standalone script: make the repo root importable for `from app import ...`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()
EMAIL_MARK = "queue-live-test@"

from app.services import worker  # noqa: E402
from app.services.ai_service import AIProviderError  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Support_Answer, SupportQuery, compute_base_priority, effective_score)

failures = []


def check(ok, label, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


# Pre-clean: remove ANY marked rows (e.g. leftovers from a crashed run) so
# the claim loops below only ever see rows this run seeded.
s = SessionLocal()
try:
    s.execute(text(
        "DELETE FROM support_answers WHERE query_id IN "
        "(SELECT id FROM support_queries WHERE customer_email LIKE :m)"),
        {"m": "%" + EMAIL_MARK})
    s.execute(text("DELETE FROM support_queries WHERE customer_email LIKE :m"),
              {"m": "%" + EMAIL_MARK})
    s.commit()
finally:
    s.close()


# ================= Phase 1: priority + aging, live Postgres ==================

NOW = datetime.now(timezone.utc)
MIN = timedelta(minutes=1)
# (label, intent, age) -- priorities are SERVER-derived via compute_base_priority
MIX = [
    ("P1 critical fresh",     "payment_issues",   1 * MIN),
    ("P2 high fresh",         "order_tracking",   timedelta(seconds=30)),
    ("P3 normal aged 10m",    "eligibility",      10 * MIN),
    ("P4 low fresh",          "how_to_request",   timedelta(0)),
    ("P5 lowest fresh",       "out_of_scope",     timedelta(0)),
    ("P6 normal aged 51m",    "eligibility",      51 * MIN),   # -> score 98.0
    ("P7 low aged 15m",       "how_to_request",   15 * MIN),
    ("P8 high aged 6m",       "order_tracking",   6 * MIN),
    ("P9 critical fresh 2",   "payment_issues",   timedelta(seconds=45)),
    ("P10 normal aged 25m",   "timing",           25 * MIN),
]

CLAIM_SQL = text("""
    UPDATE support_queries
    SET status = 'processing', locked_at = :now
    WHERE id = (
        SELECT id FROM support_queries
        WHERE status = 'queued'
          AND (next_retry_at IS NULL OR next_retry_at <= :now)
        ORDER BY priority - EXTRACT(EPOCH FROM (:now - created_at)) / 30.0
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, priority, created_at
""")


def wipe_mix():
    s = SessionLocal()
    try:
        rows = s.execute(text(
            "SELECT id FROM support_queries WHERE customer_email LIKE :m"
        ), {"m": f"{EMAIL_MARK}%"}).scalars().all()
        if rows:
            s.execute(text("DELETE FROM support_answers WHERE query_id = ANY(:ids)"),
                      {"ids": rows})
            s.execute(text("DELETE FROM support_queries WHERE id = ANY(:ids)"),
                      {"ids": rows})
            s.commit()
        return len(rows)
    finally:
        s.close()


def seed_mix():
    s = SessionLocal()
    try:
        ids = {}
        for label, intent, age in MIX:
            q = SupportQuery(question=label,
                             customer_email=f"{EMAIL_MARK}p@x.com",
                             status="queued",
                             priority=compute_base_priority(intent),
                             created_at=NOW - age)
            s.add(q)
            s.flush()
            ids[label] = q.id
        s.commit()
        return ids
    finally:
        s.close()


def claim_one(now):
    s = SessionLocal()
    try:
        row = s.execute(CLAIM_SQL, {"now": now}).first()
        s.commit()
        return row
    finally:
        s.close()


def label_of(ids, row_id):
    return next(k for k, v in ids.items() if v == row_id)

print("=" * 72)
print("PHASE 1 -- priority + aging, live Postgres")
print("=" * 72)

# Aging math, evaluated live against the shared formula
aged_norm = effective_score(200, NOW - 51 * MIN, now=NOW)
fresh_high = effective_score(100, NOW - timedelta(seconds=30), now=NOW)
check(aged_norm < fresh_high,
      "no-starvation: 51-min-old NORMAL outranks fresh HIGH",
      f"{aged_norm:.1f} < {fresh_high:.1f}")
delta90 = (effective_score(200, NOW - 51 * MIN, now=NOW)
           - effective_score(200, NOW - 49.5 * MIN, now=NOW))
check(abs(delta90 - (-3.0)) < 1e-9, "aging rate = -1 pt per 30 s",
      f"90 s wait => {delta90:+.1f} pts")

wipe_mix()
ids = seed_mix()

# 1) Sequential claim: exact ORDER BY semantics
predicted = sorted(MIX, key=lambda m: compute_base_priority(m[1])
                   - m[2].total_seconds() / 30.0)
seq_order = []
while True:
    row = claim_one(datetime.now(timezone.utc))
    if row is None:
        break
    seq_order.append(label_of(ids, row.id))
check(seq_order == [p[0] for p in predicted], "sequential claim order matches score order")
for label, intent, age in predicted:
    base = compute_base_priority(intent)
    print(f"      {label:24s} base={base:3d} score={base - age.total_seconds() / 30.0:7.1f}")

# 2) Concurrent claim: 6 sessions racing, exactly-once required
wipe_mix()
ids = seed_mix()
claims = []          # (thread, seq, label, score)
lock = threading.Lock()


def claimer():
    seq = 0
    while True:
        now = datetime.now(timezone.utc)
        row = claim_one(now)
        if row is None:
            return
        seq += 1
        created = row[2].replace(tzinfo=timezone.utc)
        score = row.priority - (now - created).total_seconds() / 30.0
        with lock:
            claims.append((threading.get_ident(), seq, label_of(ids, row.id), score))
        time.sleep(0.004)   # jitter so sessions interleave like real workers


t0 = time.perf_counter()
with ThreadPoolExecutor(max_workers=6) as pool:
    list(pool.map(lambda _: claimer(), range(6)))
elapsed = time.perf_counter() - t0

counts = Counter(c[2] for c in claims)
dupes = {k: v for k, v in counts.items() if v > 1}
missing = [m[0] for m in MIX if m[0] not in counts]
check(not dupes and not missing,
      f"6 concurrent sessions: exactly-once over {len(MIX)} rows in {elapsed:.2f}s",
      f"dupes={dupes or 'none'} missing={missing or 'none'}")
by_thread = {}
for tid, seq, label, score in claims:
    by_thread.setdefault(tid, []).append((seq, score))
inorder = all(all(a[1] <= b[1] for a, b in zip(v, v[1:])) for v in by_thread.values())
check(inorder, "every session always took the best score it could see",
      f"{len(by_thread)} sessions, {[len(v) for v in by_thread.values()]} claims")
print("      claims in global start order: "
      + " | ".join(f"{c[3]:.1f}" for c in claims))
wipe_mix()
print("      cleanup: phase-1 rows removed")


# ============== Phase 3: the REAL worker on live Postgres ====================
print("=" * 72)
print("PHASE 3 -- worker claim loop + concurrency (live Postgres, fake AI)")
print("=" * 72)


def wipe_worker_rows():
    s = SessionLocal()
    try:
        s.execute(text(
            "DELETE FROM support_answers WHERE query_id IN "
            "(SELECT id FROM support_queries WHERE customer_email LIKE :m)"),
            {"m": "%" + EMAIL_MARK})
        s.execute(text("DELETE FROM support_queries WHERE customer_email LIKE :m"),
                  {"m": "%" + EMAIL_MARK})
        s.commit()
    finally:
        s.close()


wipe_worker_rows()

# 1) claim order: critical first, then oldest base-tier row (aging)
NOW3 = datetime.now(timezone.utc)
s = SessionLocal()
try:
    s.add(SupportQuery(question="P-crit " + EMAIL_MARK,
                       customer_email="crit" + EMAIL_MARK,
                       status="queued", priority=0))
    s.add(SupportQuery(question="P-base-old " + EMAIL_MARK,
                       customer_email="old" + EMAIL_MARK,
                       status="queued", priority=400,
                       created_at=NOW3 - timedelta(minutes=30)))
    s.add(SupportQuery(question="P-base-new " + EMAIL_MARK,
                       customer_email="new" + EMAIL_MARK,
                       status="queued", priority=400))
    s.commit()
finally:
    s.close()

got = []
# Option (a) test isolation (approved): the dev queue is SHARED -- foreign
# rows (real manual-test history, other experiments) may legitimately
# interleave with our seeded rows. Drain the whole queue (that IS what the
# worker does), then assert only our rows' RELATIVE order. Foreign claims
# are reported, not failed on. Long-term fix (P1): dedicated verify DB.
while True:
    s = SessionLocal()
    try:
        q = worker.claim_next(s)
    finally:
        s.close()
    if q is None:
        break
    got.append(q.question.split()[0])
_SEEDS = ("P-crit", "P-base-old", "P-base-new")
ours = [g for g in got if g in _SEEDS]
foreign = [g for g in got if g not in _SEEDS]
check(ours == ["P-crit", "P-base-old", "P-base-new"],
      "worker.claim_next: critical first, then oldest base-tier (aging)",
      f"ours={ours} foreign_drained={foreign or 'none'}")

# 2) exactly-once hammer: 6 threads over N queued rows, zero dupes/missing
wipe_worker_rows()
N = 12
s = SessionLocal()
try:
    for i in range(N):
        s.add(SupportQuery(question=f"bulk-{i} " + EMAIL_MARK,
                           customer_email=f"bulk{i}@" + EMAIL_MARK,
                           status="queued", priority=400))
    s.commit()
finally:
    s.close()


def _claim_one(_):
    s = SessionLocal()
    try:
        q = worker.claim_next(s)
        return q.id if q else None
    finally:
        s.close()


with ThreadPoolExecutor(max_workers=6) as pool:
    claimed = [c for c in pool.map(_claim_one, range(N * 2)) if c is not None]
dupes = [cid for cid, n in Counter(claimed).items() if n > 1]
check(len(claimed) == N and not dupes,
      f"6 threads x {N} queued rows: exactly-once via worker.claim_next",
      f"claimed={len(claimed)} dupes={len(dupes)}")

# 3) Option-A bookkeeping through process_one (fake AI -- zero quota)
s = SessionLocal()
try:
    q = SupportQuery(question="retry-path " + EMAIL_MARK,
                     customer_email="retry" + EMAIL_MARK, status="queued")
    s.add(q)
    s.commit()
    qid = q.id
finally:
    s.close()

_orig_once = worker.answer_customer_question_once


def _simulated_outage(*a, **k):
    raise AIProviderError("simulated outage")


worker.answer_customer_question_once = _simulated_outage
try:
    s = SessionLocal()
    try:
        q = s.get(SupportQuery, qid)
        st1 = worker.process_one(s, q)
        s.refresh(q)
        check(st1 == "queued" and q.attempts == 1 and q.next_retry_at is not None,
              "failure #1 -> stays queued, attempts=1, ~2min backoff set",
              f"{st1}/attempts={q.attempts}")
    finally:
        s.close()

    # drain to the handoff edge: attempts 2 then 3 -> needs_human
    s = SessionLocal()
    try:
        q = s.get(SupportQuery, qid)
        q.next_retry_at = None
        s.commit()
        worker.process_one(s, q)
        s.refresh(q)
        q.next_retry_at = None
        s.commit()
        st3 = worker.process_one(s, q)
        s.refresh(q)
        check(st3 == "needs_human" and q.attempts == 3,
              "failures #2/#3 -> needs_human after 3 attempts (Option A)",
              f"{st3}/attempts={q.attempts}")
    finally:
        s.close()
finally:
    worker.answer_customer_question_once = _orig_once

# 4) success path: resolved + answer row, intents recorded
s = SessionLocal()
try:
    q = s.get(SupportQuery, qid)
    q.status, q.next_retry_at, q.attempts = "queued", None, 0
    s.commit()
finally:
    s.close()

worker.answer_customer_question_once = (
    lambda *a, **k: SimpleNamespace(intent="eligibility",
                                    answer_text="verified by fake AI"))
try:
    s = SessionLocal()
    try:
        q = s.get(SupportQuery, qid)
        st = worker.process_one(s, q)
        s.refresh(q)
        has_answer = s.query(Support_Answer).filter(
            Support_Answer.query_id == qid).count() == 1
        check(st == "resolved" and q.intent == "eligibility" and has_answer,
              "process_one success -> resolved + answer row + intent",
              f"{st}/{q.intent}/answer={has_answer}")
    finally:
        s.close()
finally:
    worker.answer_customer_question_once = _orig_once

# 5) P0 reaper: stale `processing` rows reclaimed inside claim_next
wipe_worker_rows()


def _seed_processing(label, locked_age_seconds, attempts=0, locked=True):
    s = SessionLocal()
    try:
        q = SupportQuery(question=label + " " + EMAIL_MARK,
                         customer_email=label + EMAIL_MARK,
                         status="processing", attempts=attempts,
                         locked_at=(datetime.now(timezone.utc)
                                    - timedelta(seconds=locked_age_seconds))
                         if locked else None)
        s.add(q)
        s.commit()
        return q.id
    finally:
        s.close()


# a) stale past 60s -> reclaimed (+1 attempt) AND claimed in one pass
_qid_a = _seed_processing("stale61", 61)
s = SessionLocal()
try:
    q = worker.claim_next(s)
    check(q is not None and q.customer_email.startswith("stale61")
          and q.attempts == 1 and q.status == "processing",
          "reaper: 61s-old processing row reclaimed (+1 attempt) and claimed",
          f"row={q.customer_email if q else None}/attempts={q.attempts if q else '-'}")
finally:
    s.close()

# b) fresh (30s) row left alone -- the 60s invariant window is respected
wipe_worker_rows()
_qid_b = _seed_processing("fresh30", 30)
s = SessionLocal()
try:
    q = worker.claim_next(s)
    check(q is None, "reaper: 30s-old processing row untouched (inside window)",
          f"claimed={q.customer_email if q else None}")
finally:
    s.close()

# c) crash-loop row: 2 attempts already -> reclaim pushes to 3 -> needs_human
wipe_worker_rows()
_qid_c = _seed_processing("poison", 90, attempts=worker.MAX_AI_ATTEMPTS - 1)
s = SessionLocal()
try:
    q = worker.claim_next(s)
    s2 = SessionLocal()
    try:
        row = s2.get(SupportQuery, _qid_c)
        check(q is None and row.status == "needs_human"
              and row.attempts == worker.MAX_AI_ATTEMPTS,
              "reaper: crash-loop row hits 3-strike budget -> needs_human",
              f"status={row.status}/attempts={row.attempts}")
    finally:
        s2.close()
finally:
    s.close()

# d) corrupt row (processing + NULL locked_at) reclaimed immediately
wipe_worker_rows()
_qid_d = _seed_processing("nulllock", 0, locked=False)
s = SessionLocal()
try:
    q = worker.claim_next(s)
    check(q is not None and q.customer_email.startswith("nulllock")
          and q.attempts == 1,
          "reaper: processing row with NULL locked_at reclaimed immediately",
          f"claimed={q.customer_email if q else None}")
finally:
    s.close()

wipe_worker_rows()
print("      cleanup: phase-3 rows removed")

print("=" * 72)
print(("ALL LIVE CHECKS PASSED" if not failures
       else f"{len(failures)} FAILURES: {failures}"))
print("=" * 72)
raise SystemExit(1 if failures else 0)



"""LIVE queue + order-facts verification (TEMP: delete after run).

Phase 1 (direct Postgres): seeds a realistic priority/age mix, proves the
claim SQL the Step 3b worker will issue (FOR UPDATE SKIP LOCKED, ordered by
priority - wait/30) returns exactly the predicted order, then repeats under
6 concurrent sessions and asserts exactly-once claims.

Phase 2 (live HTTP + REAL Gemini key): creates 3 real orders (in-transit,
STALLED, delivered), fires 6 concurrent POST /queries, verifies answers quote
the LIVE tracking facts, and shows the queue priority each query would get.
"""
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()
BASE = "http://127.0.0.1:8765"
ADMIN = {"X-Admin-Key": os.getenv("VERIFY_ADMIN_KEY", "live-queue-key")}
EMAIL_MARK = "queue-live-test@"

from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    SupportQuery, compute_base_priority, effective_score)

failures = []


def check(ok, label, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


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


def worker():
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
    list(pool.map(lambda _: worker(), range(6)))
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


# ============ Phase 2: live HTTP + REAL Gemini key ==========================
print("=" * 72)
print("PHASE 2 -- concurrent POST /queries + LIVE order facts (real Gemini)")
print("=" * 72)

A1, A2, A3, A4, A5 = ("alice.queue@live.test", "bob.queue@live.test",
                      "cara.queue@live.test", "dan.queue@live.test",
                      "eve.queue@live.test")
ORDERS = [
    {"order_number": "ORD-8801", "customer_email": A1, "delivery_method": "standard",
     "destination_city": "Pune", "carrier": "BlueDart",
     "events": [
         {"stage": "placed", "location": "Mumbai warehouse",
          "occurred_at": "2026-10-12T08:00:00Z"},
         {"stage": "shipped", "location": "Pune hub",
          "occurred_at": "2026-10-13T09:30:00Z"},
         {"stage": "out_for_delivery", "location": "Pune hub",
          "occurred_at": "2026-10-14T07:45:00Z"}]},
    # last scan Oct 3 => 3+ business days stale => is_stalled() True => CRITICAL
    {"order_number": "ORD-8802", "customer_email": A2, "delivery_method": "standard",
     "destination_city": "Bengaluru", "carrier": "Delhivery",
     "events": [
         {"stage": "placed", "location": "Delhi hub",
          "occurred_at": "2026-10-02T10:00:00Z"},
         {"stage": "shipped", "location": "Mumbai warehouse",
          "occurred_at": "2026-10-03T11:00:00Z"}]},
    {"order_number": "ORD-8803", "customer_email": A3, "delivery_method": "standard",
     "destination_city": "Delhi", "carrier": "BlueDart",
     "events": [
         {"stage": "placed", "location": "Delhi hub",
          "occurred_at": "2026-10-08T09:00:00Z"},
         {"stage": "shipped", "location": "Mumbai warehouse",
          "occurred_at": "2026-10-10T16:00:00Z"},
         {"stage": "delivered", "location": "Delhi home",
          "occurred_at": "2026-10-13T14:00:00Z"}]},
]
QUESTIONS = [
    (A1, "Where is my order right now? It is ORD-8801.", "ORD-8801"),
    (A2, "My order ORD-8802 has not moved in ages, what is going on?", "ORD-8802"),
    (A3, "Did my order ORD-8803 arrive? Where was it left?", "ORD-8803"),
    (A4, "What is your refund policy if an item arrives damaged?", None),
    (A5, "Tell me a joke about databases.", None),
    (A1, "Can you check the status for ORD-8801 please?", "ORD-8801"),
]

with httpx.Client(base_url=BASE, timeout=90) as c:
    for payload in ORDERS:
        r = c.post("/orders", json=payload, headers=ADMIN)
        check(r.status_code == 201, f"create {payload['order_number']}",
              str(r.json())[:80])

    # --- fire all 6 questions AT THE SAME TIME ---
    results = [None] * len(QUESTIONS)

    def ask(i):
        email, question, onum = QUESTIONS[i]
        t = time.perf_counter()
        r = c.post("/queries", json={"customer_email": email,
                                     "question": question,
                                     "order_number": onum})
        results[i] = (r, time.perf_counter() - t)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(ask, range(len(QUESTIONS))))
    total = time.perf_counter() - t0

    codes = [res[0].status_code for res in results]
    check(all(code == 202 for code in codes),
          f"6 concurrent POST /queries all 202 in {total:.1f}s wall",
          f"codes={codes}")
    check(not any(code >= 500 for code in codes), "zero 5xx under parallel load")
    for i, (r, dt) in enumerate(results):
        print(f"      Q{i + 1}: {r.status_code} in {dt:5.2f}s  {QUESTIONS[i][1][:44]}")

    # --- fetch each answer back (email-scoped) and verify LIVE facts ---
    answers = {}
    for i, (r, _) in enumerate(results):
        qid = r.json()["query_id"]
        a = c.get(f"/queries/{qid}/answer", params={"email": QUESTIONS[i][0]})
        check(a.status_code == 200, f"Q{i + 1} answer fetch 200")
        answers[i] = a.json()["answer_text"].strip()
        check(bool(answers[i]), f"Q{i + 1} answer non-empty", answers[i][:50])

    check(not any("live order facts" in a.lower() for a in answers.values()),
          "no prompt-instruction leakage in any answer")

    def hit(i, opts):
        return next((s for s in opts if s in answers[i].lower()), None)

    w = hit(0, ("pune", "oct 16", "oct 20", "eta"))
    check(w is not None, "Q1 (in-transit) quotes live location/ETA", w or answers[0][:80])
    w = hit(5, ("pune", "oct 16", "oct 20", "eta"))
    check(w is not None, "Q6 (same order, concurrent) quotes live location/ETA")
    w = hit(1, ("investigat", "stall", "scan", "mumbai", "oct 3", "not moved"))
    check(w is not None, "Q2 (STALLED order) reflects stall/last scan",
          w or answers[1][:80])
    w = hit(2, ("oct 13", "delhi", "deliver"))
    check(w is not None, "Q3 (delivered) quotes delivery fact", w or answers[2][:80])
    w = hit(3, ("refund", "return", "damag"))
    check(w is not None, "Q4 control: policy question grounded", w)
    for i in (0, 1, 2, 5):
        print(f"      --- Q{i + 1}: {answers[i][:230]}")

    # --- what priority WOULD each query get in the queue? (Step 3b preview)
    print("      would-be queue priority (server-computed):")
    s = SessionLocal()
    try:
        for i, (email, question, onum) in enumerate(QUESTIONS):
            row = s.query(SupportQuery).filter(
                SupportQuery.customer_email == email,
                SupportQuery.question == question,
            ).order_by(SupportQuery.created_at.desc()).first()
            stalled = False
            if onum:
                order = s.query(Order).filter(Order.order_number == onum).first()
                stalled = order.is_stalled()
            prio = compute_base_priority(row.intent or "eligibility",
                                         stalled=stalled)
            name = {0: "critical", 100: "high", 200: "normal",
                    300: "low", 400: "lowest"}[prio]
            print(f"        Q{i + 1}: intent={(row.intent or '?'):16s} "
                  f"stalled={str(stalled):5s} -> {name}({prio})")
    finally:
        s.close()

    # --- cleanup live rows ---
    s = SessionLocal()
    try:
        emails = sorted({q[0] for q in QUESTIONS})
        qids = s.execute(text(
            "SELECT id FROM support_queries WHERE customer_email = ANY(:e)"),
            {"e": emails}).scalars().all()
        if qids:
            s.execute(text("DELETE FROM support_answers WHERE query_id = ANY(:ids)"),
                      {"ids": qids})
            s.execute(text("DELETE FROM support_queries WHERE id = ANY(:ids)"),
                      {"ids": qids})
        nums = [o["order_number"] for o in ORDERS]
        s.execute(text(
            "DELETE FROM order_events WHERE order_id IN "
            "(SELECT id FROM orders WHERE order_number = ANY(:n))"), {"n": nums})
        s.execute(text("DELETE FROM orders WHERE order_number = ANY(:n)"), {"n": nums})
        s.commit()
        print(f"      cleanup: {len(qids)} queries + {len(nums)} orders removed")
    finally:
        s.close()

print("=" * 72)
print(("ALL LIVE CHECKS PASSED" if not failures
       else f"{len(failures)} FAILURES: {failures}"))
print("=" * 72)
raise SystemExit(1 if failures else 0)


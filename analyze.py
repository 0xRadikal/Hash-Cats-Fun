"""
analyze.py — summarize watcher_data.csv to spot quiet patterns and windows.

Run any time:  python3 analyze.py
Reports: sample span, burst distribution, min burst seen, best (lowest-bits)
moments, per-hour average burst, and whether we ever hit an actionable level.
"""
import csv
import os
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(HERE, "watcher_data.csv")

BITS_OPEN = 34
BITS_APPROACHING = 37


def main():
    if not os.path.exists(CSV):
        print("no data yet"); return
    rows = []
    with open(CSV) as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                rows.append({
                    "ts": row["ts_utc"],
                    "epoch": int(row["epoch_s"]),
                    "burst": int(row["burst"]),
                    "bits": int(row["bits"]),
                    "level": row["level"],
                })
            except Exception:
                pass
    if not rows:
        print("no rows yet"); return

    span_min = (rows[-1]["epoch"] - rows[0]["epoch"]) / 60.0
    bursts = [r["burst"] for r in rows]
    bits = [r["bits"] for r in rows]
    print(f"Samples: {len(rows)} over {span_min:.1f} min "
          f"({rows[0]['ts']} -> {rows[-1]['ts']} UTC)")
    print(f"burst: min={min(bursts)} max={max(bursts)} "
          f"avg={sum(bursts)/len(bursts):.1f}")
    print(f"bits : min={min(bits)} max={max(bits)} "
          f"avg={sum(bits)/len(bits):.1f}")

    print("\nbits distribution:")
    dist = Counter(bits)
    for b in sorted(dist):
        bar = "#" * min(60, dist[b])
        tag = ""
        if b <= BITS_OPEN: tag = " <= OPEN (capturable @4GH/s)"
        elif b <= BITS_APPROACHING: tag = " <= approaching"
        print(f"  {b:2d} bits: {dist[b]:4d} {bar}{tag}")

    opens = [r for r in rows if r["level"] == "OPEN"]
    appr = [r for r in rows if r["level"] == "APPROACHING"]
    print(f"\nactionable samples: OPEN={len(opens)} APPROACHING={len(appr)} "
          f"BUSY={len(rows)-len(opens)-len(appr)}")
    if opens:
        print("  *** OPEN windows were seen! timestamps:")
        for r in opens[:20]:
            print(f"     {r['ts']}  burst={r['burst']} bits={r['bits']}")

    # per-hour average burst (UTC hour)
    per_hour = defaultdict(list)
    for r in rows:
        hour = r["ts"][11:13]
        per_hour[hour].append(r["burst"])
    print("\navg burst by UTC hour (lower is better for us):")
    for h in sorted(per_hour):
        vals = per_hour[h]
        print(f"  {h}:00  avg_burst={sum(vals)/len(vals):4.1f}  "
              f"min={min(vals)}  n={len(vals)}")

    best = min(rows, key=lambda r: r["bits"])
    print(f"\nBEST moment so far: {best['ts']} burst={best['burst']} "
          f"bits={best['bits']}")
    if best["bits"] <= BITS_OPEN:
        print("=> We have already seen a capturable window. If it recurs, RENT & GO.")
    elif best["bits"] <= BITS_APPROACHING:
        print("=> Got close. Keep watching; a capturable dip may be near.")
    else:
        print("=> Still busy. Need burst to drop further. Keep the watcher running.")


if __name__ == "__main__":
    main()

"""
watcher.py — 24/7 quiet-window watcher for Hashcats.

Purpose (Plan A): run cheaply on the free CPU box, watch the difficulty
(burst) and mint activity, and shout when a viable mining window is
approaching / open — so the GPU is only rented at the right moment.

Verified facts it relies on (all confirmed on-chain earlier):
  * difficulty bits = epochFloor(32) + burst
  * burst rises +1 per mint, cools -1 per DECAY_HALFLIFE (10s) of no mint
  * a rig of ~R GH/s can solve within one inter-mint gap G iff 2^bits <~ R*G

Window levels (for a ~4 GH/s rig = 4x RTX 3080, gap ~9s):
  * OPEN       : bits <= 34  (burst <= 2)  -> rent NOW, we can capture
  * APPROACHING: bits <= 37  (burst <= 5) and trending down
  * BUSY       : otherwise

Outputs:
  * stdout + watcher.log : one status line per poll
  * watcher_state.json   : latest snapshot + level (machine-readable)
  * watcher_data.csv     : time-series for later pattern analysis

Robust: batch RPC reads, RPC failover, never crashes on a bad poll.
"""
import json
import os
import time
from collections import deque
from datetime import datetime, timezone

from web3 import Web3
import chain

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "watcher_state.json")
CSV = os.path.join(HERE, "watcher_data.csv")

# Rig assumption for feasibility labeling (4x RTX 3080 ~ 4 GH/s).
RIG_GHS = 4.0e9
GAP_ASSUMED = 9.0  # seconds; typical inter-mint gap when busy

# Thresholds in difficulty bits.
BITS_OPEN = 34         # burst <= 2  -> capturable now
BITS_APPROACHING = 37  # burst <= 5  -> get ready


def level_for(bits):
    if bits <= BITS_OPEN:
        return "OPEN"
    if bits <= BITS_APPROACHING:
        return "APPROACHING"
    return "BUSY"


def solve_est_seconds(bits, ghs=RIG_GHS):
    return (2 ** bits) / ghs


def read_batch(w3, c):
    """One round-trip read of the live state. Returns dict or raises."""
    batch = w3.batch_requests()
    batch.add(c.functions.currentBurst())
    batch.add(c.functions.currentTarget())
    batch.add(c.functions.totalMinted())
    batch.add(c.functions.lastMintTime())
    batch.add(c.functions.mintPrice())
    burst, target, total, last_mint_time, price = batch.execute()
    bits = 256 - int(target).bit_length()
    return {
        "burst": int(burst),
        "bits": int(bits),
        "totalMinted": int(total),
        "lastMintTime": int(last_mint_time),
        "mintPrice": int(price),
    }


def main(poll=8):
    w3, url = chain.connect()
    c = chain.get_contract(w3)
    print(f"[watcher] connected {url}. Rig assumption {RIG_GHS/1e9:.1f} GH/s. "
          f"OPEN<= {BITS_OPEN}b, APPROACHING<= {BITS_APPROACHING}b. poll={poll}s")

    if not os.path.exists(CSV):
        with open(CSV, "w") as f:
            f.write("ts_utc,epoch_s,burst,bits,totalMinted,gap_s,rolling_mints_per_min,level,solve_est_s\n")

    burst_hist = deque(maxlen=20)    # recent burst values for trend
    mint_events = deque(maxlen=200)  # timestamps (local) of observed mints
    last_total = None
    last_level = None
    consecutive_errors = 0

    while True:
        try:
            s = read_batch(w3, c)
            now = time.time()
            # detect new mints since last poll
            if last_total is not None and s["totalMinted"] > last_total:
                for _ in range(s["totalMinted"] - last_total):
                    mint_events.append(now)
            last_total = s["totalMinted"]
            burst_hist.append(s["burst"])

            # rolling mints/min over observed window
            recent = [t for t in mint_events if now - t <= 120]
            span = (now - recent[0]) if len(recent) >= 2 else 0
            mpm = (len(recent) / (span / 60)) if span > 0 else 0.0

            # gap since last mint (chain time)
            gap = None
            try:
                blk = w3.eth.get_block("latest")
                gap = blk["timestamp"] - s["lastMintTime"]
            except Exception:
                gap = None

            level = level_for(s["bits"])
            est = solve_est_seconds(s["bits"])
            trend = ""
            if len(burst_hist) >= 5:
                first = sum(list(burst_hist)[:3]) / 3
                lastv = sum(list(burst_hist)[-3:]) / 3
                if lastv < first - 0.5:
                    trend = "DOWN"
                elif lastv > first + 0.5:
                    trend = "UP"
                else:
                    trend = "flat"

            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            est_txt = (f"{est:.0f}s" if est < 90 else
                       (f"{est/60:.0f}m" if est < 5400 else f"{est/3600:.1f}h"))
            line = (f"[{ts}] burst={s['burst']:2d} bits={s['bits']:2d} "
                    f"minted={s['totalMinted']} gap={gap}s mpm={mpm:.1f} "
                    f"trend={trend:4} level={level} est_solve={est_txt}")
            # Emphasize actionable transitions
            if level != last_level:
                if level == "OPEN":
                    line += "  <<< WINDOW OPEN — RENT GPU NOW >>>"
                elif level == "APPROACHING":
                    line += "  <<< window approaching — get ready >>>"
            print(line, flush=True)
            with open(os.path.join(HERE, "watcher.log"), "a") as f:
                f.write(line + "\n")
            with open(CSV, "a") as f:
                f.write(f"{ts},{now:.0f},{s['burst']},{s['bits']},{s['totalMinted']},"
                        f"{gap if gap is not None else ''},{mpm:.2f},{level},{est:.0f}\n")
            with open(STATE, "w") as f:
                json.dump({
                    "ts_utc": ts, "epoch_s": int(now),
                    "burst": s["burst"], "bits": s["bits"],
                    "totalMinted": s["totalMinted"], "gap_s": gap,
                    "rolling_mints_per_min": round(mpm, 2),
                    "trend": trend, "level": level,
                    "solve_est_s": int(est),
                    "mintPrice_wei": s["mintPrice"],
                }, f, indent=2)

            last_level = level
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            print(f"[watcher] poll error ({consecutive_errors}): {str(e)[:80]}", flush=True)
            if consecutive_errors >= 3:
                # try to reconnect (RPC failover)
                try:
                    w3, url = chain.connect()
                    c = chain.get_contract(w3)
                    print(f"[watcher] reconnected via {url}", flush=True)
                    consecutive_errors = 0
                except Exception:
                    pass
        time.sleep(poll)


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    main(p)

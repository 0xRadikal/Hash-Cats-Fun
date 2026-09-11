"""
solver_mp.py — multi-core PoW solver (queue-based, responsive).

Each worker runs an endless loop over its own disjoint nonce lane
(stride = num_workers), checking a shared 'found' flag every small batch so
it stops promptly once any worker (or the parent) signals completion.
The winning nonce is delivered through a Queue.
"""
import multiprocessing as mp
import time
from Crypto.Hash import keccak


def _worker(miner_hex, prev_work, anchor_hex, target, start, stride,
            found, q, deadline):
    prefix = bytes.fromhex(miner_hex)
    suffix = prev_work.to_bytes(32, "big") + bytes.fromhex(anchor_hex)
    kfun = keccak.new
    nonce = start
    done = 0
    CHECK = 8192  # check the shared flag every 8192 hashes
    try:
        while True:
            if found.value or (deadline and time.time() > deadline):
                q.put(("done", done, None))
                return
            for _ in range(CHECK):
                pre = prefix + nonce.to_bytes(32, "big") + suffix
                k = kfun(digest_bits=256)
                k.update(pre)
                if int.from_bytes(k.digest(), "big") < target:
                    found.value = True
                    q.put(("found", done + 1, nonce))
                    return
                nonce += stride
                done += 1
    except (KeyboardInterrupt, SystemExit):
        q.put(("done", done, None))


def solve(miner_addr, prev_work, anchor_hash, target,
          workers=None, start_nonce=0, time_limit=None, verbose=True):
    """Returns (nonce or None, total_hashes, seconds)."""
    if workers is None:
        workers = mp.cpu_count()
    miner_hex = miner_addr[2:].lower()
    anchor_hex = anchor_hash.hex()
    deadline = (time.time() + time_limit) if time_limit else 0

    found = mp.Value("b", False)
    q = mp.Queue()
    procs = []
    for i in range(workers):
        p = mp.Process(
            target=_worker,
            args=(miner_hex, prev_work, anchor_hex, target,
                  start_nonce + i, workers, found, q, deadline),
            daemon=True,
        )
        p.start()
        procs.append(p)

    t0 = time.time()
    result_nonce = None
    total = 0
    remaining = workers
    while remaining > 0:
        kind, done, nonce = q.get()
        total += done
        remaining -= 1
        if kind == "found":
            result_nonce = nonce
            found.value = True
    for p in procs:
        p.join(timeout=2)
        if p.is_alive():
            p.terminate()
    dt = time.time() - t0
    if verbose:
        rate = total / dt if dt else 0
        print(f"[solver] workers={workers} hashes={total:,} time={dt:.1f}s "
              f"rate={rate:,.0f} H/s found={result_nonce is not None}")
    return result_nonce, total, dt


if __name__ == "__main__":
    import chain
    w3, _ = chain.connect()
    c = chain.get_contract(w3)
    addr = chain.read_wallet_address()
    snap = chain.snapshot(w3, c, addr)
    for bits in (20, 24):
        tgt = ((1 << 256) - 1) >> bits
        print(f"Self-test @ {bits}-bit ...")
        n, tot, dt = solve(addr, snap["prevWork"], snap["anchorHash"], tgt,
                           time_limit=60)
        print("  nonce:", n)

"""
csolver.py — drive the fast C keccak_miner across all CPU cores.

Launches one process per core with disjoint nonce lanes (stride = ncores).
Watches a stop_event so we can abort instantly when prevWork changes.
Returns the winning nonce (int) or None.
"""
import os
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "keccak_miner")


def solve(miner_addr, prev_work, anchor_hash, target,
          ncores=None, time_limit=0, stop_event=None, base_nonce=None,
          on_rate=None):
    """
    miner_addr : '0x...' (checksum or lower)
    prev_work  : int
    anchor_hash: bytes32
    target     : int
    Returns (nonce or None, total_hashes, seconds).
    """
    if not os.path.exists(BIN):
        raise RuntimeError("keccak_miner binary not found — compile it first")
    if ncores is None:
        ncores = os.cpu_count() or 2
    miner_hex = miner_addr[2:].lower()
    prev_hex = f"{prev_work:064x}"
    anchor_hex = anchor_hash.hex()
    target_hex = f"{target:064x}"
    # Random-ish base so restarts don't rescan the same low nonces.
    if base_nonce is None:
        base_nonce = int.from_bytes(os.urandom(6), "big")

    procs = []
    for i in range(ncores):
        start = base_nonce + i
        args = [BIN, miner_hex, prev_hex, anchor_hex, target_hex,
                str(start), str(ncores), str(time_limit or 0)]
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        procs.append(p)

    t0 = time.time()
    found_nonce = None
    total_hashes = 0
    rate_seen = {}

    def reader(p, idx):
        nonlocal found_nonce
        for line in iter(p.stdout.readline, ""):
            line = line.strip()
            if line.startswith("FOUND"):
                found_nonce = int(line.split()[1])
                return
            # NONE => that lane timed out
        return

    def err_reader(p, idx):
        for line in iter(p.stderr.readline, ""):
            line = line.strip()
            if line.startswith("RATE"):
                _, h, s = line.split()
                rate_seen[idx] = (int(h), float(s))
                if on_rate:
                    tot = sum(v[0] for v in rate_seen.values())
                    el = max((v[1] for v in rate_seen.values()), default=1)
                    on_rate(tot, el)

    threads = []
    for i, p in enumerate(procs):
        t1 = threading.Thread(target=reader, args=(p, i), daemon=True)
        t2 = threading.Thread(target=err_reader, args=(p, i), daemon=True)
        t1.start(); t2.start()
        threads += [t1, t2]

    # Supervise: stop on found, stop_event, or time_limit.
    while True:
        if found_nonce is not None:
            break
        if stop_event is not None and stop_event.is_set():
            break
        if time_limit and (time.time() - t0) > time_limit:
            break
        if all(p.poll() is not None for p in procs):
            break  # all lanes exited (NONE)
        time.sleep(0.2)

    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=2)
        except Exception:
            p.kill()

    dt = time.time() - t0
    total_hashes = sum(v[0] for v in rate_seen.values())
    return found_nonce, total_hashes, dt


if __name__ == "__main__":
    import chain
    w3, _ = chain.connect()
    c = chain.get_contract(w3)
    addr = chain.read_wallet_address()
    snap = chain.snapshot(w3, c, addr)
    tgt = ((1 << 256) - 1) >> 26  # 26-bit quick test
    print("C-solver test @ 26-bit ...")
    n, tot, dt = solve(addr, snap["prevWork"], snap["anchorHash"], tgt, time_limit=60)
    print("nonce:", n, "hashes:", f"{tot:,}", "sec:", f"{dt:.1f}",
          "rate:", f"{tot/dt:,.0f} H/s" if dt else "")

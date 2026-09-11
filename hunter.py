"""
hunter.py — the smart, patient Hashcats NFT hunter.

Core insight (all verified on-chain):
  * mint = mine(nonce, anchorBlock) with value == mintPrice()
  * PoW: keccak256(packed(miner,nonce,prevWork,anchorHash)) < targetFor(miner)
  * difficulty bits = epochFloor(32) + burst ; burst cools 1 / 10s of no mints
  * prevWork CHANGES on every mint -> any in-progress search is invalidated.
    So we must (a) mine only when burst is low enough to finish fast, and
    (b) restart instantly whenever prevWork changes.

This process:
  1. Polls chain every few seconds.
  2. Computes current difficulty bits & estimated solve time with our C miner.
  3. If bits <= max_bits, snapshots (prevWork, anchorHash, anchorBlock, target)
     and launches the C solver across all cores.
  4. A watcher thread polls prevWork; if it changes, it sets stop_event so the
     solver aborts and we re-snapshot.
  5. On solution: re-read mintPrice, simulate mine(); if --send and funded,
     broadcast and verify ownership.

Safety: never sends unless --send AND the wallet holds enough. Every send is
preceded by a successful eth_call simulation. Private key never printed.
"""
import argparse
import os
import threading
import time
from web3 import Web3

import chain
import csolver
import mint

# Our measured 2-core C hash rate (H/s). Used only for time estimates/logging.
RATE = 1_600_000


def est_seconds(bits):
    return (2 ** bits) / RATE


def fmt_time(s):
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s/60:.1f}m"
    return f"{s/3600:.2f}h"


def prevwork_watcher(w3, c, prev0, stop_event, poll=3):
    """Set stop_event as soon as prevWork changes (someone else minted)."""
    while not stop_event.is_set():
        try:
            pw = c.functions.prevWork().call()
            if pw != prev0:
                stop_event.set()
                return
        except Exception:
            pass
        time.sleep(poll)


def try_send(w3, c, addr, nonce, anchor_block, args):
    sim = mint.build_and_check(w3, c, addr, nonce, anchor_block)
    if not sim["will_succeed"]:
        print(f"[hunter] simulation failed ({sim['error'][:80]}). "
              "Likely prevWork moved; will re-hunt.")
        return False
    price, gas = sim["price"], sim["gas"]
    print(f"[hunter] SOLUTION VALID. nonce={nonce} price={w3.from_wei(price,'ether')} ETH gas={gas}")
    if not args.send:
        print("[hunter] DRY-RUN: not sending. (Re-run with --send once wallet is funded.)")
        return True
    bal = w3.eth.get_balance(Web3.to_checksum_address(addr))
    need = price + int(gas * 1.25) * int(w3.eth.gas_price * 1.2)
    if bal < need:
        print(f"[hunter] WALLET NOT FUNDED: have {w3.from_wei(bal,'ether')} ETH, "
              f"need ~{w3.from_wei(need,'ether')} ETH. Holding the solution and re-checking...")
        # Hold: poll balance for a while; prevWork may change though.
        for _ in range(40):  # ~2 min grace
            time.sleep(3)
            bal = w3.eth.get_balance(Web3.to_checksum_address(addr))
            if bal >= need:
                break
            # if prevWork changed, our solution is dead
            if c.functions.prevWork().call() != None:  # cheap liveness
                pass
        if bal < need:
            print("[hunter] still unfunded; solution may expire if a mint happens. Re-hunting.")
            return False
    # Final re-simulate right before sending (price/prevWork may have moved).
    sim2 = mint.build_and_check(w3, c, addr, nonce, anchor_block, verbose=False)
    if not sim2["will_succeed"]:
        print("[hunter] state moved right before send; re-hunting.")
        return False
    rcpt = mint.send(w3, c, nonce, anchor_block, sim2["price"], sim2["gas"], allow_send=True)
    if rcpt.status == 1:
        bal_n = c.functions.balanceOf(Web3.to_checksum_address(addr)).call()
        print(f"[hunter] SUCCESS! wallet now holds {bal_n} Hashcat(s). "
              f"tx in block {rcpt.blockNumber}")
        return True
    print("[hunter] tx reverted on-chain (status 0). Re-hunting.")
    return False


def hunt_once(w3, c, addr, args):
    # Snapshot
    snap = chain.snapshot(w3, c, addr)
    bits = snap["targetBits"]
    est = est_seconds(bits)
    print(f"[hunter] burst={snap['burst']:2d} bits={bits} est={fmt_time(est)} "
          f"minted={snap['totalMinted']} price={w3.from_wei(snap['mintPrice'],'ether')} "
          f"prevWork={str(snap['prevWork'])[:8]}...")
    if bits > args.max_bits:
        return "wait"

    print(f"[hunter] WINDOW OPEN ({bits} bits, ~{fmt_time(est)}). Mining on all cores...")
    stop_event = threading.Event()
    watcher = threading.Thread(
        target=prevwork_watcher, args=(w3, c, snap["prevWork"], stop_event),
        daemon=True)
    watcher.start()

    nonce, hashes, dt = csolver.solve(
        addr, snap["prevWork"], snap["anchorHash"], snap["target"],
        time_limit=args.budget, stop_event=stop_event,
    )
    stop_event.set()

    if nonce is None:
        if dt < args.budget - 1:
            print(f"[hunter] aborted after {dt:.0f}s ({hashes:,} hashes) — "
                  "prevWork changed (someone minted). Re-hunting.")
        else:
            print(f"[hunter] budget elapsed ({hashes:,} hashes), no solution. Re-hunting.")
        return "retry"

    print(f"[hunter] FOUND nonce={nonce} in {dt:.0f}s ({hashes:,} hashes, "
          f"{hashes/dt:,.0f} H/s)")
    ok = try_send(w3, c, addr, nonce, snap["anchorBlock"], args)
    return "done" if ok and args.send else "retry"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--max-bits", type=int, default=33,
                    help="mine only when difficulty <= this (default 33 => <=~90min)")
    ap.add_argument("--budget", type=int, default=7200,
                    help="max seconds per mining attempt (default 7200)")
    ap.add_argument("--poll", type=int, default=6, help="seconds between window checks")
    args = ap.parse_args()

    w3, url = chain.connect()
    c = chain.get_contract(w3)
    addr = chain.read_wallet_address()
    print(f"[hunter] RPC={url} miner={chain.masked_address(addr)} "
          f"mode={'SEND' if args.send else 'DRY-RUN'} max_bits={args.max_bits} "
          f"(<= ~{fmt_time(est_seconds(args.max_bits))})")
    print(f"[hunter] C miner rate ~{RATE:,} H/s. Waiting for a viable quiet window...")

    while True:
        try:
            r = hunt_once(w3, c, addr, args)
            if r == "done":
                print("[hunter] mission complete.")
                break
            if r == "wait":
                time.sleep(args.poll)
            # "retry" loops immediately
        except KeyboardInterrupt:
            print("\n[hunter] stopped by user.")
            break
        except Exception as e:
            print("[hunter] error:", str(e)[:120])
            time.sleep(args.poll)


if __name__ == "__main__":
    main()

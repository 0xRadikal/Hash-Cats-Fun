"""
gpu_hunter.py — runs ON the rented GPU box. Drives the CUDA miner and submits.

Flow (each round):
  1. Snapshot chain: prevWork, anchorHash, anchorBlock, target, mintPrice.
  2. Compute difficulty bits; if too high for a per-gap win, log and re-poll
     (unless --force is set — then mine anyway).
  3. Launch ./keccak_cuda for a short slice (e.g. one inter-mint gap budget).
     It prints FOUND <nonce> or NONE.
  4. If FOUND: re-check prevWork unchanged, simulate mine(), and if --send +
     funded, broadcast, then verify ownership.
  5. If prevWork changed mid-slice, discard and restart (fresh prevWork).

Requires: web3, eth-account, and the compiled ./keccak_cuda binary.
Copy the whole hashcats/ folder here, or at least: this file, ../chain.py,
../config.py, ../mint.py, ../hashcats_abi.json, and the compiled keccak_cuda.

Set the wallet path via env HASHCATS_WALLET or place wallet-test.env alongside.
"""
import argparse
import os
import subprocess
import sys
import time

# allow importing the parent package modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from web3 import Web3
import chain
import mint

CUDA_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keccak_cuda")


def run_cuda(miner_addr, prev_work, anchor_hash, target, base_nonce,
             seconds, blocks, threads, iters):
    args = [CUDA_BIN,
            miner_addr[2:].lower(),
            f"{prev_work:064x}",
            anchor_hash.hex(),
            f"{target:064x}",
            str(base_nonce),
            str(seconds),
            str(blocks), str(threads), str(iters)]
    p = subprocess.run(args, capture_output=True, text=True)
    nonce = None
    rate = None
    for line in p.stderr.splitlines():
        if line.startswith("RATE"):
            _, h, s = line.split()
            if float(s) > 0:
                rate = int(h) / float(s)
    for line in p.stdout.splitlines():
        if line.startswith("FOUND"):
            nonce = int(line.split()[1])
    return nonce, rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--max-bits", type=int, default=40,
                    help="mine only when difficulty <= this (default 40)")
    ap.add_argument("--force", action="store_true",
                    help="mine regardless of difficulty (for testing throughput)")
    ap.add_argument("--slice", type=int, default=20,
                    help="seconds per CUDA slice (aim ~ one inter-mint gap)")
    ap.add_argument("--blocks", type=int, default=8192)
    ap.add_argument("--threads", type=int, default=256)
    ap.add_argument("--iters", type=int, default=8192)
    args = ap.parse_args()

    if not os.path.exists(CUDA_BIN):
        print(f"ERROR: {CUDA_BIN} not found. Build it first:\n"
              f"  nvcc -O3 -arch=sm_XX keccak_cuda.cu -o keccak_cuda")
        sys.exit(2)

    w3, url = chain.connect()
    c = chain.get_contract(w3)
    addr = chain.read_wallet_address()
    print(f"[gpu] RPC={url} miner={chain.masked_address(addr)} "
          f"mode={'SEND' if args.send else 'DRY-RUN'} max_bits={args.max_bits}")

    base = int.from_bytes(os.urandom(6), "big")
    while True:
        try:
            snap = chain.snapshot(w3, c, addr)
            bits = snap["targetBits"]
            print(f"[gpu] burst={snap['burst']} bits={bits} "
                  f"minted={snap['totalMinted']} "
                  f"price={w3.from_wei(snap['mintPrice'],'ether')} ETH "
                  f"prevWork={str(snap['prevWork'])[:8]}...")
            if bits > args.max_bits and not args.force:
                time.sleep(args.slice)
                continue

            base += 0x1000000
            nonce, rate = run_cuda(addr, snap["prevWork"], snap["anchorHash"],
                                   snap["target"], base, args.slice,
                                   args.blocks, args.threads, args.iters)
            if rate:
                print(f"[gpu] rate ~{rate/1e9:.2f} GH/s")
            if nonce is None:
                continue

            # prevWork must be unchanged for the solution to be valid.
            if c.functions.prevWork().call() != snap["prevWork"]:
                print("[gpu] prevWork changed before submit; discarding solution.")
                continue

            sim = mint.build_and_check(w3, c, addr, nonce, snap["anchorBlock"])
            if not sim["will_succeed"]:
                print(f"[gpu] simulate failed: {sim['error'][:80]}")
                continue
            print(f"[gpu] SOLUTION nonce={nonce} price={w3.from_wei(sim['price'],'ether')} ETH")
            if not args.send:
                print("[gpu] DRY-RUN: not sending.")
                continue
            bal = w3.eth.get_balance(Web3.to_checksum_address(addr))
            need = sim["price"] + int(sim["gas"] * 1.25) * int(w3.eth.gas_price * 1.2)
            if bal < need:
                print(f"[gpu] NOT FUNDED: have {w3.from_wei(bal,'ether')} need "
                      f"~{w3.from_wei(need,'ether')} ETH.")
                continue
            rcpt = mint.send(w3, c, nonce, snap["anchorBlock"],
                             sim["price"], sim["gas"], allow_send=True)
            if rcpt.status == 1:
                held = c.functions.balanceOf(Web3.to_checksum_address(addr)).call()
                print(f"[gpu] SUCCESS! now holding {held} Hashcat(s).")
                break
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("[gpu] error:", str(e)[:120])
            time.sleep(args.slice)


if __name__ == "__main__":
    main()

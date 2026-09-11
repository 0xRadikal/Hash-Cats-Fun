"""
bot.py — the smart, patient Hashcats miner.

Strategy (the "brains over brawn" approach):
  1. Poll the chain. Compute current difficulty bits = f(burst).
  2. Only start mining when bits <= MAX_BITS (i.e. burst low enough that a
     2-core CPU can realistically solve it: 32-33 bits).
  3. When the window opens, grab a fresh (anchorBlock, anchorHash, prevWork,
     target) snapshot and mine with all cores, with a time budget.
  4. If a mint by someone else happens mid-search (burst jumps / anchor
     changes), re-snapshot and continue.
  5. On solution: simulate mine(), and if --send is given, broadcast it and
     verify the NFT landed in our wallet.

Run modes:
  python3 bot.py               -> DRY RUN (never sends; waits for window)
  python3 bot.py --send        -> will actually mint (requires funded wallet)
  python3 bot.py --max-bits 33 -> allow slightly harder windows
"""
import argparse
import time
from web3 import Web3

import chain
import solver_mp
import mint


def difficulty_bits(target):
    return 256 - target.bit_length()


def wait_for_window(w3, c, addr, max_bits, poll=5, verbose=True):
    """Block until difficulty bits <= max_bits. Returns a fresh snapshot."""
    while True:
        snap = chain.snapshot(w3, c, addr)
        bits = snap["targetBits"]
        if verbose:
            est = 2 ** bits
            print(f"  burst={snap['burst']:2d} bits={bits} "
                  f"(~{est/260000/3600:.2f}h) minted={snap['totalMinted']} "
                  f"-> {'WINDOW OPEN' if bits <= max_bits else 'waiting'}")
        if bits <= max_bits:
            return snap
        time.sleep(poll)


def mine_once(w3, c, addr, max_bits, time_budget, allow_send):
    snap = wait_for_window(w3, c, addr, max_bits)
    print(f"[bot] window open at {snap['targetBits']} bits. Mining "
          f"(budget {time_budget}s) ...")
    nonce, total, dt = solver_mp.solve(
        addr, snap["prevWork"], snap["anchorHash"], snap["target"],
        time_limit=time_budget,
    )
    if nonce is None:
        print(f"[bot] no solution within budget ({total:,} hashes). Re-checking window.")
        return False

    # Verify the solution locally against a FRESH read of prevWork/anchor:
    # if the network state moved, our anchor may be stale.
    fresh = chain.snapshot(w3, c, addr)
    if (fresh["prevWork"] != snap["prevWork"]
            or fresh["anchorHash"] != snap["anchorHash"]):
        print("[bot] state moved during search (prevWork/anchor changed). "
              "Solution is for the old anchor; retrying.")
        return False

    sim = mint.build_and_check(w3, c, addr, nonce, snap["anchorBlock"])
    if not sim["will_succeed"]:
        print("[bot] simulation says it would revert; not sending.")
        return False

    print(f"[bot] SOLUTION FOUND. nonce={nonce}")
    print(f"[bot] would mint for {w3.from_wei(sim['price'],'ether')} ETH, "
          f"gas={sim['gas']}")

    if not allow_send:
        print("[bot] DRY RUN — not sending. Re-run with --send (funded wallet) to mint.")
        return True

    bal = w3.eth.get_balance(Web3.to_checksum_address(addr))
    need = sim["price"] + sim["gas"] * int(w3.eth.gas_price * 1.2)
    if bal < need:
        print(f"[bot] insufficient balance: have {w3.from_wei(bal,'ether')} ETH, "
              f"need ~{w3.from_wei(need,'ether')} ETH. Fund the wallet first.")
        return True

    rcpt = mint.send(w3, c, nonce, snap["anchorBlock"], sim["price"], sim["gas"],
                     allow_send=True)
    if rcpt.status == 1:
        verify_nft(w3, c, addr)
    return True


def verify_nft(w3, c, addr):
    bal = c.functions.balanceOf(Web3.to_checksum_address(addr)).call()
    total = c.functions.totalMinted().call()
    print(f"[bot] wallet now holds {bal} Hashcat(s). totalMinted={total}")
    # newest tokenId is totalMinted-based; try to read tokenURI of last owned
    try:
        # the just-minted id is typically totalMinted-1 or returned by tx; we
        # scan a small recent range to find one we own.
        for tid in range(max(0, total - 3), total + 1):
            try:
                if c.functions.ownerOf(tid).call().lower() == addr.lower():
                    print(f"[bot] we own tokenId {tid}")
            except Exception:
                pass
    except Exception as e:
        print("[bot] verify note:", str(e)[:80])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually broadcast the mint tx")
    ap.add_argument("--max-bits", type=int, default=32,
                    help="only mine when difficulty <= this many bits (default 32)")
    ap.add_argument("--budget", type=int, default=1800,
                    help="seconds to spend per mining attempt (default 1800)")
    ap.add_argument("--attempts", type=int, default=1000,
                    help="how many window-attempts before giving up")
    args = ap.parse_args()

    w3, url = chain.connect()
    c = chain.get_contract(w3)
    addr = chain.read_wallet_address()
    print(f"[bot] RPC={url} miner={chain.masked_address(addr)} "
          f"mode={'SEND' if args.send else 'DRY-RUN'} max_bits={args.max_bits}")
    for i in range(args.attempts):
        print(f"\n[bot] attempt {i+1}")
        done = mine_once(w3, c, addr, args.max_bits, args.budget, args.send)
        if done and not args.send:
            break  # dry-run: one successful proof is enough
        if done and args.send:
            break


if __name__ == "__main__":
    main()

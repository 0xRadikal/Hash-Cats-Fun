"""
verify_correctness.py — prove, with zero guessing, that our miner is correct.

Steps:
 1. Read live snapshot from chain.
 2. For 5 random nonces, assert our work_hash_int == contract.workHash(...).
 3. Solve PoW at an EASY synthetic target (so it finishes in <1s) and
    assert the found nonce really satisfies workHash < target both locally
    AND according to the on-chain pure function.
No transaction is sent. No funds are needed.
"""
import secrets
from web3 import Web3

import chain
import miner


def main():
    w3, url = chain.connect()
    c = chain.get_contract(w3)
    addr = chain.read_wallet_address()
    print(f"RPC: {url}")
    print(f"chainId: {w3.eth.chain_id}")
    print(f"miner (masked): {chain.masked_address(addr)}")

    snap = chain.snapshot(w3, c, addr)
    print(f"anchorBlock={snap['anchorBlock']} targetBits={snap['targetBits']} "
          f"burst={snap['burst']} mintPrice={w3.from_wei(snap['mintPrice'],'ether')} ETH")

    miner_bytes = bytes.fromhex(addr[2:])
    prev = snap["prevWork"]
    ah = snap["anchorHash"]

    # 1) Cross-check hash function against the contract for random nonces.
    print("\n[1] Cross-checking work_hash against contract.workHash ...")
    ok = True
    for _ in range(5):
        nonce = secrets.randbelow(1 << 256)
        local = miner.work_hash_int(miner_bytes, nonce, prev, ah)
        onchain = c.functions.workHash(
            Web3.to_checksum_address(addr), nonce, prev, ah
        ).call()
        match = local == onchain
        ok = ok and match
        print(f"    nonce={hex(nonce)[:14]}...  match={match}")
    assert ok, "HASH MISMATCH — do not proceed"
    print("    => 100% MATCH. Our PoW hash is identical to the contract.")

    # 2) Solve at an EASY target to prove the search loop works end-to-end.
    print("\n[2] Solving at an EASY synthetic target (16-bit) ...")
    easy_bits = 16
    easy_target = ((1 << 256) - 1) >> easy_bits
    nonce, done = miner.solve_range(
        miner_bytes, prev, ah, easy_target, start_nonce=0, count=5_000_000
    )
    assert nonce is not None, "failed to find easy solution (unexpected)"
    local = miner.work_hash_int(miner_bytes, nonce, prev, ah)
    onchain = c.functions.workHash(
        Web3.to_checksum_address(addr), nonce, prev, ah
    ).call()
    print(f"    found nonce={nonce} after {done} hashes")
    print(f"    local hash  < easy_target : {local < easy_target}")
    print(f"    onchain hash== local      : {onchain == local}")
    assert local < easy_target and onchain == local
    print("    => Solver produces valid, contract-verifiable solutions.")

    print("\nALL CHECKS PASSED. Miner logic is proven correct (no tx sent).")


if __name__ == "__main__":
    main()

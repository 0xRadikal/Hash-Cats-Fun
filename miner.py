"""
miner.py — the Proof-of-Work solver.

Contract rule (verified on-chain):
    workHash = keccak256( abi.encodePacked(miner, nonce, prevWork, anchorHash) )
    where:
        miner      : address   (20 bytes)
        nonce      : uint256   (32 bytes, big-endian)
        prevWork   : uint256   (32 bytes, big-endian)
        anchorHash : bytes32   (32 bytes)
    Solution is valid iff  workHash < target.

We build the 116-byte preimage once and only splice the 32-byte nonce
window each iteration for speed.
"""
import time
import os
from Crypto.Hash import keccak


def _keccak256(b: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(b)
    return h.digest()


def work_hash_int(miner_bytes: bytes, nonce: int, prev_work: int, anchor_hash: bytes) -> int:
    """Reference implementation — matches contract.workHash exactly."""
    pre = (
        miner_bytes
        + nonce.to_bytes(32, "big")
        + prev_work.to_bytes(32, "big")
        + anchor_hash
    )
    return int.from_bytes(_keccak256(pre), "big")


def _prefix_suffix(miner_bytes: bytes, prev_work: int, anchor_hash: bytes):
    """Static parts around the 32-byte nonce."""
    prefix = miner_bytes                       # 20 bytes, before nonce
    suffix = prev_work.to_bytes(32, "big") + anchor_hash  # after nonce
    return prefix, suffix


def solve_range(miner_bytes, prev_work, anchor_hash, target, start_nonce, count,
                progress_every=0, stop_flag=None):
    """
    Search nonces in [start_nonce, start_nonce+count).
    Returns (nonce, hashes_done) on success, or (None, hashes_done) if exhausted.
    """
    prefix, suffix = _prefix_suffix(miner_bytes, prev_work, anchor_hash)
    h = keccak.new(digest_bits=256)  # template not reusable; new per hash
    done = 0
    nonce = start_nonce
    end = start_nonce + count
    kfun = keccak.new
    while nonce < end:
        if stop_flag is not None and stop_flag.value:
            return None, done
        pre = prefix + nonce.to_bytes(32, "big") + suffix
        k = kfun(digest_bits=256)
        k.update(pre)
        if int.from_bytes(k.digest(), "big") < target:
            return nonce, done + 1
        nonce += 1
        done += 1
        if progress_every and (done % progress_every == 0):
            pass
    return None, done


def benchmark(seconds=3.0):
    """Measure single-core hash rate on this machine."""
    miner_bytes = bytes.fromhex("a842e3ab069562db379c35dd52c77ef214324004")
    prev = 12345678901234567890
    anchor = bytes(32)
    prefix, suffix = _prefix_suffix(miner_bytes, prev, anchor)
    t0 = time.time()
    n = 0
    kfun = keccak.new
    while time.time() - t0 < seconds:
        for i in range(20000):
            pre = prefix + (n + i).to_bytes(32, "big") + suffix
            k = kfun(digest_bits=256)
            k.update(pre)
        n += 20000
    dt = time.time() - t0
    return n / dt

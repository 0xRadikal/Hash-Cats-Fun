"""
gpu_hunter_continuous.py — CONTINUOUS GPU miner (no waiting for quiet windows).

Runs ON the rented GPU box. Mines non-stop against the freshest prevWork.
Because PoW is memoryless, continuous mining gives expected time-to-cat =
2^bits / rate, regardless of how often others mint — as long as we:
  * refresh (prevWork, anchorHash, target) frequently (short CUDA slices), and
  * submit instantly with a gas price slightly above the crowd (front-run).

Key differences vs gpu_hunter.py:
  * No max_bits gate by default — mine at whatever difficulty is live.
  * Very short slices (default 2s) so a prevWork reset wastes <~2s of work.
  * On solution: re-verify prevWork unchanged, simulate, then send with a gas
    bump so our tx lands before competitors paying default gas.

Usage on the GPU box:
  python3 gpu_hunter_continuous.py                 # dry-run
  python3 gpu_hunter_continuous.py --send          # real (funded wallet)
  python3 gpu_hunter_continuous.py --send --gas-mult 1.5   # stronger front-run
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from web3 import Web3
import chain
import mint

CUDA_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keccak_cuda")


def run_slice(miner_addr, prev_work, anchor_hash, target, base_nonce,
              seconds, blocks, threads, iters):
    args = [CUDA_BIN, miner_addr[2:].lower(), f"{prev_work:064x}",
            anchor_hash.hex(), f"{target:064x}", str(base_nonce),
            str(seconds), str(blocks), str(threads), str(iters)]
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


def fresh_state(w3, c, addr):
    b = w3.batch_requests()
    b.add(c.functions.currentAnchor())
    b.add(c.functions.prevWork())
    b.add(c.functions.targetFor(Web3.to_checksum_address(addr)))
    b.add(c.functions.mintPrice())
    anchor, prev, target, price = b.execute()
    return {
        "anchorBlock": anchor[0], "anchorHash": anchor[1],
        "prevWork": int(prev), "target": int(target),
        "bits": 256 - int(target).bit_length(), "mintPrice": int(price),
    }


def verify_local(addr, nonce, prev_work, anchor_hash, target):
    """Locally verify a solution using our contract-identical keccak.
    This is authoritative (proven bit-exact) and needs no funds."""
    from Crypto.Hash import keccak as _k
    pre = (bytes.fromhex(addr[2:]) + nonce.to_bytes(32, "big")
           + prev_work.to_bytes(32, "big") + anchor_hash)
    h = _k.new(digest_bits=256); h.update(pre)
    return int.from_bytes(h.digest(), "big") < target


def submit(w3, c, addr, nonce, anchor_block, gas_mult, allow_send,
           prev_work=None, anchor_hash=None, target=None):
    # Local, fund-free authoritative check (payment is checked before solution
    # on-chain, so eth_call with value=0 cannot validate a solution).
    if prev_work is not None and not verify_local(addr, nonce, prev_work, anchor_hash, target):
        return False, "local check: not a valid solution (state moved?)"
    if not allow_send:
        price = c.functions.mintPrice().call()
        return True, (f"DRY-RUN VALID solution nonce={nonce} "
                      f"(would pay {w3.from_wei(price,'ether')} ETH)")
    sim = mint.build_and_check(w3, c, addr, nonce, anchor_block, verbose=False)
    if not sim["will_succeed"]:
        return False, f"sim failed: {sim['error'][:60]}"
    bal = w3.eth.get_balance(Web3.to_checksum_address(addr))
    gp = int(w3.eth.gas_price * gas_mult)
    need = sim["price"] + int(sim["gas"] * 1.3) * gp
    if bal < need:
        return False, (f"NOT FUNDED: have {w3.from_wei(bal,'ether')} "
                       f"need ~{w3.from_wei(need,'ether')} ETH")
    from eth_account import Account
    acct = Account.from_key(chain._read_private_key())
    tx = c.functions.mine(nonce, anchor_block).build_transaction({
        "from": acct.address, "value": sim["price"],
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": int(sim["gas"] * 1.3), "gasPrice": gp,
        "chainId": w3.eth.chain_id,
    })
    signed = acct.sign_transaction(tx)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
    if rcpt.status == 1:
        held = c.functions.balanceOf(acct.address).call()
        return True, f"SUCCESS tx={txh.hex()} block={rcpt.blockNumber} holding={held}"
    return False, f"tx reverted (status 0) tx={txh.hex()}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--slice", type=int, default=2, help="CUDA slice seconds (short!)")
    ap.add_argument("--blocks", type=int, default=8192)
    ap.add_argument("--threads", type=int, default=256)
    ap.add_argument("--iters", type=int, default=2048)
    ap.add_argument("--gas-mult", type=float, default=1.5, help="gas price multiplier for front-run")
    ap.add_argument("--max-bits", type=int, default=64, help="optional cap; default mine anything")
    args = ap.parse_args()

    if not os.path.exists(CUDA_BIN):
        print(f"ERROR: {CUDA_BIN} missing. Run bootstrap.sh first."); sys.exit(2)

    w3, url = chain.connect()
    c = chain.get_contract(w3)
    addr = chain.read_wallet_address()
    print(f"[cont] RPC={url} miner={chain.masked_address(addr)} "
          f"mode={'SEND' if args.send else 'DRY-RUN'} slice={args.slice}s gasx{args.gas_mult}")

    base = int.from_bytes(os.urandom(6), "big")
    slices = 0
    total_hashes = 0
    t_start = time.time()
    st = fresh_state(w3, c, addr)
    last_refresh = time.time()

    while True:
        try:
            # refresh chain state every ~ slice seconds (prevWork/anchor/target)
            if time.time() - last_refresh > max(1, args.slice):
                st = fresh_state(w3, c, addr)
                last_refresh = time.time()
            if st["bits"] > args.max_bits:
                time.sleep(args.slice); continue

            base += args.blocks * args.threads * args.iters
            nonce, rate = run_slice(addr, st["prevWork"], st["anchorHash"],
                                    st["target"], base, args.slice,
                                    args.blocks, args.threads, args.iters)
            slices += 1
            if rate:
                total_hashes += rate * args.slice
            if slices % 10 == 0:
                el = time.time() - t_start
                eff = total_hashes / el if el else 0
                print(f"[cont] bits={st['bits']} slices={slices} "
                      f"~{eff/1e9:.2f} GH/s effective, uptime {el/60:.1f}m", flush=True)

            if nonce is None:
                continue

            # verify prevWork still current
            cur_prev = c.functions.prevWork().call()
            if cur_prev != st["prevWork"]:
                # stale: refresh and keep mining
                st = fresh_state(w3, c, addr); last_refresh = time.time()
                continue
            ok, msg = submit(w3, c, addr, nonce, st["anchorBlock"],
                             args.gas_mult, args.send,
                             prev_work=st["prevWork"], anchor_hash=st["anchorHash"],
                             target=st["target"])
            print(f"[cont] {msg}", flush=True)
            if ok and args.send:
                print("[cont] MISSION COMPLETE."); break
            # after a dry-run hit, keep going to show it works
            st = fresh_state(w3, c, addr); last_refresh = time.time()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("[cont] error:", str(e)[:100], flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()

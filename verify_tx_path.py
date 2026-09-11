"""
verify_tx_path.py — prove the mint transaction PATH is correct without sending
and without needing a valid PoW solution yet.

Logic:
  * mine(nonce, anchorBlock) reverts with BadSolution when the nonce doesn't
    satisfy the PoW, and with WrongPayment when value != mintPrice.
  * By simulating with (a) a junk nonce + correct value, and (b) a junk nonce +
    wrong value, we can read which custom error the contract returns.
  * If (a) => BadSolution and (b) => WrongPayment, then our ABI encoding, the
    function selector, the value handling and the anchorBlock argument are ALL
    correct — the only remaining task is finding a nonce, which is pure compute.

Decodes 4-byte custom-error selectors against the ABI. Sends nothing.
"""
from web3 import Web3
from eth_utils import function_abi_to_4byte_selector
import chain


def build_error_table(abi):
    table = {}
    for item in abi:
        if item.get("type") == "error":
            # selector = keccak(name(types))[:4]
            sel = function_abi_to_4byte_selector(item)
            table[sel.hex()] = item["name"]
    return table


def classify_revert(exc, err_table):
    """Extract a 4-byte selector from a web3 revert and map to error name."""
    msg = str(exc)
    # web3 v8 often includes the data as hex in the message; also check .data
    data = getattr(exc, "data", None)
    hexstr = None
    if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
        hexstr = data
    else:
        import re
        m = re.search(r"0x[0-9a-fA-F]{8,}", msg)
        if m:
            hexstr = m.group(0)
    if not hexstr:
        return f"(no selector) {msg[:80]}"
    sel = hexstr[2:10].lower()
    return err_table.get(sel, f"unknown-selector 0x{sel}") + f"  [0x{sel}]"


def main():
    w3, url = chain.connect()
    c = chain.get_contract(w3)
    abi = chain.load_abi()
    addr = chain.read_wallet_address()
    err_table = build_error_table(abi)

    snap = chain.snapshot(w3, c, addr)
    price = snap["mintPrice"]
    anchor_block = snap["anchorBlock"]
    junk_nonce = 1  # almost certainly not a valid solution

    print(f"RPC={url} miner={chain.masked_address(addr)}")
    print(f"mintPrice={w3.from_wei(price,'ether')} ETH  anchorBlock={anchor_block}")
    print(f"known error selectors: {len(err_table)}\n")

    # Case A: correct value, junk nonce  -> expect BadSolution
    print("[A] simulate mine(junk_nonce, anchorBlock) with CORRECT value:")
    try:
        c.functions.mine(junk_nonce, anchor_block).call(
            {"from": Web3.to_checksum_address(addr), "value": price})
        print("    (did NOT revert — unexpected; junk nonce should fail)")
    except Exception as e:
        print("    revert ->", classify_revert(e, err_table))

    # Case B: wrong value, junk nonce -> expect WrongPayment
    print("[B] simulate mine(junk_nonce, anchorBlock) with WRONG value (price-1):")
    try:
        c.functions.mine(junk_nonce, anchor_block).call(
            {"from": Web3.to_checksum_address(addr), "value": max(0, price - 1)})
        print("    (did NOT revert — unexpected)")
    except Exception as e:
        print("    revert ->", classify_revert(e, err_table))

    print("\nInterpretation:")
    print("  A == BadSolution  => tx path, ABI, selector, value & anchor arg are correct;")
    print("                       only a valid nonce is missing (pure compute).")
    print("  B == WrongPayment => the contract checks msg.value against mintPrice exactly.")


if __name__ == "__main__":
    main()

"""
mint.py — build, simulate, and (optionally) send the mine() transaction.

Safety design:
  * We ALWAYS eth_call-simulate mine(nonce, anchorBlock) with value=mintPrice
    before doing anything else. If it reverts, we abort — no gas wasted.
  * We re-read mintPrice right before sending (the site does the same; price
    drifts between epochs -> WrongPayment otherwise).
  * send() is gated behind an explicit allow_send=True argument so nothing is
    ever broadcast by accident.
"""
from web3 import Web3
import chain


def build_and_check(w3, c, addr, nonce, anchor_block, verbose=True):
    """
    Simulate mine(nonce, anchorBlock). Returns dict with price, gas, and a
    'will_succeed' bool. Sends NOTHING.
    """
    price = c.functions.mintPrice().call()
    fn = c.functions.mine(nonce, anchor_block)
    result = {"price": price, "will_succeed": False, "gas": None, "error": None}
    try:
        # eth_call simulation from the miner account with the exact value.
        fn.call({"from": Web3.to_checksum_address(addr), "value": price})
        gas = fn.estimate_gas({"from": Web3.to_checksum_address(addr), "value": price})
        result["will_succeed"] = True
        result["gas"] = gas
    except Exception as e:
        result["error"] = str(e)
    if verbose:
        if result["will_succeed"]:
            print(f"[mint] SIMULATION OK. price={w3.from_wei(price,'ether')} ETH "
                  f"gas={result['gas']}")
        else:
            print(f"[mint] SIMULATION FAILED: {result['error'][:120]}")
    return result


def send(w3, c, nonce, anchor_block, price, gas, allow_send=False):
    """
    Broadcast mine(). Requires allow_send=True. Signs locally; the private key
    never leaves this process and is never logged.
    """
    if not allow_send:
        raise RuntimeError("send() called without allow_send=True — refusing.")
    from eth_account import Account

    pk = chain._read_private_key()
    acct = Account.from_key(pk)
    addr = acct.address

    fee = w3.eth.gas_price
    tx = c.functions.mine(nonce, anchor_block).build_transaction({
        "from": addr,
        "value": price,
        "nonce": w3.eth.get_transaction_count(addr),
        "gas": int(gas * 1.25),
        "gasPrice": int(fee * 1.2),
        "chainId": w3.eth.chain_id,
    })
    signed = acct.sign_transaction(tx)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"[mint] sent tx: {txh.hex()}")
    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
    print(f"[mint] mined in block {rcpt.blockNumber} status={rcpt.status}")
    return rcpt

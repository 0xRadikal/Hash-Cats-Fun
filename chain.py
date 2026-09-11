"""
chain.py — thin, well-tested helpers around the Hashcats contract.

No guessing: every read maps to a real ABI function we extracted from the
site bundle and confirmed against the live chain.
"""
import json
import os
from web3 import Web3

import config


def load_abi():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, config.ABI_PATH)) as f:
        return json.load(f)


def connect():
    """Return a connected Web3 instance, trying each RPC in order."""
    last = None
    for url in config.RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
            if w3.is_connected() and w3.eth.chain_id == config.CHAIN_ID:
                return w3, url
        except Exception as e:  # pragma: no cover - network dependent
            last = e
    raise RuntimeError(f"Could not connect to any Robinhood RPC: {last}")


def get_contract(w3):
    return w3.eth.contract(
        address=Web3.to_checksum_address(config.COLLECTION),
        abi=load_abi(),
    )


def read_wallet_address():
    """Derive the wallet address WITHOUT ever exposing the private key."""
    from eth_account import Account

    pk = _read_private_key()
    return Account.from_key(pk).address


def _read_private_key():
    """Internal only. Returns the raw private key string. Never log this."""
    with open(config.WALLET_ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                if key.strip() in ("private-key", "private_key", "PRIVATE_KEY"):
                    return val.strip()
    raise RuntimeError("private-key not found in wallet env file")


def masked_address(addr):
    return addr[:6] + "..." + addr[-4:]


def snapshot(w3, c, miner):
    """
    One consistent read of everything the miner needs.
    Returns a dict. All values are read live from chain.
    """
    miner = Web3.to_checksum_address(miner)
    anchor_block, anchor_hash = c.functions.currentAnchor().call()
    prev_work = c.functions.prevWork().call()
    target = c.functions.targetFor(miner).call()
    mint_price = c.functions.mintPrice().call()
    burst = c.functions.currentBurst().call()
    total = c.functions.totalMinted().call()
    epoch = c.functions.currentEpoch().call()
    block_no = w3.eth.block_number
    return {
        "miner": miner,
        "anchorBlock": anchor_block,
        "anchorHash": anchor_hash,          # bytes32
        "prevWork": prev_work,              # uint256
        "target": target,                   # uint256
        "targetBits": 256 - target.bit_length(),  # leading zero bits
        "mintPrice": mint_price,            # wei
        "burst": burst,
        "totalMinted": total,
        "epoch": epoch,
        "blockNumber": block_no,
    }

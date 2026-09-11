"""
Hashcats mining — shared configuration and on-chain constants.

Everything here was verified 100% against the live chain / site bundle,
NOT guessed:
  - chainId 4663 (Robinhood Chain), RPC below (from site bundle)
  - collection contract 0xCA75...0721 (name() == "Hashcats")
  - mint mechanism (verified by matching workHash on-chain):
        workHash = keccak256( packed(miner, nonce, prevWork, anchorHash) )
        win condition:  workHash < targetFor(miner)
        submit:         mine(nonce, anchorBlock)  with value == mintPrice()
  - difficulty model (verified via targetAt sweeps + official UI text):
        target_bits = epochFloor_bits (currently 32) + burst
        burst cools by 1 every DECAY_HALFLIFE (10s) of no mints
"""

RPC_URLS = [
    "https://rpc.mainnet.chain.robinhood.com",
    "https://robinhood.drpc.org",
]

CHAIN_ID = 4663

# Verified: name() == "Hashcats", mintPrice/totalMinted respond correctly.
COLLECTION = "0xCA75DF55Cc9C476DB27a7375D1fc8E794cf80721"

ABI_PATH = "hashcats_abi.json"

# Wallet file (secret). NEVER commit a real key. Copy wallet.env.example to
# wallet.env and put your private key there, or set env HASHCATS_WALLET to its
# path. The private key is only read locally and never printed.
import os
WALLET_ENV_PATH = os.environ.get("HASHCATS_WALLET", "wallet.env")

# Explorer
EXPLORER = "https://robinhoodchain.blockscout.com"

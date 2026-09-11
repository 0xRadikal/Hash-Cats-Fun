#!/usr/bin/env bash
###############################################################################
# setup_and_run.sh — one-command setup + run on a fresh GPU server.
#
# It will:
#   1. install prerequisites (git, build tools, python libs)
#   2. clone (or update) the project repo
#   3. detect the GPU architecture and compile the CUDA miner + verifier
#   4. run the bit-exact correctness check (aborts if it fails)
#   5. benchmark the GPU hashrate
#   6. write the wallet file from the HASHCATS_PK env var (never committed)
#   7. start the CONTINUOUS miner in the requested mode (dry-run by default)
#
# Usage on the GPU box:
#   export HASHCATS_PK=0xYOUR_PRIVATE_KEY         # required for --send
#   export HASHCATS_MODE=dry                      # dry (default) | send
#   bash setup_and_run.sh
#
# Nothing is broadcast unless HASHCATS_MODE=send AND the wallet is funded.
###############################################################################
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/0xRadikal/Hash-Cats-Fun.git}"
WORKDIR="${WORKDIR:-$HOME/hashcats}"
MODE="${HASHCATS_MODE:-dry}"
GAS_MULT="${GAS_MULT:-1.5}"

echo "==================================================================="
echo " Hashcats GPU setup — mode=$MODE  repo=$REPO_URL"
echo "==================================================================="

echo "[1/7] installing prerequisites ..."
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y -qq || true
  apt-get install -y -qq git build-essential python3 python3-pip curl >/dev/null 2>&1 || true
fi
python3 -m pip install -q --upgrade pip >/dev/null 2>&1 || true
python3 -m pip install -q web3 eth-account pycryptodome >/dev/null 2>&1 || \
  pip3 install -q web3 eth-account pycryptodome

echo "[2/7] fetching project ..."
if [ -d "$WORKDIR/.git" ]; then
  git -C "$WORKDIR" pull --ff-only || true
else
  git clone --depth 1 "$REPO_URL" "$WORKDIR"
fi
cd "$WORKDIR/gpu"

echo "[3/7] detecting GPU + compiling CUDA ..."
if ! command -v nvcc >/dev/null 2>&1; then
  echo "!! nvcc not found. This image lacks the CUDA toolkit."
  echo "   Install it or use a CUDA '-devel' image, then re-run."
  echo "   (nvidia-smi alone is NOT enough — we need nvcc to compile.)"
  exit 2
fi
nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv,noheader || true
CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.' | tr -d ' ')
ARCH="sm_${CC:-80}"
echo "   using arch $ARCH"
nvcc -O3 -arch=$ARCH keccak_cuda.cu   -o keccak_cuda
nvcc -O3 -arch=$ARCH verify_kernel.cu -o verify_kernel

echo "[4/7] correctness check (must be ALL PASS) ..."
./verify_kernel test_vectors.json
if ./verify_kernel test_vectors.json | grep -q "FAIL"; then
  echo "!! kernel mismatch — ABORTING (will not mine)."; exit 3
fi

echo "[5/7] GPU hashrate benchmark (~8s) ..."
ZERO=$(printf '0%.0s' $(seq 1 64))
timeout 10 ./keccak_cuda a842e3ab069562db379c35dd52c77ef214324004 \
  "$ZERO" "$ZERO" "$ZERO" 0 8 8192 256 8192 2>bench.txt || true
tail -1 bench.txt | awk '{if($1=="RATE") printf "   GPU rate: %.2f GH/s\n",$2/$3/1e9}'

echo "[6/7] wallet setup ..."
if [ -n "${HASHCATS_PK:-}" ]; then
  printf 'private-key=%s\n' "$HASHCATS_PK" > "$WORKDIR/wallet.env"
  export HASHCATS_WALLET="$WORKDIR/wallet.env"
  echo "   wallet written to $WORKDIR/wallet.env (from HASHCATS_PK)"
else
  echo "   HASHCATS_PK not set. Dry-run only (cannot send without a key)."
  MODE="dry"
fi

echo "[7/7] starting CONTINUOUS miner (mode=$MODE) ..."
cd "$WORKDIR/gpu"
if [ "$MODE" = "send" ]; then
  echo "   >>> LIVE MODE: will submit a real mint tx when a solution is found."
  exec python3 gpu_hunter_continuous.py --send --gas-mult "$GAS_MULT"
else
  echo "   >>> DRY-RUN: proves mining works; sends nothing."
  exec python3 gpu_hunter_continuous.py
fi

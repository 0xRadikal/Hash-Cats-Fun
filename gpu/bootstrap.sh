#!/usr/bin/env bash
# bootstrap.sh — set up the Hashcats GPU miner on a freshly rented GPU box.
# Run from inside the hashcats/gpu/ directory after copying the hashcats/ folder.
#
# It will: detect the GPU arch, compile the CUDA miner + verifier, run the
# bit-exact correctness check against the contract test vectors, and print a
# short throughput benchmark. It does NOT mine or send anything.
set -e

echo "=== 1. Environment ==="
if ! command -v nvcc >/dev/null 2>&1; then
  echo "ERROR: nvcc (CUDA toolkit) not found. Install CUDA or use a CUDA image."
  echo "On many rented boxes: 'nvidia-smi' works but toolkit is missing."
  exit 2
fi
nvcc --version | tail -2
nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv,noheader || true

# Detect compute capability (e.g. 8.9 -> sm_89)
CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')
ARCH="sm_${CC:-80}"
echo "Using arch: $ARCH"

echo "=== 2. Python deps ==="
python3 -m pip install -q --upgrade web3 eth-account pycryptodome 2>/dev/null || \
  pip3 install -q web3 eth-account pycryptodome
python3 -c "import web3, eth_account; print('web3', web3.__version__)"

echo "=== 3. Compile CUDA miner + verifier ==="
nvcc -O3 -arch=$ARCH keccak_cuda.cu   -o keccak_cuda
nvcc -O3 -arch=$ARCH verify_kernel.cu -o verify_kernel
echo "compiled: keccak_cuda, verify_kernel"

echo "=== 4. Correctness check (MUST be ALL PASS) ==="
if [ ! -f test_vectors.json ]; then
  echo "ERROR: test_vectors.json missing (contract-verified vectors)."; exit 3
fi
./verify_kernel test_vectors.json
echo "(If any FAIL above, STOP — do not mine.)"

echo "=== 5. Throughput benchmark (~8s, impossible target=0) ==="
ZERO=$(printf '0%.0s' {1..64})
timeout 10 ./keccak_cuda \
  a842e3ab069562db379c35dd52c77ef214324004 \
  "$ZERO" "$ZERO" "$ZERO" 0 8 8192 256 8192 2>bench.txt || true
tail -1 bench.txt | awk '{if($1=="RATE") printf "GPU rate: %.2f GH/s\n", $2/$3/1e9}'

echo "=== DONE. If PASS + good rate, run: ==="
echo "  python3 gpu_hunter.py            # dry-run, waits for low-burst window"
echo "  python3 gpu_hunter.py --send     # real mint (needs funded wallet)"
echo "  python3 gpu_hunter.py --force --slice 8   # test raw throughput at any difficulty"

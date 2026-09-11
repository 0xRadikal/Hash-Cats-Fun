/*
 * keccak_cuda.cu — GPU Keccak-256 PoW miner for Hashcats.
 *
 * Preimage (packed, 116 bytes):
 *   miner(20) || nonce(32, big-endian) || prevWork(32, big-endian) || anchorHash(32)
 * Win: keccak256(preimage) < target  (both 256-bit big-endian).
 *
 * The keccak-f permutation here is a byte-for-byte port of the CPU miner in
 * ../keccak_miner.c that was verified bit-exact against the on-chain
 * workHash(...). Do NOT change padding: keccak uses 0x01 .. 0x80 (NOT SHA3's
 * 0x06). The whole 116-byte message fits in one 136-byte rate block.
 *
 * Each thread tests nonces:  base + tid + i*stride  (stride = total threads).
 * On success it writes the winning nonce (as 8 bytes, we only vary the low 64
 * bits of the 32-byte nonce field, high 24 bytes stay zero) and sets found=1.
 *
 * Build:  nvcc -O3 -arch=sm_80 keccak_cuda.cu -o keccak_cuda
 *         (replace sm_80 with your GPU's arch: 4090=sm_89, A100=sm_80,
 *          3090=sm_86, H100=sm_90)
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ROTL64(x, y) (((x) << (y)) | ((x) >> (64 - (y))))

__constant__ uint64_t d_RC[24] = {
    0x0000000000000001ULL,0x0000000000008082ULL,0x800000000000808aULL,
    0x8000000080008000ULL,0x000000000000808bULL,0x0000000080000001ULL,
    0x8000000080008081ULL,0x8000000000008009ULL,0x000000000000008aULL,
    0x0000000000000088ULL,0x0000000080008009ULL,0x000000008000000aULL,
    0x000000008000808bULL,0x800000000000008bULL,0x8000000000008089ULL,
    0x8000000000008003ULL,0x8000000000008002ULL,0x8000000000000080ULL,
    0x000000000000800aULL,0x800000008000000aULL,0x8000000080008081ULL,
    0x8000000000008080ULL,0x0000000080000001ULL,0x8000000080008008ULL};

__device__ __forceinline__ void keccakf(uint64_t st[25]) {
    const int RHO[24] = {1,3,6,10,15,21,28,36,45,55,2,14,
                         27,41,56,8,25,43,62,18,39,61,20,44};
    const int PIx[24] = {10,7,11,17,18,3,5,16,8,21,24,4,
                         15,23,19,13,12,2,20,14,22,9,6,1};
    uint64_t bc[5], t;
    #pragma unroll 1
    for (int r = 0; r < 24; r++) {
        for (int i = 0; i < 5; i++)
            bc[i] = st[i] ^ st[i+5] ^ st[i+10] ^ st[i+15] ^ st[i+20];
        for (int i = 0; i < 5; i++) {
            t = bc[(i+4)%5] ^ ROTL64(bc[(i+1)%5], 1);
            for (int j = 0; j < 25; j += 5) st[j+i] ^= t;
        }
        t = st[1];
        for (int i = 0; i < 24; i++) {
            int j = PIx[i];
            bc[0] = st[j];
            st[j] = ROTL64(t, RHO[i]);
            t = bc[0];
        }
        for (int j = 0; j < 25; j += 5) {
            for (int i = 0; i < 5; i++) bc[i] = st[j+i];
            for (int i = 0; i < 5; i++)
                st[j+i] ^= (~bc[(i+1)%5]) & bc[(i+2)%5];
        }
        st[0] ^= d_RC[r];
    }
}

/* Hash a 116-byte message already laid out in `msg`, output first 32 bytes. */
__device__ __forceinline__ void keccak256_116(const uint8_t *msg, uint8_t out[32]) {
    uint64_t st[25];
    #pragma unroll
    for (int i = 0; i < 25; i++) st[i] = 0;
    uint8_t block[136];
    #pragma unroll
    for (int i = 0; i < 116; i++) block[i] = msg[i];
    #pragma unroll
    for (int i = 116; i < 136; i++) block[i] = 0;
    block[116] ^= 0x01;      /* keccak padding */
    block[135] ^= 0x80;
    for (int i = 0; i < 17; i++) {
        uint64_t lane = 0;
        for (int b = 0; b < 8; b++) lane |= ((uint64_t)block[i*8+b]) << (8*b);
        st[i] ^= lane;
    }
    keccakf(st);
    for (int i = 0; i < 4; i++)
        for (int b = 0; b < 8; b++)
            out[i*8+b] = (uint8_t)(st[i] >> (8*b));
}

/* return 1 if a < b (32-byte big-endian) */
__device__ __forceinline__ int lt_be(const uint8_t *a, const uint8_t *b) {
    for (int i = 0; i < 32; i++) {
        if (a[i] != b[i]) return a[i] < b[i];
    }
    return 0;
}

__global__ void mine_kernel(
        const uint8_t *pre_template,   /* 116 bytes: miner|0..0|prev|anchor */
        const uint8_t *target,         /* 32 bytes big-endian */
        uint64_t base_nonce,
        uint64_t stride,               /* == total threads */
        uint32_t iters,                /* nonces per thread this launch */
        uint64_t *found_nonce,
        int *found_flag) {
    uint64_t tid = (uint64_t)blockIdx.x * blockDim.x + threadIdx.x;
    uint8_t pre[116];
    #pragma unroll
    for (int i = 0; i < 116; i++) pre[i] = pre_template[i];
    uint8_t out[32];

    uint64_t nonce = base_nonce + tid;
    for (uint32_t it = 0; it < iters; it++) {
        if (*found_flag) return;
        /* write low 64 bits of nonce big-endian into pre[44..52). high 24
         * bytes of the nonce field (pre[20..44)) stay 0 from the template. */
        #pragma unroll
        for (int b = 0; b < 8; b++)
            pre[51 - b] = (uint8_t)(nonce >> (8*b));
        keccak256_116(pre, out);
        if (lt_be(out, target)) {
            if (atomicCAS(found_flag, 0, 1) == 0) {
                *found_nonce = nonce;
            }
            return;
        }
        nonce += stride;
    }
}

static int hex2bin(const char *hex, uint8_t *out, int outlen) {
    for (int i = 0; i < outlen; i++) {
        unsigned int v;
        if (sscanf(hex + 2*i, "%2x", &v) != 1) return -1;
        out[i] = (uint8_t)v;
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 7) {
        fprintf(stderr,
            "usage: %s miner(40hex) prevWork(64hex) anchorHash(64hex) "
            "target(64hex) base_nonce(dec) max_seconds [blocks threads iters]\n",
            argv[0]);
        return 2;
    }
    uint8_t miner[20], prev[32], anchor[32], target[32];
    if (hex2bin(argv[1], miner, 20) || hex2bin(argv[2], prev, 32) ||
        hex2bin(argv[3], anchor, 32) || hex2bin(argv[4], target, 32)) {
        fprintf(stderr, "bad hex arg\n"); return 2;
    }
    unsigned long long base = strtoull(argv[5], NULL, 10);
    double max_seconds = atof(argv[6]);
    int blocks  = (argc > 7) ? atoi(argv[7]) : 4096;
    int threads = (argc > 8) ? atoi(argv[8]) : 256;
    uint32_t iters = (argc > 9) ? (uint32_t)strtoul(argv[9], NULL, 10) : 4096;

    /* Build 116-byte preimage template. */
    uint8_t pre[116];
    memcpy(pre, miner, 20);
    memset(pre + 20, 0, 32);
    memcpy(pre + 52, prev, 32);
    memcpy(pre + 84, anchor, 32);

    uint8_t *d_pre, *d_target;
    uint64_t *d_found_nonce;
    int *d_found_flag;
    cudaMalloc(&d_pre, 116);
    cudaMalloc(&d_target, 32);
    cudaMalloc(&d_found_nonce, sizeof(uint64_t));
    cudaMalloc(&d_found_flag, sizeof(int));
    cudaMemcpy(d_pre, pre, 116, cudaMemcpyHostToDevice);
    cudaMemcpy(d_target, target, 32, cudaMemcpyHostToDevice);
    cudaMemset(d_found_flag, 0, sizeof(int));

    uint64_t stride = (uint64_t)blocks * threads;
    uint64_t nonce_cursor = base;

    struct timespec t0, tn;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    unsigned long long total = 0;

    while (1) {
        mine_kernel<<<blocks, threads>>>(d_pre, d_target, nonce_cursor,
                                         stride, iters, d_found_nonce, d_found_flag);
        cudaError_t err = cudaDeviceSynchronize();
        if (err != cudaSuccess) {
            fprintf(stderr, "CUDA error: %s\n", cudaGetErrorString(err));
            return 3;
        }
        total += stride * (uint64_t)iters;
        nonce_cursor += stride * (uint64_t)iters;

        int found = 0;
        cudaMemcpy(&found, d_found_flag, sizeof(int), cudaMemcpyDeviceToHost);
        clock_gettime(CLOCK_MONOTONIC, &tn);
        double el = (tn.tv_sec - t0.tv_sec) + (tn.tv_nsec - t0.tv_nsec)/1e9;
        fprintf(stderr, "RATE %llu %.2f\n", total, el);
        if (found) {
            uint64_t n = 0;
            cudaMemcpy(&n, d_found_nonce, sizeof(uint64_t), cudaMemcpyDeviceToHost);
            printf("FOUND %llu\n", (unsigned long long)n);
            fflush(stdout);
            return 0;
        }
        if (max_seconds > 0 && el > max_seconds) {
            printf("NONE\n"); fflush(stdout);
            return 1;
        }
    }
}

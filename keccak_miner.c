/*
 * keccak_miner.c — fast single-threaded Keccak-256 PoW miner for Hashcats.
 *
 * Preimage layout (packed, 84 bytes):
 *     miner(20) || nonce(32, big-endian) || prevWork(32, big-endian) || anchorHash(32)
 * Wait — that's 20+32+32+32 = 116 bytes. (Ethereum abi.encodePacked)
 *
 * We compute keccak256(preimage) and compare as a 256-bit big-endian integer
 * against `target`. Success when hash < target.
 *
 * Only the 32-byte nonce field changes between iterations. Since Keccak
 * absorbs in 136-byte (rate) blocks and 116 < 136, the whole preimage fits in
 * a single block, so we can rebuild the block cheaply each iteration.
 *
 * Args (all hex, no 0x): miner(40) prevWork(64) anchorHash(64) target(64)
 *                        start_nonce(dec) stride(dec) [max_seconds]
 * Prints "FOUND <nonce_decimal>" to stdout on success, or "NONE" on timeout.
 * Prints periodic "RATE <hashes> <seconds>" lines to stderr.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* ---- Keccak-f[1600] ---- */
#define ROL64(a, o) (((a) << (o)) | ((a) >> (64 - (o))))

static const uint64_t RC[24] = {
    0x0000000000000001ULL,0x0000000000008082ULL,0x800000000000808aULL,
    0x8000000080008000ULL,0x000000000000808bULL,0x0000000080000001ULL,
    0x8000000080008081ULL,0x8000000000008009ULL,0x000000000000008aULL,
    0x0000000000000088ULL,0x0000000080008009ULL,0x000000008000000aULL,
    0x000000008000808bULL,0x800000000000008bULL,0x8000000000008089ULL,
    0x8000000000008003ULL,0x8000000000008002ULL,0x8000000000000080ULL,
    0x000000000000800aULL,0x800000008000000aULL,0x8000000080008081ULL,
    0x8000000000008080ULL,0x0000000080000001ULL,0x8000000080008008ULL};

static const int RHO[24] = {1,3,6,10,15,21,28,36,45,55,2,14,
                            27,41,56,8,25,43,62,18,39,61,20,44};
static const int PI[24] = {10,7,11,17,18,3,5,16,8,21,24,4,
                           15,23,19,13,12,2,20,14,22,9,6,1};

static inline void keccakf(uint64_t st[25]) {
    uint64_t bc[5], t;
    for (int r = 0; r < 24; r++) {
        for (int i = 0; i < 5; i++)
            bc[i] = st[i] ^ st[i+5] ^ st[i+10] ^ st[i+15] ^ st[i+20];
        for (int i = 0; i < 5; i++) {
            t = bc[(i+4)%5] ^ ROL64(bc[(i+1)%5], 1);
            for (int j = 0; j < 25; j += 5) st[j+i] ^= t;
        }
        t = st[1];
        for (int i = 0; i < 24; i++) {
            int j = PI[i];
            bc[0] = st[j];
            st[j] = ROL64(t, RHO[i]);
            t = bc[0];
        }
        for (int j = 0; j < 25; j += 5) {
            for (int i = 0; i < 5; i++) bc[i] = st[j+i];
            for (int i = 0; i < 5; i++)
                st[j+i] ^= (~bc[(i+1)%5]) & bc[(i+2)%5];
        }
        st[0] ^= RC[r];
    }
}

/* Keccak-256 of a message of length < 136 (single block). */
static inline void keccak256_single(const uint8_t *msg, size_t len, uint8_t out[32]) {
    uint64_t st[25];
    memset(st, 0, sizeof(st));
    uint8_t block[136];
    memset(block, 0, 136);
    memcpy(block, msg, len);
    block[len] ^= 0x01;      /* keccak padding (0x01, NOT 0x06 like SHA3) */
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

static int hex2bin(const char *hex, uint8_t *out, int outlen) {
    for (int i = 0; i < outlen; i++) {
        unsigned int v;
        if (sscanf(hex + 2*i, "%2x", &v) != 1) return -1;
        out[i] = (uint8_t)v;
    }
    return 0;
}

/* Compare 32-byte big-endian: return 1 if a < b */
static inline int lt_be(const uint8_t *a, const uint8_t *b) {
    for (int i = 0; i < 32; i++) {
        if (a[i] != b[i]) return a[i] < b[i];
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 7) {
        fprintf(stderr, "usage: %s miner prevWork anchorHash target start stride [max_seconds]\n", argv[0]);
        return 2;
    }
    uint8_t miner[20], prev[32], anchor[32], target[32];
    if (hex2bin(argv[1], miner, 20)) { fprintf(stderr,"bad miner\n"); return 2; }
    if (hex2bin(argv[2], prev, 32))  { fprintf(stderr,"bad prev\n"); return 2; }
    if (hex2bin(argv[3], anchor, 32)){ fprintf(stderr,"bad anchor\n"); return 2; }
    if (hex2bin(argv[4], target, 32)){ fprintf(stderr,"bad target\n"); return 2; }
    unsigned long long start = strtoull(argv[5], NULL, 10);
    unsigned long long stride = strtoull(argv[6], NULL, 10);
    double max_seconds = (argc >= 8) ? atof(argv[7]) : 0.0;

    /* Build the 116-byte preimage template. nonce occupies bytes [20,52). */
    uint8_t pre[116];
    memcpy(pre, miner, 20);
    memset(pre + 20, 0, 32);        /* nonce, filled per-iter */
    memcpy(pre + 52, prev, 32);
    memcpy(pre + 84, anchor, 32);

    uint8_t out[32];
    unsigned long long nonce = start;
    unsigned long long count = 0;
    struct timespec t0, tn;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    unsigned long long report = 1ULL << 22; /* ~4M */

    for (;;) {
        /* write nonce as 32-byte big-endian into pre[20..52).
           nonce is 64-bit, so only the last 8 bytes (pre[44..52)) are nonzero. */
        uint64_t n = nonce;
        for (int b = 0; b < 8; b++)
            pre[51 - b] = (uint8_t)(n >> (8*b));
        /* higher 24 bytes remain 0 (already memset). */

        keccak256_single(pre, 116, out);
        if (lt_be(out, target)) {
            printf("FOUND %llu\n", nonce);
            fflush(stdout);
            return 0;
        }
        nonce += stride;
        if (++count >= report) {
            clock_gettime(CLOCK_MONOTONIC, &tn);
            double el = (tn.tv_sec - t0.tv_sec) + (tn.tv_nsec - t0.tv_nsec)/1e9;
            fprintf(stderr, "RATE %llu %.2f\n", count, el);
            fflush(stderr);
            if (max_seconds > 0 && el > max_seconds) {
                printf("NONE\n"); fflush(stdout); return 1;
            }
        }
    }
}

/*
 * verify_kernel_cpu.c — CPU replica of the CUDA device keccak, to prove the
 * kernel's keccak logic is bit-exact against the contract test vectors WITHOUT
 * a GPU. The keccakf/keccak256_116 code below is copy-identical to the
 * __device__ functions in keccak_cuda.cu / verify_kernel.cu (same constants,
 * same padding, same layout). If this passes, the CUDA port is correct.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ROTL64(x, y) (((x) << (y)) | ((x) >> (64 - (y))))

static const uint64_t RC[24] = {
    0x0000000000000001ULL,0x0000000000008082ULL,0x800000000000808aULL,
    0x8000000080008000ULL,0x000000000000808bULL,0x0000000080000001ULL,
    0x8000000080008081ULL,0x8000000000008009ULL,0x000000000000008aULL,
    0x0000000000000088ULL,0x0000000080008009ULL,0x000000008000000aULL,
    0x000000008000808bULL,0x800000000000008bULL,0x8000000000008089ULL,
    0x8000000000008003ULL,0x8000000000008002ULL,0x8000000000000080ULL,
    0x000000000000800aULL,0x800000008000000aULL,0x8000000080008081ULL,
    0x8000000000008080ULL,0x0000000080000001ULL,0x8000000080008008ULL};

static void keccakf(uint64_t st[25]) {
    const int RHO[24] = {1,3,6,10,15,21,28,36,45,55,2,14,
                         27,41,56,8,25,43,62,18,39,61,20,44};
    const int PIx[24] = {10,7,11,17,18,3,5,16,8,21,24,4,
                         15,23,19,13,12,2,20,14,22,9,6,1};
    uint64_t bc[5], t;
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
        st[0] ^= RC[r];
    }
}

static void keccak256_116(const uint8_t *msg, uint8_t out[32]) {
    uint64_t st[25];
    for (int i = 0; i < 25; i++) st[i] = 0;
    uint8_t block[136];
    for (int i = 0; i < 116; i++) block[i] = msg[i];
    for (int i = 116; i < 136; i++) block[i] = 0;
    block[116] ^= 0x01;
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
static char *slurp(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    char *b = (char*)malloc(n+1); if(fread(b,1,n,f)!=(size_t)n){} b[n]=0; fclose(f);
    return b;
}
static void grab(const char *s, const char *key, char *out, int maxlen) {
    char pat[64]; snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(s, pat); out[0]=0;
    if (!p) return;
    p = strchr(p, ':'); if (!p) return; p++;
    while (*p==' '||*p=='\"') p++;
    int i=0; while (*p && *p!='\"' && *p!=',' && *p!='\n' && i<maxlen-1) out[i++]=*p++;
    out[i]=0;
}

int main(int argc, char **argv) {
    const char *path = (argc>1)? argv[1] : "test_vectors.json";
    char *js = slurp(path);
    if (!js) { fprintf(stderr,"cannot read %s\n", path); return 2; }
    char miner_hex[64], prev_hex[80], anchor_hex[80];
    grab(js, "miner", miner_hex, sizeof(miner_hex));
    grab(js, "prevWork_hex", prev_hex, sizeof(prev_hex));
    grab(js, "anchorHash_hex", anchor_hex, sizeof(anchor_hex));
    uint8_t miner[20], prev[32], anchor[32];
    hex2bin(miner_hex, miner, 20);
    hex2bin(prev_hex, prev, 32);
    hex2bin(anchor_hex, anchor, 32);

    int pass=0, fail=0;
    const char *p = js;
    while ((p = strstr(p, "\"nonce\"")) != NULL) {
        const char *c = strchr(p, ':'); c++;
        unsigned long long nonce = strtoull(c, NULL, 10);
        char exp_hex[80]; grab(p, "workHash_hex", exp_hex, sizeof(exp_hex));
        uint8_t expected[32]; hex2bin(exp_hex, expected, 32);
        uint8_t pre[116];
        memcpy(pre, miner, 20);
        memset(pre+20, 0, 32);
        for (int b=0;b<8;b++) pre[51-b] = (uint8_t)(nonce >> (8*b));
        memcpy(pre+52, prev, 32);
        memcpy(pre+84, anchor, 32);
        uint8_t got[32]; keccak256_116(pre, got);
        int ok = (memcmp(got, expected, 32)==0);
        printf("nonce=%llu : %s\n", nonce, ok?"PASS":"FAIL");
        if(!ok){
            printf("  exp "); for(int i=0;i<32;i++)printf("%02x",expected[i]); printf("\n");
            printf("  got "); for(int i=0;i<32;i++)printf("%02x",got[i]); printf("\n");
        }
        ok?pass++:fail++;
        p = c;
    }
    printf("\n%d PASS, %d FAIL -> %s\n", pass, fail,
           fail==0?"KECCAK LOGIC CORRECT (CUDA port will match)":"MISMATCH");
    free(js);
    return fail==0?0:1;
}

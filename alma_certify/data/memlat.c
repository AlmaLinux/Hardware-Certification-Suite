/*
 * memlat: memory latency via dependent pointer chasing over a shuffled
 * permutation. Reports average ns per load for a working set larger than
 * typical LLC (64 MiB).
 *
 * Build: gcc -O2 -o memlat memlat.c
 * Output: "latency_ns: <value>"
 */
#include <stdio.h>
#include <stdlib.h>
#include <sys/time.h>

#define WSET (64UL * 1024 * 1024)          /* bytes */
#define COUNT (WSET / sizeof(size_t))
#define STEPS (1UL << 26)

static double now(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec + tv.tv_usec * 1e-6;
}

int main(void) {
    size_t *chain = malloc(WSET);
    size_t i, j, tmp, pos;
    double t;

    if (!chain) {
        fprintf(stderr, "allocation failed\n");
        return 1;
    }
    /* identity, then Fisher-Yates with a fixed seed for reproducibility */
    for (i = 0; i < COUNT; i++) chain[i] = i;
    srand(20260101);
    for (i = COUNT - 1; i > 0; i--) {
        j = (size_t)rand() % (i + 1);
        tmp = chain[i]; chain[i] = chain[j]; chain[j] = tmp;
    }
    /* warm up */
    pos = 0;
    for (i = 0; i < COUNT; i++) pos = chain[pos];

    pos = 0;
    t = now();
    for (i = 0; i < STEPS; i++) pos = chain[pos];
    t = now() - t;

    printf("latency_ns: %.2f\n", t / (double)STEPS * 1e9);
    if (pos == (size_t)-1) fprintf(stderr, "?\n");
    free(chain);
    return 0;
}

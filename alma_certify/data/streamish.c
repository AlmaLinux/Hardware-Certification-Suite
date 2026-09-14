/*
 * streamish: a STREAM-style memory bandwidth micro-benchmark.
 *
 * This is an original implementation of the classic four kernels
 * (copy/scale/add/triad). It is NOT STREAM and results must never be
 * reported as STREAM numbers.
 *
 * Build: gcc -O3 -fopenmp -o streamish streamish.c
 * Output: one "name: <MB/s>" line per kernel (best of NTIMES).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#ifndef N
#define N (64 * 1024 * 1024) /* doubles per array; 3 arrays = 1.5 GiB */
#endif
#define NTIMES 8

static double *a, *b, *c;

static double now(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec + tv.tv_usec * 1e-6;
}

int main(void) {
    size_t n = N;
    double t, best[4];
    double bytes[4] = {
        2.0 * sizeof(double) * n, /* copy  */
        2.0 * sizeof(double) * n, /* scale */
        3.0 * sizeof(double) * n, /* add   */
        3.0 * sizeof(double) * n, /* triad */
    };
    const char *names[4] = {"copy", "scale", "add", "triad"};
    int k, i;

    a = malloc(n * sizeof(double));
    b = malloc(n * sizeof(double));
    c = malloc(n * sizeof(double));
    if (!a || !b || !c) {
        fprintf(stderr, "allocation failed\n");
        return 1;
    }
#pragma omp parallel for
    for (i = 0; i < (int)n; i++) {
        a[i] = 1.0; b[i] = 2.0; c[i] = 0.0;
    }
    for (k = 0; k < 4; k++) best[k] = 1e30;

    for (k = 0; k < NTIMES; k++) {
        t = now();
#pragma omp parallel for
        for (i = 0; i < (int)n; i++) c[i] = a[i];
        t = now() - t; if (t < best[0]) best[0] = t;

        t = now();
#pragma omp parallel for
        for (i = 0; i < (int)n; i++) b[i] = 3.0 * c[i];
        t = now() - t; if (t < best[1]) best[1] = t;

        t = now();
#pragma omp parallel for
        for (i = 0; i < (int)n; i++) c[i] = a[i] + b[i];
        t = now() - t; if (t < best[2]) best[2] = t;

        t = now();
#pragma omp parallel for
        for (i = 0; i < (int)n; i++) a[i] = b[i] + 3.0 * c[i];
        t = now() - t; if (t < best[3]) best[3] = t;
    }
    for (k = 0; k < 4; k++)
        printf("%s: %.1f\n", names[k], bytes[k] / best[k] / 1e6);
    /* keep the compiler honest */
    if (a[1] < 0) fprintf(stderr, "%f\n", a[1]);
    free(a); free(b); free(c);
    return 0;
}

/* cudaprobe: the CUDA demo checks, in one small program the suite compiles itself.
 *
 * Three modes, matching the three things NVIDIA's own samples demonstrate and the three
 * questions a GPU certification has to answer:
 *
 *   devices    Can the CUDA runtime see the card, and what does it say about it?
 *              This is deviceQuery. It proves the driver, the runtime and the card agree.
 *   vectoradd  Does the card compute the right answer? This is vectorAdd, and it is the
 *              only one of the three that can catch a card that runs and is wrong.
 *   bandwidth  Do host-to-device and device-to-host transfers complete, and at a rate that
 *              is not absurd? This is bandwidthTest.
 *
 * Bundled and compiled rather than calling NVIDIA's sample binaries. The samples stopped
 * being packaged after CUDA 12 and now live in a GitHub repository, so probing a handful of
 * possible install paths would be guesswork with a short shelf life. The suite already
 * compiles streamish.c and memlat.c the same way, from the same data directory.
 *
 * Output is one `key=value` per line on stdout, so the Python side parses rather than scrapes.
 * Anything gone wrong exits non-zero after printing `error=<what>`, because a CUDA call that
 * fails is the finding, not an inconvenience.
 *
 * Kept to the CUDA runtime API and C. No Thrust, no C++ standard library beyond what nvcc
 * pulls in anyway, so it builds against any toolkit a supported AlmaLinux is likely to carry.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <cuda_runtime.h>

#define CHECK(call)                                                             \
    do {                                                                        \
        cudaError_t _err = (call);                                              \
        if (_err != cudaSuccess) {                                              \
            printf("error=%s at %s:%d\n", cudaGetErrorString(_err),             \
                   __FILE__, __LINE__);                                         \
            return 1;                                                           \
        }                                                                       \
    } while (0)

static int mode_devices(void)
{
    int count = 0;
    CHECK(cudaGetDeviceCount(&count));
    printf("device_count=%d\n", count);
    if (count < 1) {
        /* Not an error in the CUDA sense: the runtime worked and found nothing. The caller
         * decides whether that is a failure, which depends on what the run is claiming. */
        printf("error=the CUDA runtime reported no devices\n");
        return 1;
    }
    int runtime_version = 0, driver_version = 0;
    CHECK(cudaRuntimeGetVersion(&runtime_version));
    CHECK(cudaDriverGetVersion(&driver_version));
    printf("cuda_runtime_version=%d\n", runtime_version);
    printf("cuda_driver_version=%d\n", driver_version);

    for (int i = 0; i < count; i++) {
        struct cudaDeviceProp prop;
        CHECK(cudaGetDeviceProperties(&prop, i));
        printf("device.%d.name=%s\n", i, prop.name);
        printf("device.%d.compute_capability=%d.%d\n", i, prop.major, prop.minor);
        printf("device.%d.total_memory_mib=%zu\n", i,
               (size_t)(prop.totalGlobalMem / (1024 * 1024)));

        /* Through cudaDeviceGetAttribute rather than off the properties struct.
         *
         * CUDA 13 removed the deprecated scalar members, so `prop.clockRate` and
         * `prop.computeMode` do not compile there at all: reported from a real machine as two
         * errors from a toolkit that was otherwise working. `cudaDeviceGetAttribute` carries the
         * same values and has done since CUDA 5, so one spelling builds against every toolkit a
         * supported AlmaLinux is likely to carry.
         *
         * ECC and the multiprocessor count come the same way for the same reason, rather than
         * waiting to be removed in a later release and breaking this again. Only name, compute
         * capability and total memory stay on the struct, which are its stable core. */
        int clock_khz = 0, ecc = 0, mode = 0, multiprocessors = 0;
        CHECK(cudaDeviceGetAttribute(&multiprocessors, cudaDevAttrMultiProcessorCount, i));
        CHECK(cudaDeviceGetAttribute(&clock_khz, cudaDevAttrClockRate, i));
        CHECK(cudaDeviceGetAttribute(&ecc, cudaDevAttrEccEnabled, i));
        CHECK(cudaDeviceGetAttribute(&mode, cudaDevAttrComputeMode, i));
        printf("device.%d.multiprocessors=%d\n", i, multiprocessors);
        printf("device.%d.clock_khz=%d\n", i, clock_khz);
        printf("device.%d.ecc_enabled=%d\n", i, ecc);
        /* A card in the wrong compute mode is a configuration fault that looks like a
         * hardware fault: exclusive-process mode makes the second job on the machine fail
         * with "all CUDA-capable devices are busy". Worth reporting rather than diagnosing
         * twice. */
        printf("device.%d.compute_mode=%d\n", i, mode);
    }
    return 0;
}

__global__ static void vector_add(const float *a, const float *b, float *out, int n)
{
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < n) {
        out[i] = a[i] + b[i];
    }
}

static int mode_vectoradd(void)
{
    /* Large enough to cover many blocks and to matter, small enough to fit anywhere: 16 Mi
     * elements is 192 MiB across three buffers. Deliberately not sized from free memory,
     * because a test whose workload varies with the machine cannot be compared between runs. */
    const int n = 16 * 1024 * 1024;
    const size_t bytes = (size_t)n * sizeof(float);
    float *h_a = (float *)malloc(bytes);
    float *h_b = (float *)malloc(bytes);
    float *h_out = (float *)malloc(bytes);
    if (!h_a || !h_b || !h_out) {
        printf("error=could not allocate %zu bytes of host memory\n", bytes * 3);
        return 1;
    }
    for (int i = 0; i < n; i++) {
        h_a[i] = (float)(i % 1000) * 0.5f;
        h_b[i] = (float)(i % 7) * 1.25f;
    }

    float *d_a = NULL, *d_b = NULL, *d_out = NULL;
    CHECK(cudaMalloc((void **)&d_a, bytes));
    CHECK(cudaMalloc((void **)&d_b, bytes));
    CHECK(cudaMalloc((void **)&d_out, bytes));
    CHECK(cudaMemcpy(d_a, h_a, bytes, cudaMemcpyHostToDevice));
    CHECK(cudaMemcpy(d_b, h_b, bytes, cudaMemcpyHostToDevice));

    const int threads = 256;
    const int blocks = (n + threads - 1) / threads;
    vector_add<<<blocks, threads>>>(d_a, d_b, d_out, n);
    /* Kernel launches are asynchronous and report nothing. Without this the copy below would
     * hide a launch failure behind stale device memory, and the test would pass on a card
     * that never ran anything. */
    CHECK(cudaGetLastError());
    CHECK(cudaDeviceSynchronize());
    CHECK(cudaMemcpy(h_out, d_out, bytes, cudaMemcpyDeviceToHost));

    /* Every element, not a sample. The whole point of this mode is catching a card that
     * computes the wrong answer, and a card that is wrong in one lane is exactly the failure
     * a spot check misses. */
    long long wrong = 0;
    double worst = 0.0;
    for (int i = 0; i < n; i++) {
        double expected = (double)h_a[i] + (double)h_b[i];
        double diff = fabs((double)h_out[i] - expected);
        if (diff > worst) {
            worst = diff;
        }
        if (diff > 1e-4) {
            wrong++;
        }
    }
    printf("elements=%d\n", n);
    printf("mismatches=%lld\n", wrong);
    printf("worst_absolute_error=%g\n", worst);

    CHECK(cudaFree(d_a));
    CHECK(cudaFree(d_b));
    CHECK(cudaFree(d_out));
    free(h_a);
    free(h_b);
    free(h_out);

    if (wrong != 0) {
        printf("error=%lld of %d elements were wrong\n", wrong, n);
        return 1;
    }
    return 0;
}

static int timed_copy(void *dst, const void *src, size_t bytes, enum cudaMemcpyKind kind,
                      int iterations, const char *label)
{
    cudaEvent_t start, stop;
    CHECK(cudaEventCreate(&start));
    CHECK(cudaEventCreate(&stop));
    /* One untimed pass first. The first transfer of a run pays for context creation and page
     * pinning, and charging that to the measurement makes a healthy card look broken. */
    CHECK(cudaMemcpy(dst, src, bytes, kind));
    CHECK(cudaEventRecord(start, 0));
    for (int i = 0; i < iterations; i++) {
        CHECK(cudaMemcpy(dst, src, bytes, kind));
    }
    CHECK(cudaEventRecord(stop, 0));
    CHECK(cudaEventSynchronize(stop));
    float ms = 0.0f;
    CHECK(cudaEventElapsedTime(&ms, start, stop));
    CHECK(cudaEventDestroy(start));
    CHECK(cudaEventDestroy(stop));
    if (ms <= 0.0f) {
        printf("error=%s transfer measured no elapsed time\n", label);
        return 1;
    }
    double gib = ((double)bytes * iterations) / (1024.0 * 1024.0 * 1024.0);
    printf("%s_gib_per_s=%.3f\n", label, gib / (ms / 1000.0));
    return 0;
}

static int mode_bandwidth(void)
{
    const size_t bytes = 256u * 1024u * 1024u;
    const int iterations = 10;
    void *h_buf = NULL;
    /* Pinned host memory, because pageable transfers measure the kernel's copy into a staging
     * buffer as much as the link. This is what a card is capable of, which is the number worth
     * comparing between machines. */
    CHECK(cudaMallocHost(&h_buf, bytes));
    memset(h_buf, 1, bytes);
    void *d_buf = NULL;
    CHECK(cudaMalloc(&d_buf, bytes));

    printf("bytes=%zu\n", bytes);
    printf("iterations=%d\n", iterations);
    if (timed_copy(d_buf, h_buf, bytes, cudaMemcpyHostToDevice, iterations,
                   "host_to_device")) {
        return 1;
    }
    if (timed_copy(h_buf, d_buf, bytes, cudaMemcpyDeviceToHost, iterations,
                   "device_to_host")) {
        return 1;
    }
    void *d_second = NULL;
    CHECK(cudaMalloc(&d_second, bytes));
    if (timed_copy(d_second, d_buf, bytes, cudaMemcpyDeviceToDevice, iterations,
                   "device_to_device")) {
        return 1;
    }

    CHECK(cudaFree(d_second));
    CHECK(cudaFree(d_buf));
    CHECK(cudaFreeHost(h_buf));
    return 0;
}

int main(int argc, char **argv)
{
    const char *mode = argc > 1 ? argv[1] : "devices";
    int device = 0;
    /* Which device, for a machine with several. Certifying one card at a time is the honest
     * unit: two cards in a box are two products. */
    if (argc > 2) {
        device = atoi(argv[2]);
    }
    if (strcmp(mode, "devices") != 0) {
        CHECK(cudaSetDevice(device));
        printf("device=%d\n", device);
    }
    if (strcmp(mode, "devices") == 0) {
        return mode_devices();
    }
    if (strcmp(mode, "vectoradd") == 0) {
        return mode_vectoradd();
    }
    if (strcmp(mode, "bandwidth") == 0) {
        return mode_bandwidth();
    }
    printf("error=unknown mode %s\n", mode);
    return 2;
}

/* openclprobe: the basic OpenCL checks, in one small program the suite compiles itself.
 *
 * Two modes, matching what the CUDA probe beside it does and answering the same two questions for a
 * different runtime:
 *
 *   devices     Can an OpenCL runtime see the card, and what does it say about it? This proves the
 *               ICD loader found a driver, the driver initialized, and a device exists.
 *   vectoradd   Does the device compute the right answer? This is the only one of the two that can
 *               catch hardware that runs and is wrong.
 *
 * Written rather than scraped from ``clinfo``, for the same reason ``cudaprobe.cu`` exists: a tool's
 * output layout is not an interface, and this project has already been bitten by reading one.
 *
 * **Device type is reported for every device, and that is load-bearing.** A machine can have an
 * OpenCL runtime with no hardware behind it at all: pocl and Mesa's rusticl-on-llvmpipe both present
 * a perfectly working CPU device. A validation that accepted the first device it found would pass on
 * a server with no accelerator, proving nothing about the hardware. The Python side refuses to
 * certify a run whose only devices are CPU ones, and it can only do that if this says so.
 *
 * Output is one ``key=value`` per line on stdout, so the caller parses rather than scrapes. Anything
 * gone wrong exits non-zero after printing ``error=<what>``.
 *
 * OpenCL 1.2 only, and the deprecation warnings are silenced rather than avoided: 1.2 is what every
 * driver in existence implements, and using a 2.x call would narrow the machines this runs on for no
 * gain. Kept to C and the OpenCL C API, so it builds against any headers a supported AlmaLinux has.
 */

#define CL_TARGET_OPENCL_VERSION 120
#define CL_USE_DEPRECATED_OPENCL_1_2_APIS

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <CL/cl.h>

#define CHECK(call, what)                                                       \
    do {                                                                        \
        cl_int _err = (call);                                                   \
        if (_err != CL_SUCCESS) {                                               \
            printf("error=%s failed with OpenCL error %d\n", (what), (int)_err); \
            return 1;                                                           \
        }                                                                       \
    } while (0)

#define MAX_PLATFORMS 16
#define MAX_DEVICES 16

static const char *type_name(cl_device_type type)
{
    /* The names the Python side matches on. "cpu" is the one that matters: it is how a software
     * implementation is told apart from hardware. */
    if (type & CL_DEVICE_TYPE_GPU)
        return "gpu";
    if (type & CL_DEVICE_TYPE_ACCELERATOR)
        return "accelerator";
    if (type & CL_DEVICE_TYPE_CPU)
        return "cpu";
    return "other";
}

/* Print one string property, with a key the caller can find it under. */
static void print_info(cl_device_id device, cl_device_info info, const char *key, int index)
{
    char buffer[1024] = {0};
    size_t size = 0;
    if (clGetDeviceInfo(device, info, sizeof(buffer) - 1, buffer, &size) != CL_SUCCESS)
        return;
    /* Newlines would break the one-key-per-line contract: the OpenCL extension list is one long
     * space-separated string, but a driver is free to put anything in a name. */
    for (char *c = buffer; *c; c++)
        if (*c == '\n' || *c == '\r')
            *c = ' ';
    printf("device.%d.%s=%s\n", index, key, buffer);
}

static int enumerate(int quiet, cl_device_id *first_gpu)
{
    cl_platform_id platforms[MAX_PLATFORMS];
    cl_uint platform_count = 0;
    cl_int err = clGetPlatformIDs(MAX_PLATFORMS, platforms, &platform_count);
    if (err != CL_SUCCESS || platform_count == 0) {
        /* No platform at all: an ICD file is installed and the loader got nothing out of it. A fact
         * rather than an error, and reported as one, because whether it condemns the machine is not
         * this program's decision. */
        if (!quiet) {
            printf("platform_count=0\n");
            printf("device_count=0\n");
            printf("accelerated_device_count=0\n");
        }
        return 0;
    }
    if (!quiet)
        printf("platform_count=%u\n", platform_count);

    int index = 0;
    int gpu_devices = 0;
    for (cl_uint p = 0; p < platform_count; p++) {
        char platform_name[256] = {0};
        clGetPlatformInfo(platforms[p], CL_PLATFORM_NAME, sizeof(platform_name) - 1,
                          platform_name, NULL);
        cl_device_id devices[MAX_DEVICES];
        cl_uint device_count = 0;
        /* ALL, not GPU: a CPU-only platform has to be *seen* to be reported and refused. Asking
         * only for GPUs would make a software implementation look like an absent one, and those
         * need different messages. */
        err = clGetDeviceIDs(platforms[p], CL_DEVICE_TYPE_ALL, MAX_DEVICES, devices, &device_count);
        if (err != CL_SUCCESS)
            continue;
        for (cl_uint d = 0; d < device_count; d++, index++) {
            cl_device_type type = 0;
            clGetDeviceInfo(devices[d], CL_DEVICE_TYPE, sizeof(type), &type, NULL);
            if (!quiet) {
                printf("device.%d.platform=%s\n", index, platform_name);
                printf("device.%d.type=%s\n", index, type_name(type));
                print_info(devices[d], CL_DEVICE_NAME, "name", index);
                print_info(devices[d], CL_DEVICE_VENDOR, "vendor", index);
                print_info(devices[d], CL_DEVICE_VERSION, "version", index);
                print_info(devices[d], CL_DRIVER_VERSION, "driver_version", index);

                cl_uint units = 0;
                cl_ulong memory = 0;
                size_t group = 0;
                clGetDeviceInfo(devices[d], CL_DEVICE_MAX_COMPUTE_UNITS, sizeof(units), &units, NULL);
                clGetDeviceInfo(devices[d], CL_DEVICE_GLOBAL_MEM_SIZE, sizeof(memory), &memory, NULL);
                clGetDeviceInfo(devices[d], CL_DEVICE_MAX_WORK_GROUP_SIZE, sizeof(group), &group, NULL);
                printf("device.%d.compute_units=%u\n", index, units);
                printf("device.%d.global_memory_mib=%llu\n", index,
                       (unsigned long long)(memory / (1024 * 1024)));
                printf("device.%d.max_work_group_size=%zu\n", index, group);
            }
            if ((type & CL_DEVICE_TYPE_GPU) || (type & CL_DEVICE_TYPE_ACCELERATOR)) {
                gpu_devices++;
                if (first_gpu && *first_gpu == NULL)
                    *first_gpu = devices[d];
            }
        }
    }
    if (!quiet) {
        printf("device_count=%d\n", index);
        /* Counted separately from the total, because "there are devices" and "there is hardware"
         * are different claims and only the second one certifies anything. */
        printf("accelerated_device_count=%d\n", gpu_devices);
    }
    /* Zero devices is not a failure here either. A driver that loads and claims nothing has told us
     * something about the hardware, and it is the caller that knows whether that is a defect: on this
     * machine it usually means the runtime does not support this generation of card. */
    return 0;
}

static const char *KERNEL_SOURCE =
    "__kernel void vector_add(__global const float *a,\n"
    "                        __global const float *b,\n"
    "                        __global float *out)\n"
    "{\n"
    "    int i = get_global_id(0);\n"
    "    out[i] = a[i] + b[i];\n"
    "}\n";

static int mode_vectoradd(void)
{
    cl_device_id device = NULL;
    enumerate(1, &device);
    if (device == NULL) {
        /* Machine-readable first, so the caller can tell "nothing to run on" from "ran and got the
         * wrong answer" without matching on English. */
        printf("accelerated_device_count=0\n");
        printf("error=no OpenCL GPU or accelerator device to run on\n");
        return 1;
    }
    char name[256] = {0};
    clGetDeviceInfo(device, CL_DEVICE_NAME, sizeof(name) - 1, name, NULL);
    printf("device=%s\n", name);

    /* Large enough to cover many work groups and to matter, small enough to fit anywhere: 4 Mi
     * elements is 48 MiB across three buffers. Deliberately not sized from free memory, because a
     * test whose workload varies with the machine cannot be compared between runs. */
    const size_t n = 4u * 1024u * 1024u;
    const size_t bytes = n * sizeof(float);
    float *h_a = (float *)malloc(bytes);
    float *h_b = (float *)malloc(bytes);
    float *h_out = (float *)malloc(bytes);
    if (!h_a || !h_b || !h_out) {
        printf("error=could not allocate %zu bytes of host memory\n", bytes * 3);
        return 1;
    }
    for (size_t i = 0; i < n; i++) {
        h_a[i] = (float)(i % 1000) * 0.5f;
        h_b[i] = (float)(i % 7) * 1.25f;
    }

    cl_int err = CL_SUCCESS;
    cl_context context = clCreateContext(NULL, 1, &device, NULL, NULL, &err);
    CHECK(err, "clCreateContext");
    cl_command_queue queue = clCreateCommandQueue(context, device, 0, &err);
    CHECK(err, "clCreateCommandQueue");

    cl_program program = clCreateProgramWithSource(context, 1, &KERNEL_SOURCE, NULL, &err);
    CHECK(err, "clCreateProgramWithSource");
    err = clBuildProgram(program, 1, &device, NULL, NULL, NULL);
    if (err != CL_SUCCESS) {
        /* The build log is the whole diagnosis when a driver's compiler rejects a kernel this
         * simple, and printing it beats reporting a number. */
        char log[4096] = {0};
        clGetProgramBuildInfo(program, device, CL_PROGRAM_BUILD_LOG, sizeof(log) - 1, log, NULL);
        for (char *c = log; *c; c++)
            if (*c == '\n' || *c == '\r')
                *c = ' ';
        printf("build_log=%s\n", log);
        printf("error=the device's OpenCL compiler could not build a vector add kernel\n");
        return 1;
    }
    cl_kernel kernel = clCreateKernel(program, "vector_add", &err);
    CHECK(err, "clCreateKernel");

    cl_mem d_a = clCreateBuffer(context, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR, bytes, h_a, &err);
    CHECK(err, "clCreateBuffer(a)");
    cl_mem d_b = clCreateBuffer(context, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR, bytes, h_b, &err);
    CHECK(err, "clCreateBuffer(b)");
    cl_mem d_out = clCreateBuffer(context, CL_MEM_WRITE_ONLY, bytes, NULL, &err);
    CHECK(err, "clCreateBuffer(out)");

    CHECK(clSetKernelArg(kernel, 0, sizeof(d_a), &d_a), "clSetKernelArg(0)");
    CHECK(clSetKernelArg(kernel, 1, sizeof(d_b), &d_b), "clSetKernelArg(1)");
    CHECK(clSetKernelArg(kernel, 2, sizeof(d_out), &d_out), "clSetKernelArg(2)");

    CHECK(clEnqueueNDRangeKernel(queue, kernel, 1, NULL, &n, NULL, 0, NULL, NULL),
          "clEnqueueNDRangeKernel");
    /* Finish before reading, so a failed launch cannot hide behind stale buffer contents and let
     * this pass on a device that never ran anything. */
    CHECK(clFinish(queue), "clFinish");
    CHECK(clEnqueueReadBuffer(queue, d_out, CL_TRUE, 0, bytes, h_out, 0, NULL, NULL),
          "clEnqueueReadBuffer");

    /* Every element, not a sample. The whole point of this mode is catching a device that computes
     * the wrong answer, and one wrong lane is exactly what a spot check misses. */
    long long wrong = 0;
    double worst = 0.0;
    for (size_t i = 0; i < n; i++) {
        double expected = (double)h_a[i] + (double)h_b[i];
        double diff = fabs((double)h_out[i] - expected);
        if (diff > worst)
            worst = diff;
        if (diff > 1e-4)
            wrong++;
    }
    printf("elements=%zu\n", n);
    printf("mismatches=%lld\n", wrong);
    printf("worst_absolute_error=%g\n", worst);

    clReleaseMemObject(d_a);
    clReleaseMemObject(d_b);
    clReleaseMemObject(d_out);
    clReleaseKernel(kernel);
    clReleaseProgram(program);
    clReleaseCommandQueue(queue);
    clReleaseContext(context);
    free(h_a);
    free(h_b);
    free(h_out);

    if (wrong != 0) {
        printf("error=%lld of %zu elements were wrong\n", wrong, n);
        return 1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    const char *mode = argc > 1 ? argv[1] : "devices";
    if (strcmp(mode, "devices") == 0)
        return enumerate(0, NULL);
    if (strcmp(mode, "vectoradd") == 0)
        return mode_vectoradd();
    printf("error=unknown mode %s\n", mode);
    return 2;
}

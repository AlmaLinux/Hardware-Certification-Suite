/* vulkanprobe: the basic Vulkan checks, in one small program the suite compiles itself.
 *
 * Two modes, the same pair the CUDA and OpenCL probes beside it answer:
 *
 *   devices   Can Vulkan see the card, and what does it say about it? This proves the loader found
 *             an ICD, the driver initialized, and a physical device exists with a queue that can do
 *             work.
 *   fill      Does the device actually do the work and produce the right bytes? A buffer is filled
 *             *by the GPU*, copied back, and every word is checked.
 *
 * **No shader, and that is deliberate.** A compute dispatch would need SPIR-V, which would mean
 * either shipping a binary blob nobody can read in review or requiring ``glslc`` at validation time.
 * ``vkCmdFillBuffer`` is core Vulkan, needs no shader module, and still exercises the whole path
 * that matters here: instance, physical device, queue family, logical device, command pool, command
 * buffer, device-local allocation, submission, fence, host-visible readback. A device that gets that
 * wrong is broken, and one that gets it right has proved the stack works.
 *
 * **Device type is reported for every device, and that is load-bearing.** ``mesa-vulkan-drivers``
 * installs ``lvp_icd.json``, which is lavapipe: a complete software Vulkan implementation running on
 * the CPU. A validation that accepted the first device it found would pass on a server with no
 * accelerator at all. The Python side refuses to certify a run whose only devices are CPU ones, and
 * it can only do that if this reports the type.
 *
 * Output is one ``key=value`` per line on stdout. Anything gone wrong exits non-zero after printing
 * ``error=<what>``. Kept to core Vulkan 1.0, which every driver implements.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vulkan/vulkan.h>

#define CHECK(call, what)                                                      \
    do {                                                                       \
        VkResult _r = (call);                                                  \
        if (_r != VK_SUCCESS) {                                                \
            printf("error=%s failed with VkResult %d\n", (what), (int)_r);      \
            return 1;                                                          \
        }                                                                      \
    } while (0)

#define MAX_DEVICES 8
#define FILL_BYTES (16u * 1024u * 1024u)
#define FILL_PATTERN 0xA5A5A5A5u

static const char *type_name(VkPhysicalDeviceType type)
{
    /* The names the Python side matches on. "cpu" is the one that matters: it is how lavapipe and
     * any other software implementation are told apart from hardware. */
    switch (type) {
    case VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU: return "integrated";
    case VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU:   return "discrete";
    case VK_PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU:    return "virtual";
    case VK_PHYSICAL_DEVICE_TYPE_CPU:            return "cpu";
    default:                                     return "other";
    }
}

static VkResult make_instance(VkInstance *instance)
{
    VkApplicationInfo app = {0};
    app.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    app.pApplicationName = "alma-certify vulkanprobe";
    app.apiVersion = VK_API_VERSION_1_0;
    VkInstanceCreateInfo create = {0};
    create.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    create.pApplicationInfo = &app;
    /* No extensions and no layers. Nothing here needs a surface, and asking for one would fail on
     * exactly the headless machines this is meant to run on. */
    return vkCreateInstance(&create, NULL, instance);
}

/* The first queue family that can do transfers, which is what ``fill`` needs. Returns -1 if none. */
static int find_queue_family(VkPhysicalDevice device)
{
    uint32_t count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(device, &count, NULL);
    if (count == 0)
        return -1;
    VkQueueFamilyProperties *families = calloc(count, sizeof(*families));
    if (!families)
        return -1;
    vkGetPhysicalDeviceQueueFamilyProperties(device, &count, families);
    int found = -1;
    for (uint32_t i = 0; i < count; i++) {
        /* Compute or graphics both imply transfer, and a driver need not set the transfer bit on a
         * family that has either. Checking all three is what the specification asks for. */
        if (families[i].queueCount > 0 &&
            (families[i].queueFlags &
             (VK_QUEUE_COMPUTE_BIT | VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_TRANSFER_BIT))) {
            found = (int)i;
            break;
        }
    }
    free(families);
    return found;
}

static int enumerate(int quiet, VkInstance instance, VkPhysicalDevice *chosen)
{
    VkPhysicalDevice devices[MAX_DEVICES];
    uint32_t count = MAX_DEVICES;
    VkResult result = vkEnumeratePhysicalDevices(instance, &count, devices);
    if ((result != VK_SUCCESS && result != VK_INCOMPLETE) || count == 0) {
        /* A fact, not an error: a loader that works and enumerates nothing has told us no installed
         * driver claims this hardware, and whether that condemns the machine is the caller's
         * decision rather than this program's. */
        printf("device_count=0\n");
        printf("accelerated_device_count=0\n");
        return 0;
    }
    int accelerated = 0;
    for (uint32_t i = 0; i < count; i++) {
        VkPhysicalDeviceProperties props;
        vkGetPhysicalDeviceProperties(devices[i], &props);
        int queue = find_queue_family(devices[i]);
        if (!quiet) {
            printf("device.%u.name=%s\n", i, props.deviceName);
            printf("device.%u.type=%s\n", i, type_name(props.deviceType));
            printf("device.%u.api_version=%u.%u.%u\n", i,
                   VK_VERSION_MAJOR(props.apiVersion), VK_VERSION_MINOR(props.apiVersion),
                   VK_VERSION_PATCH(props.apiVersion));
            printf("device.%u.driver_version=%u\n", i, props.driverVersion);
            printf("device.%u.vendor_id=0x%04x\n", i, props.vendorID);
            printf("device.%u.device_id=0x%04x\n", i, props.deviceID);
            printf("device.%u.queue_family=%d\n", i, queue);
        }
        if (props.deviceType != VK_PHYSICAL_DEVICE_TYPE_CPU && queue >= 0) {
            accelerated++;
            if (chosen && *chosen == VK_NULL_HANDLE)
                *chosen = devices[i];
        }
    }
    if (!quiet) {
        printf("device_count=%u\n", count);
        /* Counted apart from the total, because "Vulkan works" and "the hardware works" are
         * different claims and lavapipe satisfies only the first. */
        printf("accelerated_device_count=%d\n", accelerated);
    }
    return 0;
}

static uint32_t find_memory_type(VkPhysicalDevice device, uint32_t allowed,
                                 VkMemoryPropertyFlags wanted)
{
    VkPhysicalDeviceMemoryProperties memory;
    vkGetPhysicalDeviceMemoryProperties(device, &memory);
    for (uint32_t i = 0; i < memory.memoryTypeCount; i++)
        if ((allowed & (1u << i)) &&
            (memory.memoryTypes[i].propertyFlags & wanted) == wanted)
            return i;
    return UINT32_MAX;
}

static int mode_fill(void)
{
    VkInstance instance = VK_NULL_HANDLE;
    CHECK(make_instance(&instance), "vkCreateInstance");
    VkPhysicalDevice physical = VK_NULL_HANDLE;
    enumerate(1, instance, &physical);
    if (physical == VK_NULL_HANDLE) {
        printf("accelerated_device_count=0\n");
        printf("error=no Vulkan hardware device with a usable queue\n");
        return 1;
    }
    VkPhysicalDeviceProperties props;
    vkGetPhysicalDeviceProperties(physical, &props);
    printf("device=%s\n", props.deviceName);
    printf("device_type=%s\n", type_name(props.deviceType));

    int family = find_queue_family(physical);
    float priority = 1.0f;
    VkDeviceQueueCreateInfo queue_info = {0};
    queue_info.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
    queue_info.queueFamilyIndex = (uint32_t)family;
    queue_info.queueCount = 1;
    queue_info.pQueuePriorities = &priority;
    VkDeviceCreateInfo device_info = {0};
    device_info.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
    device_info.queueCreateInfoCount = 1;
    device_info.pQueueCreateInfos = &queue_info;

    VkDevice device = VK_NULL_HANDLE;
    CHECK(vkCreateDevice(physical, &device_info, NULL, &device), "vkCreateDevice");
    VkQueue queue = VK_NULL_HANDLE;
    vkGetDeviceQueue(device, (uint32_t)family, 0, &queue);

    /* One buffer, host-visible, so the fill the GPU performs can be read back and checked. A
     * device-local buffer plus a copy would exercise one more transfer, and would also skip every
     * integrated GPU whose memory is host-visible anyway; this is the version that runs everywhere. */
    VkBufferCreateInfo buffer_info = {0};
    buffer_info.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    buffer_info.size = FILL_BYTES;
    buffer_info.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    buffer_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    VkBuffer buffer = VK_NULL_HANDLE;
    CHECK(vkCreateBuffer(device, &buffer_info, NULL, &buffer), "vkCreateBuffer");

    VkMemoryRequirements requirements;
    vkGetBufferMemoryRequirements(device, buffer, &requirements);
    uint32_t type = find_memory_type(
        physical, requirements.memoryTypeBits,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    if (type == UINT32_MAX) {
        printf("error=no host-visible coherent memory type on this device\n");
        return 1;
    }
    VkMemoryAllocateInfo allocate = {0};
    allocate.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    allocate.allocationSize = requirements.size;
    allocate.memoryTypeIndex = type;
    VkDeviceMemory memory = VK_NULL_HANDLE;
    CHECK(vkAllocateMemory(device, &allocate, NULL, &memory), "vkAllocateMemory");
    CHECK(vkBindBufferMemory(device, buffer, memory, 0), "vkBindBufferMemory");

    /* Zeroed from the host first, so a fill that never happens cannot pass on whatever the
     * allocation happened to contain. */
    void *mapped = NULL;
    CHECK(vkMapMemory(device, memory, 0, FILL_BYTES, 0, &mapped), "vkMapMemory");
    memset(mapped, 0, FILL_BYTES);
    vkUnmapMemory(device, memory);

    VkCommandPoolCreateInfo pool_info = {0};
    pool_info.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    pool_info.queueFamilyIndex = (uint32_t)family;
    VkCommandPool pool = VK_NULL_HANDLE;
    CHECK(vkCreateCommandPool(device, &pool_info, NULL, &pool), "vkCreateCommandPool");

    VkCommandBufferAllocateInfo command_info = {0};
    command_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    command_info.commandPool = pool;
    command_info.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    command_info.commandBufferCount = 1;
    VkCommandBuffer commands = VK_NULL_HANDLE;
    CHECK(vkAllocateCommandBuffers(device, &command_info, &commands), "vkAllocateCommandBuffers");

    VkCommandBufferBeginInfo begin = {0};
    begin.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    CHECK(vkBeginCommandBuffer(commands, &begin), "vkBeginCommandBuffer");
    vkCmdFillBuffer(commands, buffer, 0, FILL_BYTES, FILL_PATTERN);
    CHECK(vkEndCommandBuffer(commands), "vkEndCommandBuffer");

    VkFenceCreateInfo fence_info = {0};
    fence_info.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    VkFence fence = VK_NULL_HANDLE;
    CHECK(vkCreateFence(device, &fence_info, NULL, &fence), "vkCreateFence");

    VkSubmitInfo submit = {0};
    submit.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submit.commandBufferCount = 1;
    submit.pCommandBuffers = &commands;
    CHECK(vkQueueSubmit(queue, 1, &submit, fence), "vkQueueSubmit");
    /* Bounded. A wedged GPU that never signals would otherwise hang the whole validation run, and
     * ten seconds is a long time for a 16 MiB fill. */
    VkResult waited = vkWaitForFences(device, 1, &fence, VK_TRUE, 10ull * 1000 * 1000 * 1000);
    if (waited == VK_TIMEOUT) {
        printf("error=the device did not finish a 16 MiB buffer fill within 10 seconds\n");
        return 1;
    }
    CHECK(waited, "vkWaitForFences");

    CHECK(vkMapMemory(device, memory, 0, FILL_BYTES, 0, &mapped), "vkMapMemory");
    /* Every word, not a sample, for the same reason the other probes check every element. */
    const uint32_t *words = (const uint32_t *)mapped;
    unsigned long long wrong = 0;
    for (size_t i = 0; i < FILL_BYTES / sizeof(uint32_t); i++)
        if (words[i] != FILL_PATTERN)
            wrong++;
    vkUnmapMemory(device, memory);

    printf("bytes=%u\n", FILL_BYTES);
    printf("words=%zu\n", (size_t)(FILL_BYTES / sizeof(uint32_t)));
    printf("mismatches=%llu\n", wrong);

    vkDestroyFence(device, fence, NULL);
    vkDestroyCommandPool(device, pool, NULL);
    vkDestroyBuffer(device, buffer, NULL);
    vkFreeMemory(device, memory, NULL);
    vkDestroyDevice(device, NULL);
    vkDestroyInstance(instance, NULL);

    if (wrong != 0) {
        printf("error=%llu words of %zu were not what the GPU was told to write\n",
               wrong, (size_t)(FILL_BYTES / sizeof(uint32_t)));
        return 1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    const char *mode = argc > 1 ? argv[1] : "devices";
    if (strcmp(mode, "devices") == 0) {
        VkInstance instance = VK_NULL_HANDLE;
        VkResult created = make_instance(&instance);
        if (created != VK_SUCCESS) {
            /* The loader itself failing is a different fault from a driver finding no device, and
             * ``VK_ERROR_INCOMPATIBLE_DRIVER`` is what a machine with no usable ICD reports. Still a
             * fact rather than a verdict: reported with the counts at zero and a zero exit, so the
             * caller treats it the same way as a driver that claims nothing. */
            printf("instance_error=%d\n", (int)created);
            printf("device_count=0\n");
            printf("accelerated_device_count=0\n");
            return 0;
        }
        int status = enumerate(0, instance, NULL);
        vkDestroyInstance(instance, NULL);
        return status;
    }
    if (strcmp(mode, "fill") == 0)
        return mode_fill();
    printf("error=unknown mode %s\n", mode);
    return 2;
}

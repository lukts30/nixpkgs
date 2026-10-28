/* Direct block I/O at queue depth one; the controller releases serial barriers. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/reboot.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include <linux/fs.h>

static void fail(const char *what) { perror(what); exit(1); }
static double now(void) {
    struct timespec time;
    if (clock_gettime(CLOCK_MONOTONIC, &time)) fail("clock_gettime");
    return time.tv_sec + time.tv_nsec / 1e9;
}
static uint64_t next_random(uint64_t *state) {
    *state ^= *state << 13; *state ^= *state >> 7; *state ^= *state << 17;
    return *state;
}
static long cached(void) {
    FILE *file = fopen("/proc/meminfo", "r");
    if (!file) fail("meminfo");
    char key[128], line[256];
    long value, result = -1;
    while (fgets(line, sizeof(line), file))
        if (sscanf(line, "%127s %ld", key, &value) == 2 && !strcmp(key, "Cached:")) result = value;
    fclose(file);
    return result;
}
static void wait_for(int phase) {
    char line[64], expected[64];
    snprintf(expected, sizeof(expected), "GO %d\n", phase);
    if (!fgets(line, sizeof(line), stdin) || strcmp(line, expected)) {
        fprintf(stderr, "unexpected barrier, wanted %s", expected); exit(1);
    }
}
static uint64_t flushes(const char *device) {
    char path[128];
    snprintf(path, sizeof(path), "/sys/class/block/%s/stat", strrchr(device, '/') + 1);
    FILE *file = fopen(path, "r");
    if (!file) fail("block stat");
    unsigned long long value = 0;
    for (int i = 0; i < 16; i++)
        if (fscanf(file, "%llu", &value) != 1) fail("block stat fields");
    fclose(file);
    return value;
}
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    setbuf(stdout, NULL);
    struct termios terminal;
    if (tcgetattr(0, &terminal)) fail("tcgetattr");
    cfmakeraw(&terminal);
    if (tcsetattr(0, TCSANOW, &terminal)) fail("tcsetattr");
    double duration = atof(argv[2]);
    int disk = open(argv[1], O_RDWR | O_DIRECT);
    if (disk < 0) fail("open disk");
    uint64_t size;
    if (ioctl(disk, BLKGETSIZE64, &size)) fail("BLKGETSIZE64");
    size_t block = 1024 * 1024;
    if (size < block || size % block) { fprintf(stderr, "invalid disk size\n"); return 1; }
    void *buffer;
    if (posix_memalign(&buffer, 4096, block)) fail("posix_memalign");
    uint64_t random = 0x123456789abcdefULL;
    for (size_t i = 0; i < block / sizeof(uint64_t); i++)
        ((uint64_t *)buffer)[i] = next_random(&random);
    printf("BLOCK_READY {\"size\":%llu,\"cached_kib\":%ld}\n", (unsigned long long)size, cached());
    for (int phase = 0; phase < 4; phase++) {
        wait_for(phase);
        size_t length = phase % 2 ? 4096 : block;
        uint64_t bytes = 0, ops = 0, offset = 0;
        uint64_t flush_before = flushes(argv[1]);
        double start = now();
        do {
            if (phase % 2) offset = (next_random(&random) % (size / length)) * length;
            ssize_t n = phase < 2 ? pread(disk, buffer, length, offset)
                                 : pwrite(disk, buffer, length, offset);
            if (n != (ssize_t)length) fail("block read/write");
            bytes += n; ops++;
            if (!(phase % 2)) offset = (offset + length) % size;
        } while ((ops % 16) != 0 || now() - start < duration);
        if (phase >= 2 && fdatasync(disk)) fail("fdatasync");
        double elapsed = now() - start;
        printf("BLOCK_RESULT {\"phase\":%d,\"seconds\":%.9f,\"bytes\":%llu,\"operations\":%llu,\"cached_kib\":%ld,\"flushes\":%llu}\n",
               phase, elapsed, (unsigned long long)bytes, (unsigned long long)ops, cached(),
               (unsigned long long)(flushes(argv[1]) - flush_before));
    }
    wait_for(4);
    if (close(disk)) fail("close disk");
    puts("BLOCK_BENCHMARK_SUCCESS");
    reboot(RB_POWER_OFF);
    fail("poweroff");
}

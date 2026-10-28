/* Buffered single-threaded microbenchmarks; keep the read FD open across phases. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static void fail(const char *what) { perror(what); exit(1); }
static double now(void) {
    struct timespec time;
    if (clock_gettime(CLOCK_MONOTONIC, &time)) fail("clock_gettime");
    return time.tv_sec + time.tv_nsec / 1e9;
}
static long meminfo(const char *name) {
    FILE *file = fopen("/proc/meminfo", "r");
    if (!file) fail("meminfo");
    char key[128], line[256];
    long result = -1, value;
    while (fgets(line, sizeof(line), file))
        if (sscanf(line, "%127s %ld", key, &value) == 2 && !strcmp(key, name)) result = value;
    fclose(file);
    if (result < 0) fail("meminfo key");
    return result;
}
static void result(int phase, double seconds, uint64_t bytes, uint64_t ops) {
    char path[128];
    FILE *file = fopen("/control/result.tmp", "w");
    if (!file) fail("result");
    fprintf(file, "{\"seconds\":%.9f,\"bytes\":%llu,\"operations\":%llu,"
            "\"cached_kib\":%ld,\"mem_available_kib\":%ld}\n",
            seconds, (unsigned long long)bytes, (unsigned long long)ops,
            meminfo("Cached:"), meminfo("MemAvailable:"));
    if (fclose(file)) fail("close result");
    snprintf(path, sizeof(path), "/control/phase-%d.json", phase);
    if (rename("/control/result.tmp", path)) fail("rename result");
}
static void wait_for(int phase) {
    char path[128];
    snprintf(path, sizeof(path), "/control/go-%d", phase);
    double deadline = now() + 180;
    while (access(path, F_OK)) {
        if (now() > deadline) { fprintf(stderr, "host barrier timed out\n"); exit(1); }
        usleep(5000);
    }
}
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    double duration = atof(argv[2]);
    int input = open("/data/input", O_RDONLY);
    if (input < 0) fail("open input");
    struct stat st;
    if (fstat(input, &st)) fail("fstat input");
    size_t block = 1024 * 1024;
    char *buffer = malloc(block);
    if (!buffer) fail("malloc");
    memset(buffer, 1, block);
    uint64_t random = 0x123456789abcdefULL;
    result(-1, 0, 0, 0);
    for (int phase = 0; phase < 4; phase++) {
        wait_for(phase);
        uint64_t bytes = 0, ops = 0;
        off_t offset = 0;
        double start = now();
        if (phase == 3) {
            char path[128];
            snprintf(path, sizeof(path), "/data/write-%s", argv[1]);
            int output = open(path, O_CREAT | O_WRONLY | O_TRUNC, 0600);
            if (output < 0) fail("open output");
            while (offset < st.st_size) {
                ssize_t n = write(output, buffer, block);
                if (n != (ssize_t)block) fail("write");
                offset += n; bytes += n; ops++;
            }
            if (fdatasync(output) || close(output)) fail("fdatasync/close");
        } else {
            size_t length = phase == 2 ? 4096 : block;
            do {
                if (phase == 2) {
                    random ^= random << 13; random ^= random >> 7; random ^= random << 17;
                    offset = (random % (st.st_size / length)) * length;
                }
                ssize_t n = pread(input, buffer, length, offset);
                if (n != (ssize_t)length) fail("pread");
                bytes += n; ops++;
                if (phase != 2) offset = (offset + n) % st.st_size;
                if (phase == 0 && bytes >= (uint64_t)st.st_size) break;
            } while ((ops % 64) != 0 || now() - start < duration);
        }
        result(phase, now() - start, bytes, ops);
    }
    wait_for(4);
    close(input);
    if (umount("/data")) fail("umount data");
    puts("UML_FILESYSTEM_BENCHMARK_SUCCESS");
    fflush(stdout);
    reboot(RB_POWER_OFF);
    fail("poweroff");
}

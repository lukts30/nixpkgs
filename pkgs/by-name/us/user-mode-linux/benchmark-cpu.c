/* Identical binary in native Linux, UML, KVM and gVisor. No disk/network work. */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/futex.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/reboot.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define WORKERS 8
static pthread_barrier_t gate;
static int active, kind, quit;
static double seconds, deadline;
struct worker { uint64_t operations, checksum; int futex; };
static struct worker workers[WORKERS] __attribute__((aligned(64)));

static void fail(const char *what) { perror(what); exit(1); }
static double now(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) fail("clock_gettime");
    return t.tv_sec + t.tv_nsec / 1e9;
}
static void barrier(void) {
    int e = pthread_barrier_wait(&gate);
    if (e && e != PTHREAD_BARRIER_SERIAL_THREAD) { errno = e; fail("barrier"); }
}
static void run(struct worker *w, double end) {
    uint64_t count = 0, x = 0x123456789abcdefULL;
    long expected = syscall(SYS_gettid);
    /* A compute batch lasts several milliseconds: even UML's intercepted
     * clock_gettime must be negligible compared with the arithmetic itself. */
    unsigned batch = kind == 0 ? 4194304 : 4096;
    do {
        for (unsigned i = 0; i < batch; i++) {
            if (kind == 0) {
                x ^= x << 13; x ^= x >> 7; x ^= x << 17;
            } else if (kind == 1) {
                long r = syscall(SYS_gettid); /* Force interception: no libc cache/vDSO. */
                if (r != expected) fail("gettid result");
                x += r;
            } else {
                /* A real futex syscall with no waiter; not a pthread fast path. */
                if (syscall(SYS_futex, &w->futex, FUTEX_WAKE_PRIVATE, 1, 0, 0, 0) != 0)
                    fail("futex result");
                x++;
            }
        }
        count += batch;
    } while (now() < end);
    w->operations = count;
    w->checksum = x;
}
static void *worker(void *arg) {
    long index = (long)arg;
    for (;;) {
        barrier();
        if (quit) return NULL;
        if (index < active) run(&workers[index], now() + 0.2);
        barrier(); /* Warmup done, wait for the host's GO. */
        barrier();
        if (index < active) run(&workers[index], deadline);
        barrier(); /* Remain alive so host CPU accounting retains all threads. */
    }
}
static double timeval_seconds(struct timeval t) { return t.tv_sec + t.tv_usec / 1e6; }
int main(int argc, char **argv) {
    int guest = argc == 2 && !strcmp(argv[1], "--guest");
    setbuf(stdout, NULL);
    if (isatty(0)) {
        struct termios t;
        if (tcgetattr(0, &t)) fail("tcgetattr");
        cfmakeraw(&t);
        if (tcsetattr(0, TCSANOW, &t)) fail("tcsetattr");
    }
    if (pthread_barrier_init(&gate, NULL, WORKERS + 1)) fail("barrier init");
    pthread_t threads[WORKERS];
    for (long i = 0; i < WORKERS; i++) {
        int e = pthread_create(&threads[i], NULL, worker, (void *)i);
        if (e) { errno = e; fail("pthread_create"); }
    }
    printf("CPU_BOOT {\"online_cpus\":%ld}\n", sysconf(_SC_NPROCESSORS_ONLN));
    char line[128], name[32];
    while (fgets(line, sizeof(line), stdin)) {
        if (!strcmp(line, "QUIT\n")) break;
        if (sscanf(line, "PREP %31s %d %lf", name, &active, &seconds) != 3 ||
            active < 1 || active > WORKERS || seconds <= 0) return 2;
        kind = !strcmp(name, "compute") ? 0 : !strcmp(name, "gettid") ? 1 :
               !strcmp(name, "futex") ? 2 : -1;
        if (kind < 0) return 2;
        barrier(); barrier();
        puts("CPU_READY {}");
        if (!fgets(line, sizeof(line), stdin) || strcmp(line, "GO\n")) return 2;
        struct rusage before, after;
        if (getrusage(RUSAGE_SELF, &before)) fail("getrusage");
        double start = now();
        deadline = start + seconds;
        barrier(); barrier();
        double elapsed = now() - start;
        if (getrusage(RUSAGE_SELF, &after)) fail("getrusage");
        uint64_t operations = 0, checksum = 0;
        for (int i = 0; i < active; i++) {
            operations += workers[i].operations;
            checksum ^= workers[i].checksum;
        }
        double cpu = timeval_seconds(after.ru_utime) + timeval_seconds(after.ru_stime)
                   - timeval_seconds(before.ru_utime) - timeval_seconds(before.ru_stime);
        printf("CPU_RESULT {\"workload\":\"%s\",\"threads\":%d,\"seconds\":%.9f,"
               "\"operations\":%llu,\"checksum\":%llu,\"application_cpu_seconds\":%.6f}\n",
               name, active, elapsed, (unsigned long long)operations,
               (unsigned long long)checksum, cpu);
    }
    quit = 1;
    barrier();
    for (int i = 0; i < WORKERS; i++) pthread_join(threads[i], NULL);
    puts("CPU_SUCCESS");
    if (guest) { reboot(RB_POWER_OFF); fail("reboot"); }
    return 0;
}

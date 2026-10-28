/* Exercise shared filesystem semantics from inside the UML guest. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <unistd.h>

static void check(int ok, const char *what)
{
    if (!ok) {
        fprintf(stderr, "%s: %s\n", what, strerror(errno));
        exit(1);
    }
}

int main(int argc, char **argv)
{
    if (argc == 3 && strcmp(argv[1], "--probe-xattrs") == 0) {
        int fd = open(argv[2], O_RDWR);
        check(fd >= 0, "open host xattr probe");
        if (fsetxattr(fd, "user.preflight", "works", 5, 0) == -1) {
            if (errno == EOPNOTSUPP)
                return 77;
            check(0, "host xattr probe");
        }
        check(close(fd) == 0, "close host xattr probe");
        return 0;
    }
    check(argc == 4, "usage: test-filesystem DIRECTORY hostfs|virtiofs host-xattrs=yes|no");
    int virtio = strcmp(argv[2], "virtiofs") == 0;
    int host_xattrs = strcmp(argv[3], "yes") == 0;
    check(chdir(argv[1]) == 0, "chdir");

    char buffer[4096] = {0};
    int input = open("input", O_RDONLY);
    check(input >= 0, "open host input");
    check(read(input, buffer, sizeof(buffer)) == 14, "read host input");
    check(memcmp(buffer, "host-to-guest\n", 14) == 0, "host input contents");
    check(close(input) == 0, "close host input");

    int fd = open("output", O_CREAT | O_EXCL | O_RDWR, 0644);
    check(fd >= 0, "create");
    check(ftruncate(fd, sizeof(buffer)) == 0, "truncate");
    char *mapping = mmap(NULL, sizeof(buffer), PROT_READ | PROT_WRITE,
                         MAP_SHARED, fd, 0);
    check(mapping != MAP_FAILED, "shared mmap");
    memcpy(mapping, "guest-to-host via mmap\n", 23);
    check(msync(mapping, sizeof(buffer), MS_SYNC) == 0, "msync");
    check(munmap(mapping, sizeof(buffer)) == 0, "munmap");
    check(fsync(fd) == 0, "fsync");
    check(pread(fd, buffer, 23, 0) == 23, "read back mmap write");
    check(memcmp(buffer, "guest-to-host via mmap\n", 23) == 0, "mmap contents");

    check(fchmod(fd, 0640) == 0, "chmod");
    struct stat original, linked;
    check(fstat(fd, &original) == 0, "stat");
    check((original.st_mode & 0777) == 0640, "mode bits");
    if (virtio)
        check(original.st_uid == 0 && original.st_gid == 0, "translated ownership");
    printf("%s ownership uid=%u gid=%u\n", argv[2], original.st_uid, original.st_gid);

    check(link("output", "hardlink") == 0, "hard link");
    check(stat("hardlink", &linked) == 0, "stat hard link");
    check(original.st_ino == linked.st_ino && linked.st_nlink == 2, "hard link identity");
    check(symlink("output", "symlink") == 0, "symbolic link");
    check(readlink("symlink", buffer, sizeof(buffer)) == 6, "read symbolic link");
    check(memcmp(buffer, "output", 6) == 0, "symbolic link target");
    check(stat("symlink", &linked) == 0 && original.st_ino == linked.st_ino,
          "follow symbolic link");
    check(mkdir("directory", 0755) == 0, "mkdir");
    check(rename("hardlink", "directory/renamed") == 0, "rename");
    check(unlink("directory/renamed") == 0, "unlink");
    check(rmdir("directory") == 0, "rmdir");

    /* Verify contention between two guest processes, not host/guest locking. */
    check(flock(fd, LOCK_EX | LOCK_NB) == 0, "exclusive flock");
    pid_t child = fork();
    check(child >= 0, "fork");
    if (child == 0) {
        close(fd);
        int other = open("output", O_RDWR);
        if (other < 0)
            _exit(1);
        int result = flock(other, LOCK_EX | LOCK_NB);
        _exit(result == -1 && errno == EWOULDBLOCK ? 0 : 1);
    }
    int status;
    check(waitpid(child, &status, 0) == child, "wait for lock contender");
    check(WIFEXITED(status) && WEXITSTATUS(status) == 0, "flock contention");
    check(flock(fd, LOCK_UN) == 0, "unlock");

    int result = fsetxattr(fd, "user.uml", "works", 5, 0);
    if (virtio && host_xattrs) {
        check(result == 0, "set user xattr");
        check(fgetxattr(fd, "user.uml", buffer, sizeof(buffer)) == 5, "get user xattr");
        check(memcmp(buffer, "works", 5) == 0, "user xattr contents");
        puts("virtiofs user-xattr supported");
    } else {
        check(result == -1 && errno == EOPNOTSUPP, "xattr unavailable");
        puts(virtio ? "virtiofs user-xattr unavailable-on-host" : "hostfs user-xattr unsupported");
    }

    check(close(fd) == 0, "close");
    printf("%s read-write mmap links rename chmod guest-flock PASS\n", argv[2]);
    return 0;
}

{
  lib,
  stdenv,
  runCommand,
  writeScript,
  writeShellApplication,
  makeInitrd,
  busybox,
  coreutils,
  gnugrep,
  virtiofsd,
  attr,
  kernel,
}:
let
  probe = stdenv.mkDerivation {
    name = "uml-filesystem-probe";
    dontUnpack = true;
    buildPhase = ''
      $CC -Wall -Wextra -Werror -O2 ${./test-filesystem.c} -o test-filesystem
    '';
    installPhase = ''
      install -Dm755 test-filesystem "$out/bin/test-filesystem"
    '';
  };

  init = writeScript "uml-virtiofs-init" ''
    #!${busybox}/bin/sh
    export PATH=${busybox}/bin
    set -eu
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev
    for arg in $(cat /proc/cmdline); do
      case "$arg" in uml_test_dir=*) testdir="''${arg#uml_test_dir=}" ;; esac
    done
    mkdir -p /mnt/control /mnt/hostfs /mnt/virtiofs /mnt/readonly
    mount -t hostfs -o "$testdir/control" none /mnt/control
    # Keep diagnostics independent of the mounts tested read-only below.
    exec > /mnt/control/guest.log 2>&1
    mount -t hostfs -o "$testdir/hostfs-data" none /mnt/hostfs
    mount -t virtiofs uml-share /mnt/virtiofs
    mount -t virtiofs uml-readonly /mnt/readonly

    host_xattrs=$(cat /mnt/control/host-xattrs)
    ${probe}/bin/test-filesystem /mnt/hostfs hostfs "$host_xattrs"
    ${probe}/bin/test-filesystem /mnt/virtiofs virtiofs "$host_xattrs"

    # Both mounts must observe a host update after reopening the file.
    touch /mnt/control/guest-ready
    for attempt in $(seq 1 100); do
      if grep -q 'host-update' /mnt/hostfs/live &&
         grep -q 'host-update' /mnt/virtiofs/live; then break; fi
      sleep 0.1
    done
    grep -q 'host-update' /mnt/hostfs/live
    grep -q 'host-update' /mnt/virtiofs/live
    echo UML_FS_HOST_UPDATE_SUCCESS

    # A read-only export must reject writes even on a guest read-write mount.
    grep -q host-to-guest /mnt/readonly/input
    if touch /mnt/readonly/forbidden; then exit 1; fi
    echo UML_VIRTIOFS_READONLY_EXPORT_SUCCESS

    for fs in hostfs virtiofs; do
      mount -o remount,ro /mnt/$fs
      if touch /mnt/$fs/forbidden; then exit 1; fi
      umount /mnt/$fs
    done
    umount /mnt/readonly
    echo UML_FS_READONLY_MOUNTS_SUCCESS
    echo UML_VIRTIOFS_SUCCESS
    poweroff -f
  '';

  initrd = makeInitrd {
    name = "uml-virtiofs-initrd";
    contents = [
      {
        object = init;
        symlink = "/init";
      }
    ];
  };

  # Also runnable outside a Nix build: its sandbox blocks xattr syscalls.
  runner = writeShellApplication {
    name = "uml-virtiofs-test";
    runtimeInputs = [
      coreutils
      gnugrep
      virtiofsd
      attr
    ];
    text = ''
      out=''${1:?Usage: uml-virtiofs-test OUTPUT_DIRECTORY}
      mkdir -p "$out"
      out=$(realpath "$out")
      work=$(mktemp -d)
      cd "$work"
      mkdir runtime control hostfs-data virtiofs-data readonly-data
      pids=()
      cleanup() {
        status=$?
        trap - EXIT
        for pid in "''${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
        for pid in "''${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
        if [ -f control/guest.log ]; then cp control/guest.log "$out/guest.log"; fi
        if [ "$status" != 0 ]; then cat "$out/"*.log; fi
        cd /
        rm -rf "$work"
        exit "$status"
      }
      trap cleanup EXIT
      for directory in hostfs-data virtiofs-data readonly-data; do
        printf 'host-to-guest\n' > "$directory/input"
        echo before-update > "$directory/live"
      done
      if ${probe}/bin/test-filesystem --probe-xattrs virtiofs-data/input; then
        host_xattrs=yes
      else
        status=$?
        if [ "$status" != 77 ]; then exit "$status"; fi
        host_xattrs=no
      fi
      echo "$host_xattrs" > control/host-xattrs

      for share in share readonly; do
        directory=virtiofs-data
        extra=()
        if [ "$share" = readonly ]; then directory=readonly-data; extra+=(--readonly); fi
        # Export only temporary test directories. UID translation avoids root,
        # subordinate IDs and user namespaces, including inside a Nix sandbox.
        virtiofsd --socket-path="$PWD/runtime/$share.sock" \
          --shared-dir="$PWD/$directory" --tag="uml-$share" \
          --sandbox=none --cache=auto --xattr --inode-file-handles=never \
          --translate-uid="map:0:$(id -u):1" --translate-gid="map:0:$(id -g):1" \
          "''${extra[@]}" > "$out/$share-daemon.log" 2>&1 &
        pids+=("$!")
      done
      for _attempt in $(seq 1 100); do
        if [ -S runtime/share.sock ] && [ -S runtime/readonly.sock ]; then break; fi
        for pid in "''${pids[@]}"; do kill -0 "$pid"; done
        sleep 0.1
      done
      test -S runtime/share.sock
      test -S runtime/readonly.sock

      TMPDIR="$PWD/runtime" timeout 90 ${lib.getExe kernel} \
        mem=512M initrd=${initrd}/initrd con=null con0=fd:0,fd:1 ssl=null \
        uml_dir="$PWD/runtime" uml_test_dir="$PWD" \
        virtio_uml.device="$PWD/runtime/share.sock:26" \
        virtio_uml.device="$PWD/runtime/readonly.sock:26" > "$out/boot.log" 2>&1 &
      guest=$!
      pids+=("$guest")
      for _attempt in $(seq 1 600); do
        test ! -e control/guest-ready || break
        kill -0 "$guest"
        sleep 0.1
      done
      test -e control/guest-ready
      echo host-update > hostfs-data/live
      echo host-update > virtiofs-data/live
      wait "$guest"
      grep '^UML_VIRTIOFS_SUCCESS' control/guest.log

      for directory in hostfs-data virtiofs-data; do
        grep -a -q 'guest-to-host via mmap' "$directory/output"
        test "$(stat -c %a "$directory/output")" = 640
        test "$(stat -c %u "$directory/output")" = "$(id -u)"
        test "$(stat -c %g "$directory/output")" = "$(id -g)"
        test "$(readlink "$directory/symlink")" = output
        test ! -e "$directory/forbidden"
      done
      test ! -e readonly-data/forbidden
      if [ "$host_xattrs" = yes ]; then
        test "$(getfattr --only-values -n user.uml virtiofs-data/output)" = works
      fi
      grep -E '^(hostfs|virtiofs|UML_FS_|UML_VIRTIOFS_)' control/guest.log > "$out/comparison.txt"
      cat "$out/comparison.txt"
    '';
  };
in
runCommand "user-mode-linux-virtiofs-test"
  {
    passthru = { inherit runner; };
    meta.platforms = kernel.meta.platforms;
  }
  ''
    ${lib.getExe runner} "$out"
  ''

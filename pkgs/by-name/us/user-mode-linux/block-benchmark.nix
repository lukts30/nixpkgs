{
  lib,
  stdenv,
  runCommand,
  writeScript,
  writeShellScript,
  writeShellApplication,
  makeInitrd,
  makeModulesClosure,
  linux,
  busybox,
  coreutils,
  python3,
  qemu_test,
  qemu-utils,
  strace,
  util-linux,
  kernel,
}:
let
  probe = stdenv.mkDerivation {
    name = "uml-block-benchmark-probe";
    dontUnpack = true;
    buildPhase = ''
      $CC -Wall -Wextra -Werror -O2 ${./benchmark-block.c} -o probe
    '';
    installPhase = ''
      install -Dm755 probe "$out/bin/probe"
    '';
  };
  modules = makeModulesClosure {
    kernel = linux.modules;
    firmware = runCommand "empty-firmware" { } "mkdir -p $out";
    rootModules = [
      "virtio_pci"
      "virtio_blk"
    ];
  };
  init = writeScript "uml-block-benchmark-init" ''
    #!${busybox}/bin/sh
    export PATH=${busybox}/bin
    set -eu
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev
    for arg in $(cat /proc/cmdline); do
      case "$arg" in
        bench_device=*) device="''${arg#bench_device=}" ;;
        bench_seconds=*) seconds="''${arg#bench_seconds=}" ;;
      esac
    done
    if [ "$(uname -r)" = ${lib.escapeShellArg linux.modDirVersion} ]; then
      while read -r module; do insmod "$module"; done < ${modules}/insmod-list
    fi
    for attempt in $(seq 1 100); do
      [ ! -b "$device" ] || break
      sleep 0.1
    done
    test -b "$device"
    # Match queue settings, and disable readahead (the probe uses O_DIRECT).
    name="''${device#/dev/}"
    # QEMU's export initially advertises wce=0. Negotiate writeback so the
    # guest sends the flush requested by fdatasync, for both host cache modes.
    if [ -e "/sys/block/$name/cache_type" ]; then
      echo 'write back' > "/sys/block/$name/cache_type"
    fi
    test "$(cat "/sys/block/$name/queue/write_cache")" = 'write back'
    echo none > "/sys/block/$name/queue/scheduler"
    echo 0 > "/sys/block/$name/queue/read_ahead_kb"
    uname -r
    for setting in logical_block_size physical_block_size max_sectors_kb max_hw_sectors_kb nr_requests write_cache; do
      echo "BLOCK_QUEUE $setting=$(cat "/sys/block/$name/queue/$setting")"
    done
    exec ${probe}/bin/probe "$device" "$seconds" </dev/console >/dev/console 2>&1
  '';
  initrd = makeInitrd {
    name = "uml-block-benchmark-initrd";
    contents = [
      {
        object = init;
        symlink = "/init";
      }
    ];
  };
  scripts = runCommand "uml-block-benchmark-scripts" { } ''
    mkdir -p $out
    cp ${./benchmark-filesystem.py} $out/benchmark_metrics.py
    cp ${./test-block-root.py} $out/block_root.py
    cp ${./benchmark-block.py} $out/benchmark_block.py
  '';
  namespace = writeShellScript "uml-block-benchmark-namespace" ''
    set -eu
    runtime=$1
    shift
    ${util-linux}/bin/mount -t tmpfs -o size=6G,mode=700 tmpfs "$runtime"
    exec ${python3}/bin/python ${scripts}/benchmark_block.py \
      --kernel ${lib.getExe kernel} --native-kernel ${linux}/bzImage \
      --kernel-version ${linux.modDirVersion} --initrd ${initrd}/initrd \
      --qemu ${qemu_test}/bin/qemu-system-x86_64 --strace ${lib.getExe strace} \
      --runtime "$runtime" "$@"
  '';
in
writeShellApplication {
  name = "uml-block-benchmark";
  runtimeInputs = [
    coreutils
    qemu-utils
  ];
  text = ''
    runtime=$(mktemp -d)
    trap 'rmdir "$runtime"' EXIT
    ${util-linux}/bin/unshare --user --map-root-user --mount ${namespace} "$runtime" "$@"
  '';
  meta.platforms = [ "x86_64-linux" ];
}

{
  lib,
  stdenv,
  runCommand,
  writeScript,
  writeShellScript,
  writeShellApplication,
  makeInitrd,
  linux,
  busybox,
  coreutils,
  python3,
  qemu_test,
  gvisor,
  strace,
  util-linux,
  kernel,
}:
let
  probe = stdenv.mkDerivation {
    name = "uml-cpu-benchmark-probe";
    dontUnpack = true;
    buildPhase = ''
      $CC -Wall -Wextra -Werror -O2 -pthread ${./benchmark-cpu.c} -o probe
    '';
    installPhase = ''
      install -Dm755 probe "$out/bin/probe"
    '';
  };
  init = writeScript "uml-cpu-benchmark-init" ''
    #!${busybox}/bin/sh
    export PATH=${busybox}/bin
    set -eu
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev
    exec ${probe}/bin/probe --guest </dev/console >/dev/console 2>&1
  '';
  initrd = makeInitrd {
    name = "uml-cpu-benchmark-initrd";
    contents = [
      {
        object = init;
        symlink = "/init";
      }
    ];
  };
  scripts = runCommand "uml-cpu-benchmark-scripts" { } ''
    mkdir -p $out
    cp ${./benchmark-filesystem.py} $out/benchmark_metrics.py
    cp ${./test-block-root.py} $out/block_root.py
    cp ${./benchmark-cpu.py} $out/benchmark_cpu.py
  '';
  namespace = writeShellScript "uml-cpu-benchmark-namespace" ''
    set -eu
    runtime=$1
    shift
    ${util-linux}/bin/mount -t tmpfs -o size=4G,mode=700 tmpfs "$runtime"
    exec ${python3}/bin/python ${scripts}/benchmark_cpu.py \
      --kernel ${lib.getExe kernel} --native-kernel ${linux}/bzImage \
      --initrd ${initrd}/initrd --probe ${probe}/bin/probe \
      --qemu ${qemu_test}/bin/qemu-system-x86_64 \
      --runsc ${gvisor}/bin/runsc --strace ${lib.getExe strace} \
      --runtime "$runtime" "$@"
  '';
in
writeShellApplication {
  name = "uml-cpu-benchmark";
  runtimeInputs = [ coreutils ];
  text = ''
    runtime=$(mktemp -d)
    trap 'rmdir "$runtime"' EXIT
    ${util-linux}/bin/unshare --user --map-root-user --mount ${namespace} "$runtime" "$@"
  '';
  meta.platforms = [ "x86_64-linux" ];
}

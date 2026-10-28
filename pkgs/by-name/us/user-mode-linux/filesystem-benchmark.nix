{
  lib,
  stdenv,
  writeScript,
  writeShellScript,
  writeShellApplication,
  makeInitrd,
  busybox,
  coreutils,
  python3,
  virtiofsd,
  util-linux,
  kernel,
}:
let
  probe = stdenv.mkDerivation {
    name = "uml-filesystem-benchmark-probe";
    dontUnpack = true;
    buildPhase = ''
      $CC -Wall -Wextra -Werror -O2 ${./benchmark-filesystem.c} -o probe
    '';
    installPhase = ''
      install -Dm755 probe "$out/bin/probe"
    '';
  };
  init = writeScript "uml-filesystem-benchmark-init" ''
    #!${busybox}/bin/sh
    export PATH=${busybox}/bin
    set -eu
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev
    for arg in $(cat /proc/cmdline); do
      case "$arg" in
        bench_control=*) control="''${arg#bench_control=}" ;;
        bench_data=*) data="''${arg#bench_data=}" ;;
        bench_id=*) id="''${arg#bench_id=}" ;;
        bench_seconds=*) seconds="''${arg#bench_seconds=}" ;;
        bench_mode=*) mode="''${arg#bench_mode=}" ;;
      esac
    done
    mkdir /control /data
    mount -t hostfs -o "$control" none /control
    if [ "$mode" = hostfs ]; then
      mount -t hostfs -o "$data" none /data
    else
      mount -t virtiofs benchmark /data
    fi
    exec ${probe}/bin/probe "$id" "$seconds"
  '';
  initrd = makeInitrd {
    name = "uml-filesystem-benchmark-initrd";
    contents = [
      {
        object = init;
        symlink = "/init";
      }
    ];
  };
  namespace = writeShellScript "uml-benchmark-namespace" ''
    set -eu
    runtime=$1
    shift
    # Keep guest RAM off the host disk without changing any host mount.
    ${util-linux}/bin/mount -t tmpfs -o size=6G,mode=700 tmpfs "$runtime"
    exec ${python3}/bin/python ${./benchmark-filesystem.py} \
      --kernel ${lib.getExe kernel} --initrd ${initrd}/initrd \
      --virtiofsd ${lib.getExe virtiofsd} --runtime "$runtime" "$@"
  '';
in
writeShellApplication {
  name = "uml-filesystem-benchmark";
  runtimeInputs = [ coreutils ];
  text = ''
    runtime=$(mktemp -d)
    trap 'rmdir "$runtime"' EXIT
    ${util-linux}/bin/unshare --user --map-root-user --mount ${namespace} "$runtime" "$@"
  '';
  meta.platforms = [ "x86_64-linux" ];
}

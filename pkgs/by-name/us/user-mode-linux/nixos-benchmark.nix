{
  lib,
  nixosTests,
  writeShellScript,
  writeShellApplication,
  python3,
  coreutils,
  util-linux,
}:
let
  tests = nixosTests.user-mode-linux;
  namespace = writeShellScript "uml-nixos-benchmark-namespace" ''
    set -eu
    runtime=$1
    shift
    ${util-linux}/bin/mount -t tmpfs -o size=6G,mode=700 tmpfs "$runtime"
    exec ${python3}/bin/python ${./benchmark-nixos.py} \
      --driver tmpfs-ptrace=${lib.getExe tests.gitdaemon.driver} \
      --driver tmpfs-seccomp=${lib.getExe tests.gitdaemon-seccomp.driver} \
      --driver block-ptrace=${lib.getExe tests.gitdaemon-block-root.driver} \
      --driver block-seccomp=${lib.getExe tests.gitdaemon-block-root-seccomp.driver} \
      --runtime "$runtime" "$@"
  '';
in
writeShellApplication {
  name = "uml-nixos-benchmark";
  runtimeInputs = [ coreutils ];
  text = ''
    runtime=$(mktemp -d)
    trap 'rmdir "$runtime"' EXIT
    ${util-linux}/bin/unshare --user --map-root-user --mount ${namespace} "$runtime" "$@"
  '';
  meta.platforms = [ "x86_64-linux" ];
}

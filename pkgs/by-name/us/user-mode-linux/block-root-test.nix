{
  lib,
  runCommand,
  writeShellApplication,
  pkgsStatic,
  python3,
  e2fsprogs,
  qemu-utils,
  qemu_test,
  kernel,
}:
let
  runner = writeShellApplication {
    name = "uml-block-root-test";
    runtimeInputs = [
      e2fsprogs
      qemu-utils
    ];
    text = ''
      exec ${python3}/bin/python ${./test-block-root.py} \
        --kernel ${lib.getExe kernel} \
        --busybox ${pkgsStatic.busybox}/bin/busybox \
        --qemu ${qemu_test}/bin/qemu-system-x86_64 "$@"
    '';
  };
  test =
    mode:
    runCommand "user-mode-linux-${mode}-root-test"
      {
        passthru = { inherit runner; };
        meta.platforms = [ "x86_64-linux" ];
      }
      ''
        ${lib.getExe runner} ${mode} "$out"
      '';
in
{
  blockRoot = test "ubd";
  qemuBlockRoot = test "qemu";
}

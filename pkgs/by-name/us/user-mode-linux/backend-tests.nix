{
  lib,
  runCommand,
  writeScript,
  writeShellApplication,
  makeInitrd,
  busybox,
  python3,
  passt,
  slirp4netns,
  vhost-device-vsock,
  kernel,
}:
let
  init = writeScript "uml-backend-init" ''
    #!${busybox}/bin/sh
    export PATH=${busybox}/bin
    set -eu
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev
    for arg in $(cat /proc/cmdline); do
      case "$arg" in uml_control=*) control="''${arg#uml_control=}" ;; esac
    done
    mkdir /control
    mount -t hostfs -o "$control" none /control
    exec > /control/guest.log 2>&1
    ${python3}/bin/python ${./test-backends.py} guest /control
    echo UML_BACKEND_SUCCESS
    poweroff -f
  '';
  initrd = makeInitrd {
    name = "uml-backend-initrd";
    contents = [
      {
        object = init;
        symlink = "/init";
      }
    ];
  };
  runner = writeShellApplication {
    name = "uml-backend-test";
    text = ''
      exec ${python3}/bin/python ${./test-backends.py} host \
        --kernel ${lib.getExe kernel} --initrd ${initrd}/initrd \
        --passt ${lib.getExe passt} --slirp ${lib.getExe slirp4netns} \
        --vsock ${vhost-device-vsock}/bin/vhost-device-vsock "$@"
    '';
  };
  test =
    backend:
    runCommand "user-mode-linux-${backend}-test"
      {
        passthru = { inherit runner; };
        meta.platforms = kernel.meta.platforms;
      }
      ''
        ${lib.getExe runner} ${backend} "$out"
      '';
in
{
  inherit runner;
  slirp = test "slirp";
  passt = test "passt";
  vsock = test "vsock";
}

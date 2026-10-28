{
  lib,
  runCommand,
  writeScript,
  writeShellApplication,
  makeInitrd,
  busybox,
  python3,
  e2fsprogs,
  vhost-device-vsock,
  kernel,
}:
let
  # Reuse the pinned workspace and Cargo dependency set of the vsock package.
  # These backends need no host devices, audio server or additional libraries.
  backends = vhost-device-vsock.overrideAttrs (old: {
    pname = "uml-vhost-device-backends";
    patches = (old.patches or [ ]) ++ [
      ./vhost-device-scsi-uml.patch
      ./vhost-device-console-rx.patch
    ];
    outputs = [ "out" ];
    cargoBuildFlags = [
      "-p"
      "vhost-device-rng"
      "-p"
      "vhost-device-console"
      "-p"
      "vhost-device-scsi"
    ];
    cargoTestFlags = [
      "-p"
      "vhost-device-rng"
      "-p"
      "vhost-device-console"
      "-p"
      "vhost-device-scsi"
    ];
    postInstall = "";
    meta = old.meta // {
      description = "RNG, console and SCSI vhost-user backends for UML tests";
    };
  });
  init = writeScript "uml-device-init" ''
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
    ${python3}/bin/python ${./test-devices.py} guest /control
    echo UML_DEVICES_SUCCESS
    poweroff -f
  '';
  initrd = makeInitrd {
    name = "uml-device-initrd";
    contents = [
      {
        object = init;
        symlink = "/init";
      }
    ];
  };
  runner = writeShellApplication {
    name = "uml-device-test";
    text = ''
      exec ${python3}/bin/python ${./test-devices.py} host \
        --kernel ${lib.getExe kernel} --initrd ${initrd}/initrd \
        --backends ${backends}/bin --mkfs ${lib.getExe' e2fsprogs "mkfs.ext4"} "$@"
    '';
  };
in
runCommand "user-mode-linux-devices-test"
  {
    passthru = { inherit runner backends; };
    meta.platforms = [ "x86_64-linux" ];
  }
  ''
    ${lib.getExe runner} "$out"
  ''

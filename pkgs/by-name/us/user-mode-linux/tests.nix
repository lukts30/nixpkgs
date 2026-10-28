{
  lib,
  stdenv,
  runCommand,
  writeScript,
  writeShellApplication,
  makeInitrd,
  busybox,
  e2fsprogs,
  coreutils,
  uml-utilities,
  linuxPackagesFor,
  kernel,
}:

let
  testModule = stdenv.mkDerivation {
    __structuredAttrs = true;
    name = "user-mode-linux-test-module";
    dontUnpack = true;
    hardeningDisable = kernel.hardeningDisable;
    nativeBuildInputs = kernel.moduleBuildDependencies;
    makeFlags = (linuxPackagesFor kernel).kernelModuleMakeFlags;
    buildPhase = ''
      cp ${./test-module.c} uml_test.c
      cp ${./test-module.rs} uml_rust_test.rs
      echo 'obj-m := uml_test.o uml_rust_test.o' > Makefile
      make -C ${kernel.dev}/lib/modules/${kernel.modDirVersion}/build \
        M="$PWD" "''${makeFlags[@]}" modules
    '';
    installPhase = ''
      mkdir -p "$out"
      cp uml_test.ko uml_rust_test.ko "$out/"
    '';
    stripDebugList = [ "." ];
  };

  init = writeScript "uml-test-init" ''
    #!${busybox}/bin/sh
    export PATH=${busybox}/bin
    set -eu
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev

    test "$(uname -r)" = ${lib.escapeShellArg kernel.modDirVersion}
    insmod ${kernel.modules}/lib/modules/${kernel.modDirVersion}/kernel/drivers/net/dummy.ko numdummies=1
    ip link set dummy0 up
    test -d /sys/class/net/dummy0
    rmmod dummy

    insmod ${testModule}/uml_test.ko
    dmesg | grep -q 'UML external module loaded'
    rmmod uml_test
    dmesg | grep -q 'UML external module unloaded'

    insmod ${testModule}/uml_rust_test.ko
    dmesg | grep -Fq 'UML Rust module loaded: [42, 108]'
    rmmod uml_rust_test
    dmesg | grep -Fq 'UML Rust module unloaded: [42, 108]'

    if grep -q 'uml_test_block' /proc/cmdline; then
      mkdir -p /mnt/block /mnt/host
      mount -t ext4 /dev/ubda /mnt/block
      grep -q 'UML block input' /mnt/block/input
      echo 'UML block output' > /mnt/block/output
      umount /mnt/block
      for arg in $(cat /proc/cmdline); do
        case "$arg" in
          uml_test_host=*) hostdir="''${arg#uml_test_host=}" ;;
        esac
      done
      mount -t hostfs -o "$hostdir" none /mnt/host
      grep -q 'UML host input' /mnt/host/input
      echo 'UML host output' > /mnt/host/output
      umount /mnt/host
    fi

    echo UML_TEST_SUCCESS
    if grep -q 'uml_shell' /proc/cmdline; then
      echo 'User Mode Linux shell; exit to power off.'
      setsid cttyhack sh || true
    elif grep -q 'uml_test_mconsole' /proc/cmdline; then
      while :; do sleep 1; done
    fi
    poweroff -f
  '';

  initrd = makeInitrd {
    name = "user-mode-linux-initrd";
    contents = [
      {
        object = init;
        symlink = "/init";
      }
    ];
  };
in
{
  inherit initrd testModule;

  demo = writeShellApplication {
    name = "uml-demo";
    runtimeInputs = [ coreutils ];
    text = ''
      runtime=$(mktemp -d)
      trap 'rm -rf "$runtime"' EXIT
      TMPDIR="$runtime" ${lib.getExe kernel} \
        mem=256M initrd=${initrd}/initrd \
        con=null con0=fd:0,fd:1 ssl=null uml_dir="$runtime" \
        uml_shell "$@"
    '';
  };

  boot =
    runCommand "user-mode-linux-boot-test"
      {
        nativeBuildInputs = [
          e2fsprogs
          uml-utilities
        ];
        meta.platforms = kernel.meta.platforms;
      }
      ''
        mkdir -p "$out" runtime host-data disk-data
        echo 'UML host input' > host-data/input
        echo 'UML block input' > disk-data/input
        truncate -s 32M disk.img
        mkfs.ext4 -q -d disk-data disk.img

        export TMPDIR="$PWD/runtime"
        # With 512 MiB, UML modules can be placed above the signed 32-bit
        # address range, exercising Rust's large code model on x86-64.
        timeout 90 ${lib.getExe kernel} mem=512M initrd=${initrd}/initrd \
          con=null con0=fd:0,fd:1 ssl=null uml_dir="$PWD/runtime" umid=test \
          ubd0="$PWD/disk.img" uml_test_block uml_test_host="$PWD/host-data" \
          uml_test_mconsole > "$out/boot.log" 2>&1 &
        pid=$!
        trap 'kill "$pid" 2>/dev/null || true' EXIT

        for attempt in $(seq 1 600); do
          if grep -q '^UML_TEST_SUCCESS' "$out/boot.log"; then break; fi
          if ! kill -0 "$pid" 2>/dev/null; then cat "$out/boot.log"; exit 1; fi
          sleep 0.1
        done
        if ! grep '^UML_TEST_SUCCESS' "$out/boot.log"; then
          cat "$out/boot.log"
          exit 1
        fi
        timeout 10 uml_mconsole "$PWD/runtime/test/mconsole" version | tee "$out/mconsole.log"
        grep -F '${kernel.modDirVersion}' "$out/mconsole.log"
        timeout 10 uml_mconsole "$PWD/runtime/test/mconsole" halt
        wait "$pid"
        trap - EXIT

        grep -q 'UML host output' host-data/output
        debugfs -R 'cat /output' disk.img | grep -q 'UML block output'
      '';
}

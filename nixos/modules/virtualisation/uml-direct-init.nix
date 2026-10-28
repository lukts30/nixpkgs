# The kernel mounts the real root read-only. This bootstrap lives on that root,
# not in an initramfs, and hands PID 1 to the ordinary NixOS stage 2.
{ config, pkgs }:
let
  bootstrap = pkgs.writeShellScript "uml-direct-init" ''
    set -eu
    export PATH=${
      pkgs.lib.makeBinPath [
        pkgs.coreutils
        pkgs.util-linux
      ]
    }
    echo 'uml-boot: bootstrap-start' > /dev/kmsg

    # Do this before fsck or the first writable root mount, just as in stage 1.
    echo 'write back' > /sys/block/vda/cache_type
    read -r mode < /sys/block/vda/queue/write_cache
    test "$mode" = 'write back'
    echo 'uml-boot: root-writeback' > /dev/kmsg
    status=0
    ${pkgs.e2fsprogs}/bin/e2fsck -p /dev/vda || status=$?
    case "$status" in
      0|1) ;;
      *) echo "UML root fsck requires recovery (exit $status)" >&2; exit 1 ;;
    esac
    echo 'uml-boot: root-fsck-done' > /dev/kmsg
    mount -n -o remount,rw /dev/vda /

    # Stage 2 skips its entire early-mount block when /proc/1 already exists.
    # Complete that same mount list here, preserving our initial mounts.
    specialMount() {
      if mountpoint -q "$2"; then
        mount -n -t "$4" -o "remount,$3" "$1" "$2"
      else
        install -m 0755 -d "$2"
        mount -n -t "$4" -o "$3" "$1" "$2"
      fi
    }
    source ${config.system.build.earlyMountScript}
    ln -sfn /proc/self/fd /dev/fd
    ln -sfn /proc/self/fd/0 /dev/stdin
    ln -sfn /proc/self/fd/1 /dev/stdout
    ln -sfn /proc/self/fd/2 /dev/stderr
    echo 'uml-boot: stage2-start' > /dev/kmsg
    exec "$uml_stage2"
  '';
in
{
  inherit bootstrap;
  init = pkgs.writeText "uml-init" ''
    #!/uml-busybox sh
    set -eu
    /uml-busybox mount -t proc proc /proc
    /uml-busybox mount -t sysfs sysfs /sys
    if ! /uml-busybox mountpoint -q /dev; then
      /uml-busybox mount -t devtmpfs devtmpfs /dev
    fi
    # BusyBox uses mount(2); hostfs's legacy mount data is the bare host path.
    /uml-busybox mount -t hostfs -o /nix/store,ro none /nix/store
    exec "$uml_bootstrap"
  '';
}

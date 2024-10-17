{
  lib,
  libguestfs,
  libguestfs-appliance,
}:

# https://github.com/NixOS/nixpkgs/issues/280881
lib.warnIf (builtins.compareVersions libguestfs.version libguestfs-appliance.version >= 0)
  "libguestfs and libguestfs-appliance versions are out of sync, runtime errors may occur!"

  libguestfs.override
  { appliance = libguestfs-appliance; }

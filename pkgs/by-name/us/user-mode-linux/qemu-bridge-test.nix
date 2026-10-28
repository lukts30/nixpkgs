{
  lib,
  callPackage,
  qemu,
  qemu_test,
  runCommand,
  writeShellApplication,
  kernel,
}:
let
  # Upstream marks this development tool install:false. Build only its target;
  # the normal QEMU package does not install the contrib backends.
  bridge =
    (qemu.override {
      toolsOnly = true;
      enableDocs = false;
      enableBlobs = false;
    }).overrideAttrs
      (old: {
        pname = "qemu-vhost-user-bridge";
        separateDebugInfo = false;
        patches = old.patches ++ [ ./qemu-vhost-user-bridge-features.patch ];
        buildPhase = ''
          runHook preBuild
          ninja -C build -j "$NIX_BUILD_CORES" contrib/vhost-user-bridge/vhost-user-bridge
          runHook postBuild
        '';
        installPhase = ''
          runHook preInstall
          install -Dm755 build/contrib/vhost-user-bridge/vhost-user-bridge $out/bin/vhost-user-bridge
          runHook postInstall
        '';
        meta = old.meta // {
          mainProgram = "vhost-user-bridge";
        };
      });
  backend = callPackage ./backend-tests.nix { inherit kernel; };
  runner = writeShellApplication {
    name = "uml-qemu-network-test";
    text = ''
      exec ${lib.getExe backend.runner} \
        --qemu ${qemu_test}/bin/qemu-system-x86_64 \
        --bridge ${lib.getExe bridge} qemu "$@"
    '';
  };
in
runCommand "user-mode-linux-qemu-network-test"
  {
    passthru = { inherit runner bridge; };
    meta.platforms = [ "x86_64-linux" ];
  }
  ''
    ${lib.getExe runner} "$out"
  ''

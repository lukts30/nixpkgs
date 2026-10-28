{ lib, pkgs, ... }:
let
  lighttpd = import ./lighttpd.nix { inherit lib pkgs; };
in
{
  name = "user-mode-linux-lighttpd";
  umls = lighttpd.nodes;
  testScript = lighttpd.testScript + ''
    assert "-uml" in server.succeed("uname -r")
    server.succeed("echo shared-from-uml > /tmp/shared/uml-marker")
    server.succeed("cat /etc/os-release > /tmp/os-release")
    server.copy_from_machine("/tmp/os-release", "os-release")
    assert (server.shared_dir / "uml-marker").read_text().strip() == "shared-from-uml"
    server.copy_from_host("${pkgs.writeText "uml-copy-input" "host-to-UML"}", "/tmp/copy-input")
    assert server.succeed("cat /tmp/copy-input") == "host-to-UML"
    server.shutdown()
    assert not server.is_up()
  '';
}

{ pkgs, ... }:
let
  original = import ./gitdaemon.nix { inherit pkgs; };
  udpEcho = pkgs.writeText "uml-udp-echo.py" ''
    import pathlib
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("0.0.0.0", 18080))
        pathlib.Path("/run/uml-udp-ready").touch()
        while True:
            data, peer = server.recvfrom(4096)
            server.sendto(data.upper(), peer)
  '';
  udpCheck = pkgs.writeText "uml-udp-check.py" ''
    import socket
    import sys
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(5)
        client.connect((sys.argv[1], 18080))
        client.send(b"uml udp")
        assert client.recv(4096) == b"UML UDP"
  '';
in
{
  name = "user-mode-linux-gitdaemon";
  umls = original.nodes;
  umlDefaults.virtualisation.uml.networking.enable = true;
  umlDefaults.virtualisation.uml.networking.extraUDPPorts = [ 18080 ];
  testScript = original.testScript + ''
    with subtest("UDP between guests in both directions"):
        for machine in [client, server]:
            machine.succeed("systemd-run --unit=uml-udp ${pkgs.python3}/bin/python ${udpEcho}")
            machine.wait_for_file("/run/uml-udp-ready")
        client.succeed("${pkgs.python3}/bin/python ${udpCheck} server")
        server.succeed("${pkgs.python3}/bin/python ${udpCheck} client")
    with subtest("restart the client and its passt backend"):
        client.shutdown()
        client.start()
        client.wait_for_unit("network-online.target")
        client.succeed("git clone git://server/project.git /project")
        assert client.succeed("cat /project/bar").strip() == "foo"
    client.shutdown()
    server.shutdown()
  '';
}

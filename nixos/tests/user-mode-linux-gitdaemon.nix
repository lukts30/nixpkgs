{
  blockRoot ? false,
  seccomp ? false,
}:
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
  name =
    "user-mode-linux-gitdaemon"
    + pkgs.lib.optionalString blockRoot "-block-root"
    + pkgs.lib.optionalString seccomp "-seccomp";
  umls = original.nodes;
  umlDefaults.virtualisation.uml.networking.enable = true;
  umlDefaults.virtualisation.uml.networking.extraUDPPorts = [ 18080 ];
  umlDefaults.virtualisation.uml.rootDisk.enable = pkgs.lib.mkIf blockRoot true;
  umlDefaults.virtualisation.uml.seccomp.enable = seccomp;
  testScript = ''
    from pathlib import Path
  ''
  + original.testScript
  + ''
    with subtest("UML execution mode"):
        for machine in [client, server]:
            assert machine.process is not None
            arguments = Path(f"/proc/{machine.process.pid}/cmdline").read_bytes().split(b"\0")
            assert b"seccomp=${if seccomp then "on" else "off"}" in arguments, arguments
  ''
  + pkgs.lib.optionalString blockRoot ''
    with subtest("block roots and shared store"):
        for machine in [client, server]:
            assert machine.succeed("findmnt -n -o FSTYPE /").strip() == "ext4"
            assert machine.succeed("findmnt -n -o SOURCE /").strip() == "/dev/vda"
            store_types = machine.succeed("findmnt -n -o FSTYPE -T /nix/store").splitlines()
            assert set(store_types) == {"hostfs"}, repr(store_types)
            assert machine.succeed("cat /sys/block/vda/queue/write_cache").strip() == "write back"
            journal = machine.succeed("journalctl -b -o cat --no-pager")
            assert journal.index("Finished Configure UML root disk flushes.") < journal.index("Starting File System Check on /dev/vda")
            machine.succeed("echo persisted > /var/lib/uml-root-marker")
        client.copy_from_host("${pkgs.writeText "uml-block-copy" "block-root transfer"}", "/root/copied")
        assert client.succeed("cat /root/copied") == "block-root transfer"
        client.copy_from_machine("/root/copied", "client-copy")
  ''
  + ''
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
        client.succeed("git clone git://server/project.git /project-after-restart")
        assert client.succeed("cat /project-after-restart/bar").strip() == "foo"
        assert client.process is not None
        arguments = Path(f"/proc/{client.process.pid}/cmdline").read_bytes().split(b"\0")
        assert b"seccomp=${if seccomp then "on" else "off"}" in arguments, arguments
  ''
  + pkgs.lib.optionalString blockRoot ''
    with subtest("root data survives restarting the client and storage backend"):
        assert client.succeed("cat /var/lib/uml-root-marker").strip() == "persisted"
        assert client.succeed("cat /project/bar").strip() == "foo"
        assert client.succeed("cat /root/copied") == "block-root transfer"
        assert client.succeed("findmnt -n -o FSTYPE /").strip() == "ext4"
    with subtest("server repository survives restarting all its backends"):
        server.shutdown()
        server.start()
        server.wait_for_unit("git-daemon.service")
        server.wait_for_unit("network-online.target")
        assert server.succeed("cat /var/lib/uml-root-marker").strip() == "persisted"
        client.succeed("git clone git://server/project.git /project-after-server-restart")
        assert client.succeed("cat /project-after-server-restart/bar").strip() == "foo"
  ''
  + ''
    client.shutdown()
    server.shutdown()
  '';
}

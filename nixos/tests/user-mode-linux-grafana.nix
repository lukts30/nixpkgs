{ lib, pkgs, ... }:
let
  # Reuse all five configurations and the entire script from the nspawn test.
  grafana = import ./grafana/basic.nix { inherit lib pkgs; };
in
{
  name = "user-mode-linux-grafana";
  umls = grafana.containers;
  inherit (grafana) testScript;
}

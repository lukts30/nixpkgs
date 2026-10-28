{
  lib,
  stdenv,
  fetchurl,
  readline,
}:

stdenv.mkDerivation {
  __structuredAttrs = true;
  pname = "uml-utilities";
  version = "20070815.4";

  src = fetchurl {
    url = "mirror://debian/pool/main/u/uml-utilities/uml-utilities_20070815.4.orig.tar.gz";
    hash = "sha256-fXOOWWJIAHH0y5ypBTxxMNaXsjLIHz+Asgq8jLmYMVQ=";
  };

  buildInputs = [ readline ];

  # Build the management, console, disk-image, and networking utilities.
  makeFlags = [
    "SUBDIRS=lib mconsole moo port-helper tunctl uml_net uml_switch watchdog"
    "BIN_DIR=$(out)/bin"
    "LIB_DIR=$(out)/lib/uml"
    "CFLAGS=-O2 -Wall -std=gnu17"
    "TUNCTL=tunctl"
    "TUNTAP=-DTUNTAP"
  ];

  postPatch = ''
    substituteInPlace uml_net/Makefile --replace-fail '-m 04755' '-m 0755'
  '';

  meta = {
    description = "Host utilities for User Mode Linux, including the management console";
    homepage = "https://user-mode-linux.sourceforge.net/";
    license = lib.licenses.gpl2Only;
    platforms = lib.platforms.linux;
    mainProgram = "uml_mconsole";
    maintainers = [ ];
  };
}

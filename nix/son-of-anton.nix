# nix/son-of-anton.nix — Son of Anton Agent package
#
# The Go command and daemon (packages.nix), wrapped so they find the runtime
# tools on PATH and the Python the physics mode runs on.
#
# callPackage auto-wires nixpkgs args; the rest is passed explicitly.
# Override the tools on the wrapper's PATH with
#   pkgs.son-of-anton.override { extraPackages = [...]; }
{
  lib,
  stdenv,
  makeWrapper,
  bash,
  coreutils,
  git,
  ripgrep,
  openssh,
  poppler-utils,
  bubblewrap,
  ffmpeg,
  # The Go daemon and command: the TUI, -q and -z are its clients (packages.nix).
  soa-d,
  # The physics harness environment (nix/physics-harness.nix): python3 with
  # autophysicist importable.
  physicsHarness,
  # More tools on the wrapper's PATH (and the dev shell's), e.g. the language
  # servers an editor config enables: the daemon's LSP uses whatever is here.
  extraPackages ? [ ],
}:
let
  runtimeDeps = [
    bash
    coreutils
    git
    ripgrep
    # git over SSH (the module's git.github deploy key).
    openssh
    # web_extract converts PDFs with pdftotext.
    poppler-utils
    # The physics mode refuses to execute model-authored code without it.
    bubblewrap
    # Agents convert audio and video in their terminal (voice notes, clips).
    ffmpeg
  ]
  ++ extraPackages;

  runtimePath = lib.makeBinPath runtimeDeps;
in
stdenv.mkDerivation {
  pname = "son-of-anton";
  inherit (soa-d) version;

  dontUnpack = true;
  dontBuild = true;
  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall

    mkdir -p $out/bin

    # `son-of-anton` is the Go command. It execs soa-d (the wrapped one below)
    # and, for `problem`, the harness python (the physics mode, until its Go
    # port).
    makeWrapper ${soa-d}/bin/son-of-anton $out/bin/son-of-anton \
      --set SOA_D_BIN $out/bin/soa-d \
      --suffix PATH : "${runtimePath}" \
      --set SON_OF_ANTON_PYTHON ${physicsHarness}/bin/python3

    # The unit's daemon (backend = "soa-d"): the same runtime tools as the
    # CLI, and the harness python for execute_code. soa-d is itself a
    # wrapper, so this wraps the wrapper.
    makeWrapper ${soa-d}/bin/soa-d $out/bin/soa-d \
      --suffix PATH : "${runtimePath}" \
      --set SON_OF_ANTON_PYTHON ${physicsHarness}/bin/python3

    runHook postInstall
  '';

  passthru = {
    inherit runtimeDeps physicsHarness;
  };

  meta = with lib; {
    description = "Always-on agent harness with physics research modes";
    homepage = "https://github.com/ewtodd/son-of-anton";
    mainProgram = "son-of-anton";
    license = licenses.mit;
    platforms = platforms.linux;
  };
}

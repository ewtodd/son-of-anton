# nix/son-of-anton.nix — the two commands, wrapped to run anywhere.
#
# soa-d and son-of-anton (built in packages.nix) get the tools they shell out
# to on PATH, and SON_OF_ANTON_PYTHON, the interpreter of execute_code and
# cron script jobs. The CLI also learns where its daemon is (SOA_D_BIN).
#
#   pkgs.son-of-anton.override { extraPackages = [ ... ]; }
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
  # Both commands, unwrapped (packages.nix).
  soa-d,
  # Plain nixpkgs python3 (SON_OF_ANTON_PYTHON): execute_code and cron scripts.
  python3,
  # More tools on PATH, such as the language servers an editor config enables.
  extraPackages ? [ ],
}:
let
  tools = [
    bash
    coreutils
    git
    ripgrep
    openssh # git over ssh
    poppler-utils # web_extract reads PDFs with pdftotext
    bubblewrap # sandboxes model-authored code
    ffmpeg # agents convert audio and video in the terminal
  ]
  ++ extraPackages;

  common = lib.escapeShellArgs [
    "--suffix"
    "PATH"
    ":"
    (lib.makeBinPath tools)
    "--set"
    "SON_OF_ANTON_PYTHON"
    "${python3}/bin/python3"
  ];
in
stdenv.mkDerivation {
  pname = "son-of-anton";
  inherit (soa-d) version;

  dontUnpack = true;
  dontBuild = true;
  nativeBuildInputs = [ makeWrapper ];

  # soa-d is already a wrapper (it carries its own minimal PATH), so these wrap
  # a wrapper, with the full tool set on top.
  installPhase = ''
    runHook preInstall
    mkdir -p $out/bin
    makeWrapper ${soa-d}/bin/soa-d $out/bin/soa-d ${common}
    makeWrapper ${soa-d}/bin/son-of-anton $out/bin/son-of-anton ${common} \
      --set SOA_D_BIN $out/bin/soa-d
    runHook postInstall
  '';

  passthru = {
    runtimeDeps = tools;
    inherit python3;
  };

  meta = {
    description = "Always-on agent daemon (Signal, cron, a terminal UI) and its command";
    homepage = "https://github.com/ewtodd/son-of-anton";
    mainProgram = "son-of-anton";
    license = [ lib.licenses.bsd0 lib.licenses.mit ]; # 0BSD; opencode-derived TUI parts MIT (see LICENSE)
    platforms = lib.platforms.linux;
  };
}

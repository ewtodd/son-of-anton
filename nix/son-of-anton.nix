# nix/son-of-anton.nix — Son of Anton Agent package (Python only)
#
# The fork ships no JS surfaces, so this is a single sealed uv2nix venv plus
# a wrapper that exposes the bundled locales.
#
# callPackage auto-wires nixpkgs args; flake inputs are passed explicitly.
# Override the tools on the wrapper's PATH with
#   pkgs.son-of-anton.override { extraPackages = [...]; }
{
  lib,
  stdenv,
  makeWrapper,
  callPackage,
  ripgrep,
  git,
  openssh,
  ffmpeg,
  poppler-utils,
  tirith,
  bubblewrap,
  wl-clipboard,
  xclip,
  libnotify,

  # Flake inputs — passed explicitly by packages.nix and overlays.nix
  uv2nix,
  pyproject-nix,
  pyproject-build-systems,
  # More tools on the wrapper's PATH (and the dev shell's), e.g. the language
  # servers an editor config enables: the agent's LSP uses whatever is here.
  extraPackages ? [ ],
}:
let
  # Filtered Python source — keeps nix/ edits from
  # invalidating the venv derivation. README.md and LICENSE must stay:
  # pyproject.toml references them (readme / license-files).
  pythonSrc = lib.cleanSourceWith {
    src = ../.;
    name = "son-of-anton-python-source";
    filter =
      path: _type:
      let
        relPath = lib.removePrefix (toString ../. + "/") (toString path);
        components = lib.splitString "/" relPath;
        topComponent = if components == [ ] then "" else builtins.head components;
        excludedDirs = [
          "ui-tui"
          "apps"
          "tests"
          "tests-js"
          "nix"
          ".github"
        ];
        excludedFiles = [
          "package.json"
          "package-lock.json"
          "flake.nix"
          "flake.lock"
          "AGENTS.md"
          "CONTRIBUTING.md"
          "SECURITY.md"
        ];
      in
      !(builtins.elem topComponent excludedDirs)
      && !(builtins.elem relPath excludedFiles);
  };

  mkSonOfAntonVenv =
    dependency-groups:
    callPackage ./python.nix {
      inherit uv2nix pyproject-nix pyproject-build-systems pythonSrc dependency-groups;
    };

  sonOfAntonVenv = (mkSonOfAntonVenv [ ]).venv;

  bundledLocales = lib.cleanSourceWith {
    src = ../locales;
    filter = path: _type: !(lib.hasInfix "/__pycache__/" path);
  };

  runtimeDeps = [
    ripgrep
    git
    openssh
    ffmpeg
    # read_file converts PDFs with pdftotext, and its scanned-page warning
    # points at pdftoppm; nothing is pip-installed at runtime.
    poppler-utils
    tirith
    # The physics modes refuse to execute model-authored code without it.
    bubblewrap
    wl-clipboard
    xclip
    # notify-send: an unanswered clarify question nudges the user.
    libnotify
  ]
  ++ extraPackages;

  runtimePath = lib.makeBinPath runtimeDeps;
in
stdenv.mkDerivation (finalAttrs: {
  pname = "son-of-anton";
  version = (fromTOML (builtins.readFile ../pyproject.toml)).project.version;

  dontUnpack = true;
  dontBuild = true;
  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall

    # Symlinks, not copies: these are all store paths already, and the
    # wrapper env vars just hold paths.
    mkdir -p $out/share/son-of-anton $out/bin
    ln -s ${bundledLocales} $out/share/son-of-anton/locales

    makeWrapper ${sonOfAntonVenv}/bin/son-of-anton $out/bin/son-of-anton \
      --suffix PATH : "${runtimePath}" \
      --set SON_OF_ANTON_BUNDLED_LOCALES $out/share/son-of-anton/locales \
      --set-default SON_OF_ANTON_BIN $out/bin/son-of-anton \
      --set SON_OF_ANTON_PYTHON ${sonOfAntonVenv}/bin/python3

    runHook postInstall
  '';

  passthru =
    let
      devPython = (mkSonOfAntonVenv [ "dev" ]).editableVenv;
    in
    {
      inherit sonOfAntonVenv;

      devShellHook = ''
        export SON_OF_ANTON_PYTHON=${devPython}/bin/python3
      '';

      devDeps = runtimeDeps ++ [ devPython ];
    };

  meta = with lib; {
    description = "Always-on agent harness with physics research modes";
    homepage = "https://github.com/ewtodd/son-of-anton";
    mainProgram = "son-of-anton";
    license = licenses.mit;
    platforms = platforms.linux;
  };
})

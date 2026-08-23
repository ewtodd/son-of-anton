# nix/son-of-anton.nix — Son of Anton Agent package (Python only)
#
# The fork ships no JS surfaces, so this is a single sealed uv2nix venv plus
# a wrapper that exposes the bundled skills and plugins.
#
# callPackage auto-wires nixpkgs args; flake inputs are passed explicitly.
# Users override via:
#   pkgs.son-of-anton.override { extraPythonPackages = [...]; }
#   pkgs.son-of-anton.override { extraDependencyGroups = [ "supermemory" ]; }
{
  lib,
  stdenv,
  makeWrapper,
  callPackage,
  python312,
  ripgrep,
  git,
  openssh,
  ffmpeg,
  tirith,

  # linux-only deps
  wl-clipboard,
  xclip,

  # Flake inputs — passed explicitly by packages.nix and overlays.nix
  uv2nix,
  pyproject-nix,
  pyproject-build-systems,
  # Locked git revision of the flake source — embedded so banner.py can
  # check for updates without needing a local .git directory. Null for
  # impure / dirty builds where flakes can't determine a rev.
  rev ? null,
  # Overridable parameters
  extraPythonPackages ? [ ],
  extraDependencyGroups ? [ ],
}:
let
  # Filtered Python source — keeps skills/plugins/nix/JS edits from
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
          "skills"
          "plugins"
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
    extraDependencyGroups:
    callPackage ./python.nix {
      inherit uv2nix pyproject-nix pyproject-build-systems pythonSrc;
      dependency-groups = [ "all" ] ++ extraDependencyGroups;
    };

  sonOfAntonVenv = (mkSonOfAntonVenv extraDependencyGroups).venv;

  # Import bundled skills and plugins. Keeping them out of the Python
  # site-packages keeps import semantics identical to a dev checkout — the
  # loader reads them from SON_OF_ANTON_BUNDLED_SKILLS / _PLUGINS.
  bundledSkills = lib.cleanSourceWith {
    src = ../skills;
    filter = path: _type: !(lib.hasInfix "/__pycache__/" path);
  };

  bundledPlugins = lib.cleanSourceWith {
    src = ../plugins;
    filter = path: _type: !(lib.hasInfix "/__pycache__/" path);
  };

  bundledLocales = lib.cleanSourceWith {
    src = ../locales;
    filter = path: _type: !(lib.hasInfix "/__pycache__/" path);
  };

  runtimeDeps = [
    ripgrep
    git
    openssh
    ffmpeg
    tirith
  ]
  ++ lib.optionals stdenv.isLinux [
    wl-clipboard
    xclip
  ];

  runtimePath = lib.makeBinPath runtimeDeps;

  sitePackagesPath = python312.sitePackages;

  # Walk propagatedBuildInputs to include transitive Python deps in PYTHONPATH.
  # Without this, a plugin listing e.g. requests as a dep would fail at runtime
  # if requests isn't already in the sealed uv2nix venv.
  allExtraPythonPackages = python312.pkgs.requiredPythonModules extraPythonPackages;

  pythonPath = lib.makeSearchPath sitePackagesPath allExtraPythonPackages;
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
    ln -s ${bundledSkills} $out/share/son-of-anton/skills
    ln -s ${bundledPlugins} $out/share/son-of-anton/plugins
    ln -s ${bundledLocales} $out/share/son-of-anton/locales

    makeWrapper ${sonOfAntonVenv}/bin/son-of-anton $out/bin/son-of-anton \
      --suffix PATH : "${runtimePath}" \
      --set SON_OF_ANTON_BUNDLED_SKILLS $out/share/son-of-anton/skills \
      --set SON_OF_ANTON_BUNDLED_PLUGINS $out/share/son-of-anton/plugins \
      --set SON_OF_ANTON_BUNDLED_LOCALES $out/share/son-of-anton/locales \
      --set-default SON_OF_ANTON_BIN $out/bin/son-of-anton \
      --set SON_OF_ANTON_PYTHON ${sonOfAntonVenv}/bin/python3 \
    ${
      lib.optionalString (rev != null)
      "--set SON_OF_ANTON_REVISION ${rev}"
    } \
    ${
      lib.optionalString (extraPythonPackages != [ ])
      "--suffix PYTHONPATH : \"${pythonPath}\""
    }

    runHook postInstall
  '';

  passthru =
    let
      devPython = (mkSonOfAntonVenv (extraDependencyGroups ++ [ "dev" ])).editableVenv;
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
    platforms = platforms.unix;
  };
})

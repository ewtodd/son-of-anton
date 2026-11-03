# What both modules do with a configured instance, apart from systemd:
# render its files from the options and set its state directory up.
#
# The unit-only parts (users, sandbox, tmpfiles) stay in nixosModules.nix; the
# Home Manager module has none of those. Everything here takes plain values, so
# the same code serves a NixOS instance and a Home Manager user.
{ lib }:
let
  # Directories under SON_OF_ANTON_HOME that exist from the first activation.
  subdirs = [
    "cron"
    "sessions"
    "logs"
    "memories"
  ];

  # The package an instance runs. Its extraPackages must reach the wrapped
  # command too, so the person's own `son-of-anton` sees the same tools.
  packageFor =
    cfg:
    if cfg.extraPackages == [ ] then cfg.package else cfg.package.override { inherit (cfg) extraPackages; };

  # ── Files rendered from the options ──────────────────────────────────────
  toml = pkgs: pkgs.formats.toml { };

  configFile =
    pkgs: cfg:
    (toml pkgs).generate "son-of-anton-config.toml" (
      lib.recursiveUpdate { terminal.cwd = cfg.workingDirectory; } cfg.settings
    );

  # What the rules merge reads. With memoryRulesFile only the path string is
  # used, so the file is not read at evaluation and never reaches the store.
  rulesSource =
    pkgs: cfg:
    if cfg.memoryRulesFile != null then
      lib.escapeShellArg cfg.memoryRulesFile
    else
      toString (
        (toml pkgs).generate "son-of-anton-rules.toml" {
          rule = map (lib.filterAttrs (_: v: v != null)) cfg.memoryRules;
        }
      );

  # The non-secret part of .env, as one store file.
  envBase =
    pkgs: cfg:
    pkgs.writeText "son-of-anton-env-base" (
      lib.concatMapStrings (n: "${n}=${cfg.environment.${n}}\n") (lib.attrNames cfg.environment)
    );

  # Writes DEST (mode MODE) from the store file, then appends each secrets file
  # that can be read. Starting over each time keeps a second run from
  # duplicating the secrets.
  envWriter =
    pkgs: cfg:
    pkgs.writeShellScript "son-of-anton-env-merge" ''
      set -eu
      dest=$1 mode=$2
      shift 2
      install -m "$mode" ${envBase pkgs cfg} "$dest"
      for secrets in "$@"; do
        if [ -r "$secrets" ]; then
          { echo; cat "$secrets"; } >> "$dest"
        else
          echo "son-of-anton: skipping $secrets in .env: it cannot be read" >&2
        fi
      done
    '';

  # config.toml and .env are read once, at process start. These are what a
  # unit restarts on: a changed value gives a new store path. (Secrets live at
  # run-time paths Nix cannot see; rotating one means restarting by hand.)
  restartOn = pkgs: cfg: [
    (configFile pkgs cfg)
    (envWriter pkgs cfg)
  ];

  # ── Activation ───────────────────────────────────────────────────────────
  # The state setup of one instance, as shell text for system.activationScripts
  # or home.activation.
  #   dry     command prefix; Home Manager passes "$DRY_RUN_CMD "
  #   owner   null, or { user; group } that must own what is written
  #   modes   { config; env; marker } file modes
  #   system  what the .managed marker says owns the install
  setupScript =
    {
      pkgs,
      cfg,
      home,
      dry ? "",
      owner ? null,
      modes,
      system,
    }:
    let
      q = lib.escapeShellArg;
      soad = "${packageFor cfg}/bin/soa-d";
      at = rel: q "${home}/${rel}";

      # Shell lines that give each path its mode and, if there is one, its owner.
      settleLines =
        mode: paths:
        map (p: "${dry}chmod ${mode} ${p}") paths
        ++ lib.optional (owner != null) "${dry}chown ${owner.user}:${owner.group} ${lib.concatStringsSep " " paths}";
      settle = mode: paths: lib.concatStringsSep "\n" (settleLines mode paths);

      installAs = lib.optionalString (owner != null) "-o ${q owner.user} -g ${q owner.group} ";
      secrets = cfg.environment != { } || cfg.environmentFiles != [ ];
    in
    ''
      # Directories, made now so that they start with the right owner and mode.
      ${dry}mkdir -p ${
        lib.concatStringsSep " " (
          [
            (q home)
            (q cfg.workingDirectory)
          ]
          ++ map at subdirs
        )
      }

      # config.toml. The program rewrites it at run time, so a link into the
      # store would break its saves; the declared keys are merged into the
      # real file instead. --state keeps what was written, which is how a key
      # Nix stops declaring can be told apart from one the program added.
      # Whatever activation creates is root's, and the service could not read
      # a root-owned config.toml, so owner and mode are set explicitly.
      ${dry}${soad} config merge ${configFile pkgs cfg} ${at "config.toml"} --state ${at ".nix-managed.json"}${lib.optionalString cfg.pruneUnmanagedSettings " --adopt"}
      ${settle modes.config [
        (at "config.toml")
        (at ".nix-managed.json")
      ]}
      # memories/rules.toml: seeded and then left editable; the state file
      # records what Nix wrote so only an untouched rule is ever retired.
      ${dry}${soad} rules merge ${rulesSource pkgs cfg} ${at "memories/rules.toml"} --state ${at "memories/.nix-rules.json"}
      for rulefile in ${at "memories/rules.toml"} ${at "memories/.nix-rules.json"}; do
        if [ -e "$rulefile" ]; then
          ${lib.concatStringsSep "\n    " (settleLines modes.config [ ''"$rulefile"'' ])}
        fi
      done

      # The marker that tells an interactive shell this configuration is Nix's.
      ${dry}install ${installAs}-m ${modes.marker} ${pkgs.writeText "son-of-anton-managed" system} ${at ".managed"}
      ${lib.optionalString secrets ''
        ${dry}${envWriter pkgs cfg} ${at ".env"} ${modes.env} ${lib.escapeShellArgs cfg.environmentFiles}
        ${lib.optionalString (owner != null) "${dry}chown ${owner.user}:${owner.group} ${at ".env"}"}
      ''}
    '';

  # ── The running process ──────────────────────────────────────────────────
  argv = cfg: [
    "${packageFor cfg}/bin/soa-d"
    "serve"
  ];

  # Defaults for [daemon], placed first in the merge so that an explicit
  # settings.daemon.<key> replaces just that key.
  daemonDefaults =
    extra:
    lib.mkBefore {
      daemon = {
        signal = true;
        cron = true;
        # The unit serves this home; a TUI attaches to it and never starts one.
        autostart = false;
        # The first start brings in the old Python state.db and retires it.
        import_python = true;
      }
      // extra;
    };

  runtimeEnv = home: {
    SON_OF_ANTON_HOME = home;
    SON_OF_ANTON_MANAGED = "true";
  };

  servicePath =
    pkgs: cfg:
    [
      (packageFor cfg)
      pkgs.bash
      pkgs.coreutils
      pkgs.git
    ]
    ++ cfg.extraPackages;
in
{
  inherit
    argv
    daemonDefaults
    packageFor
    restartOn
    runtimeEnv
    servicePath
    setupScript
    subdirs
    ;
}

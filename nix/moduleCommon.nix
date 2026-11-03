# nix/moduleCommon.nix — the code that the NixOS and Home Manager modules share
#
# `services.son-of-anton` is the same option set on both modules. Both modules
# get their options, their renderers for config.toml and .env, and their state
# setup from this file. An option added here appears on both modules at once.
#
# Each module keeps only the parts that belong to its own scope:
#
#   nixosModules.nix        the service user and group, stateDir,
#                           addToSystemPackages, tmpfiles,
#                           system.activationScripts, system systemd units
#   homeManagerModules.nix  son-of-antonHome, installPackage, home.activation
#
# The split is by scope, not by feature. Code that needs root or a system
# identity stays in the NixOS module. All other code is here.
{ lib }:

let
  inherit (lib)
    literalExpression
    mkOption
    types
    ;

  # ── Configuration type ──────────────────────────────────────────────────
  # More than one module can set `settings = { ... }`. recursiveUpdate joins
  # all of the definitions. Without it, only the last definition applies.
  deepConfigType = types.mkOptionType {
    name = "son-of-anton-config-attrs";
    description = "Son of Anton TOML config (attrset), merged deeply via lib.recursiveUpdate.";
    check = builtins.isAttrs;
    merge = _loc: defs: lib.foldl' lib.recursiveUpdate { } (map (d: d.value) defs);
  };

  # ── The options that both modules share ─────────────────────────────────
  # `defaultPackage` and `defaultWorkingDirectory` are different on each
  # module, so the caller gives them. All other options are the same.
  sharedOptions =
    {
      defaultPackage,
      defaultPackageText,
      defaultWorkingDirectory,
      defaultWorkingDirectoryText,
    }:
    {
      enable = lib.mkEnableOption "Son of Anton Agent";

      # ── Package ────────────────────────────────────────────────────────
      package = mkOption {
        type = types.package;
        default = defaultPackage;
        defaultText = defaultPackageText;
        description = "The son-of-anton package to use.";
      };

      workingDirectory = mkOption {
        type = types.str;
        default = defaultWorkingDirectory;
        defaultText = defaultWorkingDirectoryText;
        description = ''
          The working directory for the agent. The module also writes this
          path to config.toml as `terminal.cwd`. The terminal and file tools
          of the agent use that value.
        '';
      };

      backend = mkOption {
        type = types.enum [
          "python"
          "soa-d"
        ];
        default = "python";
        description = ''
          What serves the instance. `python` runs the Python gateway
          (`son-of-anton gateway`). `soa-d` runs the Go daemon (`soa-d serve`:
          Signal, cron and the socket the TUI attaches to) in the same unit, so
          the Python gateway and soa-d never both serve one instance. The
          module also seeds `settings.daemon` for `soa-d`
          (signal and cron on, plus `instance` on NixOS); an explicit
          `settings.daemon.*` wins key by key. Bring the Python sessions over
          once with `soa-d import --from ~/.son-of-anton`.
        '';
      };

      # ── Declarative config ─────────────────────────────────────────────
      settings = mkOption {
        type = deepConfigType;
        default = { };
        description = ''
          The Son of Anton configuration, as an attribute set. The module joins the
          definitions from all modules and writes the result to config.toml.

          The merge into the config.toml on disk is a three-way merge
          against `.nix-managed.json`, which records what the previous
          activation wrote. These keys replace the keys on disk; a key this
          option USED to declare and no longer does is removed from disk,
          unless its value changed after Nix wrote it. Every key Nix has never
          declared is kept.

          The first activation after this state file is introduced cannot
          attribute anything to Nix, so it removes nothing and instead reports
          the undeclared keys it found. See nix/config_merge.py (--adopt).
        '';
        example = literalExpression ''
          {
            model.default = "auto";
            compaction = { enabled = true; threshold = 0.85; };
          }
        '';
      };

      memoryRules = mkOption {
        type = types.listOf (
          types.submodule {
            options = {
              id = mkOption {
                type = types.strMatching "[a-z0-9][a-z0-9-]*";
                description = "Stable id. The agent and the user address the rule by it.";
              };
              text = mkOption {
                type = types.str;
                description = "The rule, phrased as an instruction.";
              };
              why = mkOption {
                type = types.nullOr types.str;
                default = null;
                description = "Why the rule exists (shown beside it in the prompt).";
              };
              scope = mkOption {
                type = types.enum [
                  "common"
                  "gateway"
                ];
                default = "common";
                description = "`common` is in every session; `gateway` only in Signal sessions.";
              };
              source = mkOption {
                type = types.enum [
                  "user"
                  "agent"
                ];
                default = "user";
                description = "Who the rule is attributed to.";
              };
            };
          }
        );
        default = [ ];
        description = ''
          Standing rules seeded into `memories/rules.toml`. Nix CREATES the file;
          it then stays mutable (the agent adds rules with the user's approval
          and the user edits it by hand). Activation merges these in three-way
          by id against `memories/.nix-rules.json`, which records what the
          previous activation wrote: a declared rule that is missing is added,
          one whose on-disk copy is still what Nix wrote follows the declared
          value, and one that was changed on disk since is kept and reported.
          A rule no longer declared is removed only if it is still what Nix
          wrote. Rules Nix never declared are untouched; nothing is removed on
          the first run. See nix/rules_merge.py.
        '';
        example = literalExpression ''
          [
            {
              id = "never-delete-proton-mail";
              text = "Never delete Proton mail.";
              why = "Mail is not recoverable.";
            }
          ]
        '';
      };

      memoryRulesFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "/run/agenix/son-of-anton-rules";
        description = ''
          Runtime path of a TOML file of `[[rule]]` tables (id, text, optional
          why, scope `common`|`gateway` default `common`, source `user`|`agent`
          default `user`), merged into `memories/rules.toml` exactly like
          `memoryRules`. For rules that must not go in a public repo or the Nix
          store, e.g. an agenix or sops secret: only this path string is in the
          activation script, never the contents. Mutually exclusive with
          `memoryRules`. The merge validates the file itself; if it is missing,
          unreadable, unparseable or has an invalid entry, activation warns and
          changes nothing (rules.toml and its state file stay as they are).
        '';
      };

      pruneUnmanagedSettings = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Treat `settings` as the whole truth for config.toml: on every
          activation, remove any key on disk that Nix does not declare.

          Off by default, because the keys Nix does not declare are the ones
          runtime writers (/skin, /perm) add, and a rebuild silently
          discarding them is worse than a stale key. The ordinary three-way
          merge already retracts anything Nix declared and has since dropped;
          this option only additionally claims the keys Nix has never
          declared at all.
        '';
      };

      # ── Secrets / environment ──────────────────────────────────────────
      environmentFiles = mkOption {
        # The type is `str` and not `path` for a reason. A Nix path literal
        # copies the secret into the Nix store, which all users can read. Use
        # a runtime path from sops-nix or agenix instead, for example
        # `config.sops.secrets."x".path`.
        type = types.listOf types.str;
        default = [ ];
        description = ''
          The paths to environment files that contain secrets, for example
          API keys and tokens. Activation adds the contents of these files to
          $SON_OF_ANTON_HOME/.env. Son of Anton reads that file at each start, with
          load_son_of_anton_dotenv().

          Each activation writes .env again from the start. Thus a secret
          file cannot go into .env two times.
        '';
        example = literalExpression ''[ config.sops.secrets."son-of-anton/env".path ]'';
      };

      environment = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = ''
          Environment variables that are not secret. Activation writes them
          to $SON_OF_ANTON_HOME/.env.

          CAUTION: Do not put secrets in this option. All users can read the
          Nix store. Use environmentFiles for secrets.
        '';
      };

      # ── Packages ───────────────────────────────────────────────────────
      extraPackages = mkOption {
        type = types.listOf types.package;
        default = [ ];
        description = "More packages on the PATH of the agent. The agent can run these tools.";
      };
    };

  # ── Package resolution ──────────────────────────────────────────────────
  # extraPackages go into the wrapper too, not just the service PATH, so the
  # instance's `son-of-anton` CLI sees the same tools (language servers…).
  effectivePackage =
    cfg:
    if cfg.extraPackages == [ ] then
      cfg.package
    else
      cfg.package.override {
        inherit (cfg) extraPackages;
      };

  # ── The rendered config.toml ────────────────────────────────────────────
  # TOML is not a JSON superset, so the settings are rendered through
  # nixpkgs' TOML generator. The order of the recursiveUpdate lets an explicit
  # settings.terminal.cwd replace the default value.
  mkConfigFiles =
    {
      pkgs,
      cfg,
      workingDirectory,
    }:
    {
      generated = (pkgs.formats.toml { }).generate "son-of-anton-config.toml" (
        lib.recursiveUpdate { terminal.cwd = workingDirectory; } cfg.settings
      );
      mergeScript = pkgs.callPackage ./configMergeScript.nix { };
      # The declared input of the rules merge: a runtime path when
      # memoryRulesFile is set (the path string only; the file is never read
      # at evaluation), else the store file generated from memoryRules.
      rulesDeclared =
        if cfg.memoryRulesFile != null then
          lib.escapeShellArg cfg.memoryRulesFile
        else
          "${(pkgs.formats.toml { }).generate "son-of-anton-rules.toml" {
            rule = map (lib.filterAttrs (_: v: v != null)) cfg.memoryRules;
          }}";
      rulesMergeScript = pkgs.callPackage ./rulesMergeScript.nix { };
    };

  # ── How .env is built ───────────────────────────────────────────────────
  # The values that are not secret come from the Nix store. Activation adds
  # the secrets from paths outside the store. This is one command, so it is
  # safe in a dry run. A second activation writes .env again and does not add
  # the same secrets a second time.
  mkEnvScript =
    { pkgs, environment }:
    let
      base = pkgs.writeText "son-of-anton-env-base" (
        lib.concatStringsSep "\n" (lib.mapAttrsToList (k: v: "${k}=${v}") environment)
        + lib.optionalString (environment != { }) "\n"
      );
    in
    pkgs.writeShellScript "son-of-anton-env-merge" ''
      set -eu

      dest="$1"
      mode="$2"
      shift 2

      install -m "$mode" ${base} "$dest"
      for file in "$@"; do
        if [ -r "$file" ]; then
          printf '\n' >> "$dest"
          cat "$file" >> "$dest"
        else
          echo "son-of-anton: WARNING cannot read environmentFile $file" >&2
        fi
      done
    '';

  # ── State setup ─────────────────────────────────────────────────────────
  # The activation code that both modules run. It makes the directories and
  # installs config.toml and .env. The differences between the two modules
  # are only the install flags for the owner and the file modes. Thus they are
  # arguments, and not a second copy of the script.
  #
  #   run       the command prefix ("" on NixOS, "$DRY_RUN_CMD " on
  #             Home Manager)
  #   owner     "user:group" that owns each file, or null for the user that
  #             runs the activation
  #   modes     the file mode for each kind of file
  mkStateScript =
    {
      pkgs,
      cfg,
      son-of-antonHome,
      workingDirectory,
      run ? "",
      owner ? null,
      modes,
      stateDirs ? [ ],
      # The module writes this value into the .managed marker. An
      # interactive shell reads the marker, because it does not see the
      # SON_OF_ANTON_MANAGED variable of the service. The value tells the shell
      # which system owns the install.
      managedSystem ? "nixos",
    }:
    let
      installFlags = lib.optionalString (owner != null) (
        let
          parts = lib.splitString ":" owner;
        in
        "-o ${lib.head parts} -g ${lib.last parts}"
      );
      configFiles = mkConfigFiles { inherit pkgs cfg workingDirectory; };
      envScript = mkEnvScript {
        inherit pkgs;
        inherit (cfg) environment;
      };

      inst = "${run}install ${installFlags}";
    in
    ''
      # Directories. The service units and Son of Anton make most of these
      # directories when they first need them. Activation makes them here so
      # that the first activation sets the correct owner and mode, and does
      # not use the umask.
      ${run}mkdir -p ${
        lib.escapeShellArgs (
          [
            son-of-antonHome
            workingDirectory
          ]
          ++ map (d: "${son-of-antonHome}/${d}") stateDirs
        )
      }

      # config.toml: merge the Nix settings into the file on disk. Son of Anton
      # writes this file at runtime. A read-only symlink to the Nix store
      # breaks each save from the application. The Nix keys replace the keys
      # on disk, and the module keeps all other keys.
      #
      # Activation runs as root, so anything it CREATES is root-owned. The
      # merge only inherited the right owner by rewriting a config.toml that
      # already had it — which silently stopped being true the moment the file
      # had to be created, and was never true for the state file, which is new
      # every install. A 0600 root:root config.toml is unreadable by the very
      # service that needs it. Hence an explicit chown, like .env has.
      #
      # --state is what makes retiring a key work. The merge records its own
      # output there each run, so the next run can tell a key Nix has dropped
      # from a key something wrote at runtime, and remove the first without
      # touching the second. Without it the merge can only ever add and
      # overwrite, and a `nixos-rebuild` that drops a setting leaves it live on
      # disk with nothing reporting that it did.
      ${run}${configFiles.mergeScript} ${configFiles.generated} ${son-of-antonHome}/config.toml --state ${son-of-antonHome}/.nix-managed.json${lib.optionalString cfg.pruneUnmanagedSettings " --adopt"}
      ${run}chmod ${modes.config} ${son-of-antonHome}/config.toml
      ${run}chmod ${modes.config} ${son-of-antonHome}/.nix-managed.json
      ${lib.optionalString (
        owner != null
      ) "${run}chown ${owner} ${son-of-antonHome}/config.toml ${son-of-antonHome}/.nix-managed.json"}

      # memories/rules.toml: seed the declared standing rules. Same idea as the
      # config merge: Nix creates the file, it stays mutable, and the state
      # file next to it records what Nix wrote so a rule retired in Nix goes
      # away only if nobody has touched it since. Created files get the
      # state-file mode and owner like the other files here.
      ${run}${configFiles.rulesMergeScript} ${configFiles.rulesDeclared} ${son-of-antonHome}/memories/rules.toml --state ${son-of-antonHome}/memories/.nix-rules.json
      for _f in ${son-of-antonHome}/memories/rules.toml ${son-of-antonHome}/memories/.nix-rules.json; do
        if [ -e "$_f" ]; then
          ${run}chmod ${modes.config} "$_f"
          ${lib.optionalString (owner != null) "${run}chown ${owner} \"$_f\""}
        fi
      done

      # The managed-mode marker. It makes an interactive shell also refuse to
      # change the configuration that Nix owns.
      ${inst} -m ${modes.managed} ${pkgs.writeText "son-of-anton-managed" managedSystem} ${son-of-antonHome}/.managed

      ${lib.optionalString (cfg.environment != { } || cfg.environmentFiles != [ ]) ''
        ${run}${envScript} ${son-of-antonHome}/.env ${modes.env} ${lib.escapeShellArgs cfg.environmentFiles}
        ${lib.optionalString (owner != null) "${run}chown ${owner} ${son-of-antonHome}/.env"}
      ''}
    '';

  # ── Process argv ────────────────────────────────────────────────────────
  gatewayArgv =
    cfg:
    if cfg.backend == "soa-d" then
      [
        "${effectivePackage cfg}/bin/soa-d"
        "serve"
      ]
    else
      [
        "${effectivePackage cfg}/bin/son-of-anton"
        "gateway"
      ];

  # The `daemon` settings that `backend = "soa-d"` seeds. mkBefore puts this
  # definition first in the settings merge (recursiveUpdate, later wins), so
  # an explicit `settings.daemon.*` replaces a key without dropping the rest,
  # which mkDefault would not do once any other setting exists.
  soadSettings =
    cfg: extra:
    lib.mkIf (cfg.backend == "soa-d") (
      lib.mkBefore {
        daemon = {
          signal = true;
          cron = true;
        }
        // extra;
      }
    );

  # The environment that each Son of Anton process needs.
  #
  # managedSystem gives the value of SON_OF_ANTON_MANAGED. The CLI reads that
  # variable to refuse a configuration change that it cannot keep.
  processEnvironment =
    {
      son-of-antonHome,
      managedSystem ? "true",
    }:
    {
      SON_OF_ANTON_HOME = son-of-antonHome;
      SON_OF_ANTON_MANAGED = managedSystem;
    };

  processPath =
    { pkgs, cfg }:
    [
      (effectivePackage cfg)
      pkgs.bash
      pkgs.coreutils
      pkgs.git
    ]
    ++ cfg.extraPackages;

  # The subdirectories of SON_OF_ANTON_HOME that both modules make.
  stateSubdirs = [
    "cron"
    "sessions"
    "logs"
    "memories"
  ];
in
{
  inherit
    deepConfigType
    effectivePackage
    gatewayArgv
    mkStateScript
    processEnvironment
    processPath
    sharedOptions
    soadSettings
    stateSubdirs
    ;
}

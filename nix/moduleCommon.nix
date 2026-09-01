# nix/moduleCommon.nix — the code that the NixOS and Home Manager modules share
#
# `services.son-of-anton` is the same option set on both modules. Both modules
# get their options, their renderers for config.yaml, .env and documents, and
# their state setup from this file. A NixOS example works on Home Manager
# without a change. An option added here appears on both modules at once.
#
# Each module keeps only the parts that belong to its own scope:
#
#   nixosModules.nix        the service user and group, stateDir,
#                           addToSystemPackages, container mode, tmpfiles,
#                           system.activationScripts, system systemd units
#   homeManagerModules.nix  son-of-antonHome, installPackage, home.activation,
#                           systemd.user.services, launchd.agents
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
    description = "Son of Anton YAML config (attrset), merged deeply via lib.recursiveUpdate.";
    check = builtins.isAttrs;
    merge = _loc: defs: lib.foldl' lib.recursiveUpdate { } (map (d: d.value) defs);
  };

  # ── MCP server submodule ────────────────────────────────────────────────
  mcpServerType = types.submodule {
    options = {
      # Stdio transport
      command = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "MCP server command (stdio transport).";
      };
      args = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Command-line arguments (stdio transport).";
      };
      env = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = "Environment variables for the server process (stdio transport).";
      };

      # HTTP/StreamableHTTP transport
      url = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "MCP server endpoint URL (HTTP/StreamableHTTP transport).";
      };
      headers = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = "HTTP headers, e.g. for authentication (HTTP transport).";
      };

      # Authentication
      auth = mkOption {
        type = types.nullOr (types.enum [ "oauth" ]);
        default = null;
        description = ''
          Authentication method. Set to "oauth" for OAuth 2.1 PKCE flow
          (remote MCP servers). Tokens are stored in $SON_OF_ANTON_HOME/mcp-tokens/.
        '';
      };

      # Enable/disable
      enabled = mkOption {
        type = types.bool;
        default = true;
        description = "Enable or disable this MCP server.";
      };

      # Common options
      timeout = mkOption {
        type = types.nullOr types.int;
        default = null;
        description = "Tool call timeout in seconds (default: 120).";
      };
      connect_timeout = mkOption {
        type = types.nullOr types.int;
        default = null;
        description = "Initial connection timeout in seconds (default: 60).";
      };

      # Tool filtering
      tools = mkOption {
        type = types.nullOr (
          types.submodule {
            options = {
              include = mkOption {
                type = types.listOf types.str;
                default = [ ];
                description = "Tool allowlist — only these tools are registered.";
              };
              exclude = mkOption {
                type = types.listOf types.str;
                default = [ ];
                description = "Tool blocklist — these tools are hidden.";
              };
            };
          }
        );
        default = null;
        description = "Filter which tools are exposed by this server.";
      };

      # Sampling (server-initiated LLM requests)
      sampling = mkOption {
        type = types.nullOr (
          types.submodule {
            options = {
              enabled = mkOption {
                type = types.bool;
                default = true;
                description = "Enable sampling.";
              };
              model = mkOption {
                type = types.nullOr types.str;
                default = null;
                description = "Override model for sampling requests.";
              };
              max_tokens_cap = mkOption {
                type = types.nullOr types.int;
                default = null;
                description = "Max tokens per request.";
              };
              timeout = mkOption {
                type = types.nullOr types.int;
                default = null;
                description = "LLM call timeout in seconds.";
              };
              max_rpm = mkOption {
                type = types.nullOr types.int;
                default = null;
                description = "Max requests per minute.";
              };
              max_tool_rounds = mkOption {
                type = types.nullOr types.int;
                default = null;
                description = "Max tool-use rounds per sampling request.";
              };
              allowed_models = mkOption {
                type = types.listOf types.str;
                default = [ ];
                description = "Models the server is allowed to request.";
              };
              log_level = mkOption {
                type = types.nullOr (
                  types.enum [
                    "debug"
                    "info"
                    "warning"
                  ]
                );
                default = null;
                description = "Audit log level for sampling requests.";
              };
            };
          }
        );
        default = null;
        description = "Sampling configuration for server-initiated LLM requests.";
      };
    };
  };

  # Convert the mcpServers submodules into the shape that config.yaml uses.
  mcpServersToConfig =
    servers:
    lib.mapAttrs (
      _name: srv:
      # Stdio transport
      lib.optionalAttrs (srv.command != null) { inherit (srv) command args; }
      // lib.optionalAttrs (srv.env != { }) { inherit (srv) env; }
      # HTTP transport
      // lib.optionalAttrs (srv.url != null) { inherit (srv) url; }
      // lib.optionalAttrs (srv.headers != { }) { inherit (srv) headers; }
      # Auth
      // lib.optionalAttrs (srv.auth != null) { inherit (srv) auth; }
      # Enable/disable
      // {
        inherit (srv) enabled;
      }
      # Common options
      // lib.optionalAttrs (srv.timeout != null) { inherit (srv) timeout; }
      // lib.optionalAttrs (srv.connect_timeout != null) { inherit (srv) connect_timeout; }
      # Tool filtering
      // lib.optionalAttrs (srv.tools != null) {
        tools = lib.filterAttrs (_: v: v != [ ]) {
          inherit (srv.tools) include exclude;
        };
      }
      # Sampling
      // lib.optionalAttrs (srv.sampling != null) {
        sampling = lib.filterAttrs (_: v: v != null && v != [ ]) {
          inherit (srv.sampling)
            enabled
            model
            max_tokens_cap
            timeout
            max_rpm
            max_tool_rounds
            allowed_models
            log_level
            ;
        };
      }
    ) servers;

  documentsType = types.attrsOf (types.either types.str types.path);

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
          path to config.yaml as `terminal.cwd`. The terminal and file tools
          of the agent use that value.
        '';
      };

      # ── Declarative config ─────────────────────────────────────────────
      configFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = ''
          The path to an existing config.yaml. If you set this option, it
          replaces the `settings` option. The module installs the file
          without a change and overwrites all runtime edits on each
          activation.
        '';
      };

      settings = mkOption {
        type = deepConfigType;
        default = { };
        description = ''
          The Son of Anton configuration, as an attribute set. The module joins the
          definitions from all modules and writes the result to config.yaml.

          The merge into the config.yaml on disk is a three-way merge
          against `.nix-managed.json`, which records what the previous
          activation wrote. These keys replace the keys on disk; a key this
          option USED to declare and no longer does is removed from disk,
          unless its value changed after Nix wrote it. Every key Nix has never
          declared is kept, which is what `son-of-anton config set` and the
          settings panes of the TUI and the desktop app rely on.

          The first activation after this state file is introduced cannot
          attribute anything to Nix, so it removes nothing and instead reports
          the undeclared keys it found. See nix/config_merge.py (--adopt).
        '';
        example = literalExpression ''
          {
            model.default = "anthropic/claude-sonnet-4";
            terminal.backend = "local";
            compression = { enabled = true; threshold = 0.85; };
          }
        '';
      };

      pruneUnmanagedSettings = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Treat `settings` as the whole truth for config.yaml: on every
          activation, remove any key on disk that Nix does not declare.

          Off by default, because the keys Nix does not declare are the ones
          `son-of-anton config set`, the TUI settings panes and the desktop
          app write, and a rebuild silently discarding a user's settings is
          worse than a stale key.

          Two reasons to turn it on. As a one-off, to adopt an install whose
          config.yaml predates `.nix-managed.json`: the first activation after
          that state file appears cannot attribute anything to Nix, so it
          removes nothing and instead lists the undeclared keys it found — set
          this for one rebuild to clear the ones that are leftovers, then unset
          it. Or permanently, if this deployment wants config.yaml to be purely
          declarative and is willing to lose runtime edits on every rebuild.

          Leaving it off does not mean keys can never be removed. The ordinary
          three-way merge already retracts anything Nix declared and has since
          dropped; this option only additionally claims the keys Nix has never
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

      authFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = ''
          The path to a file that gives the first contents of auth.json, the
          OAuth credentials. The module copies the file only when auth.json
          does not exist. Thus a token that Son of Anton refreshes at runtime stays
          after an activation.
        '';
      };

      authFileForceOverwrite = mkOption {
        type = types.bool;
        default = false;
        description = "Always overwrite auth.json from authFile on activation.";
      };

      # ── Documents ──────────────────────────────────────────────────────
      documents = mkOption {
        type = documentsType;
        default = { };
        description = ''
          Workspace files. The module installs them into workingDirectory.
          Each key is a path relative to that directory, and the module makes
          the necessary subdirectories. Each value is a string or a path.

          Use this option for the project context that the agent reads from
          its working directory, for example AGENTS.md, notes and checklists.
          Son of Anton reads SOUL.md and memories/ from SON_OF_ANTON_HOME, so put those
          files in `son-of-antonHomeFiles`.

          If you set this option, you must also set `workingDirectory`. The
          default of that option is different on each module. Thus an unset
          default puts these files in a directory that you did not select.
        '';
        example = literalExpression ''
          {
            "AGENTS.md" = ./AGENTS.md;
            "notes/oncall.md" = "Page #infra before restarting anything.";
          }
        '';
      };

      son-of-antonHomeFiles = mkOption {
        type = documentsType;
        default = { };
        description = ''
          Files that the module installs into SON_OF_ANTON_HOME. Each key is a path
          relative to that directory, and the module makes the necessary
          subdirectories. Each value is a string or a path.

          Son of Anton reads SOUL.md and the memory files from SON_OF_ANTON_HOME and not
          from the working directory. Declare those files here, or Son of Anton
          does not load them.
        '';
        example = literalExpression ''
          {
            "SOUL.md" = "You are a helpful AI assistant.";
            "memories/USER.md" = ./USER.md;
          }
        '';
      };

      # ── MCP Servers ────────────────────────────────────────────────────
      mcpServers = mkOption {
        type = types.attrsOf mcpServerType;
        default = { };
        description = ''
          MCP server configurations (merged into settings.mcp_servers).
          Each server uses either stdio (command/args) or HTTP (url) transport.
        '';
        example = literalExpression ''
          {
            filesystem = {
              command = "npx";
              args = [ "-y" "@modelcontextprotocol/server-filesystem" "/home/user" ];
            };
            remote-api = {
              url = "http://my-server:8080/v0/mcp";
              headers = { Authorization = "Bearer ..."; };
            };
            remote-oauth = {
              url = "https://mcp.example.com/mcp";
              auth = "oauth";
            };
          }
        '';
      };

      # ── Packages / plugins ─────────────────────────────────────────────
      extraPackages = mkOption {
        type = types.listOf types.package;
        default = [ ];
        description = "More packages on the PATH of the agent. The agent can run these tools.";
      };

      extraPlugins = mkOption {
        type = types.listOf types.package;
        default = [ ];
        description = ''
          Directory-based plugin packages to symlink into the son-of-anton plugins
          directory. Each package must contain a plugin.yaml and __init__.py
          at its root. Son of Anton discovers these automatically on startup.
        '';
        example = literalExpression ''
          [
            (pkgs.fetchFromGitHub {
              owner = "stephenschoettler";
              repo = "son-of-anton-lcm";
              name = "son-of-anton-lcm";
              rev = "v0.7.0";
              hash = "sha256-...";
            })
          ]
        '';
      };

      extraPythonPackages = mkOption {
        type = types.listOf types.package;
        default = [ ];
        description = ''
          Python packages to add to PYTHONPATH for entry-point plugin discovery.
          These are pip-packaged plugins that register via the
          son_of_anton_agent.plugins entry-point group. Each package must be built
          with the same Python interpreter as son-of-anton (python312).
        '';
        example = literalExpression ''
          [
            (pkgs.python312Packages.buildPythonPackage {
              pname = "rtk-son-of-anton";
              version = "1.0.0";
              src = pkgs.fetchFromGitHub {
                owner = "ogallotti";
                repo = "rtk-son-of-anton";
                rev = "main";
                hash = "sha256-...";
              };
            })
          ]
        '';
      };

      extraDependencyGroups = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = ''
          Additional pyproject.toml optional-dependency groups to include in
          the sealed Python venv. These are resolved by uv alongside core
          dependencies — no PYTHONPATH patching or collision risk.

          Use this for optional extras already declared in son-of-anton's
          pyproject.toml (e.g. "hindsight", "honcho", "voice").
          Use extraPythonPackages for external packages not in pyproject.toml.
        '';
        example = [ "hindsight" ];
      };

      # ── Service behaviour ──────────────────────────────────────────────
      extraArgs = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Extra command-line arguments for `son-of-anton gateway`.";
      };

      restart = mkOption {
        type = types.str;
        default = "always";
        description = "The systemd Restart= policy. Darwin does not use this option.";
      };

      restartSec = mkOption {
        type = types.int;
        default = 5;
        description = "The systemd RestartSec= value.";
      };
    };

  # ── Package resolution ──────────────────────────────────────────────────
  effectivePackage =
    cfg:
    if cfg.extraPythonPackages == [ ] && cfg.extraDependencyGroups == [ ] then
      cfg.package
    else
      cfg.package.override { inherit (cfg) extraPythonPackages extraDependencyGroups; };

  # ── The rendered config.yaml ────────────────────────────────────────────
  # YAML contains JSON, so the output of toJSON is a correct config.yaml.
  # terminal.cwd replaces the old MESSAGING_CWD environment variable. The
  # order of the recursiveUpdate lets an explicit settings.terminal.cwd
  # replace the default value.
  mkConfigFiles =
    {
      pkgs,
      cfg,
      workingDirectory,
    }:
    let
      generated = pkgs.writeText "son-of-anton-config.yaml" (
        builtins.toJSON (lib.recursiveUpdate { terminal.cwd = workingDirectory; } cfg.settings)
      );
    in
    {
      inherit generated;
      effective = if cfg.configFile != null then cfg.configFile else generated;
      mergeScript = pkgs.callPackage ./configMergeScript.nix { };
    };

  # ── Documents ───────────────────────────────────────────────────────────
  # A key can contain subdirectories. The tree has the same shape, so the
  # install loop can copy each entry with `install -D`.
  mkDocumentTree =
    { pkgs, documents }:
    pkgs.runCommand "son-of-anton-documents" { } (
      ''
        mkdir -p $out
      ''
      + lib.concatStringsSep "\n" (
        lib.mapAttrsToList (
          name: value:
          let
            dir = builtins.dirOf name;
            mkdir = lib.optionalString (dir != ".") "mkdir -p $out/${dir}";
          in
          if builtins.isPath value || lib.isStorePath value then
            "${mkdir}\ncp ${value} $out/${name}"
          else
            "${mkdir}\ncat > $out/${name} <<'SON_OF_ANTON_DOC_EOF'\n${value}\nSON_OF_ANTON_DOC_EOF"
        ) documents
      )
    );

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
  # installs config.yaml, .env, auth.json, the documents and the plugins. The
  # differences between the two modules are only the install flags for the
  # owner and the file modes. Thus they are arguments, and not a second copy
  # of the script.
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
      # The value to write as terminal.cwd. It is different from
      # workingDirectory only in the container mode of NixOS. There the agent
      # sees the directory at its mount point in the container, but
      # activation writes to the path on the host.
      configWorkingDirectory ? workingDirectory,
      run ? "",
      owner ? null,
      modes,
      stateDirs ? [ ],
      # The module writes this value into the .managed marker. An
      # interactive shell reads the marker, because it does not see the
      # SON_OF_ANTON_MANAGED variable of the service. The value tells the shell
      # which system owns the install and which rebuild command to name.
      managedSystem ? "nixos",
    }:
    let
      installFlags = lib.optionalString (owner != null) (
        let
          parts = lib.splitString ":" owner;
        in
        "-o ${lib.head parts} -g ${lib.last parts}"
      );
      configFiles = mkConfigFiles {
        inherit pkgs cfg;
        workingDirectory = configWorkingDirectory;
      };
      envScript = mkEnvScript {
        inherit pkgs;
        inherit (cfg) environment;
      };
      documentTree = mkDocumentTree {
        inherit pkgs;
        inherit (cfg) documents;
      };
      homeDocumentTree = mkDocumentTree {
        inherit pkgs;
        documents = cfg.son-of-antonHomeFiles;
      };

      inst = "${run}install ${installFlags}";

      installDocuments =
        tree: root: docs:
        lib.concatStringsSep "\n" (
          lib.mapAttrsToList (
            name: _value: "${inst} -m ${modes.document} -D ${tree}/${name} ${root}/${name}"
          ) docs
        );
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

      # config.yaml: merge the Nix settings into the file on disk. Son of Anton
      # writes this file at runtime. A read-only symlink to the Nix store
      # breaks each save from the application. The Nix keys replace the keys
      # on disk, and the module keeps all other keys.
      #
      # Activation runs as root, so anything it CREATES is root-owned. The
      # merge only inherited the right owner by rewriting a config.yaml that
      # already had it — which silently stopped being true the moment the file
      # had to be created, and was never true for the state file, which is new
      # every install. A 0600 root:root config.yaml is unreadable by the very
      # service that needs it. Hence an explicit chown, like .env has.
      #
      # --state is what makes retiring a key work. The merge records its own
      # output there each run, so the next run can tell a key Nix has dropped
      # from a key something wrote at runtime, and remove the first without
      # touching the second. Without it the merge can only ever add and
      # overwrite, and a `nixos-rebuild` that drops a setting leaves it live on
      # disk with nothing reporting that it did.
      ${
        if cfg.configFile != null then
          "${inst} -m ${modes.config} -D ${configFiles.effective} ${son-of-antonHome}/config.yaml"
        else
          ''
            ${run}${configFiles.mergeScript} ${configFiles.generated} ${son-of-antonHome}/config.yaml --state ${son-of-antonHome}/.nix-managed.json${lib.optionalString cfg.pruneUnmanagedSettings " --adopt"}
            ${run}chmod ${modes.config} ${son-of-antonHome}/config.yaml
            ${run}chmod ${modes.config} ${son-of-antonHome}/.nix-managed.json
            ${lib.optionalString (
              owner != null
            ) "${run}chown ${owner} ${son-of-antonHome}/config.yaml ${son-of-antonHome}/.nix-managed.json"}
          ''
      }

      # The managed-mode marker. It makes an interactive shell also refuse to
      # change the configuration that Nix owns.
      ${inst} -m ${modes.managed} ${pkgs.writeText "son-of-anton-managed" managedSystem} ${son-of-antonHome}/.managed

      ${lib.optionalString (cfg.environment != { } || cfg.environmentFiles != [ ]) ''
        ${run}${envScript} ${son-of-antonHome}/.env ${modes.env} ${lib.escapeShellArgs cfg.environmentFiles}
        ${lib.optionalString (owner != null) "${run}chown ${owner} ${son-of-antonHome}/.env"}
      ''}

      ${lib.optionalString (cfg.authFile != null) (
        if cfg.authFileForceOverwrite then
          "${inst} -m ${modes.auth} ${cfg.authFile} ${son-of-antonHome}/auth.json"
        else
          ''
            if [ ! -e ${son-of-antonHome}/auth.json ]; then
              ${inst} -m ${modes.auth} ${cfg.authFile} ${son-of-antonHome}/auth.json
            fi
          ''
      )}

      ${installDocuments documentTree workingDirectory cfg.documents}
      ${installDocuments homeDocumentTree son-of-antonHome cfg.son-of-antonHomeFiles}

      # Declarative plugins. Activation first deletes the old managed
      # symlinks. Thus a plugin that you remove from the configuration also
      # goes away from the plugins directory.
      ${run}find ${son-of-antonHome}/plugins -maxdepth 1 -type l -name 'nix-managed-*' -delete 2>/dev/null || true
      ${lib.concatMapStringsSep "\n" (plugin: ''
        if [ ! -f ${plugin}/plugin.yaml ]; then
          echo "son-of-anton: ERROR extraPlugins entry '${plugin}' has no plugin.yaml" >&2
          exit 1
        fi
        ${run}ln -sfn ${plugin} ${son-of-antonHome}/plugins/nix-managed-${lib.getName plugin}
      '') cfg.extraPlugins}
    '';

  # ── Process argv ────────────────────────────────────────────────────────
  gatewayArgv =
    cfg:
    [
      "${effectivePackage cfg}/bin/son-of-anton"
      "gateway"
    ]
    ++ cfg.extraArgs;

  # The environment that each Son of Anton process needs, from either module.
  #
  # managedSystem gives the value of SON_OF_ANTON_MANAGED. The CLI reads that
  # variable to refuse a configuration change that it cannot keep, and to
  # name the correct rebuild command. The answer is different on each module,
  # so each module gives its own value.
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

  # workingDirectory has a default on both modules, but a bad one. It is the
  # home directory of the user on Home Manager, and ${stateDir}/workspace on
  # NixOS. A user who declares files without a directory therefore gets a
  # place that the user did not select. The place is also different on each
  # module. The modules refuse that combination.
  #
  # The test is on the priority of the option and not on its value. An option
  # that nothing sets keeps the priority of its own default, and each
  # definition from a user is stronger. Thus a directory that has the same
  # text as the default is still a selection, and so is a mkDefault. A
  # comparison of values detects neither.
  workspaceFilesAssertions =
    {
      cfg,
      opt,
      optionPath,
    }:
    let
      untouched = (lib.mkOptionDefault null).priority; # 1500, derived not spelled
    in
    [
      {
        assertion = cfg.documents == { } || opt.highestPrio < untouched;
        message = ''
          ${optionPath}.documents needs an explicit ${optionPath}.workingDirectory.

          The files go into workingDirectory. The default of that option is
          different on each module, so an unset default puts the files in a
          directory that you did not select. Set the directory:

            ${optionPath}.workingDirectory = "/path/you/want";

          To give Son of Anton an identity and a memory, use
          ${optionPath}.son-of-antonHomeFiles instead. Those files go to
          SON_OF_ANTON_HOME. Son of Anton reads SOUL.md and memories/ only from there.
        '';
      }
    ];

  # Two plugins with the same name use one nix-managed-<name> symlink. One of
  # the plugins then disappears without a message. Both modules assert
  # against this condition.
  pluginNameAssertions =
    { cfg, optionPath }:
    let
      names = map lib.getName cfg.extraPlugins;
    in
    [
      {
        assertion = (lib.length names) == (lib.length (lib.unique names));
        message = "${optionPath}.extraPlugins: duplicate plugin names detected: ${toString names}. If using fetchFromGitHub, set name = \"plugin-name\" to disambiguate.";
      }
    ];

  # The subdirectories of SON_OF_ANTON_HOME that both modules make.
  stateSubdirs = [
    "cron"
    "sessions"
    "logs"
    "memories"
    "plugins"
  ];
in
{
  inherit
    deepConfigType
    effectivePackage
    gatewayArgv
    mcpServerType
    mcpServersToConfig
    mkConfigFiles
    mkDocumentTree
    mkEnvScript
    mkStateScript
    pluginNameAssertions
    processEnvironment
    processPath
    sharedOptions
    stateSubdirs
    workspaceFilesAssertions
    ;
}

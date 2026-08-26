# nix/nixosModules.nix — the NixOS module for son-of-anton
#
# This module shares its options, its renderers for config.yaml, .env and
# documents, and its state setup with the Home Manager module
# (nix/homeManagerModules.nix). The shared code is in nix/moduleCommon.nix.
# This file holds only the parts that need root: service users, state
# directories, and the systemd services.
#
# MULTI-INSTANCE. Each entry under `instances` becomes its own systemd system
# service with its own user and its own SON_OF_ANTON_HOME. That is the
# one-agent-per-account shape: a service can run AS a real login account, so
# its Signal sessions and that account's own CLI sessions share one state.db.
# System services (not Home Manager user services) because they must start at
# boot without a login and without lingering.
#
# Usage:
#   services.son-of-anton.instances.work = {
#     enable = true;
#     user = "e-work";
#     createUser = false;
#     managedAccount = true;                 # runs as a pre-existing human account
#     son-of-antonHome = "/home/e-work/.son-of-anton";
#     workingDirectory = "/home/e-work";
#     environmentFiles = [ config.age.secrets."son-of-anton-env".path ];
#   };
#
{ inputs, ... }:
{
  flake.nixosModules.default =
    {
      config,
      lib,
      options,
      pkgs,
      ...
    }:

    let
      outerConfig = config;
      cfg = config.services.son-of-anton;
      common = import ./moduleCommon.nix { inherit lib; };

      son-of-anton = inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.default;

      enabledInstances = lib.filterAttrs (_: inst: inst.enable) cfg.instances;

      # ── One instance's option set ─────────────────────────────────────────
      instanceModule =
        {
          name,
          config,
          options,
          ...
        }:
        {
          options =
            common.sharedOptions {
              defaultPackage = son-of-anton;
              defaultPackageText = lib.literalExpression "son-of-anton.packages.\${system}.default";
              # Derived from `name`, NOT from config.stateDir: an option
              # default that reads its own submodule's config is an infinite
              # recursion (the config cannot resolve before the options it is
              # defined by). Instances that relocate state set these
              # explicitly, and keeping the default config-free preserves the
              # `highestPrio` check workspaceFilesAssertions relies on.
              defaultWorkingDirectory = "/var/lib/son-of-anton-${name}/workspace";
              defaultWorkingDirectoryText = lib.literalExpression ''"/var/lib/son-of-anton-''${name}/workspace"'';
            }
            // (
              with lib;
              {
                # ── Service identity ───────────────────────────────────────
                user = mkOption {
                  type = types.str;
                  default = "son-of-anton-${name}";
                  defaultText = lib.literalExpression ''"son-of-anton-''${name}"'';
                  description = "System user running this gateway instance.";
                };

                group = mkOption {
                  type = types.str;
                  default = "son-of-anton";
                  description = "Primary group for this gateway instance.";
                };

                createUser = mkOption {
                  type = types.bool;
                  default = true;
                  description = ''
                    Create the user/group automatically. Set false when the
                    instance runs as a pre-existing login account.
                  '';
                };

                managedAccount = mkOption {
                  type = types.bool;
                  default = false;
                  description = ''
                    The instance runs as a pre-existing HUMAN login account.

                    This is not cosmetic. When false the module treats the
                    state directory as its own and makes it setgid +
                    group-writable (2770) so a shared service group can reach
                    it. Applied to a real home that would (a) make sshd's
                    StrictModes reject ~/.ssh/authorized_keys and (b) hand
                    every other member of the account's primary group write
                    access to the whole home.

                    When true the module touches ONLY son-of-antonHome and its
                    subdirectories, never the parent home and never
                    workingDirectory; state is owner-only (0700/0600) and the
                    unit runs with UMask 0077.
                  '';
                };

                # ── Directories ────────────────────────────────────────────
                stateDir = mkOption {
                  type = types.str;
                  default = "/var/lib/son-of-anton-${name}";
                  defaultText = lib.literalExpression ''"/var/lib/son-of-anton-''${name}"'';
                  description = ''
                    State directory owned by this instance. Used as HOME for
                    the unit. Ignored for directory creation when
                    managedAccount is set.
                  '';
                };

                son-of-antonHome = mkOption {
                  type = types.str;
                  default = "/var/lib/son-of-anton-${name}/.son-of-anton";
                  defaultText = lib.literalExpression ''"/var/lib/son-of-anton-''${name}/.son-of-anton"'';
                  description = ''
                    SON_OF_ANTON_HOME for this instance: config, skills,
                    memory, sessions (state.db). Point it at a login account's
                    ~/.son-of-anton to share sessions with that account's CLI.
                  '';
                };

                addToSystemPackages = mkOption {
                  type = types.bool;
                  default = false;
                  description = ''
                    Add the son-of-anton CLI to environment.systemPackages and
                    export SON_OF_ANTON_HOME system-wide. At most one instance
                    may set this: environment.variables is global, so a second
                    would silently point every interactive shell at the wrong
                    home.
                  '';
                };

                # Assertions are built inside the submodule because
                # workspaceFilesAssertions inspects option METADATA
                # (options.workingDirectory.highestPrio), which has no
                # addressable path under attrsOf submodule. Lifted to the
                # top level by the config block below.
                _assertions = mkOption {
                  type = types.listOf types.attrs;
                  internal = true;
                  default = [ ];
                };
              }
            );

          config = {
            settings = lib.mkIf (config.mcpServers != { }) {
              mcp_servers = common.mcpServersToConfig config.mcpServers;
            };

            _assertions =
              common.pluginNameAssertions {
                cfg = config;
                optionPath = "services.son-of-anton.instances.${name}";
              }
              ++ common.workspaceFilesAssertions {
                cfg = config;
                opt = options.workingDirectory;
                optionPath = "services.son-of-anton.instances.${name}";
              };
          };
        };

      # ── Per-instance renderers ────────────────────────────────────────────

      # config.yaml mode: group-writable (0660) when interactive users share
      # this SON_OF_ANTON_HOME via addToSystemPackages. A managedAccount owns
      # its home outright, so everything stays owner-only.
      configYamlMode =
        inst:
        if inst.managedAccount then
          "0600"
        else if inst.addToSystemPackages then
          "0660"
        else
          "0640";

      envMode = inst: if inst.managedAccount then "0600" else "0640";

      dirMode = inst: if inst.managedAccount then "0700" else "2770";

      serviceConfigFor = inst: {
        User = inst.user;
        Group = inst.group;
        WorkingDirectory = inst.workingDirectory;

        Restart = inst.restart;
        RestartSec = inst.restartSec;

        # Shared-state deployments want group-writable files so interactive
        # users in the service group can reach them. A managedAccount is the
        # sole owner of its home, so its files stay private.
        UMask = if inst.managedAccount then "0077" else "0007";

        # Hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = false;
        ReadWritePaths = lib.unique [
          inst.stateDir
          inst.son-of-antonHome
          inst.workingDirectory
        ];
        PrivateTmp = true;
      };

      unitEnvironmentFor = inst: {
        HOME = if inst.managedAccount then inst.workingDirectory else inst.stateDir;

        # REQUIRED for multi-instance on one Signal account.
        #
        # gateway/platforms/signal.py acquires a scoped lock keyed on
        # sha256(SIGNAL_ACCOUNT) — it exists precisely to stop two gateways
        # listening to one Signal number, and connect() returns False when it
        # cannot take it, so the losing instance never starts Signal at all.
        # Here we deliberately DO run several gateways on one account and
        # separate them by group id instead, so each needs its own lock
        # namespace. gateway/status.py:_get_lock_dir falls back to
        # $XDG_STATE_HOME (i.e. $HOME/.local/state), which is only accidentally
        # distinct and is not guaranteed writable under ProtectSystem=strict.
        # Pin it inside SON_OF_ANTON_HOME, which is always in ReadWritePaths.
        SON_OF_ANTON_GATEWAY_LOCK_DIR = "${inst.son-of-antonHome}/locks";
      }
      // common.processEnvironment { son-of-antonHome = inst.son-of-antonHome; };

    in
    {
      options.services.son-of-anton.instances = lib.mkOption {
        type = lib.types.attrsOf (lib.types.submodule instanceModule);
        default = { };
        description = ''
          Gateway instances. Each becomes systemd.services.son-of-anton-<name>
          with its own user and its own SON_OF_ANTON_HOME.
        '';
      };

      # Top-level keys are STATIC. Building `config` as
      # `mkMerge (mapAttrsToList ...)` makes the SHAPE of config depend on
      # config values, and the module system cannot resolve the option set
      # (it needs _module.freeformType) before that shape is known — an
      # infinite recursion. Keying each attribute individually keeps the
      # structure fixed and lets only the VALUES depend on instances.
      config = {
        assertions =
          lib.concatMap (inst: inst._assertions) (lib.attrValues enabledInstances)
          ++ (
            let
              users = lib.mapAttrsToList (_: i: i.user) enabledInstances;
              homes = lib.mapAttrsToList (_: i: i.son-of-antonHome) enabledInstances;
              exporters = lib.attrNames (lib.filterAttrs (_: i: i.addToSystemPackages) enabledInstances);
            in
            [
              {
                assertion = (lib.length users) == (lib.length (lib.unique users));
                message = ''
                  Two son-of-anton instances share a Unix user.

                  users.users.<name>.home would then get conflicting
                  definitions, and both gateways would write each other's
                  state. Give each instance its own `user`.
                '';
              }
              {
                assertion = (lib.length homes) == (lib.length (lib.unique homes));
                message = ''
                  Two son-of-anton instances share a SON_OF_ANTON_HOME.

                  They would write the same state.db, the same session routing
                  index and the same .env. Give each instance its own
                  `son-of-antonHome`.
                '';
              }
              {
                assertion = (lib.length exporters) <= 1;
                message = ''
                  More than one son-of-anton instance sets addToSystemPackages
                  (${lib.concatStringsSep ", " exporters}).

                  It exports SON_OF_ANTON_HOME through the global
                  environment.variables, so only one instance can own it.
                '';
              }
            ]
          );

        users.groups = lib.listToAttrs (
          map (inst: lib.nameValuePair inst.group { }) (
            lib.attrValues (lib.filterAttrs (_: i: i.createUser) enabledInstances)
          )
        );

        users.users = lib.listToAttrs (
          lib.mapAttrsToList (
            _: inst:
            lib.nameValuePair inst.user (
              (lib.optionalAttrs inst.createUser {
                isSystemUser = true;
                group = inst.group;
                home = inst.stateDir;
                createHome = true;
                shell = pkgs.bashInteractive;
              })
              // (lib.optionalAttrs (inst.extraPackages != [ ]) {
                packages = inst.extraPackages;
              })
            )
          ) (lib.filterAttrs (_: i: i.createUser || i.extraPackages != [ ]) enabledInstances)
        );

        environment.systemPackages = lib.concatMap (inst: [ (common.effectivePackage inst) ]) (
          lib.attrValues (lib.filterAttrs (_: i: i.addToSystemPackages) enabledInstances)
        );

        environment.variables = lib.listToAttrs (
          map (inst: lib.nameValuePair "SON_OF_ANTON_HOME" inst.son-of-antonHome) (
            lib.attrValues (lib.filterAttrs (_: i: i.addToSystemPackages) enabledInstances)
          )
        );

        # managedAccount instances get SON_OF_ANTON_HOME and its subdirs only.
        # Creating or chmod-ing the parent home or workingDirectory of a login
        # account breaks sshd StrictModes and leaks the home to the account's
        # primary group.
        systemd.tmpfiles.rules = lib.concatLists (
          lib.mapAttrsToList (
            _: inst:
            (lib.optionals (!inst.managedAccount) [
              "d ${inst.stateDir} 2770 ${inst.user} ${inst.group} - -"
              "d ${inst.stateDir}/home 0750 ${inst.user} ${inst.group} - -"
              "d ${inst.workingDirectory} 2770 ${inst.user} ${inst.group} - -"
            ])
            ++ [
              "d ${inst.son-of-antonHome} ${dirMode inst} ${inst.user} ${inst.group} - -"
              "d ${inst.son-of-antonHome}/locks 0700 ${inst.user} ${inst.group} - -"
            ]
            ++ map (d: "d ${inst.son-of-antonHome}/${d} ${dirMode inst} ${inst.user} ${inst.group} - -")
              common.stateSubdirs
          ) enabledInstances
        );

        system.activationScripts = lib.listToAttrs (
          lib.mapAttrsToList (
            name: inst:
            lib.nameValuePair "son-of-anton-setup-${name}" (
              lib.stringAfter
                (
                  [ "users" ]
                  ++ lib.optional (outerConfig.system.activationScripts ? setupSecrets) "setupSecrets"
                )
                ''
                  # Activation runs before tmpfiles, so create what we own.
                  mkdir -p ${inst.son-of-antonHome}
                  chown ${inst.user}:${inst.group} ${inst.son-of-antonHome}
                  chmod ${dirMode inst} ${inst.son-of-antonHome}
                  ${lib.optionalString (!inst.managedAccount) ''
                    mkdir -p ${inst.stateDir}/home
                    mkdir -p ${inst.workingDirectory}
                    chown ${inst.user}:${inst.group} ${inst.stateDir} ${inst.stateDir}/home ${inst.workingDirectory}
                    chmod 2770 ${inst.stateDir} ${inst.workingDirectory}
                    chmod 0750 ${inst.stateDir}/home

                    find ${inst.son-of-antonHome} -maxdepth 1 \
                      \( -name "*.db" -o -name "*.db-wal" -o -name "*.db-shm" -o -name "SOUL.md" \) \
                      -exec chmod g+rw {} + 2>/dev/null || true
                  ''}
                  for _subdir in ${lib.concatStringsSep " " common.stateSubdirs}; do
                    mkdir -p "${inst.son-of-antonHome}/$_subdir"
                    chown ${inst.user}:${inst.group} "${inst.son-of-antonHome}/$_subdir"
                    chmod ${dirMode inst} "${inst.son-of-antonHome}/$_subdir"
                    ${lib.optionalString (!inst.managedAccount) ''
                      find "${inst.son-of-antonHome}/$_subdir" -type f \
                        -exec chmod g+rw {} + 2>/dev/null || true
                    ''}
                  done

                  ${common.mkStateScript {
                    inherit pkgs;
                    cfg = inst;
                    son-of-antonHome = inst.son-of-antonHome;
                    workingDirectory = inst.workingDirectory;
                    configWorkingDirectory = inst.workingDirectory;
                    owner = "${inst.user}:${inst.group}";
                    stateDirs = common.stateSubdirs;
                    modes = {
                      config = configYamlMode inst;
                      env = envMode inst;
                      managed = "0644";
                      auth = "0600";
                      document = envMode inst;
                    };
                  }}

                  chown -h ${inst.user}:${inst.group} ${inst.son-of-antonHome}/plugins/nix-managed-* 2>/dev/null || true
                ''
            )
          ) enabledInstances
        );

        systemd.services = lib.listToAttrs (
          lib.mapAttrsToList (
            name: inst:
            lib.nameValuePair "son-of-anton-${name}" {
              description = "Son of Anton Agent Gateway (${name})";
              wantedBy = [ "multi-user.target" ];
              after = [ "network-online.target" ];
              wants = [ "network-online.target" ];

              # inst.environment and inst.environmentFiles are written to
              # $SON_OF_ANTON_HOME/.env by the activation script.
              # load_son_of_anton_dotenv() reads them at Python startup — no
              # systemd EnvironmentFile needed.
              environment = unitEnvironmentFor inst;

              serviceConfig = (serviceConfigFor inst) // {
                ExecStart = lib.escapeShellArgs (common.gatewayArgv inst);
              };

              path = common.processPath {
                inherit pkgs;
                cfg = inst;
              };
            }
          ) enabledInstances
        );
      };
    };
}

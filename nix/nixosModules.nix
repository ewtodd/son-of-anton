# nix/nixosModules.nix — the NixOS module for son-of-anton
#
# This module shares its options, its renderers for config.yaml, .env and
# documents, and its state setup with the Home Manager module
# (nix/homeManagerModules.nix). The shared code is in nix/moduleCommon.nix.
# This file holds only the parts that need root: the service user, a system
# state directory, and the systemd service.
#
# For one-agent-per-user deployments (separate work/play accounts), prefer
# the Home Manager module: each account then runs its own gateway under its
# own systemd user service with its own ~/.son-of-anton state.
#
# Usage:
#   services.son-of-anton = {
#     enable = true;
#     settings.model.default = "...";
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
      cfg = config.services.son-of-anton;
      common = import ./moduleCommon.nix { inherit lib; };

      effectivePackage = common.effectivePackage cfg;
      son-of-anton = inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.default;

      son-of-antonHome = "${cfg.stateDir}/.son-of-anton";

      # config.yaml mode: group-writable (0660) when interactive users share this
      # SON_OF_ANTON_HOME via addToSystemPackages, so they can save settings through the
      # CLI/TUI without hitting EACCES; otherwise group-read-only (0640). Secrets
      # (.env) stay 0640 regardless.
      configYamlMode = if cfg.addToSystemPackages then "0660" else "0640";

      # The hardening and the environment that the gateway unit shares.
      commonServiceConfig = {
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.workingDirectory;

        Restart = cfg.restart;
        RestartSec = cfg.restartSec;

        # Shared-state: files created by the service should be group-writable
        # so interactive users in the son-of-anton group can read/write them.
        UMask = "0007";

        # Hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = false;
        ReadWritePaths = [
          cfg.stateDir
          cfg.workingDirectory
        ];
        PrivateTmp = true;
      };

      commonUnitEnvironment = {
        HOME = cfg.stateDir;
      }
      // common.processEnvironment { inherit son-of-antonHome; };

      unitPath = common.processPath { inherit pkgs cfg; };

    in
    {
      options.services.son-of-anton =
        common.sharedOptions {
          defaultPackage = son-of-anton;
          defaultPackageText = lib.literalExpression "son-of-anton.packages.\${system}.default";
          defaultWorkingDirectory = "${cfg.stateDir}/workspace";
          defaultWorkingDirectoryText = lib.literalExpression ''"''${cfg.stateDir}/workspace"'';
        }
        // (
          with lib;
          {
            # ── Service identity ───────────────────────────────────────────
            user = mkOption {
              type = types.str;
              default = "son-of-anton";
              description = "System user running the gateway.";
            };

            group = mkOption {
              type = types.str;
              default = "son-of-anton";
              description = "System group running the gateway.";
            };

            createUser = mkOption {
              type = types.bool;
              default = true;
              description = "Create the user/group automatically.";
            };

            # ── Directories ────────────────────────────────────────────────
            stateDir = mkOption {
              type = types.str;
              default = "/var/lib/son-of-anton";
              description = "State directory. Contains .son-of-anton/ subdir (SON_OF_ANTON_HOME).";
            };

            addToSystemPackages = mkOption {
              type = types.bool;
              default = false;
              description = ''
                Add the son-of-anton CLI to environment.systemPackages and export
                SON_OF_ANTON_HOME system-wide (via environment.variables) so interactive
                shells share state with the gateway service.
              '';
            };
          }
        );

      config = lib.mkIf cfg.enable (
        lib.mkMerge [

          # ── Merge MCP servers into settings ────────────────────────────────
          (lib.mkIf (cfg.mcpServers != { }) {
            services.son-of-anton.settings.mcp_servers = common.mcpServersToConfig cfg.mcpServers;
          })

          # ── User / group ──────────────────────────────────────────────────
          (lib.mkIf cfg.createUser {
            users.groups.${cfg.group} = { };
            users.users.${cfg.user} = {
              isSystemUser = true;
              group = cfg.group;
              home = cfg.stateDir;
              createHome = true;
              shell = pkgs.bashInteractive;
            };
          })

          # ── Host CLI ──────────────────────────────────────────────────────
          (lib.mkIf cfg.addToSystemPackages {
            environment.systemPackages = [ effectivePackage ];
            environment.variables.SON_OF_ANTON_HOME = son-of-antonHome;
          })

          # ── Assertions ─────────────────────────────────────────────────────
          {
            assertions =
              common.pluginNameAssertions {
                inherit cfg;
                optionPath = "services.son-of-anton";
              }
              ++ common.workspaceFilesAssertions {
                inherit cfg;
                opt = options.services.son-of-anton.workingDirectory;
                optionPath = "services.son-of-anton";
              };
          }

          # ── Per-user profile for extraPackages ─────────────────────────────
          (lib.mkIf (cfg.extraPackages != [ ]) {
            users.users.${cfg.user}.packages = cfg.extraPackages;
          })

          # ── Directories ───────────────────────────────────────────────────
          {
            systemd.tmpfiles.rules = [
              "d ${cfg.stateDir}                2770 ${cfg.user} ${cfg.group} - -"
              "d ${son-of-antonHome}                  2770 ${cfg.user} ${cfg.group} - -"
              "d ${cfg.stateDir}/home           0750 ${cfg.user} ${cfg.group} - -"
              "d ${cfg.workingDirectory}        2770 ${cfg.user} ${cfg.group} - -"
            ]
            ++ map (d: "d ${son-of-antonHome}/${d} 2770 ${cfg.user} ${cfg.group} - -") common.stateSubdirs;
          }

          # ── Activation: link config + auth + documents ────────────────────
          {
            system.activationScripts."son-of-anton-setup" =
              lib.stringAfter
                (
                  [ "users" ] ++ lib.optional (config.system.activationScripts ? setupSecrets) "setupSecrets"
                )
                ''
                  # Ensure directories exist (activation runs before tmpfiles)
                  mkdir -p ${son-of-antonHome}
                  mkdir -p ${cfg.stateDir}/home
                  mkdir -p ${cfg.workingDirectory}
                  chown ${cfg.user}:${cfg.group} ${cfg.stateDir} ${son-of-antonHome} ${cfg.stateDir}/home ${cfg.workingDirectory}
                  chmod 2770 ${cfg.stateDir} ${son-of-antonHome} ${cfg.workingDirectory}
                  chmod 0750 ${cfg.stateDir}/home

                  # Create subdirs, set setgid + group-writable, migrate existing files.
                  # Nix-managed .env/.managed stay 0640/0644; config.yaml uses
                  # configYamlMode (0660 under addToSystemPackages, else 0640).
                  find ${son-of-antonHome} -maxdepth 1 \
                    \( -name "*.db" -o -name "*.db-wal" -o -name "*.db-shm" -o -name "SOUL.md" \) \
                    -exec chmod g+rw {} + 2>/dev/null || true
                  for _subdir in ${lib.concatStringsSep " " common.stateSubdirs}; do
                    mkdir -p "${son-of-antonHome}/$_subdir"
                    chown ${cfg.user}:${cfg.group} "${son-of-antonHome}/$_subdir"
                    chmod 2770 "${son-of-antonHome}/$_subdir"
                    find "${son-of-antonHome}/$_subdir" -type f \
                      -exec chmod g+rw {} + 2>/dev/null || true
                  done

                  ${common.mkStateScript {
                    inherit pkgs cfg son-of-antonHome;
                    workingDirectory = cfg.workingDirectory;
                    configWorkingDirectory = cfg.workingDirectory;
                    owner = "${cfg.user}:${cfg.group}";
                    stateDirs = common.stateSubdirs;
                    modes = {
                      config = configYamlMode;
                      env = "0640";
                      managed = "0644";
                      auth = "0600";
                      document = "0640";
                    };
                  }}

                  chown -h ${cfg.user}:${cfg.group} ${son-of-antonHome}/plugins/nix-managed-* 2>/dev/null || true
                '';
          }

          # ── The gateway: the one long-running service ────────────────────
          {
            systemd.services.son-of-anton = {
              description = "Son of Anton Agent Gateway";
              wantedBy = [ "multi-user.target" ];
              after = [ "network-online.target" ];
              wants = [ "network-online.target" ];

              # cfg.environment and cfg.environmentFiles are written to
              # $SON_OF_ANTON_HOME/.env by the activation script. load_son_of_anton_dotenv()
              # reads them at Python startup — no systemd EnvironmentFile needed.
              environment = commonUnitEnvironment;

              serviceConfig = commonServiceConfig // {
                ExecStart = lib.escapeShellArgs (common.gatewayArgv cfg);
              };

              path = unitPath;
            };
          }
        ]
      );
    };
}

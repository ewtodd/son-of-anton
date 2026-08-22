# nix/homeManagerModules.nix — the Home Manager module for son-of-anton
#
# This module is the user-level equivalent of nixosModules.default. Son of
# Anton is an agent for one person. The credentials, the memory, the sessions
# and the cron jobs all belong to that person. Thus a user-level module is
# correct on each distribution, and not only on NixOS. It is also the natural
# fit for per-account isolation: each account (work/play) runs its own
# gateway under its own systemd user service with its own SON_OF_ANTON_HOME.
#
# `services.son-of-anton` is the same option set on both modules. All of the
# options except the system-level ones come from nix/moduleCommon.nix, so an
# example from the NixOS documentation works here without a change. Only the
# necessary parts are different:
#
#   removed   user, group, createUser  — Home Manager runs as the user
#   removed   UMask 0007               — that mode shares state with a UNIX
#                                        group, but this state has one user
#   changed   systemd.services         -> systemd.user.services
#   changed   system.activationScripts -> home.activation
#   changed   addToSystemPackages      -> installPackage and
#                                        home.sessionVariables
#   changed   stateDir (+ "/.son-of-anton")  -> son-of-antonHome, set directly
#
# To use the module:
#   imports = [ son-of-anton.homeManagerModules.default ];
#   services.son-of-anton = {
#     enable = true;
#     gateway.enable = true;
#     settings.model.default = "...";
#     environmentFiles = [ config.sops.secrets."son-of-anton/env".path ];
#   };
#
# CAUTION: Enable linger for the account. Without linger, systemd stops the
# user manager at logout, and both units stop with it. Home Manager cannot
# run `loginctl enable-linger`. On NixOS, set
#   users.users.<name>.linger = true;
# On other systems, run `loginctl enable-linger <name>` one time.
{ inputs, ... }:
{
  flake.homeManagerModules.default =
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

      processEnvironment = common.processEnvironment {
        inherit (cfg) son-of-antonHome;
        # The CLI reads this value and names it when it refuses a
        # configuration change.
        managedSystem = "home-manager";
      };
      unitPath = lib.makeBinPath (common.processPath { inherit pkgs cfg; });

      mkUnit =
        {
          description,
          argv,
        }:
        {
          Unit = {
            Description = description;
            # Do not use network-online.target here. That is a system target.
            # A user unit that orders against it has no effect, and systemd
            # gives no message.
            After = [ "default.target" ];
          };
          Install.WantedBy = [ "default.target" ];
          Service = {
            Type = "simple";
            Environment = (lib.mapAttrsToList (k: v: "${k}=${v}") processEnvironment) ++ [
              "PATH=${unitPath}"
            ];
            ExecStart = lib.escapeShellArgs argv;
            WorkingDirectory = cfg.workingDirectory;
            Restart = cfg.restart;
            RestartSec = cfg.restartSec;
            # This state has one user. Keep it private. The NixOS module uses
            # 0007 to share the state with a UNIX group.
            UMask = "0077";
            NoNewPrivileges = true;
            PrivateTmp = true;
          };
        };

    in
    {
      options.services.son-of-anton =
        common.sharedOptions {
          defaultPackage = son-of-anton;
          defaultPackageText = lib.literalExpression "son-of-anton.packages.\${system}.default";
          defaultWorkingDirectory = config.home.homeDirectory;
          defaultWorkingDirectoryText = lib.literalExpression "config.home.homeDirectory";
        }
        // {
          son-of-antonHome = lib.mkOption {
            type = lib.types.str;
            default = "${config.home.homeDirectory}/.son-of-anton";
            defaultText = lib.literalExpression ''"''${config.home.homeDirectory}/.son-of-anton"'';
            description = ''
              The value of SON_OF_ANTON_HOME. This state directory holds
              config.yaml, .env, auth.json, the sessions, the skills, the
              memory and the cron jobs.

              The NixOS module takes a `stateDir` and adds `/.son-of-anton` to it.
              This module sets SON_OF_ANTON_HOME directly. Thus an existing
              ~/.son-of-anton continues to work, and you can give the directory any
              name.
            '';
            example = "/home/alice/.son-of-anton-work";
          };

          installPackage = lib.mkOption {
            type = lib.types.bool;
            default = true;
            description = ''
              Add the son-of-anton CLI to home.packages, and export SON_OF_ANTON_HOME
              with home.sessionVariables. Interactive shells then use the
              same state as the services.

              The equivalent NixOS option, `addToSystemPackages`, exports
              SON_OF_ANTON_HOME with environment.variables. That variable applies
              to the full system and replaces the SON_OF_ANTON_HOME of each other
              user. This module exports the variable for one user session
              only, which is the reason to use Home Manager.
            '';
          };

          gateway.enable = lib.mkEnableOption "the messaging gateway service (Discord, Slack, Signal)";
        };

      config = lib.mkIf cfg.enable (
        lib.mkMerge [

          # ── Merge MCP servers into settings ────────────────────────────
          (lib.mkIf (cfg.mcpServers != { }) {
            services.son-of-anton.settings.mcp_servers = common.mcpServersToConfig cfg.mcpServers;
          })

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

          # ── Packages and interactive-shell environment ─────────────────
          (lib.mkIf cfg.installPackage {
            home.packages = [ effectivePackage ] ++ cfg.extraPackages;
            home.sessionVariables.SON_OF_ANTON_HOME = cfg.son-of-antonHome;
          })

          # ── Activation: directories, config, secrets, documents ────────
          {
            # The activation runs after writeBoundary, when the home.file
            # symlinks are in place. It also runs after linkGeneration, when
            # Home Manager completes the switch. A secret that the activation
            # entry of sops-nix writes exists at that point.
            home.activation.son-of-antonAgentSetup =
              lib.hm.dag.entryAfter
                [
                  "writeBoundary"
                  "linkGeneration"
                ]
                (
                  common.mkStateScript {
                    inherit pkgs cfg;
                    inherit (cfg) son-of-antonHome workingDirectory;
                    run = "$DRY_RUN_CMD ";
                    stateDirs = common.stateSubdirs;
                    managedSystem = "home-manager";
                    # This state has one user. No group needs access to it.
                    modes = {
                      config = "0600";
                      env = "0600";
                      managed = "0600";
                      auth = "0600";
                      document = "0600";
                    };
                  }
                );
          }

          # ── Linux: systemd user service ────────────────────────────────
          (lib.mkIf cfg.gateway.enable {
            systemd.user.services.son-of-anton = mkUnit {
              description = "Son of Anton Agent Gateway";
              argv = common.gatewayArgv cfg;
            };
          })
        ]
      );
    };
}

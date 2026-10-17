# nix/homeManagerModules.nix — the Home Manager module for son-of-anton
#
# The user-level install: the CLI/TUI for one account, with its config.toml
# and .env managed by Home Manager. Gateways run from the NixOS module.
#
# `services.son-of-anton` shares its options with the NixOS module (see
# nix/moduleCommon.nix). Only the necessary parts are different:
#
#   removed   user, group, createUser  — Home Manager runs as the user
#   changed   system.activationScripts -> home.activation
#   changed   addToSystemPackages      -> installPackage and
#                                        home.sessionVariables
#   changed   stateDir (+ "/.son-of-anton")  -> son-of-antonHome, set directly
#
# To use the module:
#   imports = [ son-of-anton.homeManagerModules.default ];
#   services.son-of-anton = {
#     enable = true;
#     settings.model.default = "...";
#     environmentFiles = [ config.sops.secrets."son-of-anton/env".path ];
#   };
{ inputs, ... }:
{
  flake.homeManagerModules.default =
    {
      config,
      lib,
      pkgs,
      ...
    }:

    let
      cfg = config.services.son-of-anton;
      common = import ./moduleCommon.nix { inherit lib; };
      son-of-anton = inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.default;
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
              config.toml, .env, the sessions, the memory and the cron jobs.

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
              with home.sessionVariables.

              The equivalent NixOS option, `addToSystemPackages`, exports
              SON_OF_ANTON_HOME with environment.variables. That variable applies
              to the full system and replaces the SON_OF_ANTON_HOME of each other
              user. This module exports the variable for one user session
              only, which is the reason to use Home Manager.
            '';
          };
        };

      config = lib.mkIf cfg.enable (
        lib.mkMerge [
          # ── Packages and interactive-shell environment ─────────────────
          (lib.mkIf cfg.installPackage {
            home.packages = [ (common.effectivePackage cfg) ] ++ cfg.extraPackages;
            home.sessionVariables.SON_OF_ANTON_HOME = cfg.son-of-antonHome;
          })

          # ── Activation: directories, config, secrets ───────────────────
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
                    };
                  }
                );
          }
        ]
      );
    };
}

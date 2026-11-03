# nix/homeManagerModules.nix — services.son-of-anton for one user's Home Manager.
#
# The user-level install: the command on the profile, SON_OF_ANTON_HOME in the
# session, and config.toml and .env kept up to date by an activation step. It
# runs no service; gateways come from the NixOS module.
#
# Same options as an instance of the NixOS module (options.nix), minus what a
# system identity needs (user, group, sandbox, tmpfiles), plus `installPackage`
# and `son-of-antonHome` (named whole here, not derived from a stateDir).
#
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
      options' = import ./options.nix { inherit lib; };
      state = import ./state.nix { inherit lib; };
      home = config.home.homeDirectory;
    in
    {
      options.services.son-of-anton =
        options'.common {
          package = inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.default;
          packageText = lib.literalExpression "son-of-anton.packages.\${system}.default";
          workingDirectory = home;
          workingDirectoryText = lib.literalExpression "config.home.homeDirectory";
        }
        // options'.homeOnly { homeDirectory = home; };

      config = lib.mkIf cfg.enable (
        lib.mkMerge [
          {
            assertions = lib.singleton {
              assertion = cfg.memoryRules == [ ] || cfg.memoryRulesFile == null;
              message = ''
                services.son-of-anton sets both memoryRules and memoryRulesFile;
                keep one. memoryRules ends up in the public Nix store,
                memoryRulesFile is read from a run-time path.
              '';
            };

            # Runs once the home.file links exist and the generation is
            # linked, so a secret that sops-nix wrote during its own
            # activation is already in place.
            home.activation.son-of-antonAgentSetup = lib.hm.dag.entryAfter [ "writeBoundary" "linkGeneration" ] (
              state.setupScript {
                inherit pkgs cfg;
                home = cfg.son-of-antonHome;
                dry = "$DRY_RUN_CMD ";
                # One user owns all of it; nothing is shared with a group.
                modes = {
                  config = "0600";
                  env = "0600";
                  marker = "0600";
                };
                system = "home-manager";
              }
            );
          }

          (lib.mkIf cfg.installPackage {
            home.packages = [ (state.packageFor cfg) ] ++ cfg.extraPackages;
            home.sessionVariables.SON_OF_ANTON_HOME = cfg.son-of-antonHome;
          })
        ]
      );
    };
}

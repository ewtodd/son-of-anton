# nix/checks.nix — build-time verification tests
#
# Checks are Linux-only by convention: the full Python venv (via uv2nix)
# includes transitive deps without compatible wheels on aarch64-darwin.
{ inputs, ... }:
{
  perSystem =
    { pkgs, lib, self', ... }:
    let
      son-of-anton = self'.packages.default;
      sonOfAntonVenv = son-of-anton.sonOfAntonVenv;

      # ── How the checks evaluate the modules ───────────────────────────
      # Both modules are evaluated for real. The NixOS module goes through
      # lib.evalModules with the NixOS module list; the Home Manager module
      # goes through the homeManagerConfiguration function of home-manager.
      evalNixosModule =
        settings:
        inputs.nixpkgs.lib.evalModules {
          modules = import "${inputs.nixpkgs}/nixos/modules/module-list.nix" ++ [
            inputs.self.nixosModules.default
            { _module.args.lib = inputs.nixpkgs.lib; }
            { nixpkgs.hostPlatform = pkgs.stdenv.hostPlatform.system; }
            {
              system.stateVersion = "24.11";
              boot.loader.grub.enable = false;
              fileSystems."/" = {
                device = "/dev/null";
                fsType = "ext4";
              };
            }
            { services.son-of-anton.instances = settings; }
          ];
        };

      evalHomeModule =
        settings:
        inputs.home-manager.lib.homeManagerConfiguration {
          inherit pkgs;
          modules = [
            inputs.self.homeManagerModules.default
            {
              home = {
                username = "son-of-anton-check";
                homeDirectory = "/home/son-of-anton-check";
                stateVersion = "24.11";
              };
            }
            { services.son-of-anton = settings; }
          ];
        };

      # ExecStart of the gateway unit, normalized to a string.
      execStr = exec: if builtins.isList exec then lib.concatStringsSep " " exec else exec;

      nixosGatewayExec = eval: name:
        eval.config.systemd.services."son-of-anton-${name}".serviceConfig.ExecStart;

      homeGatewayExec = eval:
        eval.config.systemd.user.services.son-of-anton.Service.ExecStart;

      # Auto-generated config key reference — always in sync with Python.
      configKeys = pkgs.runCommand "son-of-anton-config-keys" { } ''
        set -euo pipefail
        export HOME=$TMPDIR
        ${sonOfAntonVenv}/bin/python3 -c '
import json, sys
from son_of_anton_cli.config import DEFAULT_CONFIG

def leaf_paths(d, prefix=""):
    paths = []
    for k, v in sorted(d.items()):
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and v:
            paths.extend(leaf_paths(v, path))
        else:
            paths.append(path)
    return paths

json.dump(sorted(leaf_paths(DEFAULT_CONFIG)), sys.stdout, indent=2)
' > $out
      '';
    in
    {
      packages.configKeys = configKeys;

      checks = {
        # Cross-platform evaluation — catches "not supported for interpreter"
        # errors without needing a darwin builder.
        cross-eval =
          let
            targetSystems = builtins.filter
              (s: inputs.self.packages ? ${s})
              [
                "x86_64-linux"
                "aarch64-linux"
                "aarch64-darwin"
              ];
            tryEvalPkg =
              sys:
              let
                pkg = inputs.self.packages.${sys}.default;
              in
              builtins.tryEval (builtins.seq pkg.drvPath true);
            results = map (sys: { inherit sys; result = tryEvalPkg sys; }) targetSystems;
            failures = builtins.filter (r: !r.result.success) results;
            failMsg = lib.concatMapStringsSep "\n" (r: "  - ${r.sys}") failures;
          in
          pkgs.runCommand "son-of-anton-cross-eval" { } (
            if failures != [ ] then
              throw "Package fails to evaluate on:\n${failMsg}"
            else
              ''
                echo "PASS: package evaluates on all ${toString (builtins.length targetSystems)} platforms"
                mkdir -p $out
                echo "ok" > $out/result
              ''
          );

        # Verify the default package builds successfully.
        build-package = pkgs.runCommand "son-of-anton-build-package" { } ''
          echo "PASS: package built at ${son-of-anton}"
          mkdir -p $out
          echo "ok" > $out/result
        '';

        # Verify the devShell builds successfully.
        build-devshell = pkgs.runCommand "son-of-anton-build-devshell" { } ''
          echo "PASS: devShell built at ${self'.devShells.default}"
          mkdir -p $out
          echo "ok" > $out/result
        '';

        # ── The NixOS module ─────────────────────────────────────────────
        nixos-module =
          let
            single = evalNixosModule { main.enable = true; };
            exec = execStr (nixosGatewayExec single "main");

            # Two instances must coexist: distinct units, distinct homes,
            # distinct users. That is the whole point of the multi-instance
            # shape — one gateway per account, each with its own state.db.
            pair = evalNixosModule {
              work = {
                enable = true;
                user = "e-work";
                createUser = false;
                managedAccount = true;
                son-of-antonHome = "/home/e-work/.son-of-anton";
                workingDirectory = "/home/e-work";
              };
              play = {
                enable = true;
                user = "e-play";
                createUser = false;
                managedAccount = true;
                son-of-antonHome = "/home/e-play/.son-of-anton";
                workingDirectory = "/home/e-play";
              };
            };
            units = pair.config.systemd.services;
            workUnit = units."son-of-anton-work";
            playUnit = units."son-of-anton-play";
            rules = pair.config.systemd.tmpfiles.rules;
          in
          assert lib.hasInfix "bin/son-of-anton gateway" exec;
          assert units ? "son-of-anton-work" && units ? "son-of-anton-play";
          assert workUnit.environment.SON_OF_ANTON_HOME == "/home/e-work/.son-of-anton";
          assert playUnit.environment.SON_OF_ANTON_HOME == "/home/e-play/.son-of-anton";
          assert workUnit.serviceConfig.User == "e-work";
          assert playUnit.serviceConfig.User == "e-play";
          # managedAccount must not loosen a human home: owner-only umask...
          assert workUnit.serviceConfig.UMask == "0077";
          # Each instance needs its own signal-phone lock namespace, or the
          # second gateway on a shared Signal account never starts Signal.
          assert workUnit.environment.SON_OF_ANTON_GATEWAY_LOCK_DIR
              != playUnit.environment.SON_OF_ANTON_GATEWAY_LOCK_DIR;
          # ...and must never emit a tmpfiles rule for the home itself, which
          # would set 2770 and make sshd StrictModes reject authorized_keys.
          assert !(lib.any (r: lib.hasInfix "d /home/e-work " r) rules);
          assert !(lib.any (r: lib.hasInfix "d /home/e-play " r) rules);
          pkgs.runCommand "son-of-anton-nixos-module" { } ''
            echo "PASS: NixOS module units correct (single + two-instance isolation)"
            mkdir -p $out
          '';

        # ── The Home Manager module ──────────────────────────────────────
        home-manager-module =
          let
            exec = execStr (homeGatewayExec (
              evalHomeModule {
                enable = true;
                gateway.enable = true;
              }
            ));
          in
          assert lib.hasInfix "bin/son-of-anton gateway" exec;
          pkgs.runCommand "son-of-anton-home-manager-module" { } ''
            echo "PASS: Home Manager module gateway unit is correct"
            mkdir -p $out
          '';

        # ── Bundled assets + venv smoke ──────────────────────────────────
        bundled-assets = pkgs.runCommand "son-of-anton-bundled-assets" { } ''
          set -euo pipefail
          test -d ${son-of-anton}/share/son-of-anton/skills \
            || (echo "FAIL: bundled skills missing"; exit 1)
          test -d ${son-of-anton}/share/son-of-anton/plugins \
            || (echo "FAIL: bundled plugins missing"; exit 1)
          test -d ${son-of-anton}/share/son-of-anton/locales \
            || (echo "FAIL: bundled locales missing"; exit 1)
          ${son-of-anton}/bin/son-of-anton --help 2>&1 | grep -q "gateway" \
            || (echo "FAIL: gateway subcommand missing"; exit 1)
          echo "PASS: bundled skills + plugins + locales present, binary works"
          mkdir -p $out
        '';

        venv-imports = pkgs.runCommand "son-of-anton-venv-imports" { } ''
          set -euo pipefail
          export HOME=$TMPDIR
          # Plugins ship outside the wheel (bundled dir + env var), matching
          # the wrapped binary's runtime resolution.
          export PYTHONPATH="${son-of-anton}/share/son-of-anton"
          export SON_OF_ANTON_BUNDLED_PLUGINS="${son-of-anton}/share/son-of-anton/plugins"
          export SON_OF_ANTON_BUNDLED_SKILLS="${son-of-anton}/share/son-of-anton/skills"
          ${sonOfAntonVenv}/bin/python3 -c "
import cli
import run_agent
import model_tools
import gateway.run
import cron.scheduler
import son_of_anton_cli.main
import son_of_anton_cli.cli_commands_mixin
import son_of_anton_cli.commands
import tools.terminal_tool
import tools.approval
import tools.web_tools
import plugins.web.firecrawl.provider
import plugins.web.tavily.provider
import agent.system_prompt
import agent.prompt_builder
import plugins.platforms.discord.adapter
import plugins.platforms.slack.adapter
import gateway.platforms.signal
import physics_intern.engine
import physics_intern.autophysicist.runner
print('imports ok')
" || (echo "FAIL: core modules do not import from the sealed venv"; exit 1)
          echo "PASS: core modules import from the sealed venv"
          mkdir -p $out
        '';
      };
    };
}

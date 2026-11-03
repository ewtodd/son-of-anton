# nix/checks.nix — build-time verification tests
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
        # errors without needing an aarch64 builder.
        cross-eval =
          let
            targetSystems = builtins.filter
              (s: inputs.self.packages ? ${s})
              [
                "x86_64-linux"
                "aarch64-linux"
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

        # Build the Go daemon (its check phase runs the Go contract tests)
        # and smoke-run the binary. bash/ripgrep/coreutils ride along so the
        # terminal and search tools can run under the check.
        build-soa-d = pkgs.runCommand "soa-d-build" {
          nativeBuildInputs = [ pkgs.bash pkgs.ripgrep pkgs.coreutils ];
        } ''
          ${self'.packages.soa-d}/bin/soa-d version
          echo "PASS: soa-d built and runs"
          mkdir -p $out
          echo "ok" > $out/result
        '';

        # ── An instance on soa-d (phase 8b) ───────────────────────────────
        # backend = "soa-d" makes the instance's unit run the Go daemon
        # instead of the Python gateway (one unit, so the two can never both
        # serve an instance), and seeds [daemon] for it; an explicit setting
        # still wins. The package ships soa-d wrapped with the runtime tools
        # and a Python for execute_code.
        nixos-soad-backend =
          let
            account = name: {
              enable = true;
              user = "e-${name}";
              createUser = false;
              managedAccount = true;
              son-of-antonHome = "/home/e-${name}/.son-of-anton";
              workingDirectory = "/home/e-${name}";
            };
            eval = evalNixosModule {
              play = account "play" // { backend = "soa-d"; };
              work = account "work" // {
                backend = "soa-d";
                settings.daemon.cron = false;
                settings.model.default = "auto";
              };
              house = account "house";
              shop = {
                enable = true;
                user = "soa-shop";
                createUser = true;
                managedAccount = false;
                stateDir = "/var/lib/soa-shop";
                son-of-antonHome = "/var/lib/soa-shop/.son-of-anton";
                workingDirectory = "/srv/shop";
                backend = "soa-d";
              };
            };
            sc = name: units."son-of-anton-${name}".serviceConfig;
            units = eval.config.systemd.services;
            exec = name: execStr units."son-of-anton-${name}".serviceConfig.ExecStart;
            inst = eval.config.services.son-of-anton.instances;
          in
          assert lib.hasSuffix "/bin/soa-d serve" (exec "play");
          assert lib.hasSuffix "/bin/soa-d serve" (exec "work");
          assert lib.hasInfix "bin/son-of-anton gateway" (exec "house");
          assert inst.house.backend == "python";
          assert inst.play.settings.daemon == { instance = "play"; signal = true; cron = true; autostart = false; };
          assert inst.work.settings.daemon == { instance = "work"; signal = true; cron = false; autostart = false; };
          # An owner's own account on soa-d: no filesystem sandbox, never privileges.
          assert (sc "play").ProtectSystem == false && (sc "play").PrivateTmp == false;
          assert (sc "play").NoNewPrivileges;
          # A service account on soa-d, and the Python gateway, keep the sandbox.
          assert (sc "shop").ProtectSystem == "strict" && (sc "shop").PrivateTmp && (sc "shop").NoNewPrivileges;
          assert (sc "house").ProtectSystem == "strict" && (sc "house").PrivateTmp;
          assert inst.work.settings.model.default == "auto";
          assert !(inst.house.settings ? daemon);
          assert units."son-of-anton-play".environment.SON_OF_ANTON_HOME == "/home/e-play/.son-of-anton";
          # Settings changes restart the process: the rendered config is a trigger,
          # so two instances with different settings have different triggers.
          assert lib.any (t: lib.hasSuffix "son-of-anton-config.toml" (toString t)) units."son-of-anton-play".restartTriggers;
          assert units."son-of-anton-play".restartTriggers != units."son-of-anton-work".restartTriggers;
          pkgs.runCommand "son-of-anton-soad-backend" { } ''
            test -x ${son-of-anton}/bin/soa-d
            grep -q SON_OF_ANTON_PYTHON ${son-of-anton}/bin/soa-d
            grep -q ripgrep ${son-of-anton}/bin/soa-d
            ${son-of-anton}/bin/soa-d version
            echo "PASS: backend = soa-d runs the daemon, seeds [daemon], and soa-d is wrapped"
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

            # Activation runs as root, so every file it CREATES is root-owned
            # until something says otherwise. config.toml only ever inherited
            # the right owner by having the merge rewrite a file that already
            # had it, and .nix-managed.json is new on every install — a 0600
            # root:root config.toml is unreadable by the service that needs it.
            activation = lib.concatStringsSep "\n" (
              map (v: if builtins.isString v then v else v.text) (
                builtins.attrValues (
                  lib.filterAttrs (
                    n: _: lib.hasPrefix "son-of-anton" n
                  ) pair.config.system.activationScripts
                )
              )
            );
          in
          assert lib.hasInfix "bin/son-of-anton gateway" exec;

          # Everything the merge path writes must be chowned to the instance.
          assert lib.hasInfix
            "chown e-work:son-of-anton /home/e-work/.son-of-anton/config.toml"
            activation;
          assert lib.hasInfix "/home/e-work/.son-of-anton/.nix-managed.json" activation;
          assert lib.hasInfix
            "chown e-play:son-of-anton /home/e-play/.son-of-anton/config.toml"
            activation;

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
          # Credential stores are denied in the kernel, resolved against the
          # instance's own home (an ACL allowlist stopped being a boundary once
          # the gateway runs AS the account it serves).
          assert lib.elem "-/home/e-work/.gnupg" workUnit.serviceConfig.InaccessiblePaths;
          assert lib.elem "-/home/e-work/.git-credentials" workUnit.serviceConfig.InaccessiblePaths;
          assert lib.elem "-/home/e-play/.gnupg" playUnit.serviceConfig.InaccessiblePaths;
          # ...but ~/.ssh stays reachable so git over SSH keeps working; its
          # key files are covered by agent/file_safety.py at the app layer.
          assert !(lib.any (p: lib.hasSuffix "/.ssh" p) workUnit.serviceConfig.InaccessiblePaths);
          # ...and must never emit a tmpfiles rule for the home itself, which
          # would set 2770 and make sshd StrictModes reject authorized_keys.
          assert !(lib.any (r: lib.hasInfix "d /home/e-work " r) rules);
          assert !(lib.any (r: lib.hasInfix "d /home/e-play " r) rules);
          # Every writable path must actually exist at start. ProtectSystem=
          # strict makes systemd refuse to start on a missing ReadWritePaths
          # entry (226/NAMESPACE), and the two rules above are exactly why a
          # managedAccount has directories the module deliberately never
          # creates. So each entry must be either the account's own
          # pre-existing home or something tmpfiles does create.
          assert (
            let
              created = map (r: lib.elemAt (lib.splitString " " r) 1) (
                lib.filter (r: lib.hasPrefix "d " r) rules
              );
              guaranteed = inst: created ++ [ inst.workingDirectory ];
              ok =
                inst: unit:
                lib.all (p: lib.elem p (guaranteed inst)) unit.serviceConfig.ReadWritePaths;
              insts = pair.config.services.son-of-anton.instances;
            in
            ok insts.work workUnit && ok insts.play playUnit
          );
          pkgs.runCommand "son-of-anton-nixos-module" { } ''
            echo "PASS: NixOS module units correct (single + two-instance isolation)"
            mkdir -p $out
          '';

        # ── The Home Manager module ──────────────────────────────────────
        # The activation merges settings into config.toml and environmentFiles
        # into .env; the CLI lands on the profile with SON_OF_ANTON_HOME set.
        home-manager-module =
          let
            eval = evalHomeModule {
              enable = true;
              settings.model.default = "auto";
              environmentFiles = [ "/run/agenix/check-env" ];
              memoryRules = [
                {
                  id = "check-rule";
                  text = "Ask before deleting mail.";
                }
              ];
            };
            activation = eval.config.home.activation.son-of-antonAgentSetup.data;
          in
          assert lib.hasInfix "--state /home/son-of-anton-check/.son-of-anton/.nix-managed.json" activation;
          assert lib.hasInfix "/run/agenix/check-env" activation;
          assert lib.hasInfix "memories/.nix-rules.json" activation;
          assert eval.config.home.sessionVariables.SON_OF_ANTON_HOME
            == "/home/son-of-anton-check/.son-of-anton";

          pkgs.runCommand "son-of-anton-home-manager-module" { } ''
            echo "PASS: Home Manager module activation and environment are correct"
            mkdir -p $out
          '';

        # ── memoryRulesFile ──────────────────────────────────────────────
        # The activation names the runtime path and carries no rule text (the
        # text would be in the store); setting both options fails an assertion
        # in both modules.
        rules-file =
          let
            secretText = "ZZ-secret-standing-rule-text";
            fileEval = evalHomeModule {
              enable = true;
              memoryRulesFile = "/run/agenix/check-rules";
            };
            activation = fileEval.config.home.activation.son-of-antonAgentSetup.data;
            failed = eval: lib.filter (a: !a.assertion) eval.config.assertions;
            bothHome = evalHomeModule {
              enable = true;
              memoryRulesFile = "/run/agenix/check-rules";
              memoryRules = [
                {
                  id = "x";
                  text = secretText;
                }
              ];
            };
            bothNixos = evalNixosModule {
              main = {
                enable = true;
                memoryRulesFile = "/run/agenix/check-rules";
                memoryRules = [
                  {
                    id = "x";
                    text = secretText;
                  }
                ];
              };
            };
            mentions = eval: lib.any (a: lib.hasInfix "memoryRulesFile" a.message) (failed eval);
          in
          assert lib.hasInfix "memories/rules.toml" activation;
          assert lib.hasInfix "/run/agenix/check-rules" activation;
          assert !(lib.hasInfix "son-of-anton-rules.toml" activation);
          assert failed fileEval == [ ];
          assert !(builtins.tryEval bothHome.config.home.activationPackage.drvPath).success;
          assert mentions bothNixos;
          pkgs.runCommand "son-of-anton-rules-file" { } ''
            echo "PASS: memoryRulesFile is referenced by path; both options is refused"
            mkdir -p $out
          '';

        # ── The memoryRules seed ─────────────────────────────────────────
        # The merge script runs from the store the way activation runs it:
        # it creates rules.toml and its state file, and a second run with
        # nothing changed leaves both as they were.
        rules-seed =
          let
            script = pkgs.callPackage ./rulesMergeScript.nix { };
            declared = (pkgs.formats.toml { }).generate "rules-declared.toml" {
              rule = [
                {
                  id = "check-rule";
                  text = "Ask before deleting mail.";
                  scope = "common";
                  source = "user";
                }
              ];
            };
          in
          pkgs.runCommand "son-of-anton-rules-seed" { } ''
            set -euo pipefail
            mkdir -p mem
            ${script} ${declared} mem/rules.toml --state mem/.nix-rules.json
            grep -q 'id = "check-rule"' mem/rules.toml \
              || (echo "FAIL: rule not seeded"; exit 1)
            test -f mem/.nix-rules.json
            cp mem/rules.toml before.toml
            ${script} ${declared} mem/rules.toml --state mem/.nix-rules.json
            cmp before.toml mem/rules.toml \
              || (echo "FAIL: second run changed rules.toml"; exit 1)
            echo "PASS: memoryRules seed creates and re-merges rules.toml"
            mkdir -p $out
          '';

        # ── Bundled assets + venv smoke ──────────────────────────────────
        bundled-assets = pkgs.runCommand "son-of-anton-bundled-assets" { } ''
          set -euo pipefail
          test -d ${son-of-anton}/share/son-of-anton/locales \
            || (echo "FAIL: bundled locales missing"; exit 1)
          ${son-of-anton}/bin/son-of-anton --help 2>&1 | grep -q "gateway" \
            || (echo "FAIL: gateway subcommand missing"; exit 1)
          echo "PASS: bundled locales present, binary works"
          mkdir -p $out
        '';

        venv-imports = pkgs.runCommand "son-of-anton-venv-imports" { } ''
          set -euo pipefail
          export HOME=$TMPDIR
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
import agent.system_prompt
import agent.prompt_builder
import gateway.platforms.signal
import autophysicist.run
import autophysicist.runner
import agent.auxiliary_client
print('imports ok')
" || (echo "FAIL: core modules do not import from the sealed venv"; exit 1)
          echo "PASS: core modules import from the sealed venv"
          mkdir -p $out
        '';
      };
    };
}

# nix/checks.nix — build-time verification tests
{ inputs, ... }:
{
  perSystem =
    { pkgs, lib, self', ... }:
    let
      son-of-anton = self'.packages.default;

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
    in
    {
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

        # ── An instance on soa-d ──────────────────────────────────────────
        # Every instance's unit runs the Go daemon (backend = "soa-d" is the
        # only value, and the default), and the module seeds [daemon] for it;
        # an explicit setting still wins. The package ships soa-d wrapped with
        # the runtime tools and the harness python for execute_code.
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
              };
            };
            sc = name: units."son-of-anton-${name}".serviceConfig;
            units = eval.config.systemd.services;
            exec = name: execStr units."son-of-anton-${name}".serviceConfig.ExecStart;
            inst = eval.config.services.son-of-anton.instances;
          in
          assert lib.hasSuffix "/bin/soa-d serve" (exec "play");
          assert lib.hasSuffix "/bin/soa-d serve" (exec "work");
          assert lib.hasSuffix "/bin/soa-d serve" (exec "house");
          assert lib.hasSuffix "/bin/soa-d serve" (exec "shop");
          assert inst.house.backend == "soa-d";
          assert inst.play.settings.daemon == { instance = "play"; signal = true; cron = true; autostart = false; import_python = true; };
          assert inst.work.settings.daemon == { instance = "work"; signal = true; cron = false; autostart = false; import_python = true; };
          # An owner's own account on soa-d: no filesystem sandbox, never privileges.
          assert (sc "play").ProtectSystem == false && (sc "play").PrivateTmp == false;
          assert (sc "play").NoNewPrivileges;
          # A service account keeps the sandbox.
          assert (sc "shop").ProtectSystem == "strict" && (sc "shop").PrivateTmp && (sc "shop").NoNewPrivileges;
          assert inst.work.settings.model.default == "auto";
          assert inst.house.settings.daemon.instance == "house";
          assert units."son-of-anton-play".environment.SON_OF_ANTON_HOME == "/home/e-play/.son-of-anton";
          # Settings changes restart the process: the rendered config is a trigger,
          # so two instances with different settings have different triggers.
          assert lib.any (t: lib.hasSuffix "son-of-anton-config.toml" (toString t)) units."son-of-anton-play".restartTriggers;
          assert units."son-of-anton-play".restartTriggers != units."son-of-anton-work".restartTriggers;
          pkgs.runCommand "son-of-anton-soad-backend" { } ''
            test -x ${son-of-anton}/bin/soa-d
            grep -q SON_OF_ANTON_PYTHON ${son-of-anton}/bin/soa-d
            ! grep -qi venv ${son-of-anton}/bin/soa-d ${son-of-anton}/bin/son-of-anton
            grep -q ripgrep ${son-of-anton}/bin/soa-d
            ${son-of-anton}/bin/soa-d version
            echo "PASS: backend = soa-d runs the daemon, seeds [daemon], and soa-d is wrapped"
            mkdir -p $out
            echo "ok" > $out/result
          '';

        # ── The configuration shape /etc/nixos uses ──────────────────────
        # Several instances with the full option surface (accounts, nested
        # settings, secrets files, git deploy key, protected paths, extra
        # packages), and the package's own override.
        nixos-owner-shape =
          let
            owner = name: {
              enable = true;
              user = "e-${name}";
              createUser = false;
              managedAccount = true;
              son-of-antonHome = "/home/e-${name}/.son-of-anton";
              workingDirectory = "/home/e-${name}";
              stateDir = "/home/e-${name}/.local/state/soa";
              environmentFiles = [ "/run/agenix/${name}-env" ];
              environment.SOA_CHECK = "1";
              extraPackages = [ pkgs.hello ];
              protectedPaths = [ ".gnupg" ".config/secret" ];
              git.github = "/run/agenix/${name}-github";
              memoryRulesFile = "/run/agenix/${name}-rules";
              pruneUnmanagedSettings = true;
              backend = "soa-d";
              settings = {
                model.default = "auto";
                mcp_servers.search = {
                  url = "http://127.0.0.1:9000/mcp";
                  tools.include = [ "web_search" ];
                };
                gateway.model = "radiance/qwen3.8-27b";
                relay.worker_hours = {
                  start = "08:00";
                  end = "22:00";
                };
                models.renamed = {
                  old-name = "new-name";
                };
              };
            };
            eval = evalNixosModule {
              play = owner "play";
              work = owner "work" // { settings.daemon.cron = false; };
            };
            units = eval.config.systemd.services;
            u = name: units."son-of-anton-${name}";
            overridden = son-of-anton.override { extraPackages = [ pkgs.hello ]; };
            activation = lib.concatStringsSep "\n" (
              map (v: if builtins.isString v then v else v.text) (
                builtins.attrValues (
                  lib.filterAttrs (n: _: lib.hasPrefix "son-of-anton" n) eval.config.system.activationScripts
                )
              )
            );
          in
          assert lib.hasSuffix "/bin/soa-d serve" (execStr (u "play").serviceConfig.ExecStart);
          assert lib.hasSuffix "/bin/soa-d serve" (execStr (u "work").serviceConfig.ExecStart);
          assert (u "play").serviceConfig.NoNewPrivileges && (u "work").serviceConfig.NoNewPrivileges;
          assert (u "play").serviceConfig.ProtectSystem == false && (u "play").serviceConfig.PrivateTmp == false;
          assert lib.elem "-/home/e-play/.config/secret" (u "play").serviceConfig.InaccessiblePaths;
          assert lib.hasInfix "hello" (toString (u "play").path);
          assert lib.hasInfix "/bin/soa-d config merge " activation && lib.hasInfix "/bin/soa-d rules merge " activation;
          assert lib.hasInfix "--adopt" activation;
          assert lib.hasInfix "/run/agenix/play-rules" activation;
          assert (u "play").restartTriggers != (u "work").restartTriggers;
          pkgs.runCommand "son-of-anton-owner-shape" { } ''
            grep -q 'hello' ${overridden}/bin/soa-d
            grep -q 'hello' ${overridden}/bin/son-of-anton
            ${overridden}/bin/soa-d version
            echo "PASS: the owner's option surface evaluates; override extraPackages works"
            mkdir -p $out
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
          assert lib.hasSuffix "/bin/soa-d serve" exec;

          # Everything the merge path writes must be chowned to the instance.
          assert lib.hasInfix
            "chown e-work:son-of-anton /home/e-work/.son-of-anton/config.toml"
            activation;
          assert lib.hasInfix "/home/e-work/.son-of-anton/.nix-managed.json" activation;
          assert lib.hasInfix "/bin/soa-d config merge " activation;
          assert lib.hasInfix "/bin/soa-d rules merge " activation;
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
          assert lib.hasInfix "/bin/soa-d config merge " activation;
          assert lib.hasInfix "/bin/soa-d rules merge " activation;
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
        # The merge runs from the store the way activation runs it:
        # it creates rules.toml and its state file, and a second run with
        # nothing changed leaves both as they were.
        rules-seed =
          let
            script = "${self'.packages.soa-d}/bin/soa-d rules merge";
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

        # ── The package, smoke-run ───────────────────────────────────────
        # Both commands exist and run, and `problem` reaches the physics mode
        # through the harness python (SON_OF_ANTON_PYTHON), not a venv.
        package-smoke = pkgs.runCommand "son-of-anton-package-smoke" { } ''
          set -euo pipefail
          export HOME=$TMPDIR
          ${son-of-anton}/bin/son-of-anton --version
          ${son-of-anton}/bin/soa-d version
          ${son-of-anton}/bin/son-of-anton problem --help | grep -q create
          ${son-of-anton}/bin/son-of-anton problem run --help | grep -q -- --max-iterations
          grep -q SON_OF_ANTON_PYTHON ${son-of-anton}/bin/son-of-anton
          grep -q SOA_D_BIN ${son-of-anton}/bin/son-of-anton
          echo "PASS: son-of-anton and soa-d run; problem reaches the physics harness"
          mkdir -p $out
        '';

        # ── The physics mode's tests ─────────────────────────────────────
        # Run under the harness environment (python3 with autophysicist and
        # pytest), from a copy of the source tree so the tests see the source,
        # not the installed copy. git and bubblewrap are what the mode's runs
        # shell out to.
        physics-tests = pkgs.runCommand "son-of-anton-physics-tests" {
          nativeBuildInputs = [ self'.packages.physics-harness-test pkgs.git pkgs.bubblewrap ];
        } ''
          set -euo pipefail
          export HOME=$TMPDIR
          # The OpenAI client builds a TLS context at construction, even for
          # the tests' loopback endpoints; the sandbox has no CA bundle.
          export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
          cp -r ${../autophysicist} autophysicist
          cp -r ${../problems} problems
          chmod -R u+w .
          python3 -m pytest -p no:cacheprovider -q autophysicist/tests
          echo "PASS: physics tests"
          mkdir -p $out
        '';
      };
    };
}

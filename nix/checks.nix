# nix/checks.nix — what `nix flake check` verifies.
#
# Module checks evaluate the real NixOS and Home Manager module systems and
# test the result with `holds`, which names the claim that fails. A check that
# needs a build (running a binary, the Go and Python test suites) is a
# derivation; the rest are evaluation-only and end in a trivial one.
{ inputs, ... }:
{
  perSystem =
    {
      pkgs,
      lib,
      self',
      system,
      ...
    }:
    let
      package = self'.packages.default;
      soad = self'.packages.soa-d;
      version = import ./version.nix { inherit (inputs) self; };

      # ── Helpers ────────────────────────────────────────────────────────────
      # holds [ [ "what" cond ] ... ] result: result if every cond is true,
      # else an evaluation error that names the first false one.
      holds =
        claims: result:
        lib.foldr (
          claim: rest: if builtins.elemAt claim 1 then rest else throw "check failed: ${builtins.head claim}"
        ) result claims;

      # An evaluation-only check: the claims, then a derivation that says so.
      evalCheck =
        name: claims: message:
        holds claims (
          pkgs.runCommand name { } ''
            echo "ok: ${message}"
            mkdir -p $out
          ''
        );

      # A check that runs a script against the built packages.
      scriptCheck =
        name: script:
        pkgs.runCommand name { } ''
          set -euo pipefail
          export HOME=$TMPDIR
          ${script}
          mkdir -p $out
        '';

      execOf = unit: lib.concatStringsSep " " (lib.toList unit.serviceConfig.ExecStart);

      # ── Evaluating the modules ─────────────────────────────────────────────
      nixos =
        instances:
        inputs.nixpkgs.lib.evalModules {
          modules = import "${inputs.nixpkgs}/nixos/modules/module-list.nix" ++ [
            inputs.self.nixosModules.default
            { _module.args.lib = inputs.nixpkgs.lib; }
            { nixpkgs.hostPlatform = system; }
            {
              system.stateVersion = "24.11";
              boot.loader.grub.enable = false;
              fileSystems."/" = {
                device = "/dev/null";
                fsType = "ext4";
              };
            }
            { services.son-of-anton = { inherit instances; }; }
          ];
        };

      hm =
        settings:
        inputs.home-manager.lib.homeManagerConfiguration {
          inherit pkgs;
          modules = [
            inputs.self.homeManagerModules.default
            {
              home = {
                username = "soa-checker";
                homeDirectory = "/home/soa-checker";
                stateVersion = "24.11";
              };
              services.son-of-anton = settings;
            }
          ];
        };

      unitsOf = eval: eval.config.systemd.services;
      activationOf =
        eval:
        lib.concatStringsSep "\n" (
          lib.mapAttrsToList (_: s: if builtins.isString s then s else s.text) (
            lib.filterAttrs (n: _: lib.hasPrefix "son-of-anton" n) eval.config.system.activationScripts
          )
        );
      hmActivationOf = eval: eval.config.home.activation.sonOfAntonState.data;
      failedAssertions = eval: lib.filter (a: !a.assertion) eval.config.assertions;

      # An instance running as a login account (what /etc/nixos does).
      account = name: {
        enable = true;
        user = "e-${name}";
        createUser = false;
        managedAccount = true;
        son-of-antonHome = "/home/e-${name}/.son-of-anton";
        workingDirectory = "/home/e-${name}";
      };
    in
    {
      checks = {
        # ── Packages ─────────────────────────────────────────────────────────
        # The default package evaluates on every system, so a missing platform
        # is caught without an aarch64 builder.
        cross-eval =
          let
            systems = builtins.filter (s: inputs.self.packages ? ${s}) [
              "x86_64-linux"
              "aarch64-linux"
            ];
            broken = builtins.filter (
              s: !(builtins.tryEval (builtins.seq inputs.self.packages.${s}.default.drvPath true)).success
            ) systems;
          in
          evalCheck "son-of-anton-cross-eval" [
            [ "package evaluates on ${lib.concatStringsSep ", " broken}" (broken == [ ]) ]
          ] "package evaluates on ${toString (builtins.length systems)} platforms";

        build-package = pkgs.runCommand "son-of-anton-build-package" { } ''
          echo "ok: the package builds (${package})"
          mkdir -p $out
        '';

        build-devshell = pkgs.runCommand "son-of-anton-build-devshell" { } ''
          echo "ok: the dev shell builds (${self'.devShells.default})"
          mkdir -p $out
        '';

        # Building soa-d runs the Go tests (its check phase); this also runs
        # the binary, with the tools the terminal and search tools shell out to.
        build-soa-d =
          pkgs.runCommand "soa-d-build"
            {
              nativeBuildInputs = [
                pkgs.bash
                pkgs.ripgrep
                pkgs.coreutils
              ];
            }
            ''
              test "$(${soad}/bin/soa-d version)" = "soa-d ${version}"
              test "$(${soad}/bin/son-of-anton --version)" = "son-of-anton ${version}"
              mkdir -p $out
            '';

        # Both commands run and the wrapper has what they need. `problem`
        # reaches the physics mode through the harness Python, not a venv.
        package-smoke = scriptCheck "son-of-anton-package-smoke" ''
          test "$(${package}/bin/son-of-anton --version)" = "son-of-anton ${version}"
          test "$(${package}/bin/soa-d version)" = "soa-d ${version}"
          ${package}/bin/son-of-anton problem --help | grep -q create
          ${package}/bin/son-of-anton problem run --help | grep -q -- --max-iterations
          for wrapper in soa-d son-of-anton; do
            grep -q SON_OF_ANTON_PYTHON ${package}/bin/$wrapper
            ! grep -qi venv ${package}/bin/$wrapper
          done
          grep -q SOA_D_BIN ${package}/bin/son-of-anton
          grep -q ripgrep ${package}/bin/soa-d
          echo "ok: both commands run, report ${version}, and reach the physics harness"
        '';

        # ── NixOS module ─────────────────────────────────────────────────────
        # Four instances: three login accounts (one with settings of its own)
        # and a service account that keeps the sandbox.
        nixos-soad-backend =
          let
            eval = nixos {
              play = account "play";
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
            units = unitsOf eval;
            unit = n: units."son-of-anton-${n}";
            sc = n: (unit n).serviceConfig;
            inst = eval.config.services.son-of-anton.instances;
          in
          evalCheck "son-of-anton-soad-backend"
            (
              map (n: [ "${n} runs soa-d serve" (lib.hasSuffix "/bin/soa-d serve" (execOf (unit n))) ]) [
                "play"
                "work"
                "house"
                "shop"
              ]
              ++ [
                [ "the backend option is gone" (!(inst.house ? backend)) ]
                [
                  "play's [daemon] is seeded"
                  (
                    inst.play.settings.daemon == {
                      instance = "play";
                      signal = true;
                      cron = true;
                      autostart = false;
                      import_python = true;
                    }
                  )
                ]
                [
                  "an explicit daemon key wins and the rest stays"
                  (
                    inst.work.settings.daemon == {
                      instance = "work";
                      signal = true;
                      cron = false;
                      autostart = false;
                      import_python = true;
                    }
                  )
                ]
                [ "other settings pass through" (inst.work.settings.model.default == "auto") ]
                [ "instance name is seeded" (inst.house.settings.daemon.instance == "house") ]
                [
                  "an owner's account has no filesystem sandbox"
                  ((sc "play").ProtectSystem == false && (sc "play").PrivateTmp == false)
                ]
                [ "an owner's account still has NoNewPrivileges" (sc "play").NoNewPrivileges ]
                [
                  "a service account keeps the sandbox"
                  ((sc "shop").ProtectSystem == "strict" && (sc "shop").PrivateTmp && (sc "shop").NoNewPrivileges)
                ]
                [
                  "SON_OF_ANTON_HOME is the instance's"
                  ((unit "play").environment.SON_OF_ANTON_HOME == "/home/e-play/.son-of-anton")
                ]
                [
                  "the rendered config is a restart trigger"
                  (lib.any (t: lib.hasSuffix "son-of-anton-config.toml" (toString t)) (unit "play").restartTriggers)
                ]
                [
                  "different settings, different triggers"
                  ((unit "play").restartTriggers != (unit "work").restartTriggers)
                ]
              ]
            )
            "instances run the daemon, seed [daemon], and keep or drop the sandbox by account kind";

        # The configuration shape /etc/nixos uses: the whole option surface on
        # two instances, and the package's own override.
        nixos-owner-shape =
          let
            owner = name: account name // {
              stateDir = "/home/e-${name}/.local/state/soa";
              environmentFiles = [ "/run/agenix/${name}-env" ];
              environment.SOA_CHECK = "1";
              extraPackages = [ pkgs.hello ];
              protectedPaths = [
                ".gnupg"
                ".config/secret"
              ];
              git.github = "/run/agenix/${name}-github";
              memoryRulesFile = "/run/agenix/${name}-rules";
              pruneUnmanagedSettings = true;
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
                models.renamed.old-name = "new-name";
              };
            };
            eval = nixos {
              play = owner "play";
              work = owner "work" // { settings.daemon.cron = false; };
            };
            u = n: (unitsOf eval)."son-of-anton-${n}";
            activation = activationOf eval;
            overridden = package.override { extraPackages = [ pkgs.hello ]; };
          in
          holds
            [
              [ "play serves" (lib.hasSuffix "/bin/soa-d serve" (execOf (u "play"))) ]
              [ "work serves" (lib.hasSuffix "/bin/soa-d serve" (execOf (u "work"))) ]
              [ "NoNewPrivileges on both" ((u "play").serviceConfig.NoNewPrivileges && (u "work").serviceConfig.NoNewPrivileges) ]
              [
                "no sandbox for the owner's account"
                ((u "play").serviceConfig.ProtectSystem == false && (u "play").serviceConfig.PrivateTmp == false)
              ]
              [
                "a protected path resolves against HOME"
                (lib.elem "-/home/e-play/.config/secret" (u "play").serviceConfig.InaccessiblePaths)
              ]
              [ "extraPackages reach the unit PATH" (lib.hasInfix "hello" (toString (u "play").path)) ]
              [ "config merge in activation" (lib.hasInfix "/bin/soa-d config merge " activation) ]
              [ "rules merge in activation" (lib.hasInfix "/bin/soa-d rules merge " activation) ]
              [ "pruneUnmanagedSettings adopts" (lib.hasInfix "--adopt" activation) ]
              [ "the rules file is named by path" (lib.hasInfix "/run/agenix/play-rules" activation) ]
              [ "the git key is installed" (lib.hasInfix "/run/agenix/play-github" activation) ]
              [ "different settings, different triggers" ((u "play").restartTriggers != (u "work").restartTriggers) ]
            ]
            (
              scriptCheck "son-of-anton-owner-shape" ''
                for wrapper in soa-d son-of-anton; do
                  grep -q hello ${overridden}/bin/$wrapper
                done
                ${overridden}/bin/soa-d version
                echo "ok: the owner's option surface evaluates; override extraPackages works"
              ''
            );

        nixos-module =
          let
            single = nixos { main.enable = true; };
            pair = nixos {
              work = account "work";
              play = account "play";
            };
            units = unitsOf pair;
            work = units.son-of-anton-work;
            play = units.son-of-anton-play;
            rules = pair.config.systemd.tmpfiles.rules;
            activation = activationOf pair;
            created = map (r: lib.elemAt (lib.splitString " " r) 1) (lib.filter (r: lib.hasPrefix "d " r) rules);
            instances = pair.config.services.son-of-anton.instances;
            # Under ProtectSystem=strict a missing writable path stops the unit
            # (226/NAMESPACE). Each must be the account's existing home or
            # something tmpfiles creates.
            startable = i: u: lib.all (p: lib.elem p (created ++ [ i.workingDirectory ])) u.serviceConfig.ReadWritePaths;
            refused = nixos {
              a = account "a" // {
                memoryRules = [ { id = "x"; text = "y"; } ];
                memoryRulesFile = "/p";
              };
            };
            sharedUser = nixos {
              a = account "a";
              b = account "b" // { user = "e-a"; };
            };
            sharedHome = nixos {
              a = account "a";
              b = account "b" // { son-of-antonHome = "/home/e-a/.son-of-anton"; };
            };
            twoExporters = nixos {
              a = account "a" // { addToSystemPackages = true; };
              b = account "b" // { addToSystemPackages = true; };
            };
            says = eval: word: lib.any (a: lib.hasInfix word a.message) (failedAssertions eval);
          in
          evalCheck "son-of-anton-nixos-module"
            [
              [ "a bare instance serves" (lib.hasSuffix "/bin/soa-d serve" (execOf single.config.systemd.services.son-of-anton-main)) ]
              # What the merge writes is owned by the instance: activation runs
              # as root, and a root-owned config.toml is unreadable to the unit.
              [
                "work's config.toml is chowned"
                (lib.hasInfix "chown e-work:son-of-anton /home/e-work/.son-of-anton/config.toml" activation)
              ]
              [
                "play's config.toml is chowned"
                (lib.hasInfix "chown e-play:son-of-anton /home/e-play/.son-of-anton/config.toml" activation)
              ]
              [ "the state record is written" (lib.hasInfix "/home/e-work/.son-of-anton/.nix-managed.json" activation) ]
              [ "config merge in activation" (lib.hasInfix "/bin/soa-d config merge " activation) ]
              [ "rules merge in activation" (lib.hasInfix "/bin/soa-d rules merge " activation) ]
              [ "both units exist" (units ? "son-of-anton-work" && units ? "son-of-anton-play") ]
              [ "work's home" (work.environment.SON_OF_ANTON_HOME == "/home/e-work/.son-of-anton") ]
              [ "play's home" (play.environment.SON_OF_ANTON_HOME == "/home/e-play/.son-of-anton") ]
              [ "work's user" (work.serviceConfig.User == "e-work") ]
              [ "play's user" (play.serviceConfig.User == "e-play") ]
              [ "a human home stays owner-only" (work.serviceConfig.UMask == "0077") ]
              [
                "each instance has its own Signal lock directory"
                (work.environment.SON_OF_ANTON_GATEWAY_LOCK_DIR != play.environment.SON_OF_ANTON_GATEWAY_LOCK_DIR)
              ]
              [ "gnupg is denied for work" (lib.elem "-/home/e-work/.gnupg" work.serviceConfig.InaccessiblePaths) ]
              [ "git-credentials is denied for work" (lib.elem "-/home/e-work/.git-credentials" work.serviceConfig.InaccessiblePaths) ]
              [ "gnupg is denied for play" (lib.elem "-/home/e-play/.gnupg" play.serviceConfig.InaccessiblePaths) ]
              # ~/.ssh stays reachable so that git over SSH works.
              [ "ssh is not denied" (!(lib.any (p: lib.hasSuffix "/.ssh" p) work.serviceConfig.InaccessiblePaths)) ]
              # A 2770 rule on the home would make sshd refuse authorized_keys.
              [ "no tmpfiles rule for work's home" (!(lib.any (r: lib.hasInfix "d /home/e-work " r) rules)) ]
              [ "no tmpfiles rule for play's home" (!(lib.any (r: lib.hasInfix "d /home/e-play " r) rules)) ]
              [ "work can start" (startable instances.work work) ]
              [ "play can start" (startable instances.play play) ]
              [ "both rule sources is refused" (says refused "memoryRulesFile") ]
              [ "one user twice is refused" (says sharedUser "same user") ]
              [ "one home twice is refused" (says sharedHome "SON_OF_ANTON_HOME") ]
              [ "two exporters is refused" (says twoExporters "addToSystemPackages") ]
              [ "a valid pair passes every assertion" (failedAssertions pair == [ ]) ]
            ]
            "units are correct for one instance and isolated across two";

        # ── Home Manager module ──────────────────────────────────────────────
        home-manager-module =
          let
            eval = hm {
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
            activation = hmActivationOf eval;
          in
          evalCheck "son-of-anton-home-manager-module"
            [
              [
                "the state record is beside config.toml"
                (lib.hasInfix "--state /home/soa-checker/.son-of-anton/.nix-managed.json" activation)
              ]
              [ "config merge in activation" (lib.hasInfix "/bin/soa-d config merge " activation) ]
              [ "rules merge in activation" (lib.hasInfix "/bin/soa-d rules merge " activation) ]
              [ "secrets file is read" (lib.hasInfix "/run/agenix/check-env" activation) ]
              [ "rules state record" (lib.hasInfix "memories/.nix-rules.json" activation) ]
              [
                "session variable"
                (eval.config.home.sessionVariables.SON_OF_ANTON_HOME == "/home/soa-checker/.son-of-anton")
              ]
            ]
            "activation and session environment are correct";

        # ── memoryRulesFile ──────────────────────────────────────────────────
        # Activation names the run-time path and carries no rule text (that
        # would be in the store); both options together fail an assertion.
        rules-file =
          let
            secret = "ZZ-secret-standing-rule-text";
            viaFile = hm {
              enable = true;
              memoryRulesFile = "/run/agenix/check-rules";
            };
            both = {
              memoryRulesFile = "/run/agenix/check-rules";
              memoryRules = [
                {
                  id = "x";
                  text = secret;
                }
              ];
            };
            activation = hmActivationOf viaFile;
            mentions = eval: lib.any (a: lib.hasInfix "memoryRulesFile" a.message) (failedAssertions eval);
          in
          evalCheck "son-of-anton-rules-file"
            [
              [ "rules.toml is the target" (lib.hasInfix "memories/rules.toml" activation) ]
              [ "the run-time path is used" (lib.hasInfix "/run/agenix/check-rules" activation) ]
              [ "no rendered rules file" (!(lib.hasInfix "son-of-anton-rules.toml" activation)) ]
              [ "a lone memoryRulesFile passes" (failedAssertions viaFile == [ ]) ]
              [
                "both options fail in Home Manager"
                (!(builtins.tryEval (hm ({ enable = true; } // both)).config.home.activationPackage.drvPath).success)
              ]
              [ "both options fail in NixOS" (mentions (nixos { main = { enable = true; } // both; })) ]
            ]
            "memoryRulesFile is referenced by path; both options is refused";

        # The rules merge, run from the store as activation runs it: it creates
        # rules.toml and its record, and a second run changes nothing.
        rules-seed =
          let
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
            merge = "${soad}/bin/soa-d rules merge ${declared} mem/rules.toml --state mem/.nix-rules.json";
          in
          scriptCheck "son-of-anton-rules-seed" ''
            mkdir mem
            ${merge}
            grep -q 'id = "check-rule"' mem/rules.toml || { echo "FAIL: rule not seeded"; exit 1; }
            test -f mem/.nix-rules.json
            cp mem/rules.toml before.toml
            ${merge}
            cmp before.toml mem/rules.toml || { echo "FAIL: a second run changed rules.toml"; exit 1; }
            echo "ok: memoryRules seed creates and re-merges rules.toml"
          '';

        # ── Physics mode ─────────────────────────────────────────────────────
        # pytest under the harness environment, on a copy of the source tree
        # (so the tests import the source, not the installed copy). The mode's
        # runs shell out to git and bubblewrap.
        physics-tests =
          pkgs.runCommand "son-of-anton-physics-tests"
            {
              nativeBuildInputs = [
                self'.packages.physics-harness-test
                pkgs.git
                pkgs.bubblewrap
              ];
            }
            ''
              set -euo pipefail
              export HOME=$TMPDIR
              # The OpenAI client builds a TLS context even for loopback
              # endpoints, and the sandbox has no CA bundle.
              export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
              cp -r ${../autophysicist} autophysicist
              cp -r ${../problems} problems
              chmod -R u+w .
              python3 -m pytest -p no:cacheprovider -q autophysicist/tests
              echo "ok: physics tests"
              mkdir -p $out
            '';
      };
    };
}

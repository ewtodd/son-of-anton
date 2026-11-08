# nix/nixosModules.nix — services.son-of-anton.instances on NixOS.
#
# Each enabled instance is a systemd system service with its own user and its
# own SON_OF_ANTON_HOME. System services, not Home Manager user ones, so that
# they come up at boot with no login. An instance can also run as an existing
# human account (managedAccount): the owner's terminal agent and the account's
# Signal sessions then share one state.
#
#   services.son-of-anton.instances.work = {
#     enable = true;
#     user = "e-work";
#     createUser = false;
#     managedAccount = true;
#     son-of-antonHome = "/home/e-work/.son-of-anton";
#     workingDirectory = "/home/e-work";
#     environmentFiles = [ config.age.secrets."son-of-anton-env".path ];
#   };
#
# Options are in options.nix, rendering and state setup in state.nix. This file
# keeps what needs root or a system identity.
{ inputs, ... }:
{
  flake.nixosModules.default =
    {
      config,
      lib,
      pkgs,
      ...
    }:
    let
      options' = import ./options.nix { inherit lib; };
      state = import ./state.nix { inherit lib; };

      package = inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      # The tools every instance's agent gets by default (owner, 2026-11-06):
      # the desktop nudge (notify-send), and Nix maintenance — the build path,
      # its formatter, dead-option sweeping — the things that lived in
      # extraPackages on every deployed instance anyway. extraPackages adds
      # on top of these, per instance.
      defaultExtras = [
        pkgs.libnotify
        pkgs.nixfmt
        pkgs.deadnix
        config.nix.package
      ];
      instances = lib.filterAttrs (_: i: i.enable) config.services.son-of-anton.instances;
      # The instance view for the daemon's PATH and the wrapped commands:
      # users.users keeps the owner's own extraPackages only, so an instance
      # on an existing account still defines no user of its own.
      withExtras = i: i // { extraPackages = i.extraPackages ++ defaultExtras; };
      each = f: lib.mapAttrsToList f instances;

      instanceModule =
        { name, config, ... }:
        {
          options =
            options'.common {
              inherit package;
              packageText = lib.literalExpression "son-of-anton.packages.\${system}.default";
              # From `name`, not from stateDir: a default that reads its own
              # submodule's config cannot resolve. An instance that moves its
              # state sets workingDirectory itself.
              workingDirectory = "/var/lib/son-of-anton-${name}/workspace";
              workingDirectoryText = lib.literalExpression ''"/var/lib/son-of-anton-''${name}/workspace"'';
            }
            // options'.instanceOnly { inherit name; };

          config.settings = state.daemonDefaults { instance = name; };
        };

      # What follows from an instance's options, in one place.
      shape =
        i:
        let
          own = i.managedAccount;
          # The unit's HOME. A managed account's is its real home, which the
          # module must never create or re-mode.
          home = if own then i.workingDirectory else i.stateDir;
          sshDir = "${home}/.ssh";
        in
        {
          inherit home sshDir;
          sshKey = "${sshDir}/id_github";
          sshConfig = "${sshDir}/config-github";
          # A shared service group gets group access; an account owns its home
          # outright.
          dirMode = if own then "0700" else "2770";
          umask = if own then "0077" else "0007";
          modes = {
            config =
              if own then
                "0600"
              else if i.addToSystemPackages then
                "0660"
              else
                "0640";
            env = if own then "0600" else "0640";
            marker = "0644";
          };
          owner = {
            inherit (i) user group;
          };
        };

      # Dedicated file names, so that a managed account's own ~/.ssh/config and
      # default identity are left alone; git is pointed at these through
      # GIT_SSH_COMMAND.
      sshConfigText = s: ''
        Host github.com
          IdentityFile ${s.sshKey}
          IdentitiesOnly yes
          User git
          StrictHostKeyChecking accept-new
      '';

      # The view socket's directory (daemon/phases/12-view-socket.md): outside
      # the home, which a viewer cannot traverse. client.ViewSocketPath names
      # the same path from the instance name.
      viewDir = name: "/run/son-of-anton-${name}";
      viewArgs =
        name: i:
        lib.optionals (i.viewerGroup != null) [
          "--view-socket"
          "${viewDir name}/view.sock"
          "--view-group"
          i.viewerGroup
        ];

      unitFor =
        name: i:
        let
          s = shape i;
        in
        {
          description = "Son of Anton daemon (${name})";
          wantedBy = [ "multi-user.target" ];
          after = [ "network-online.target" ];
          wants = [ "network-online.target" ];

          # The program reads its .env itself; activation writes it, so the
          # unit has no EnvironmentFile.
          environment = {
            HOME = s.home;
            # Signal's adapter locks on the account number so that two daemons
            # do not listen on one number. Several instances here share one
            # account on purpose (they are told apart by group), so each needs
            # its own lock directory, inside the home the unit can write.
            SON_OF_ANTON_GATEWAY_LOCK_DIR = "${i.son-of-antonHome}/locks";
          }
          // state.runtimeEnv i.son-of-antonHome
          // lib.optionalAttrs (i.git.github != null) {
            GIT_SSH_COMMAND = "ssh -F ${s.sshConfig} -i ${s.sshKey} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new";
          };

          serviceConfig = {
            ExecStart = lib.escapeShellArgs (state.argv i ++ viewArgs name i);
            User = i.user;
            Group = i.group;
            WorkingDirectory = i.workingDirectory;
            Restart = "always";
            RestartSec = 5;
            UMask = s.umask;

            NoNewPrivileges = true;
            ProtectSystem = "strict";
            ProtectHome = false;
            # Under strict, systemd refuses to start (226/NAMESPACE) if a
            # writable path is missing, and the module creates nothing in a
            # managed account's home. Hence the unit's HOME, not stateDir.
            # The view socket's directory too: under strict, /run is read-only.
            ReadWritePaths = lib.unique (
              [
                s.home
                i.son-of-antonHome
                i.workingDirectory
              ]
              ++ lib.optional (i.viewerGroup != null) (viewDir name)
            );
            # Applied after ReadWritePaths, so a denied path inside a writable
            # home stays denied. "-": a missing path is not an error.
            InaccessiblePaths = map (
              p: "-" + (if lib.hasPrefix "/" p then p else "${s.home}/${p}")
            ) i.protectedPaths;
            PrivateTmp = true;
          }
          # The daemon of an owner's own account is also their terminal agent
          # and needs their real filesystem. NoNewPrivileges stays, so nothing
          # it runs can gain privileges.
          // lib.optionalAttrs i.managedAccount {
            ProtectSystem = false;
            PrivateTmp = false;
          };

          restartTriggers = state.restartOn pkgs i;
          path = state.servicePath pkgs (withExtras i);
        };

      # Activation runs before tmpfiles and as root: create what the instance
      # owns, with its owner, before the shared state setup writes into it.
      activationFor =
        i:
        let
          s = shape i;
          own = "${i.user}:${i.group}";
          q = lib.escapeShellArg;
          dirs = map (d: "${i.son-of-antonHome}/${d}") state.subdirs;
        in
        ''
          mkdir -p ${q i.son-of-antonHome}
          chown ${own} ${q i.son-of-antonHome}
          chmod ${s.dirMode} ${q i.son-of-antonHome}
          ${lib.optionalString (!i.managedAccount) ''
            mkdir -p ${q "${i.stateDir}/home"} ${q i.workingDirectory}
            chown ${own} ${q i.stateDir} ${q "${i.stateDir}/home"} ${q i.workingDirectory}
            chmod 2770 ${q i.stateDir} ${q i.workingDirectory}
            chmod 0750 ${q "${i.stateDir}/home"}
            # Databases and SOUL.md must stay writable to the service group.
            find ${q i.son-of-antonHome} -maxdepth 1 \
              \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' -o -name SOUL.md \) \
              -exec chmod g+rw {} + 2>/dev/null || true
          ''}
          for dir in ${lib.concatMapStringsSep " " q dirs}; do
            mkdir -p "$dir"
            chown ${own} "$dir"
            chmod ${s.dirMode} "$dir"
            ${lib.optionalString (!i.managedAccount) ''find "$dir" -type f -exec chmod g+rw {} + 2>/dev/null || true''}
          done
          ${lib.optionalString (i.git.github != null) ''
            # Git over SSH: the key and the ssh config that names it.
            mkdir -p ${q s.sshDir}
            chmod 0700 ${q s.sshDir}
            install -m 0600 ${q i.git.github} ${q s.sshKey}
            printf %s ${q (sshConfigText s)} > ${q s.sshConfig}
            chmod 0600 ${q s.sshConfig}
            chown ${own} ${q s.sshDir} ${q s.sshKey} ${q s.sshConfig}
          ''}
          ${state.setupScript {
            inherit pkgs;
            cfg = i;
            home = i.son-of-antonHome;
            inherit (s) owner modes;
            system = "nixos";
          }}
        '';

      tmpfilesFor =
        name: i:
        let
          s = shape i;
          dir = mode: path: "d ${path} ${mode} ${i.user} ${i.group} - -";
        in
        # The view socket's directory, owner-writable only and setgid with the
        # viewer group: the socket the daemon makes in it is born with that
        # group, so the service user needs no membership of it (which would
        # let its agent watch every other instance sharing the group).
        lib.optional (i.viewerGroup != null) "d ${viewDir name} 2750 ${i.user} ${i.viewerGroup} - -"
        # A managed account's home above son-of-antonHome is never touched: a
        # 2770 rule on it would make sshd's StrictModes refuse its keys.
        ++ lib.optionals (!i.managedAccount) [
          (dir "2770" i.stateDir)
          (dir "0750" "${i.stateDir}/home")
          (dir "2770" i.workingDirectory)
        ]
        ++ [
          (dir s.dirMode i.son-of-antonHome)
          (dir "0700" "${i.son-of-antonHome}/locks")
        ]
        ++ map (d: dir s.dirMode "${i.son-of-antonHome}/${d}") state.subdirs;

      exporters = lib.filterAttrs (_: i: i.addToSystemPackages) instances;
      duplicated = f: let xs = each (_: f); in lib.length xs != lib.length (lib.unique xs);
    in
    {
      options.services.son-of-anton.instances = lib.mkOption {
        type = lib.types.attrsOf (lib.types.submodule instanceModule);
        default = { };
        description = ''
          The agent instances. Instance <name> becomes the unit
          son-of-anton-<name>, with its own user and SON_OF_ANTON_HOME.
        '';
      };

      # Each top-level key below is spelled out. Building `config` with one
      # mkMerge over the instances would make its shape depend on option
      # values, which the module system cannot resolve.
      config = {
        assertions = [
          {
            assertion = lib.all (i: i.memoryRules == [ ] || i.memoryRulesFile == null) (lib.attrValues instances);
            message = ''
              An instance sets both memoryRules and memoryRulesFile; keep one.
              memoryRules ends up in the public Nix store, memoryRulesFile is
              read from a run-time path.
            '';
          }
          {
            assertion = !duplicated (i: i.user);
            message = ''
              Two son-of-anton instances run as the same user. They would
              fight over users.users.<name>.home and write each other's state;
              give each its own `user`.
            '';
          }
          {
            assertion = !duplicated (i: i.son-of-antonHome);
            message = ''
              Two son-of-anton instances share one SON_OF_ANTON_HOME, and with
              it the database, the session index and .env. Give each its own
              `son-of-antonHome`.
            '';
          }
          {
            assertion = lib.all (i: i.viewerGroup == null || i.viewerGroup != i.group) (lib.attrValues instances);
            message = ''
              A son-of-anton instance's viewerGroup is its own group. Members
              of the instance's group can already read and write its home, so
              they are not viewers; give viewerGroup a group of its own.
            '';
          }
          {
            assertion = lib.all (
              n: instances.${n}.viewerGroup == null || builtins.match "[A-Za-z0-9_][A-Za-z0-9._-]*" n != null
            ) (lib.attrNames instances);
            message = ''
              An instance with a viewerGroup needs a name of letters, digits,
              '.', '_' and '-' (not starting with '.'): it names
              /run/son-of-anton-<name>, which `son-of-anton view <name>` dials.
            '';
          }
          {
            assertion = lib.length (lib.attrNames exporters) <= 1;
            message = ''
              These son-of-anton instances all set addToSystemPackages: ${lib.concatStringsSep ", " (lib.attrNames exporters)}.
              It exports SON_OF_ANTON_HOME through the global
              environment.variables, so at most one instance can.
            '';
          }
        ];

        users.groups = lib.mkMerge [
          (lib.listToAttrs (
            lib.concatLists (each (_: i: lib.optional i.createUser (lib.nameValuePair i.group { })))
          ))
          # A viewer group exists unless something else defines it.
          (lib.listToAttrs (
            lib.concatLists (
              each (_: i: lib.optional (i.viewerGroup != null) (lib.nameValuePair i.viewerGroup (lib.mkDefault { })))
            )
          ))
        ];

        users.users = lib.listToAttrs (
          lib.concatLists (
            each (
              _: i:
              lib.optional (i.createUser || i.extraPackages != [ ]) (
                lib.nameValuePair i.user (
                  lib.optionalAttrs i.createUser {
                    isSystemUser = true;
                    group = i.group;
                    home = i.stateDir;
                    createHome = true;
                    shell = pkgs.bashInteractive;
                  }
                  // lib.optionalAttrs (i.extraPackages != [ ]) { packages = i.extraPackages; }
                )
              )
            )
          )
        );

        environment.systemPackages = map (i: state.packageFor (withExtras i)) (lib.attrValues exporters);
        environment.variables = lib.mapAttrs' (
          _: i: lib.nameValuePair "SON_OF_ANTON_HOME" i.son-of-antonHome
        ) exporters;

        systemd.tmpfiles.rules = lib.concatLists (each tmpfilesFor);

        system.activationScripts = lib.mapAttrs' (
          name: i:
          lib.nameValuePair "son-of-anton-setup-${name}" (
            lib.stringAfter (
              [ "users" ] ++ lib.optional (config.system.activationScripts ? setupSecrets) "setupSecrets"
            ) (activationFor i)
          )
        ) instances;

        systemd.services = lib.mapAttrs' (name: i: lib.nameValuePair "son-of-anton-${name}" (unitFor name i)) instances;
      };
    };
}

# The option declarations of services.son-of-anton.
#
# Three sets, so each module takes what its scope has:
#   common        both modules (NixOS: once per instance)
#   instanceOnly  the NixOS module's per-instance identity, paths and sandbox
#   homeOnly      the Home Manager module's own two options
# A NixOS instance is a submodule of common // instanceOnly; the Home Manager
# module declares common // homeOnly directly on services.son-of-anton.
{ lib }:
let
  inherit (lib) mkOption types literalExpression;

  # `settings` may be defined by several modules. Plain attrset merging keeps
  # one definition, so they are folded with recursiveUpdate instead (later
  # definitions win key by key).
  settingsType = types.mkOptionType {
    name = "son-of-anton-settings";
    description = "settings for config.toml, combined key by key across definitions";
    check = builtins.isAttrs;
    merge = _loc: defs: lib.foldl' lib.recursiveUpdate { } (map (d: d.value) defs);
  };

  ruleType = types.submodule {
    options = {
      id = mkOption {
        type = types.strMatching "[a-z0-9][a-z0-9-]*";
        description = "Stable name of the rule; the agent and the user refer to it by this.";
      };
      text = mkOption {
        type = types.str;
        description = "The rule itself, worded as an instruction.";
      };
      why = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "The reason for the rule, shown next to it in the prompt.";
      };
      scope = mkOption {
        type = types.enum [
          "common"
          "gateway"
        ];
        default = "common";
        description = "`common` applies to every session, `gateway` only to Signal sessions.";
      };
      source = mkOption {
        type = types.enum [
          "user"
          "agent"
        ];
        default = "user";
        description = "Whom the rule is credited to.";
      };
    };
  };
in
{
  inherit settingsType;

  common =
    {
      package,
      packageText,
      workingDirectory,
      workingDirectoryText,
    }:
    {
      enable = lib.mkEnableOption "Son of Anton Agent";

      package = mkOption {
        type = types.package;
        default = package;
        defaultText = packageText;
        description = "The son-of-anton package to run and to put on the PATH.";
      };

      workingDirectory = mkOption {
        type = types.str;
        default = workingDirectory;
        defaultText = workingDirectoryText;
        description = ''
          Where the agent starts and works. It is also written to config.toml
          as `terminal.cwd` unless `settings` sets that key.
        '';
      };

      settings = mkOption {
        type = settingsType;
        default = { };
        description = ''
          The contents of config.toml, as an attribute set. Definitions from
          different modules are merged key by key.

          Activation does not overwrite the file on disk. It merges these keys
          into it, three ways, against `.nix-managed.json`, a record of what
          the previous activation wrote: declared keys replace the disk's; a
          key that was declared before and no longer is gets removed, unless
          someone changed its value since; keys that were never declared are
          left alone. The first activation with no record removes nothing and
          lists the undeclared keys it saw (see `soa-d config merge`, --adopt).
        '';
        example = literalExpression ''
          {
            model.default = "auto";
            compaction = { enabled = true; threshold = 0.85; };
          }
        '';
      };

      memoryRules = mkOption {
        type = types.listOf ruleType;
        default = [ ];
        description = ''
          Standing rules for `memories/rules.toml`. Nix only seeds the file; it
          stays editable, and the agent adds rules to it with the user's
          consent.

          Activation merges the rules in by id, against
          `memories/.nix-rules.json` (what the last activation wrote). A
          declared rule that is absent is added. One that still matches what
          Nix wrote follows the new declaration; one edited on disk is kept and
          reported. A rule dropped from the declaration goes only if it is
          still as Nix wrote it. Rules Nix never declared are never touched,
          and a first run removes nothing. See `soa-d rules merge`.
        '';
        example = literalExpression ''
          [
            {
              id = "never-delete-proton-mail";
              text = "Never delete Proton mail.";
              why = "Mail is not recoverable.";
            }
          ]
        '';
      };

      memoryRulesFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "/run/agenix/son-of-anton-rules";
        description = ''
          Path, at run time, of a TOML file of `[[rule]]` tables (id, text,
          and optionally why, scope and source, with the same values and
          defaults as `memoryRules`). It is merged exactly like `memoryRules`,
          but its contents never enter the Nix store: only the path is in the
          activation script. Use it for rules that must stay out of a public
          repository, such as an agenix secret. Cannot be combined
          with `memoryRules`. If the file is missing, unreadable or has a bad
          entry, activation warns and changes nothing.
        '';
      };

      pruneUnmanagedSettings = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Make `settings` the whole of config.toml: each activation also drops
          every key it did not declare.

          Off by default. Keys that the program itself writes (/skin, /perm)
          are among the undeclared ones, and losing them on a rebuild is worse
          than keeping a stale key. The ordinary merge already retracts keys
          Nix declared earlier and has since dropped.
        '';
      };

      environmentFiles = mkOption {
        # A string, never a path: a path literal is copied into the Nix store,
        # which every user can read. Give a run-time location such as
        # config.age.secrets.son-of-anton-env.path.
        type = types.listOf types.str;
        default = [ ];
        description = ''
          Run-time paths of files with secrets (API keys, tokens) in
          KEY=value form. Activation appends them to $SON_OF_ANTON_HOME/.env,
          which the program reads when it starts. The file is rebuilt from
          scratch every time, so nothing is appended twice.
        '';
        example = literalExpression ''[ config.age.secrets.son-of-anton-env.path ]'';
      };

      environment = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = ''
          Variables for $SON_OF_ANTON_HOME/.env that are not secret.
          Anything here is world-readable in the Nix store; secrets belong in
          `environmentFiles`.
        '';
      };

      extraPackages = mkOption {
        type = types.listOf types.package;
        default = [ ];
        description = "Further tools for the agent's PATH, in the unit and in the wrapped command.";
      };
    };

  instanceOnly =
    { name }:
    {
      user = mkOption {
        type = types.str;
        default = "son-of-anton-${name}";
        defaultText = literalExpression ''"son-of-anton-''${name}"'';
        description = "The system user the instance runs as.";
      };

      group = mkOption {
        type = types.str;
        default = "son-of-anton";
        description = "The user's primary group.";
      };

      createUser = mkOption {
        type = types.bool;
        default = true;
        description = "Create `user` and `group`. Turn off when the user is an existing login account.";
      };

      managedAccount = mkOption {
        type = types.bool;
        default = false;
        description = ''
          The instance runs as an existing human login account, and the module
          must treat its home as someone else's.

          Off, the module owns the state directory and makes it setgid and
          group-writable (2770) so a service group can use it. On a real home
          that would make sshd's StrictModes reject ~/.ssh/authorized_keys and
          give the whole home to the account's primary group.

          On, the module touches only son-of-antonHome and what is under it
          (never the home above it or workingDirectory), keeps everything
          owner-only (0700 and 0600, UMask 0077), and runs the unit without
          the filesystem sandbox.
        '';
      };

      stateDir = mkOption {
        type = types.str;
        default = "/var/lib/son-of-anton-${name}";
        defaultText = literalExpression ''"/var/lib/son-of-anton-''${name}"'';
        description = ''
          The instance's own directory, used as HOME of the unit. Not created
          for a managedAccount, whose HOME is its workingDirectory.
        '';
      };

      son-of-antonHome = mkOption {
        type = types.str;
        default = "/var/lib/son-of-anton-${name}/.son-of-anton";
        defaultText = literalExpression ''"/var/lib/son-of-anton-''${name}/.son-of-anton"'';
        description = ''
          SON_OF_ANTON_HOME: config, memory, sessions. Point it at a login
          account's ~/.son-of-anton and that account's own CLI shares the
          instance's sessions.
        '';
      };

      protectedPaths = mkOption {
        type = types.listOf types.str;
        default = [
          ".gnupg"
          ".aws"
          ".age"
          ".git-credentials"
          ".config/sops"
        ];
        description = ''
          Credential stores the agent must not reach: they become the unit's
          InaccessiblePaths. A relative entry is under the unit's HOME, an
          absolute one is used as given, and one that does not exist is
          skipped.

          This is a short denylist, not an allowlist. Once the instance runs
          as the account it serves, an allowlist of directories stops being a
          boundary, since the account owns them all. A mount namespace also
          binds the terminal tool and everything it starts.

          ~/.ssh is not listed by default: blocking it breaks git over SSH, and
          its key files are guarded only by the program's own file rules. Add
          ".ssh" if that is not acceptable.
        '';
      };

      addToSystemPackages = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Install the son-of-anton command system-wide and export
          SON_OF_ANTON_HOME for every user. Allowed for one instance only,
          because the variable is global.
        '';
      };

      git = mkOption {
        type = types.submodule {
          options.github = mkOption {
            type = types.nullOr types.str;
            default = null;
            description = ''
              Run-time path of a private SSH key (OpenSSH format) whose public
              half GitHub knows, as a deploy key or on a bot account.

              Activation copies it, with an ssh config for github.com, into the
              unit's HOME (stateDir; workingDirectory for a managedAccount),
              owned by the instance user. Git over ssh, from the agent's
              terminal too, then uses that identity with no ssh-agent.

              A string, so that the key is not copied into the store.
            '';
            example = literalExpression ''config.age.secrets."soa-ricky-github-key".path'';
          };
        };
        default = { };
        description = "Git credentials of the instance; see `git.github`.";
      };
    };

  homeOnly =
    { homeDirectory }:
    {
      son-of-antonHome = mkOption {
        type = types.str;
        default = "${homeDirectory}/.son-of-anton";
        defaultText = literalExpression ''"''${config.home.homeDirectory}/.son-of-anton"'';
        description = ''
          SON_OF_ANTON_HOME, the directory of config.toml, .env, sessions,
          memory and cron jobs. The NixOS module appends `/.son-of-anton` to a
          `stateDir`; here the directory is named whole, so an existing one
          keeps working under any name.
        '';
        example = "/srv/lab/agent-home";
      };

      installPackage = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Put the command in home.packages and set SON_OF_ANTON_HOME in
          home.sessionVariables. The variable covers this user's sessions
          only, unlike the NixOS module's system-wide one.
        '';
      };
    };
}

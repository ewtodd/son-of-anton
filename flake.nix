{
  description = "son-of-anton: an always-on agent daemon (Signal, cron, a terminal UI) with physics research modes";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };

    # The lab's analysis library, which the physics computation runtime
    # (nix/physics-runtime.nix) is built from. Its nixpkgs is left unfollowed
    # on purpose: with that pin the runtime's ROOT and Python package are
    # already built and cached, whereas ours would compile ROOT here and pair
    # it with a Python that does not match the package's interpreter.
    analysis-utilities.url = "github:ewtodd/Analysis-Utilities";

    # For the module checks only (nix/checks.nix), which evaluate the Home
    # Manager module against the real thing. Using the module does not need
    # this input.
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs:
    inputs.flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      imports = [
        ./nix/packages.nix
        ./nix/devShell.nix
        ./nix/overlays.nix
        ./nix/nixosModules.nix
        ./nix/homeManagerModules.nix
        ./nix/checks.nix
      ];
    };
}

{
  description = "son-of-anton — an always-on agent harness with physics research modes (hard fork of Nous Research's hermes-agent)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
    # The lab's analysis library, for the physics modes' computation runtime
    # (nix/physics-runtime.nix). Its nixpkgs is deliberately NOT followed: the
    # runtime is built from Analysis-Utilities' own pin, where its Python
    # package and ROOT are already built and cached. Following ours would make
    # a physics runtime that compiles ROOT from source and pairs a
    # differently-built python with that package's interpreter.
    analysis-utilities.url = "github:ewtodd/Analysis-Utilities";

    # Used only by nix/checks.nix, to evaluate homeManagerModules.default
    # against the real Home Manager module system rather than a stub of it.
    # Consuming the module does not require this input — import it from your
    # own home-manager, exactly as you would any other HM module.
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
        "aarch64-darwin"
      ];

      imports = [
        ./nix/packages.nix
        ./nix/overlays.nix
        ./nix/nixosModules.nix
        ./nix/homeManagerModules.nix
        ./nix/checks.nix
        ./nix/devShell.nix
      ];
    };
}

{
  description = "Jeeves -- local-first voice assistant for Linux (NixOS)";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: rec {
        jeeves = pkgs.callPackage ./nix/package.nix { };
        default = jeeves;
      });

      apps = forAll (pkgs: {
        default = { type = "app"; program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.jeeves}/bin/jeeves"; };
      });

      nixosModules.default = { pkgs, ... }: {
        imports = [ ./nix/module.nix ];
        services.jeeves.package = nixpkgs.lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.jeeves;
      };

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pyside6 ps.evdev ps.pytest ]))
            pkgs.espeak-ng pkgs.wl-clipboard pkgs.tesseract
          ];
        };
      });
    };
}

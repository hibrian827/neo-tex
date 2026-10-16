{ config, lib, pkgs, ... }:
let
  cfg = config.programs.neo-tex;
in {
  options.programs.neo-tex = {
    enable = lib.mkEnableOption "LaTeX builds and local/remote PDF viewing";
    wsl.enable = lib.mkEnableOption "WSL viewer and automatic SSH bridge integration";
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      home.packages = [
        (pkgs.writeScriptBin "tex-view" (builtins.replaceStrings
          [ "#!/usr/bin/env python3" ] [ "#!${pkgs.python3}/bin/python3" ]
          (builtins.readFile ./tex-view.py)))
        (pkgs.writeShellScriptBin "tex-ssh" ''exec tex-view ssh "$@"'')
        pkgs.texlive.combined.scheme-medium
      ];
      home.file.".config/nvim/lua/neo_tex".source = ./nvim/lua/neo_tex;
      home.file.".config/nvim/autoload/vimtex/view/texbridge.vim".source = ./nvim/autoload/vimtex/view/texbridge.vim;
    }
    (lib.mkIf cfg.wsl.enable {
      programs.zsh.shellAliases.pdf = "tex-view bridge";
      programs.zsh.initContent = lib.mkAfter ''
        ssh() {
          if [[ -t 0 && -t 1 ]]; then
            tex-view connect -- "$@"
          else
            command ssh "$@"
          fi
        }
      '';
    })
  ]);
}

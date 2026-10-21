# neo-tex

Build LaTeX papers in Neovim, view PDFs locally, and jump between PDF text and source—even when compiling over SSH.

**Nix and NixOS are optional.**

## Install

Requires Linux or WSL, Python 3, Neovim 0.11+, VimTeX v2.18, and your paper's build tools. The default compiler is `latexmk`. The viewing machine requires SumatraPDF on Windows through WSL; Linux servers can build and send PDFs through the SSH bridge.

```sh
git clone https://github.com/hibrian827/neo-tex.git
cd neo-tex
mkdir -p ~/.local/bin ~/.config/nvim/lua ~/.config/nvim/autoload/vimtex/view
ln -s "$PWD/tex-view.py" ~/.local/bin/tex-view
ln -s "$PWD/tex-ssh" ~/.local/bin/tex-ssh
ln -s "$PWD/pdf" ~/.local/bin/pdf
ln -s "$PWD/nvim/lua/neo_tex" ~/.config/nvim/lua/neo_tex
ln -s "$PWD/nvim/autoload/vimtex/view/texbridge.vim" ~/.config/nvim/autoload/vimtex/view/texbridge.vim
```

Keep the checkout in place and add `~/.local/bin` to PATH. Add VimTeX to your lazy.nvim plugins:

```lua
{
  "lervag/vimtex",
  tag = "v2.18",
  lazy = false,
  init = function() require("neo_tex").setup() end,
}
```

For SumatraPDF, run this in WSL, then restart Neovim:

```sh
tex-view install-sumatra
```

This installs a portable viewer without changing Windows file associations. SumatraPDF is required on the viewing machine; missing viewer or Windows interop produces an error before a watcher or bridge starts. There is no Linux viewer fallback.

## Use

From a terminal:

```sh
pdf p.pdf
```

This opens an existing PDF and watches external rebuilds without Neovim. With no
filename, `pdf` uses `.texbuild.json`, or the only PDF in the current directory.
It is shorthand for `tex-view open`. Opening a connected PDF keeps its editor
attached; otherwise, open its source in Neovim and run `:pdf` for inverse search.

Open a `.tex` file in Neovim:

| Command | Action |
| --- | --- |
| `:pdf` | Save project buffers, compile, and open or refresh the PDF |
| `:VimtexStop` | Stop automatic builds |
| `:VimtexErrors` | Show compilation errors |
| `:VimtexInfo` | Show project and compiler information |

After `:pdf`, saving rebuilds the PDF. `:wq` completes a final build before exiting. Unrelated buffers stay unsaved; failed saves or builds cancel viewing.

After Neovim exits, the local watcher continues copying rebuilt PDFs and SyncTeX
files into SumatraPDF's cache. For example, running `make` externally refreshes an
already-open preview. This does not compile source edits automatically. Closing
the PDF does not reopen it on the next build, and inverse search requires Neovim.
The watcher stops automatically once no attached Neovim session has a buffer for
the project and the PDF is closed in SumatraPDF. Checks run every few seconds,
with a short grace period for startup and reloads. Multiple editors and background
PDF tabs are tracked. If Windows cannot report the viewer state, watching continues
until the state can be confirmed or you stop it explicitly.
To stop watching a document, run `tex-view stop /path/to/p.pdf` on the machine
where the original PDF is built. Run `:pdf` again to resume watching. Local watchers
do not survive a WSL shutdown.

Remote previews keep refreshing after Neovim exits while their existing SSH bridge
remains connected. Keep that SSH session open, or use a separate
`tex-view bridge SERVER` session started before requesting the preview. Disconnecting
the bridge ends the subscription; reconnect and run `:pdf` to subscribe again.
Unused remote documents are unsubscribed automatically under the same rule; the
shared bridge remains available for other documents and future preview requests.

**Double-click** PDF text in SumatraPDF to return to its source. Navigation reuses editor panes. Reopening a previously viewed project reconnects its existing PDF; closing a PDF keeps it closed until you request it again.

## Project build command

To use a Makefile or another command, put `.texbuild.json` in the paper's root:

```json
{
  "main": "p.tex",
  "command": "make",
  "pdf": "p.pdf"
}
```

Paths are relative to this directory, and the command runs there. Commands such as `make paper` or `./make-docker.sh` also work. They must finish after one build and generate SyncTeX data (`-synctex=1`) for source navigation.

The configuration applies to included chapters, builds on save, and `:wq`. Builds run one at a time per project in each editor. Restart Neovim after changing it. Without this file, neo-tex uses `latexmk`.

For chapters without a configuration file, identify the main document when needed:

```latex
% !TeX root = ../main.tex
```

## Remote papers

Install neo-tex and VimTeX on both machines. Keep the paper and `.texbuild.json` on the server. From local WSL:

```sh
tex-ssh my-server
# On the server: pdf p.pdf, or open the source in Neovim and run :pdf.
```

The bridge runs alongside SSH and stops when the connection closes. For an existing SSH session, run `tex-view bridge my-server` in another local terminal; stop it with Ctrl+C.

To start bridges with ordinary `ssh`, add this to local WSL `~/.zshrc`:

```zsh
alias pdf='tex-view open'
ssh() {
  if [[ -t 0 && -t 1 ]]; then
    tex-view connect -- "$@"
  else
    command ssh "$@"
  fi
}
```

Enable each server in `~/.ssh/config`, then reload your shell:

```sshconfig
Host my-server
  HostName server.example.com
  SetEnv TEX_VIEW_BRIDGE=1
```

Concurrent connections share a bridge until the last one disconnects. After reconnecting, run `:pdf` again.

## Optional: Home Manager

Instead of the manual symlinks, import the module and apply Home Manager:

```nix
imports = [ /path/to/neo-tex/home-manager.nix ];
programs.neo-tex.enable = true;
programs.neo-tex.wsl.enable = true; # local WSL only
```

This installs the helper, Neovim files, and TeX Live medium. The WSL option adds the SSH wrapper. Configure VimTeX as above; install SumatraPDF separately if wanted.

## Troubleshooting and tests

Logs are under `~/.cache/tex-view/`: `build-*.log`, `final-build-*.log`, `bridge-*.log`, and `viewers/*/viewer.log`.

Keep the requesting Neovim session open for source navigation. Install any missing TeX packages required by your paper. Navigation selects the editor's tmux pane; you may need to bring its terminal window to the front.

Run from this checkout after installation:

```sh
python3 tests/test-tex-view.py
python3 tests/test-ssh-pdf-bridge.py
python3 tests/test-terminal-pdf.py
python3 tests/test-tex-pdf-command.py --make
nvim --headless -u NONE -l tests/test-tex-navigation.lua
```

Additional viewer and remote integration checks are in `tests/`.

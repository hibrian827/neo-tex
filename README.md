# tex-workflow

LaTeX project builds, Neovim navigation, and local or remote PDF viewing.

## Installation

Requirements: Linux (WSL for SumatraPDF), Python 3, Neovim 0.11+, VimTeX v2.18,
and your project's build tools. Zathura is the Linux viewer fallback. Remote
viewing also needs SSH and the helper on the remote machine.

For Home Manager, import `home-manager.nix` and enable:

```nix
imports = [ /path/to/tex-workflow/home-manager.nix ];
programs.tex-workflow.enable = true;
programs.tex-workflow.wsl.enable = true; # local WSL only
```

Apply Home Manager on each machine. This installs the helper, TeX Live medium,
and Neovim Lua/viewer files; the WSL option adds Zathura and the zsh SSH wrapper.
Load VimTeX through your plugin manager, with this lazy.nvim specification:

```lua
{
  "lervag/vimtex",
  tag = "v2.18",
  lazy = false,
  init = function() require("tex_workflow").setup() end,
}
```

Without Home Manager, from this checkout:

```sh
mkdir -p ~/.local/bin ~/.config/nvim/lua ~/.config/nvim/autoload/vimtex/view
ln -s "$PWD/tex-view.py" ~/.local/bin/tex-view
ln -s "$PWD/tex-ssh" ~/.local/bin/tex-ssh
ln -s "$PWD/nvim/lua/tex_workflow" ~/.config/nvim/lua/tex_workflow
ln -s "$PWD/nvim/autoload/vimtex/view/texbridge.vim" ~/.config/nvim/autoload/vimtex/view/texbridge.vim
```

Keep this checkout in place and put `~/.local/bin` on PATH. For automatic SSH
bridges without Home Manager, add this to local WSL `~/.zshrc`:

```zsh
alias pdf='tex-view bridge'
ssh() {
  if [[ -t 0 && -t 1 ]]; then
    tex-view connect -- "$@"
  else
    command ssh "$@"
  fi
}
```

## Project layout and development

- `nvim/lua/tex_workflow/init.lua`: project discovery, saving, compilation,
  build scheduling, editor lifecycle, and inverse navigation.
- `nvim/autoload/vimtex/view/texbridge.vim`: VimTeX viewer adapter and PDF path selection.
- `tex-view.py`: PDF/SyncTeX publication, viewer lifecycle, hidden Windows callbacks,
  and SSH transport/session management. Python standard library only.
- `home-manager.nix`: optional package, Neovim, and WSL shell integration.
- `tests/`: regression and integration checks, run from this repository's root.

`.texbuild.json` belongs to each paper, not to this tool's repository. It contains
an executable shell command selected by the project's owner. A command should run
one build and exit; automatic save builds are coordinated within one editor.

## Usage (local WSL and remote SSH)

VimTeX uses latexmk for continuous compilation. The PDF opens in native Windows
SumatraPDF when installed through the helper below, with Zathura in WSLg as the
fallback. Opening a new project's `.tex` file does **not** compile it or open a viewer.
For a project previously viewed with `:VimtexView`, reopening its source resumes
continuous compilation and reattaches inverse search to the new editor session.
This also works when reopening an included chapter. Reattachment updates the
existing PDF window without moving its view; it never opens a closed viewer.
Previously viewed projects retain this preview preference across editor restarts;
use `:VimtexStop` to stop compilation in the current session.

To enable SumatraPDF after installing the helper:

```bash
tex-view install-sumatra
```

This installs the official portable Windows x64 release 3.6.1 under
`%LOCALAPPDATA%\tex-view`, without changing PDF file associations. Start a new
Neovim session afterward so an older viewer watcher does not retain Zathura.
SumatraPDF supports normal Windows minimize/maximize and Win+Arrow snapping;
`F11` toggles fullscreen. **Double-click** PDF text for inverse search in
SumatraPDF; use **Ctrl+left-click** in Zathura.
SumatraPDF inverse search starts WSL through a hidden Windows Script Host
launcher, so navigation does not flash a console window.

The helper remembers the executable in `~/.config/tex-view/sumatra.json`.
To select another executable, set `TEX_VIEW_SUMATRA` to its WSL path. To force
Zathura, launch Neovim (or the remote bridge) with `TEX_VIEW_VIEWER=zathura`.
The portable Windows installation is separate from Nix and must be repeated on
each Windows device. Existing remote servers do not need redeployment solely
to change the local viewer.

| Action inside Neovim | Command |
| --- | --- |
| Start/stop continuous compilation | `:VimtexCompile` (usually `\ll`) |
| Save the LaTeX project, compile, and open/jump in the PDF | `:pdf` |
| Open/jump in an already built PDF without saving buffers first | `:VimtexView` (usually `\lv`) |
| Stop compilation | `:VimtexStop` |
| Inspect compilation errors | `:VimtexErrors` |
| Inspect the detected main document and compiler | `:VimtexInfo` |

Use `:pdf` to save modified main/chapter buffers and project bibliography/style
files, complete a build, and then view its PDF. Unrelated buffers are not saved.
It also handles the first build when no PDF exists. A save or compilation failure
cancels the view request; compilation logs are under `~/.cache/tex-view/build-*.log`.
The lowercase spelling is a command-line abbreviation for Neovim's internal
uppercase user command. Existing scripted `:VimtexView` usage remains supported.

Compile once before using `:VimtexView`; it also starts continuous compilation if
it is stopped; repeated view requests keep it running. `:VimtexCompile` toggles
the compiler, so running it again stops automatic builds. While compilation is running, saving an
included source file rebuilds the main document. Once explicitly opened, the PDF
refreshes after automatic **or external/manual** builds. Closing the PDF keeps it
closed until the next `:VimtexView`. Repeating `:VimtexView` reuses its window.
Saving with `:wq` also completes a final build before exiting. The watcher/SSH
transport keeps a 15-second delivery window afterward so the final PDF reaches
the viewer even though Neovim has closed. Quitting can wait for that build;
failures are recorded in `~/.cache/tex-view/final-build-*.log` on the build machine.

Double-click (SumatraPDF) or Ctrl+left-click (Zathura) a passage to return to its source in the editor that last
requested this PDF. Existing source windows are reused; otherwise navigation
switches files in the current pane and preserves unsaved buffers. It does not
create extra splits, so closing chapter buffers does not leave duplicate panes.
SumatraPDF's inverse-search command is shared across documents. The hidden
launcher therefore routes each click by its unique cached SyncTeX source path,
which selects the matching project's editor even in another PDF tab. Closing
one project's editor does not disconnect other projects.
The editor's tmux window and pane are selected. Raising the Windows WezTerm window
is not implemented; select WezTerm if Windows leaves the PDF in front.

Commands work from included files. For unambiguous main-document selection, put
this within the first 20 lines of a chapter (path relative to that chapter):

```latex
% !TeX root = ../main.tex
```

To use a project's Makefile or another build command, add `.texbuild.json` beside
the Makefile:

```json
{
  "main": "p.tex",
  "command": "make",
  "pdf": "p.pdf"
}
```

The nearest configuration found above a source file selects the project, including
when opening a chapter directly. Paths are relative to the configuration directory,
and the shell command runs there. For example, `make paper` or `./make-docker.sh`
can replace `make`; `pdf` can be `build/p.pdf`. The command must finish after one
build and generate matching SyncTeX data (`-synctex=1`) for source navigation.
Use `:pdf` to save/build/view and enable subsequent builds on save. Saves during a
build queue another build; commands run serially per project in this editor.
`:VimtexStop` disables automatic builds; `:pdf` enables them again.
Saving with `:wq` also uses the configured command. Bibliography/style saves trigger
builds when that project's source remains open. Reopen the editor after changing
the configuration. Put the file on the build server for remote projects; projects
without it continue to use latexmk. An invalid configuration reports an error rather
than falling back to latexmk.

For local work, use Neovim normally. For remote work, start from **WSL**:

```bash
ssh Cyclonus
# On the server, attach/create tmux and open your .tex file as usual.
# In Neovim: :pdf (save, compile, and view)
```

A host can be enabled for an automatic background PDF bridge. The WSL zsh `ssh`
function reads the resolved SSH configuration and starts the bridge when this
line is present in the matching `Host` block of `~/.ssh/config`:

```sshconfig
Host Cyclonus
  HostName cyclonus.gtisc.gatech.edu
  SetEnv TEX_VIEW_BRIDGE=1
```

Add that line to enable another configured server, or remove it to disable the
automatic bridge. Reapply Home Manager if it manages your SSH configuration. Open a new shell or run
`source ~/.zshrc` once after installing the SSH wrapper. The local wrapper reads
the marker; it does not depend on the server accepting that environment variable.

The bridge logs to `~/.cache/tex-view/bridge-*.log` and stops when SSH disconnects.
Concurrent SSH sessions to the same connection share one bridge; it stops after
the last session exits. A killed wrapper is detected through its session lease.
An already-running manual bridge is reused and remains manually controlled.
Unconfigured hosts, noninteractive scripts, and forwarding-only/background SSH
commands use ordinary SSH. `command ssh ...` explicitly bypasses the wrapper.
This automation is for WSL zsh; native Windows SSH does not load that function.

`tex-ssh` runs an SSH PDF bridge alongside the interactive SSH connection. If you
want explicit bridge management regardless of the host marker, it remains available.
For an existing SSH session, run **`pdf Cyclonus`** in another WSL terminal
(short for `tex-view bridge Cyclonus`), then issue `:pdf` remotely. Keep that bridge running while
viewing. Reconnecting does not replay old view requests: explicitly request the
PDF again. Only one bridge per SSH alias is needed. With nested local tmux,
`tex-ssh` also remembers the local SSH pane for inverse search; a standalone
bridge does not switch local panes.
A standalone `tex-view bridge SERVER` stays in the foreground independently of
ordinary SSH sessions. Press **Ctrl+C** in its terminal when done to return to
the shell; disconnecting a separate SSH terminal does not stop it. `tex-ssh`
manages this lifecycle automatically. Exited viewer launchers are reaped, and
SumatraPDF debug output goes to `~/.cache/tex-view/viewers/*/viewer.log` rather
than the bridge terminal.

The bridge uses an authenticated SSH stdio connection (no listening network
port). It checks output changes every 0.5 seconds and transfers changed PDFs and
`.synctex.gz` files into `~/.cache/tex-view/viewers/`. Local viewing also uses
stable cached copies, so an incomplete compiler write does not replace the
visible PDF. SyncTeX data is published before its PDF. Manual compilation must
use `-synctex=1` for source navigation. The original source files stay on the
machine where Neovim runs. SumatraPDF additionally uses an NTFS cache under
`%LOCALAPPDATA%\tex-view\viewers` for reliable file watching and Windows-compatible
SyncTeX paths. These copies contain the PDF, transformed SyncTeX, and empty source
placeholders; callbacks map those placeholders to the original source paths.
Each project has a separate viewer/cache; for a PDF
opened by multiple editors, the last explicit view request chooses the target.

For reading unrelated local research papers, run `zathura /path/to/paper.pdf`.
Use `/` to search, `n`/`N` for the next/previous match, and `q` to close. PDFs without
matching LaTeX sources and SyncTeX data cannot provide inverse search.

Install the helper and Neovim integration on both the local and remote machines.
VimTeX v2.18 is tested with Neovim 0.11. When using lazy.nvim, install it ahead of time with:

```bash
nvim --headless '+Lazy! install vimtex' +qa
```

Logs: `~/.cache/tex-view/local.log` for local viewing and
`~/.cache/tex-view/bridge-*.log` for bridges launched by `tex-ssh`. A standalone
bridge prints diagnostics in its terminal. An active Neovim session is required;
Zathura additionally needs WSLg, and remote viewing needs working SSH authentication.
TeX Live's medium scheme is installed;
papers requiring additional classes/packages may need additions to the Nix TeX
package selection.

Regression checks:

```bash
python3 tests/test-tex-view.py
python3 tests/test-ssh-pdf-bridge.py           # isolated automatic SSH bridge lifecycle tests
python3 tests/test-tex-pdf-command.py         # latexmk save/build/view, no GUI windows
python3 tests/test-tex-pdf-command.py --make  # custom Makefile, queued saves, and final build
nvim --headless -u NONE -l tests/test-tex-navigation.lua
python3 tests/test-sumatra-workflow.py        # local Windows viewer + editor restart
python3 tests/test-tex-save-quit.py           # final build after :wq
python3 tests/test-tex-save-quit.py Cyclonus  # requires a running local Cyclonus bridge
TEX_VIEW_VIEWER=zathura python3 tests/test-tex-workflow.py            # local WSLg integration test
TEX_VIEW_VIEWER=zathura python3 tests/test-tex-workflow.py Cyclonus   # remote integration test
```

Integration tests create disposable papers and isolated tmux sessions, open PDF
windows, verify reload and inverse-search callbacks, and close their windows.
They do not simulate the physical Ctrl+click gesture or Windows focus changes.

"""Local Windows/WSL test: editor restart, included-file save, and hidden inverse search.

Requires installed tex-view, SumatraPDF, Neovim and latexmk. Opens a disposable
PDF window; closes only its own Sumatra process and editors on completion.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def run(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=30).strip()


def powershell(code):
    code = "$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'; " + code
    encoded = base64.b64encode(code.encode('utf-16le')).decode()
    return run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded])


def wait(check, description):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(.2)
    raise RuntimeError('Timed out: ' + description)


with tempfile.TemporaryDirectory(prefix="sumatra reopen's ") as tmp:
    root = Path(tmp)
    (root / 'sections').mkdir()
    main = root / 'main.tex'
    source = root / 'sections/chapter.tex'
    main.write_text('\\documentclass{article}\n\\begin{document}\n\\input{sections/chapter}\n\\end{document}\n')
    source.write_text('% !TeX root = ../main.tex\nFirst page.\n\nInverse search target.\n')
    run(['latexmk', '-cd', '-pdf', '-synctex=1', '-interaction=nonstopmode', str(main)])
    key = hashlib.sha256(str(main.with_suffix('.pdf')).encode()).hexdigest()[:24]
    ident = hashlib.sha256(str(Path.home() / '.cache/tex-view/requests' / (key + '.json')).encode()).hexdigest()[:24]
    metadata = Path.home() / '.cache/tex-view/viewers' / ident / 'callback.json'
    config = json.loads((Path.home() / '.config/tex-view/sumatra.json').read_text())
    folder = Path(config.get('cache', str(Path(config['executable']).parent / 'viewers'))) / ident
    log = metadata.parent / 'viewer.log'
    offset = log.stat().st_size if log.exists() else 0
    editor = None
    socket = None

    def expr(expression):
        return run(['nvim', '--server', socket, '--remote-expr', expression])

    def command(text):
        return expr('execute(' + json.dumps(text) + ')')

    def start_editor(number):
        global editor, socket
        socket = str(root / ('editor-' + str(number) + '.sock'))
        environment = dict(os.environ)
        environment.pop('TEX_VIEW_VIEWER', None)
        environment.pop('SSH_CONNECTION', None)
        editor = subprocess.Popen(['nvim', '--headless', '--listen', socket, str(source)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment)
        wait(lambda: expr('exists("b:vimtex")') == '1', 'VimTeX initialization')

    def stop_editor():
        if editor and editor.poll() is None:
            run(['nvim', '--server', socket, '--remote-send', '<Cmd>qa!<CR>'])
            editor.wait(timeout=10)

    def viewer_process():
        return powershell("@(Get-CimInstance Win32_Process -Filter \"Name='SumatraPDF.exe'\" | "
                          "Where-Object {$_.CommandLine -like '*" + ident + "*'} | "
                          "Where-Object {$p=Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
                          "$p -and -not $p.HasExited -and $p.MainWindowHandle -ne 0} | "
                          "ForEach-Object {$_.ProcessId}) -join ','")

    try:
        start_editor(1)
        assert expr('b:vimtex.compiler.is_running() ? 1 : 0') == '0'
        command('Pdf')
        first_pid = wait(viewer_process, 'Sumatra process')
        wait(lambda: metadata.exists(), 'callback metadata')
        stop_editor()
        start_editor(2)
        wait(lambda: json.loads(metadata.read_text())['server'] == socket, 'automatic callback reconnection')
        wait(lambda: expr('b:vimtex.compiler.is_running() ? 1 : 0') == '1', 'automatic compilation restart')
        assert viewer_process() == first_pid, 'Reconnection must keep the same PDF viewer'
        old_pdf = (folder / 'main.pdf').read_bytes()
        expr("luaeval(\"vim.api.nvim_buf_set_lines(0, -1, -1, false, {string.char(92)..'newpage', 'Second page after editor restart.'})\")")
        command('write')
        wait(lambda: (folder / 'main.pdf').read_bytes() != old_pdf, 'reopened chapter save publishes a changed PDF')
        win_pdf = run(['wslpath', '-w', str(folder / 'main.pdf')])
        wait(lambda: any(win_pdf in line and '2 pages' in line
                         for line in log.read_text(errors='replace')[offset:].splitlines()),
             'existing Sumatra window renders the second page')
        data = json.loads(metadata.read_text())
        mapped = next(name for name, original in data['windows_sources'].items() if original == str(source))
        command('normal! 1G')
        run(['/mnt/c/Windows/System32/wscript.exe', '//B', '//Nologo',
             run(['wslpath', '-w', str(folder / 'inverse-search.vbs')]), mapped, '4'])
        assert json.loads(expr('json_encode([expand("%:p"),line(".")])')) == [str(source), 4]
        print('PASS Sumatra editor restart: automatic reattachment, compile, rendered reload, hidden inverse search')
        # Wiping and reopening a source buffer within the same editor also reconnects.
        command('enew | bwipeout ' + str(source).replace(' ', '\\ '))
        command('edit ' + str(source).replace(' ', '\\ '))
        wait(lambda: expr('b:vimtex.compiler.is_running() ? 1 : 0') == '1', 'buffer reopen resumes compilation')
        print('PASS source buffer close/reopen resumes preview within the same editor')
        # A shared Sumatra inverse-search command must route each PDF separately.
        second = root / 'second project'
        second.mkdir()
        second_source = second / 'other.tex'
        second_source.write_text('\\documentclass{article}\n\\begin{document}\nOther PDF.\nAnother target.\n\\end{document}\n')
        run(['latexmk', '-cd', '-pdf', '-synctex=1', '-interaction=nonstopmode', str(second_source)])
        second_socket = str(root / 'second-project.sock')
        second_editor = subprocess.Popen(['nvim', '--headless', '--listen', second_socket, str(second_source)],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        second_key = hashlib.sha256(str(second_source.with_suffix('.pdf')).encode()).hexdigest()[:24]
        second_ident = hashlib.sha256(str(Path.home() / '.cache/tex-view/requests' / (second_key + '.json')).encode()).hexdigest()[:24]
        second_metadata = metadata.parent.parent / second_ident / 'callback.json'
        second_folder = folder.parent / second_ident
        def second_expr(value):
            return run(['nvim', '--server', second_socket, '--remote-expr', value])
        try:
            wait(lambda: second_expr('exists("b:vimtex")') == '1', 'second project initializes')
            second_expr('execute("VimtexView")')
            wait(lambda: second_metadata.exists(), 'second PDF metadata')
            second_data = json.loads(second_metadata.read_text())
            second_mapped = next(name for name, original in second_data['windows_sources'].items()
                                 if original == str(second_source))
            command('normal! 1G')
            # Reproduce a second document/tab using the first PDF's command.
            wrapper = run(['wslpath', '-w', str(folder / 'inverse-search.vbs')])
            run(['/mnt/c/Windows/System32/wscript.exe', '//B', '//Nologo', wrapper, second_mapped, '4'])
            assert json.loads(second_expr('json_encode([expand("%:p"),line(".")])')) == [str(second_source), 4]
            assert expr('line(".")') == '1'
            stop_editor()
            run(['/mnt/c/Windows/System32/wscript.exe', '//B', '//Nologo', wrapper, second_mapped, '3'])
            assert second_expr('line(".")') == '3'
            print('PASS second PDF uses its own editor even through the first command and after first editor closes')
        finally:
            run(['nvim', '--server', second_socket, '--remote-send', '<Cmd>qa!<CR>'])
            second_editor.wait(timeout=10)
            powershell("Get-CimInstance Win32_Process -Filter \"Name='SumatraPDF.exe'\" | "
                       "Where-Object {$_.CommandLine -like '*" + second_ident + "*'} | "
                       "ForEach-Object {Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue}")
        powershell("Get-CimInstance Win32_Process -Filter \"Name='SumatraPDF.exe'\" | "
                   "Where-Object {$_.CommandLine -like '*" + ident + "*'} | "
                   "ForEach-Object {Stop-Process -Id $_.ProcessId}")
        stop_editor()
        wait(lambda: viewer_process() == '', 'test PDF closes')
        def watcher_stopped():
            lock = Path.home() / '.cache/tex-view/locks' / ident
            with lock.open('a') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                return True
        wait(watcher_stopped, 'watcher stops after both editor and PDF close')
        print('PASS watcher automatically exits after both editor and PDF close')
        start_editor(3)
        wait(lambda: json.loads(metadata.read_text())['server'] == socket, 'closed viewer project reattaches')
        wait(lambda: expr('b:vimtex.compiler.is_running() ? 1 : 0') == '1', 'preview compilation resumes')
        assert viewer_process() == '', 'Automatic reattachment must not reopen a closed viewer'
        print('PASS reopening source leaves a closed PDF viewer closed')
        # Save-and-quit must deliver the last edit without reopening Neovim.
        old_pdf = (folder / 'main.pdf').read_bytes()
        expr("luaeval(\"vim.api.nvim_buf_set_lines(0, -1, -1, false, {string.char(92)..'newpage', 'Final page saved with wq.'})\")")
        run(['nvim', '--server', socket, '--remote-send', '<Cmd>wq<CR>'])
        editor.wait(timeout=125)
        wait(lambda: (folder / 'main.pdf').read_bytes() != old_pdf, 'save-and-quit final PDF delivery')
        print('PASS :wq final build reaches Windows PDF cache after editor exits')
    finally:
        stop_editor()
        powershell("Get-CimInstance Win32_Process -Filter \"Name='SumatraPDF.exe'\" | "
                   "Where-Object {$_.CommandLine -like '*" + ident + "*'} | "
                   "ForEach-Object {Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue}")

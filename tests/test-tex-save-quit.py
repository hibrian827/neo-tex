"""Verify :wq builds and delivers the last included-file edit without editor reopen.

Usage: python3 tests/test-tex-save-quit.py [SSH_HOST]
For remote testing, keep `tex-view bridge SSH_HOST` running locally first.
Creates a disposable project and PDF window; does not alter existing projects.
"""
import base64
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

host = sys.argv[1] if len(sys.argv) > 1 else None


def target(args):
    if not host:
        return args
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'ClearAllForwardings=yes', host, shlex.join(args)]


def run(args):
    return subprocess.check_output(target(args), text=True, stderr=subprocess.STDOUT, timeout=30).strip()


def wait(check, description):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(.3)
    raise RuntimeError('Timed out: ' + description)


with tempfile.TemporaryDirectory(prefix='tex-save-quit-') as tmp:
    root = Path(tmp)
    main = root / 'main.tex'
    source = root / 'chapter.tex'
    socket = str(root / 'editor.sock')
    files = {'main.tex': '\\documentclass{article}\n\\begin{document}\n\\input{chapter}\n\\end{document}\n',
             'chapter.tex': '% !TeX root = main.tex\nOriginal first page.\n'}
    code = f'from pathlib import Path; p=Path({tmp!r}); p.mkdir(exist_ok=True); '
    code += f'[(p/name).write_text(value) for name,value in {files!r}.items()]'
    run(['python3', '-c', code])
    pdf_key = hashlib.sha256(str(main.with_suffix('.pdf')).encode()).hexdigest()[:24]
    if host:
        ident = hashlib.sha256((host + ':' + pdf_key).encode()).hexdigest()[:24]
    else:
        ident = hashlib.sha256(str(Path.home() / '.cache/tex-view/requests' / (pdf_key + '.json')).encode()).hexdigest()[:24]
    config = json.loads((Path.home() / '.config/tex-view/sumatra.json').read_text())
    folder = Path(config.get('cache', str(Path(config['executable']).parent / 'viewers'))) / ident
    editor_log = root / 'editor.log'
    log_handle = editor_log.open('w')
    editor = subprocess.Popen(target(['nvim', '--headless', '--listen', socket, str(source)]),
                              stdout=log_handle, stderr=subprocess.STDOUT)
    second_editor = None
    second_socket = str(root / 'second-editor.sock')

    def expr(value):
        return run(['nvim', '--server', socket, '--remote-expr', value])

    try:
        wait(lambda: expr('exists("b:vimtex")') == '1', 'editor initializes')
        expr("luaeval(\"vim.api.nvim_buf_set_lines(0, -1, -1, false, {'Unsaved content saved by lowercase pdf.'})\")")
        run(['nvim', '--server', socket, '--remote-send', ':pdf<CR>'])
        wait(lambda: (folder / 'main.pdf').exists(), 'initial PDF reaches Windows viewer cache')
        assert 'Unsaved content saved by lowercase pdf.' in run(['python3', '-c', f'from pathlib import Path; print(Path({str(source)!r}).read_text())'])
        print('PASS :pdf saves unsaved chapter, builds first PDF, and transfers it to Windows:', host or 'local')
        old = (folder / 'main.pdf').read_bytes()
        second = root / 'second'
        second_code = f'from pathlib import Path; p=Path({str(second)!r}); p.mkdir(); '
        second_code += f'[(p/name).write_text(value) for name,value in {files!r}.items()]'
        run(['python3', '-c', second_code])
        run(['latexmk', '-cd', '-pdf', '-synctex=1', '-interaction=nonstopmode', str(second / 'main.tex')])
        second_editor = subprocess.Popen(target(['nvim', '--headless', '--listen', second_socket, str(second / 'chapter.tex')]),
                                         stdout=log_handle, stderr=subprocess.STDOUT)
        def second_expr(value):
            return run(['nvim', '--server', second_socket, '--remote-expr', value])
        wait(lambda: second_expr('exists("b:vimtex")') == '1', 'second editor initializes')
        second_expr('execute("VimtexView")')
        second_key = hashlib.sha256(str(second / 'main.pdf').encode()).hexdigest()[:24]
        if host:
            second_ident = hashlib.sha256((host + ':' + second_key).encode()).hexdigest()[:24]
        else:
            second_ident = hashlib.sha256(str(Path.home() / '.cache/tex-view/requests' / (second_key + '.json')).encode()).hexdigest()[:24]
        second_metadata = Path.home() / '.cache/tex-view/viewers' / second_ident / 'callback.json'
        wait(lambda: second_metadata.exists(), 'second PDF callback metadata')
        second_data = json.loads(second_metadata.read_text())
        mapped = next(name for name, original in second_data['windows_sources'].items()
                      if original == str(second / 'chapter.tex'))
        wrapper = subprocess.check_output(['wslpath', '-w', str(folder / 'inverse-search.vbs')], text=True).strip()
        def navigate_second(line):
            subprocess.run(['/mnt/c/Windows/System32/wscript.exe', '//B', '//Nologo', wrapper, mapped, str(line)],
                           check=True, timeout=30)
        navigate_second(2)
        assert json.loads(second_expr('json_encode([expand("%:p"),line(".")])')) == [str(second / 'chapter.tex'), 2]
        assert json.loads(expr('json_encode(expand("%:p"))')) == str(source)
        expr("luaeval(\"vim.api.nvim_buf_set_lines(0, -1, -1, false, {string.char(92)..'newpage', 'Final edit saved with wq.'})\")")
        run(['nvim', '--server', socket, '--remote-send', '<Cmd>wq<CR>'])
        editor.wait(timeout=125)
        assert editor.returncode == 0
        expected = run(['python3', '-c', f'import hashlib; from pathlib import Path; print(hashlib.sha256(Path({str(main.with_suffix(".pdf"))!r}).read_bytes()).hexdigest())'])
        wait(lambda: (folder / 'main.pdf').read_bytes() != old and
             hashlib.sha256((folder / 'main.pdf').read_bytes()).hexdigest() == expected,
             'final PDF reaches Windows after editor exit')
        assert run(['python3', '-c', f'from pathlib import Path; print(Path({socket!r}).exists())']) == 'False'
        print('PASS :wq builds saved chapter and delivers final PDF after editor exit:', host or 'local')
        navigate_second(1)
        assert json.loads(second_expr('json_encode([expand("%:p"),line(".")])')) == [str(second / 'chapter.tex'), 1]
        print('PASS second PDF routes independently through first callback after first editor exits:', host or 'local')
    except Exception:
        print(editor_log.read_text(errors='replace'), file=sys.stderr)
        raise
    finally:
        if second_editor and second_editor.poll() is None:
            subprocess.run(target(['nvim', '--server', second_socket, '--remote-send', '<Cmd>qa!<CR>']), timeout=125)
            second_editor.wait(timeout=125)
        if editor.poll() is None:
            subprocess.run(target(['nvim', '--server', socket, '--remote-send', '<Cmd>qa!<CR>']), timeout=125)
            editor.wait(timeout=125)
        cleanup = ("$ProgressPreference='SilentlyContinue'; Get-CimInstance Win32_Process -Filter \"Name='SumatraPDF.exe'\" | "
                   "Where-Object {$_.CommandLine -like '*" + ident + "*'} | "
                   "ForEach-Object {Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue}")
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                        base64.b64encode(cleanup.encode('utf-16le')).decode()], capture_output=True, timeout=15)
        if host:
            run(['python3', '-c', f'import shutil; shutil.rmtree({tmp!r})'])
        log_handle.close()

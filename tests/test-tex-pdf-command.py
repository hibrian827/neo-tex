"""Verify interactive :pdf saves its project, builds from scratch, then requests viewing.

Uses a fake viewer to record render requests. No PDF windows are opened.
Run after installation: python3 tests/test-tex-pdf-command.py
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

custom = '--make' in sys.argv

with tempfile.TemporaryDirectory(prefix="pdf unsaved's ") as tmp:
    root = Path(tmp)
    main = root / ('p.tex' if custom else 'main.tex')
    chapter = root / 'chapter.tex'
    output = root / 'build/p.pdf' if custom else main.with_suffix('.pdf')
    note = root / 'notes.txt'
    main.write_text('\\documentclass{article}\n\\begin{document}\n\\input{chapter}\n\\end{document}\n')
    chapter.write_text(('' if custom else '% !TeX root = main.tex\n') + 'Original chapter.\n')
    note.write_text('Unrelated note.\n')
    if custom:
        (root / '.texbuild.json').write_text(json.dumps({'main': 'p.tex', 'command': 'make paper', 'pdf': 'build/p.pdf'}))
        (root / 'Makefile').write_text('.PHONY: paper\npaper:\n\t@python3 builder.py\n')
        (root / 'builder.py').write_text(
            'import os,subprocess,time\nfrom pathlib import Path\n'
            'fd=os.open("building.lock",os.O_CREAT|os.O_EXCL|os.O_WRONLY)\n'
            'try:\n'
            ' with open("builds.log","a") as f: f.write("start\\n")\n'
            ' time.sleep(.5)\n'
            ' Path("build").mkdir(exist_ok=True)\n'
            ' subprocess.run(["pdflatex","-halt-on-error","-synctex=1","-interaction=nonstopmode","-output-directory=build","p.tex"],check=True)\n'
            'finally:\n'
            ' os.close(fd)\n Path("building.lock").unlink()\n'
            ' with open("builds.log","a") as f: f.write("end\\n")\n')
    binary = root / 'bin'
    binary.mkdir()
    rendered = root / 'rendered.jsonl'
    fake = binary / 'SumatraPDF'
    fake.write_text('#!' + sys.executable + '\nimport sys,json,hashlib\nfrom pathlib import Path\n'
                    + f'p=Path({str(rendered)!r})\n'
                    + 'with p.open("a") as out: out.write(json.dumps({"pdf":sys.argv[-1],'
                      '"hash":hashlib.sha256(Path(sys.argv[-1]).read_bytes()).hexdigest()})+"\\n")\n')
    fake.chmod(0o700)
    wrapper = binary / 'tex-view'
    source = Path(__file__).resolve().parents[1] / 'tex-view.py'
    wrapper.write_text('#!' + sys.executable + '\nimport runpy,sys\nfrom pathlib import Path\n'
                       + f'm=runpy.run_path({str(source)!r})\n'
                       + "g=m['main'].__globals__\ng['SELF']=sys.argv[0]\n"
                       + f"g['sumatra_executable']=lambda: Path({str(fake)!r})\n"
                       + f"g['windows_cache']=lambda: Path({str(root / 'windows-cache')!r})\n"
                       + "g['windows_path']=str\ng['require_sumatra']=lambda: None\n"
                       + "sys.exit(m['main']())\n")
    wrapper.chmod(0o700)
    env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'], TEX_VIEW_VIEWER='sumatra',
               XDG_CACHE_HOME=str(root / 'cache'))
    env.pop('SSH_CONNECTION', None)
    socket = str(root / 'editor.sock')
    log = (root / 'editor.log').open('w')
    editor = subprocess.Popen(['nvim', '--headless', '--listen', socket, str(chapter)],
                              stdout=log, stderr=subprocess.STDOUT, env=env)

    def expr(value):
        return subprocess.check_output(['nvim', '--server', socket, '--remote-expr', value],
                                       text=True, stderr=subprocess.STDOUT, timeout=30).strip()

    def command(value):
        return expr('execute(' + json.dumps(value) + ')')

    def wait(check):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if check(): return
            except (OSError, subprocess.SubprocessError): pass
            time.sleep(.1)
        raise RuntimeError('PDF command test timed out')

    def pdf(failure=None):
        subprocess.run(['nvim', '--server', socket, '--remote-send', ':pdf<CR>'], check=True, timeout=10)
        if failure:
            wait(lambda: failure in expr('execute("messages")'))

    try:
        wait(lambda: expr('exists("b:vimtex")') == '1')
        command('set hidden')
        command('edit ' + str(note).replace(' ', '\\ '))
        expr("luaeval(\"vim.api.nvim_buf_set_lines(0, 0, -1, false, {'Unsaved unrelated note.'})\")")
        command('edit ' + str(main).replace(' ', '\\ '))
        expr("luaeval(\"vim.api.nvim_buf_set_lines(0, 2, 2, false, {'Saved main-buffer edit.'})\")")
        command('edit ' + str(chapter).replace(' ', '\\ '))
        expr("luaeval(\"vim.api.nvim_buf_set_lines(0, -1, -1, false, {string.char(92)..'newpage', 'Saved chapter-buffer edit.'})\")")
        assert not output.exists()
        assert 'Saved' not in main.read_text() and 'Saved' not in chapter.read_text()
        pdf()
        wait(rendered.exists)
        assert 'Saved main-buffer edit.' in main.read_text()
        assert 'Saved chapter-buffer edit.' in chapter.read_text()
        assert note.read_text() == 'Unrelated note.\n'
        assert expr('&modified') == '0'
        assert expr('winnr("$")') == '1'
        assert '2 pages' in output.with_suffix('.log').read_text()
        render = json.loads(rendered.read_text().splitlines()[0])
        assert render['hash'] == hashlib.sha256(output.read_bytes()).hexdigest()
        print('PASS lowercase :pdf saves modified main/chapter buffers, leaves unrelated notes unsaved, builds first PDF, then renders')
        metadata = next((root / 'cache/tex-view/viewers').glob('*/callback.json'))
        command('edit ' + str(main).replace(' ', '\\ '))
        subprocess.run(['tex-view', 'callback', str(metadata), str(chapter), '2'], env=env, check=True, timeout=15)
        assert expr('expand("%:p")') == str(chapter)
        assert expr('line(".")') == '2'
        assert expr('winnr("$")') == '1'
        print('PASS helper inverse callback loads the standalone Neovim module and returns to the chapter')

        if custom:
            assert expr('b:vimtex.tex') == str(main)
            assert expr('b:vimtex.compiler.name') == 'generic'
            assert (root / 'builds.log').read_text() == 'start\nend\n', 'viewing launched a duplicate build'
            command('call append("$", "Automatic Makefile edit.") | write')
            wait((root / 'building.lock').exists)
            command('call append("$", "Queued Makefile edit.") | write')
            wait(lambda: (root / 'builds.log').read_text().count('end\n') >= 3)
            assert (root / 'builds.log').read_text() == 'start\nend\n' * 3
            command('VimtexStop')
            command('call append("$", "Stopped Makefile edit.") | write')
            time.sleep(.4)
            assert (root / 'builds.log').read_text().count('start\n') == 3
            print('PASS per-project Makefile, custom PDF path, chapter detection, serialized queued saves, and VimtexStop')

            other = root / 'other'
            other.mkdir()
            (other / 'main.tex').write_text('\\documentclass{article}\n\\begin{document}Other paper.\\end{document}\n')
            (other / '.texbuild.json').write_text(json.dumps({
                'main': 'main.tex', 'command': 'printf other > command-used', 'pdf': 'different.pdf'}))
            command('edit ' + str(other / 'main.tex').replace(' ', '\\ '))
            command('VimtexCompile')
            wait((other / 'command-used').exists)
            command('edit ' + str(chapter).replace(' ', '\\ '))
            assert 'make paper' in expr('b:vimtex.compiler.command')

            broken = root / 'broken'
            broken.mkdir()
            (broken / 'main.tex').write_text((other / 'main.tex').read_text())
            (broken / '.texbuild.json').write_text('{"main":"main.tex", "pdf":"main.pdf"}')
            command('silent! edit ' + str(broken / 'main.tex').replace(' ', '\\ '))
            assert expr('b:vimtex.compiler.name') == 'generic'
            assert expr('b:vimtex.compiler.command') == 'false'
            pdf('command must be a nonempty string')
            command('edit ' + str(chapter).replace(' ', '\\ '))
            print('PASS separate projects retain separate commands; invalid config cannot fall back to latexmk')

        expr("luaeval(\"vim.api.nvim_buf_set_lines(0, -1, -1, false, {string.char(92)..'undefinedCommandForTest'})\")")
        before = rendered.read_text()
        pdf('compilation failed')
        assert 'undefinedCommandForTest' in chapter.read_text()
        assert rendered.read_text() == before
        assert 'compilation failed' in expr('execute("messages")')
        print('PASS compilation failure does not issue a new PDF view request')

        command('setlocal readonly')
        expr("luaeval(\"vim.api.nvim_buf_set_lines(0, -1, -1, false, {'Unsaved read-only edit.'})\")")
        pdf('E45')
        assert expr('&modified') == '1'
        assert 'Unsaved read-only edit.' not in chapter.read_text()
        assert rendered.read_text() == before
        print('PASS save failure preserves the unsaved buffer and cancels compilation/viewing')
        command('setlocal noreadonly')
        if custom:
            command('silent %g/undefinedCommandForTest/d')
            command('write')
            # Discard the unrelated note explicitly before testing editor exit.
            command('call setbufvar(' + json.dumps(str(note)) + ', "&modified", 0)')
            subprocess.run(['nvim', '--server', socket, '--remote-send', ':wq<CR>'], check=True, timeout=10)
            editor.wait(timeout=125)
            assert 'Stopped Makefile edit.' in chapter.read_text()
            assert 'Saved chapter-buffer edit.' in subprocess.check_output(['pdftotext', str(output), '-'], text=True)
            print('PASS save-and-quit finishes the configured Makefile build')
    except Exception as error:
        if isinstance(error, subprocess.CalledProcessError):
            print(error.output, file=sys.stderr)
        print((root / 'editor.log').read_text(errors='replace'), file=sys.stderr)
        raise
    finally:
        if editor.poll() is None:
            subprocess.run(['nvim', '--server', socket, '--remote-send', '<Cmd>qa!<CR>'], capture_output=True, timeout=125)
            editor.wait(timeout=125)
        log.close()

"""Exercise the installed local/remote workflow with a disposable multi-file paper.

Requires WSLg and gdbus locally. Opens temporary PDF windows, then closes them.
Usage: python3 tests/test-neo-tex.py [SSH_HOST]
"""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

host = sys.argv[1] if len(sys.argv) > 1 else None
ident = 'tex-check-' + uuid.uuid4().hex[:10]
tmux_name = ident
root = '/tmp/' + ident + ' space'
socket = '/tmp/' + ident + '.sock'
bridge = None
viewer_pids = set()
cache = Path.home() / '.cache/tex-view'


def run(args, **kwargs):
    if host:
        args = ['ssh', '-o', 'BatchMode=yes', '-o', 'ClearAllForwardings=yes', host, shlex.join(args)]
    return subprocess.run(args, text=True, capture_output=True, timeout=60, **kwargs)


def checked(args):
    result = run(args)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


def expr(value):
    return checked(['nvim', '--server', socket, '--remote-expr', value])


def command(value):
    return expr('execute(' + json.dumps(value) + ')')


def wait(test, description):
    for _ in range(100):
        try:
            result = test()
            if result:
                return result
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
            pass
        time.sleep(.3)
    raise RuntimeError('Timed out: ' + description)


def record():
    for p in cache.glob('viewers/*/callback.json'):
        data = json.loads(p.read_text())
        if data.get('root') == root and data.get('host') == host:
            return p, data


def pids(pdf):
    found = []
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = path.read_bytes().split(b'\0')
            if args and b'zathura' in args[0] and b'--synctex-pid' not in args and str(pdf).encode() in args:
                found.append(int(path.parent.name))
        except OSError:
            pass
    return found


def pages(pid):
    result = subprocess.run(['gdbus', 'call', '--session', '--dest', f'org.pwmt.zathura.PID-{pid}',
                             '--object-path', '/org/pwmt/zathura', '--method',
                             'org.freedesktop.DBus.Properties.Get', 'org.pwmt.zathura', 'numberofpages'],
                            text=True, capture_output=True, timeout=5)
    match = re.search(r'uint32 (\d+)', result.stdout)
    return int(match[1]) if match else None


try:
    if host:
        bridge = subprocess.Popen(['tex-view', 'bridge', host], stdout=open('/tmp/' + ident + '-bridge.log', 'w'),
                                  stderr=subprocess.STDOUT)
        time.sleep(2)
    main = '\\documentclass{article}\n\\begin{document}\n\\input{sections/chapter}\n\\end{document}\n'
    chapter = '% !TeX root = ../main.tex\nFirst page.\n\nSource navigation target.\n'
    code = f"from pathlib import Path; p=Path({root!r}); (p/'sections').mkdir(parents=True); (p/'main.tex').write_text({main!r}); (p/'sections/chapter.tex').write_text({chapter!r})"
    checked(['python3', '-c', code])
    checked(['tmux', '-L', tmux_name, 'new-session', '-d', '-s', 'test', '-n', 'editor',
             shlex.join(['nvim', '--headless', '--listen', socket, root + '/sections/chapter.tex'])])
    wait(lambda: expr('exists("b:vimtex")') == '1', 'VimTeX initialization')
    assert json.loads(expr('json_encode(b:vimtex.tex)')) == root + '/main.tex'
    assert expr('g:vimtex_view_automatic') == '0'
    assert record() is None
    command('VimtexCompile')
    wait(lambda: checked(['test', '-f', root + '/main.pdf']) == '', 'initial compilation')
    assert record() is None
    print('PASS main-file detection from subfile; no viewer on open/compile', flush=True)
    command('VimtexStop')
    wait(lambda: expr('b:vimtex.compiler.is_running() ? 1 : 0') == '0', 'compiler stopped before viewing')
    command('VimtexView')
    wait(lambda: expr('b:vimtex.compiler.is_running() ? 1 : 0') == '1', 'viewing starts continuous compilation')
    metadata, data = wait(record, 'explicit viewer request')
    pdf = Path(data.get('cache_dir', root)) / 'main.pdf'
    pid = wait(lambda: next(iter(pids(pdf)), None), 'Zathura process')
    viewer_pids.add(pid)
    wait(lambda: pages(pid) == 1, 'initial rendered page')
    command('VimtexView')
    assert expr('b:vimtex.compiler.is_running() ? 1 : 0') == '1'
    wait(lambda: pids(pdf) == [pid], 'reuse existing viewer')
    forward = subprocess.run(['gdbus', 'call', '--session', '--dest', f'org.pwmt.zathura.PID-{pid}',
                              '--object-path', '/org/pwmt/zathura', '--method',
                              'org.pwmt.zathura.SynctexView', root + '/sections/chapter.tex', '4', '1'],
                             text=True, capture_output=True, timeout=5)
    assert forward.returncode == 0 and 'true' in forward.stdout, forward.stderr
    print('PASS source-to-PDF SyncTeX search', flush=True)
    # Edit through Neovim and save; latexmk must notice the included source.
    expr("luaeval(\"vim.api.nvim_buf_set_lines(0, -1, -1, false, {'', string.char(92)..'newpage', 'Second page.'})\")")
    command('write')
    wait(lambda: pages(pid) == 2, 'automatic compilation and viewer reload')
    print('PASS explicit open, reuse existing window, included-file save/reload', flush=True)
    command('VimtexStop')
    code = f"from pathlib import Path; p=Path({root!r})/'sections/chapter.tex'; p.write_text(p.read_text()+'\\n\\\\newpage\\nThird page.\\n')"
    checked(['python3', '-c', code])
    checked(['latexmk', '-cd', '-pdf', '-synctex=1', '-interaction=nonstopmode', root + '/main.tex'])
    wait(lambda: pages(pid) == 3, 'external compilation and viewer reload')
    print('PASS external/manual compilation reload', flush=True)
    command('edit ' + root.replace(' ', '\\ ') + '/main.tex')
    checked(['tmux', '-L', tmux_name, 'new-window', '-n', 'other', 'sleep 120'])
    subprocess.run(['tex-view', 'callback', str(metadata), './sections/chapter.tex', '4'], check=True, timeout=30)
    assert json.loads(expr('json_encode([expand("%:p"), line(".")])')) == [root + '/sections/chapter.tex', 4]
    assert int(expr('winnr("$")')) == 1
    assert checked(['tmux', '-L', tmux_name, 'display-message', '-p', '#W']) == 'editor'
    print('PASS inverse search to correct editor, subfile, line, existing pane, and tmux window', flush=True)
    # Keep the PDF window alive while replacing the editor and its socket.
    old_socket = socket
    checked(['nvim', '--server', socket, '--remote-send', '<Cmd>qa!<CR>'])
    wait(lambda: run(['test', '!', '-e', old_socket]).returncode == 0, 'former editor exits')
    checked(['tmux', '-L', tmux_name, 'kill-server'])
    socket = '/tmp/' + ident + '-reopened.sock'
    tmux_name = ident + '-reopened'
    checked(['tmux', '-L', tmux_name, 'new-session', '-d', '-s', 'test', '-n', 'editor',
             shlex.join(['nvim', '--headless', '--listen', socket, root + '/sections/chapter.tex'])])
    wait(lambda: record()[1]['server'] == socket, 'reopened source automatically reattaches callback')
    wait(lambda: expr('b:vimtex.compiler.is_running() ? 1 : 0') == '1', 'reopened source resumes compilation')
    assert pids(pdf) == [pid]
    expr("luaeval(\"vim.api.nvim_buf_set_lines(0, -1, -1, false, {'', string.char(92)..'newpage', 'Fourth page after reopening.'})\")")
    command('write')
    wait(lambda: pages(pid) == 4, 'reopened chapter save rebuilds and refreshes existing PDF')
    subprocess.run(['tex-view', 'callback', str(metadata), './sections/chapter.tex', '4'], check=True, timeout=30)
    assert json.loads(expr('json_encode([expand("%:p"), line(".")])')) == [root + '/sections/chapter.tex', 4]
    print('PASS editor restart restores compiler, existing viewer refresh, and inverse search without explicit view', flush=True)
    os.kill(pid, 15)
    wait(lambda: not pids(pdf), 'close viewer')
    checked(['touch', root + '/main.pdf'])
    time.sleep(2)
    assert not pids(pdf)
    command('VimtexView')
    pid = wait(lambda: next(iter(pids(pdf)), None), 'explicit reopen')
    viewer_pids.add(pid)
    wait(lambda: pages(pid) == 4, 'reopened rendered PDF')
    print('PASS closed window stays closed until explicit reopen', flush=True)
    print('ALL CHECKS PASSED:', host or 'local WSL', flush=True)
finally:
    for pid in viewer_pids:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    run(['nvim', '--server', socket, '--remote-send', '<Cmd>qa!<CR>'])
    run(['tmux', '-L', tmux_name, 'kill-server'])
    if bridge:
        bridge.terminate()
        bridge.wait(timeout=15)

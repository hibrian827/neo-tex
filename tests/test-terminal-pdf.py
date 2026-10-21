"""Exercise terminal-only opening, external rebuilds, and watcher cleanup.

Uses the installed pdf command and a fake Windows viewer; no Neovim or GUI opens.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def wait(check, description):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.1)
    raise RuntimeError('Timed out: ' + description)


with tempfile.TemporaryDirectory(prefix="terminal paper's ") as tmp:
    root = Path(tmp)
    pdf = root / 'paper with spaces.pdf'
    original = b'%PDF-1.5\noriginal\n%%EOF\n'
    rebuilt = b'%PDF-1.5\nrebuilt by an external agent\n%%EOF\n'
    pdf.write_bytes(original)
    (root / '.texbuild.json').write_text(json.dumps({'main': 'paper.tex', 'pdf': pdf.name, 'command': 'make'}))
    binary = root / 'bin'
    binary.mkdir()
    opened = root / 'viewer-open'
    opened.touch()
    launches = root / 'launches'
    fake = binary / 'SumatraPDF'
    fake.write_text('#!' + sys.executable + '\nfrom pathlib import Path\n'
                    + f'with Path({str(launches)!r}).open("a") as out: out.write("open\\n")\n')
    fake.chmod(0o700)
    source = Path(__file__).resolve().parents[1] / 'tex-view.py'
    helper = binary / 'tex-view'
    helper.write_text('#!' + sys.executable + '\nimport runpy,sys\nfrom pathlib import Path\n'
                      + f'm=runpy.run_path({str(source)!r})\ng=m["main"].__globals__\n'
                      + 'g["SELF"]=sys.argv[0]\ng["require_sumatra"]=lambda: None\n'
                      + f'g["sumatra_executable"]=lambda: Path({str(fake)!r})\n'
                      + f'g["windows_cache"]=lambda: Path({str(root / "windows")!r})\n'
                      + f'g["pdf_is_open"]=lambda *args: Path({str(opened)!r}).exists()\n'
                      + 'g["windows_path"]=str\nsys.exit(m["main"]())\n')
    helper.chmod(0o700)
    env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'], XDG_CACHE_HOME=str(root / 'cache'))
    env.pop('SSH_CONNECTION', None)
    request_key = hashlib.sha256(str(pdf).encode()).hexdigest()[:24]
    request = root / 'cache/tex-view/requests' / (request_key + '.json')
    identity = hashlib.sha256(str(request).encode()).hexdigest()[:24]
    target = root / 'windows' / identity / pdf.name
    lock = root / 'cache/tex-view/locks' / identity

    def watcher_stopped():
        if not lock.exists():
            return False
        with lock.open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False

    try:
        subprocess.run(['pdf'], cwd=root, env=env, check=True, timeout=10)
        wait(lambda: target.exists() and target.read_bytes() == original, 'terminal PDF opens')
        wait(launches.exists, 'viewer launches')
        assert json.loads(request.read_text())['editors'] == []
        pdf.write_bytes(b'%PDF-1.5\nincomplete build')
        time.sleep(1.2)
        assert target.read_bytes() == original
        pdf.write_bytes(rebuilt)
        wait(lambda: target.read_bytes() == rebuilt, 'external rebuild reaches viewer cache')
        assert launches.read_text() == 'open\n'
        print('PASS terminal pdf selects project output and refreshes external builds without Neovim or reopening the viewer', flush=True)
        subprocess.run(['pdf', str(pdf)], cwd=root, env=env, check=True, timeout=10)
        wait(lambda: launches.read_text().count('open\n') == 2, 'explicit PDF reopen')
        opened.unlink()
        wait(watcher_stopped, 'watcher exits after PDF closes with no attached editor')
        print('PASS repeated terminal open reuses the subscription; closing the PDF stops its watcher', flush=True)
    finally:
        subprocess.run([str(helper), 'stop', str(pdf)], env=env, check=True, timeout=10)
        if lock.exists():
            wait(watcher_stopped, 'test watcher cleanup')

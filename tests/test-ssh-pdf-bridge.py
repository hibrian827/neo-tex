"""Exercise automatic SSH bridge lifetimes using a fake SSH binary and real PTYs.

Run after installation: python3 tests/test-ssh-pdf-bridge.py
No network connections or PDF windows are opened.
"""
import json
import os
from pathlib import Path
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def alive(pid):
    try:
        return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


def wait(check, description):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.03)
    raise RuntimeError('Timed out: ' + description)


tex_view = shutil.which('tex-view')
assert tex_view, 'Install the current configuration first'
with tempfile.TemporaryDirectory(prefix='ssh-pdf-lifecycle-') as tmp:
    root = Path(tmp)
    binary = root / 'bin'
    binary.mkdir()
    fake = binary / 'ssh'
    fake.write_text('#!' + sys.executable + '\n' + '''
import os,sys,time
from pathlib import Path
root=Path(os.environ['TEST_SSH_ROOT'])
args=sys.argv[1:]
if '-G' in args:
 host='off-host' if 'off-host' in args else 'fake-host'
 print('host '+host+'\\nuser tester\\nport 22\\nsessiontype default\\nforkafterauthentication no')
 if host=='fake-host': print('setenv TEX_VIEW_BRIDGE=1')
 sys.exit(0)
if args and args[-1].endswith('tex-view serve'):
 (root/'transport.pid').write_text(str(os.getpid()))
 for line in sys.stdin: print('[]',flush=True)
 sys.exit(0)
name=args[-1]
(root/('session-'+name+'.pid')).write_text(str(os.getpid()))
if name=='fail': sys.exit(9)
while not (root/('release-'+name)).exists(): time.sleep(.03)
sys.exit(7 if name=='two' else 0)
''')
    fake.chmod(0o700)
    environment = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'],
                       XDG_CACHE_HOME=str(root / 'cache'), TEST_SSH_ROOT=tmp)
    processes = []
    descriptors = []
    sessions = root / 'cache/tex-view/ssh-sessions'

    def launch(name, host='fake-host'):
        master, slave = pty.openpty()
        descriptors.append(master)
        child = subprocess.Popen([tex_view, 'connect', '--', host, name],
                                 stdin=slave, stdout=slave, stderr=subprocess.PIPE, env=environment)
        os.close(slave)
        processes.append(child)
        wait(lambda: (root / ('session-' + name + '.pid')).exists(), name + ' SSH starts')
        return child

    def state():
        paths = list(sessions.glob('*/bridge.json'))
        assert len(paths) == 1, paths
        return json.loads(paths[0].read_text())

    def release(child, name, expected=0):
        (root / ('release-' + name)).touch()
        child.wait(timeout=8)
        errors = child.stderr.read().decode()
        assert child.returncode == expected, (child.returncode, errors)
        assert not errors, errors

    try:
        first = launch('one')
        wait(lambda: (root / 'transport.pid').exists(), 'background transport starts')
        bridge_pid = state()['pid']
        transport_pid = int((root / 'transport.pid').read_text())
        second = launch('two')
        assert state()['pid'] == bridge_pid
        assert len(list(sessions.glob('*/*.lease'))) == 2
        release(first, 'one')
        assert alive(bridge_pid) and alive(transport_pid)
        release(second, 'two', 7)
        wait(lambda: not alive(bridge_pid) and not alive(transport_pid), 'last disconnect cleans bridge and transport')
        assert not list(sessions.glob('*/*.lease'))
        assert not list(sessions.glob('*/bridge.json'))
        print('PASS configured ordinary SSH shares one background bridge; last disconnect cleans up; exit code preserved')

        crashed = launch('crash')
        wait(lambda: state()['pid'] != bridge_pid, 'new bridge starts')
        bridge_pid = state()['pid']
        main_pid = int((root / 'session-crash.pid').read_text())
        crashed.kill()
        crashed.wait(timeout=5)
        wait(lambda: not alive(bridge_pid), 'bridge cleans up a killed owner')
        if alive(main_pid): os.kill(main_pid, signal.SIGTERM)
        assert not list(sessions.glob('*/*.lease'))
        recovered = launch('recovered')
        release(recovered, 'recovered')
        print('PASS killed SSH wrapper leaves no running bridge; next connection recovers stale state')

        manual = subprocess.Popen([tex_view, 'bridge', 'fake-host'], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, env=environment)
        processes.append(manual)
        old_transport = int((root / 'transport.pid').read_text())
        wait(lambda: int((root / 'transport.pid').read_text()) != old_transport, 'manual bridge starts')
        using_manual = launch('manual')
        release(using_manual, 'manual')
        assert manual.poll() is None
        manual.terminate()
        manual.wait(timeout=5)
        print('PASS automatic SSH reuses a manually started bridge without taking ownership')

        direct = launch('plain', 'off-host')
        assert not list(sessions.glob('*/*.lease'))
        release(direct, 'plain')
        failed = launch('fail')
        failed.wait(timeout=8)
        assert failed.returncode == 9
        wait(lambda: not list(sessions.glob('*/bridge.json')), 'failed SSH cleans background state')
        print('PASS unconfigured hosts stay direct and failed connections clean up')
    finally:
        for child in processes:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=8)
            if child.stderr: child.stderr.close()
        for descriptor in descriptors: os.close(descriptor)

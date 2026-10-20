"""Regression tests for the bridge's lifecycle, snapshots, and inverse routing."""
import importlib.util
import json
import gzip
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path
import tempfile
import unittest
import sys
sys.dont_write_bytecode = True
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('tex_view', Path(__file__).resolve().parents[1] / 'tex-view.py')
tex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tex)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tex space ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = patch.object(tex, 'CACHE', self.root / 'cache')
        self.cache.start()
        self.addCleanup(self.cache.stop)
        backend = patch.object(tex, 'sumatra_executable', return_value=self.root / 'SumatraPDF.exe')
        backend.start()
        self.addCleanup(backend.stop)
        for mock in (patch.object(tex, 'require_sumatra'),
                     patch.object(tex, 'windows_cache', return_value=self.root / 'win-cache'),
                     patch.object(tex, 'windows_path', side_effect=lambda p: 'C:/' + Path(p).name)):
            mock.start()
            self.addCleanup(mock.stop)
        self.pdf = self.root / 'main paper.pdf'
        self.pdf.write_bytes(b'%PDF-1.5\ncontent\n%%EOF\n')
        self.socket = self.root / 'editor.sock'
        self.socket.touch()
        self.data = dict(pdf=str(self.pdf), root=str(self.root), source=str(self.root / "chapter's file.tex"),
                         server=str(self.socket), line=3, remote=True, id='one', time=1)

    def test_all_attached_editors_are_retained(self):
        second = self.root / 'second.sock'
        second.touch()
        tex.request(dict(self.data))
        tex.request(dict(self.data, server=str(second)))
        path = tex.CACHE / 'requests' / (tex.key(str(self.pdf)) + '.json')
        data = json.loads(path.read_text())
        self.assertEqual({e['server'] for e in data['editors']}, {str(self.socket), str(second)})
        second.unlink()  # The latest editor exits; the first still references the PDF.
        self.assertTrue(tex.serving(data))
        self.socket.unlink()
        self.assertFalse(tex.serving(data))

    def test_editor_buffer_reference_and_rpc_failure(self):
        entry = dict(server=str(self.socket), main=str(self.root / "paper's main.tex"))
        tex.EDITOR_CHECKS.clear()
        with patch.object(tex.subprocess, 'run', return_value=Mock(returncode=0, stdout='0')):
            self.assertFalse(tex.editor_references(entry))
        tex.EDITOR_CHECKS.clear()
        with patch.object(tex.subprocess, 'run', return_value=Mock(returncode=0, stdout='1')):
            self.assertTrue(tex.editor_references(entry))

        tex.EDITOR_CHECKS.clear()
        with patch.object(tex.subprocess, 'run', side_effect=subprocess.TimeoutExpired('nvim', 3)):
            self.assertTrue(tex.editor_references(entry))

    @unittest.skipUnless(shutil.which('nvim'), 'Neovim is required for the RPC test')
    def test_real_editor_reference_disappears_after_buffer_wipe(self):
        socket = str(self.root / 'real-editor.sock')
        main = str(self.root / "paper's main.tex")
        process = subprocess.Popen(['nvim', '--clean', '--headless', '--listen', socket,
                                    '-c', 'lua vim.b.vimtex = {tex = vim.env.NEO_TEX_TEST_MAIN}'],
                                   env=dict(os.environ, NEO_TEX_TEST_MAIN=main),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not Path(socket).exists() and time.monotonic() < deadline:
                time.sleep(.02)
            entry = dict(server=socket, main=main)
            tex.EDITOR_CHECKS.clear()
            self.assertTrue(tex.editor_references(entry))
            subprocess.run(['nvim', '--server', socket, '--remote-expr', "execute('enew | bwipeout 1')"],
                           capture_output=True, check=True, timeout=5)
            tex.EDITOR_CHECKS.clear()
            self.assertFalse(tex.editor_references(entry))
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_unused_requires_both_closed_and_grace_period(self):
        with patch.object(tex.time, 'monotonic', return_value=0):
            viewer = tex.Viewer('lifecycle')
        with patch.object(tex, 'pdf_is_open', return_value=False), \
             patch.object(tex.time, 'monotonic', return_value=20):
            self.assertFalse(viewer.unused(True, 'main.pdf'))
            self.assertFalse(viewer.unused(False, 'main.pdf'))
        with patch.object(tex, 'pdf_is_open', return_value=False), \
             patch.object(tex.time, 'monotonic', return_value=26):
            self.assertTrue(viewer.unused(False, 'main.pdf'))
            self.assertFalse(viewer.unused(False, 'main.pdf', 'new-request'))
        for state in (True, None):
            with patch.object(tex, 'pdf_is_open', return_value=state), \
                 patch.object(tex.time, 'monotonic', return_value=40):
                self.assertFalse(viewer.unused(False, 'main.pdf'))
                self.assertIsNone(viewer.closed_since)

    def test_sumatra_tab_detection_is_scoped_and_includes_background_tabs(self):
        record = dict(command='SumatraPDF.exe -appdata "C:/preview"', windows=[
            dict(title='other.pdf - SumatraPDF', tabs=['Home', 'main.pdf', 'other.pdf'])])
        with patch.object(tex, 'sumatra_windows', return_value=[record]), \
             patch.object(tex, 'windows_path', return_value='C:/preview'):
            self.assertTrue(tex.pdf_is_open(self.root, 'main.pdf'))
            record['windows'][0]['tabs'].remove('main.pdf')
            self.assertFalse(tex.pdf_is_open(self.root, 'main.pdf'))
            record['command'] = 'SumatraPDF.exe -appdata "C:/another-preview"'
            record['windows'][0]['tabs'].append('main.pdf')
            self.assertFalse(tex.pdf_is_open(self.root, 'main.pdf'))
        with patch.object(tex, 'sumatra_windows', return_value=None):
            self.assertIsNone(tex.pdf_is_open(self.root, 'main.pdf'))

    def test_local_watcher_exits_when_both_consumers_are_gone(self):
        path = tex.CACHE / 'requests' / (tex.key(str(self.pdf)) + '.json')
        tex.write_json(path, dict(self.data, remote=False))
        self.socket.unlink()
        with patch.object(tex.Viewer, 'unused', return_value=True) as unused, \
             patch.object(tex.subprocess, 'Popen') as launch:
            tex.watch(path)
            unused.assert_called_once_with(False, self.pdf.name, self.data['id'])
            launch.assert_not_called()
        self.assertTrue(path.exists())  # Preserve history so a later editor can resume.

    def test_unavailable_viewer_rejects_local_request_before_spawning(self):
        for reason in ('SumatraPDF missing', 'Windows interop unavailable', 'executable cannot run'):
            with self.subTest(reason=reason), \
                 patch.object(tex, 'require_sumatra', side_effect=RuntimeError(reason)), \
                 patch.object(tex.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(RuntimeError, reason):
                    tex.request(dict(self.data, remote=False))
                launch.assert_not_called()
                self.assertEqual(list((tex.CACHE / 'requests').glob('*.json')), [])

    def test_unavailable_viewer_skips_bridge_but_preserves_ssh(self):
        with patch.object(tex, 'require_sumatra', side_effect=RuntimeError('missing viewer')), \
             patch.object(tex.subprocess, 'Popen') as launch, \
             patch.object(tex.subprocess, 'call', return_value=0) as ssh:
            self.assertEqual(tex.managed_ssh('host', ['ssh', 'host']), 0)
            launch.assert_not_called()
            ssh.assert_called_once_with(['ssh', 'host'])
            self.assertFalse((tex.CACHE / 'ssh-sessions').exists())

    def test_explicit_request_only_opens_once_even_after_close(self):
        with patch.object(tex.subprocess, 'Popen') as launch, patch.object(tex.subprocess, 'run'):
            launch.return_value.poll.return_value = None
            viewer = tex.Viewer('project')
            viewer.update(self.data, self.pdf)
            viewer.update(self.data, self.pdf)
            self.assertEqual(launch.call_count, 1)
            launch.return_value.poll.return_value = 0
            viewer.update(self.data, self.pdf)
            self.assertEqual(launch.call_count, 1)
            viewer.update(dict(self.data, id='two'), self.pdf)
            self.assertEqual(launch.call_count, 2)

    def test_local_watcher_delivers_external_rebuild_after_editor_exit_until_stopped(self):
        path = tex.CACHE / 'requests' / (tex.key(str(self.pdf)) + '.json')
        tex.write_json(path, dict(self.data, remote=False))
        self.socket.unlink()
        original = self.pdf.read_bytes()
        rebuilt = b'%PDF-1.5\nexternal rebuild\n%%EOF\n'
        sync = self.pdf.with_suffix('.synctex.gz')
        target = self.root / 'win-cache' / tex.key(str(path)) / self.pdf.name
        ticks = 0

        def advance(_):
            nonlocal ticks
            ticks += 1
            if ticks == 2:
                self.assertEqual(target.read_bytes(), original)
                self.pdf.unlink()  # A clean build can temporarily remove its output.
            elif ticks == 3:
                self.pdf.write_bytes(b'%PDF-1.5\npartial')
            elif ticks == 5:
                self.assertEqual(target.read_bytes(), original)
                self.pdf.write_bytes(rebuilt)
                sync.write_bytes(gzip.compress(b'SyncTeX Version:1\n'))
            elif ticks == 7:
                self.assertEqual(target.read_bytes(), rebuilt)
                self.assertTrue(target.with_suffix('.synctex.gz').exists())
                tex.stop(self.pdf)
            elif ticks > 7:
                self.fail('Watcher did not stop after removing the subscription')

        with patch.object(tex.time, 'sleep', side_effect=advance), \
             patch.object(tex.subprocess, 'Popen') as launch:
            launch.return_value.poll.return_value = None
            tex.watch(path)
            self.assertEqual(launch.call_count, 1)  # Rebuilds update, never reopen.
        self.assertEqual(ticks, 7)

    def test_subscribed_remote_pdf_keeps_updating_after_editor_exit(self):
        ident = tex.key(str(self.pdf))
        path = tex.CACHE / 'requests' / (ident + '.json')
        tex.write_json(path, self.data)
        pending = {}
        tex.poll({}, pending)
        item = tex.poll({}, pending)[0]
        known = {ident: item['stamp']}
        self.socket.unlink()
        self.pdf.write_bytes(b'%PDF-1.5\nexternal rebuild\n%%EOF\n')
        self.assertNotIn('files', tex.poll(known, pending)[0])
        self.assertIn('files', tex.poll(known, pending)[0])
        self.assertEqual(tex.poll({}, {}), [])  # A new bridge cannot revive it.
        tex.stop(self.pdf)
        self.assertEqual(tex.poll(known, pending), [])

    def test_stable_snapshot_manual_build_and_missing_synctex(self):
        tex.write_json(tex.CACHE / 'requests' / 'project.json', self.data)
        pending = {}
        first = tex.poll({}, pending)[0]
        self.assertNotIn('files', first)
        second = tex.poll({}, pending)[0]
        self.assertIn('files', second)
        self.assertIsNone(second['files'][1])
        known = {'project': second['stamp']}
        self.assertNotIn('files', tex.poll(known, pending)[0])
        self.pdf.write_bytes(b'%PDF-1.5\nmanual rebuild\n%%EOF\n')
        self.assertNotIn('files', tex.poll(known, pending)[0])
        self.assertIn('files', tex.poll(known, pending)[0])
        self.pdf.write_bytes(b'%PDF-1.5\npartial')
        tex.poll(known, pending)
        self.assertNotIn('files', tex.poll(known, pending)[0])

    def test_reconnect_ignores_old_request_but_new_request_gets_pdf(self):
        path = tex.CACHE / 'requests' / 'project.json'
        tex.write_json(path, self.data)
        pending = {}
        self.assertEqual(tex.poll({}, pending, ['one']), [])
        tex.write_json(path, dict(self.data, id='two'))
        self.assertNotIn('files', tex.poll({}, pending, ['one'])[0])
        self.assertIn('files', tex.poll({}, pending, ['one'])[0])

    def test_dead_editor_is_not_served(self):
        tex.write_json(tex.CACHE / 'requests' / 'project.json', self.data)
        self.socket.unlink()
        self.assertEqual(tex.poll({}, {}), [])

    def test_ssh_auto_bridge_obeys_host_marker_and_keeps_connection_options(self):
        baseline = {'host': ['Cyclonus'], 'user': ['brian'], 'port': ['22'], 'setenv': ['TEX_VIEW_BRIDGE=1']}
        configured = dict(baseline, user=['another-user'], port=['2222'])
        with patch.object(sys.stdin, 'isatty', return_value=True), \
             patch.object(sys.stdout, 'isatty', return_value=True), \
             patch.object(tex, 'ssh_config', side_effect=[configured, baseline]), \
             patch.object(tex, 'managed_ssh', return_value=7) as managed:
            args = ['-p', '2222', '-l', 'another-user', 'Cyclonus']
            self.assertEqual(tex.connect(args), 7)
            managed.assert_called_once_with('Cyclonus', ['ssh'] + args,
                                            ['-o', 'user=another-user', '-o', 'port=2222'])

    def test_ssh_scripts_unconfigured_hosts_and_forwarding_remain_direct(self):
        cases = [(False, {}, ['Cyclonus']),
                 (True, {'host': ['Other']}, ['Other']),
                 (True, {'host': ['Cyclonus'], 'setenv': ['TEX_VIEW_BRIDGE=1'], 'sessiontype': ['none']}, ['-N', 'Cyclonus']),
                 (True, {}, ['-G', 'Cyclonus'])]
        for interactive, config, args in cases:
            with self.subTest(args=args, interactive=interactive), \
                 patch.object(sys.stdin, 'isatty', return_value=interactive), \
                 patch.object(sys.stdout, 'isatty', return_value=interactive), \
                 patch.object(tex, 'ssh_config', return_value=config), \
                 patch.object(tex, 'managed_ssh') as managed, \
                 patch.object(tex.subprocess, 'call', return_value=0) as direct:
                self.assertEqual(tex.connect(args), 0)
                managed.assert_not_called()
                direct.assert_called_once_with(['ssh'] + args)

    def test_finished_viewer_child_is_reaped(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        try:
            deadline = time.monotonic() + 3
            status = Path('/proc') / str(child.pid) / 'stat'
            while time.monotonic() < deadline and status.read_text().split()[2] != 'Z':
                time.sleep(.02)
            viewer = tex.Viewer('reap')
            viewer.launchers = [child]
            viewer.reap()
            self.assertEqual(child.returncode, 0)
            self.assertEqual(viewer.launchers, [])
            self.assertFalse(status.exists())
        finally:
            child.wait(timeout=3)

    def test_ctrl_c_stops_bridge_transport_without_traceback(self):
        fake_bin = self.root / 'bin'
        fake_bin.mkdir()
        ssh = fake_bin / 'ssh'
        ssh.write_text('#!' + sys.executable + '\nimport sys\nfor line in sys.stdin:\n print("[]", flush=True)\n')
        ssh.chmod(0o700)
        env = dict(os.environ, PATH=str(fake_bin) + ':' + os.environ['PATH'], XDG_CACHE_HOME=str(self.root))
        bridge = subprocess.Popen([sys.executable, str(Path(tex.__file__)), 'bridge', 'fake-host'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            self.assertIn('PDF bridge connected', bridge.stdout.readline())
            children = Path('/proc') / str(bridge.pid) / 'task' / str(bridge.pid) / 'children'
            deadline = time.monotonic() + 3
            while not children.read_text().strip() and time.monotonic() < deadline:
                time.sleep(.02)
            child_ids = children.read_text().split()
            self.assertTrue(child_ids)
            bridge.send_signal(signal.SIGINT)
            _, errors = bridge.communicate(timeout=5)
            self.assertEqual(bridge.returncode, 130)
            self.assertNotIn('Traceback', errors)
            self.assertTrue(all(not (Path('/proc') / pid).exists() for pid in child_ids))
        finally:
            if bridge.poll() is None:
                bridge.terminate()
                bridge.communicate(timeout=5)

    def test_final_build_output_is_served_after_editor_exit_then_expires(self):
        path = tex.CACHE / 'requests' / (tex.key(self.data['pdf']) + '.json')
        tex.write_json(path, self.data)
        with patch.object(tex.subprocess, 'run') as build:
            tex.finish(dict(self.data, build_command='latexmk -pdf main.tex'))
            self.assertEqual(build.call_args.kwargs['cwd'], self.data['root'])
        self.socket.unlink()
        pending = {}
        self.assertTrue(tex.poll({}, pending))
        self.assertIn('files', tex.poll({}, pending)[0])
        with patch.object(tex.time, 'time', return_value=json.loads(path.read_text())['drain_until'] + 1):
            self.assertEqual(tex.poll({}, {}), [])

    def test_multiple_sumatra_documents_route_independently_even_with_first_command(self):
        first = tex.CACHE / 'viewers/first/callback.json'
        second = tex.CACHE / 'viewers/second/callback.json'
        source1 = 'c:/preview/first/source-1.tex'
        source2 = 'c:/preview/second/source-1.tex'
        tex.write_json(first, dict(self.data, remote=False, windows_sources={source1: '/first/main.tex'}))
        tex.write_json(second, dict(self.data, server='/second/editor.sock', windows_sources={source2: '/second/main.tex'}))
        with patch.object(tex, 'inverse') as navigate:
            tex.callback(first, source2.replace('/', '\\'), 9)
            self.assertEqual(navigate.call_args.args[0]['server'], '/second/editor.sock')
            self.assertEqual(navigate.call_args.args[1:], ('/second/main.tex', 9))
            self.socket.unlink()
            tex.sumatra_callback(source2, 3)
            self.assertEqual(navigate.call_args.args[0]['server'], '/second/editor.sock')
            with self.assertRaises(ValueError):
                tex.sumatra_callback('C:/unrelated/nonexistent.tex', 2)
            self.assertEqual(navigate.call_count, 2)

    def test_resume_requires_previous_view_and_replaces_dead_editor(self):
        data = dict(self.data, remote=False)
        with patch.object(tex.subprocess, 'Popen') as launch:
            self.assertFalse(tex.request(dict(data), resume=True))
            launch.assert_not_called()
            tex.request(dict(data))
            launch.reset_mock()
            self.assertTrue(tex.request(dict(data, server='/new/editor.sock'), resume=True))
            path = tex.CACHE / 'requests' / (tex.key(data['pdf']) + '.json')
            record = json.loads(path.read_text())
            self.assertEqual(record['server'], '/new/editor.sock')
            self.assertTrue(record['reconnect'])
            launch.assert_called_once()
            launch.reset_mock()
            viewer = tex.Viewer('resume')
            viewer.update(record, self.pdf)
            launch.assert_not_called()  # Resume never opens a closed PDF.
            self.assertEqual(json.loads((viewer.folder / 'callback.json').read_text())['server'], '/new/editor.sock')

    def test_inverse_preserves_special_names_and_maps_cached_path(self):
        data = dict(self.data, cache_dir='/tmp/view-cache')
        with patch.object(tex.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            tex.inverse(data, "/tmp/view-cache/chapter's file.tex", 7)
            args = run.call_args.args[0]
            self.assertEqual(args[:3], ['nvim', '--server', str(self.socket)])
            self.assertIn("chapter''s file.tex", args[-1])
            self.assertNotIn('/tmp/view-cache/', args[-1])

    def test_remote_request_does_not_launch_remote_gui(self):
        with patch.object(tex.subprocess, 'Popen') as launch:
            tex.request(self.data)
            launch.assert_not_called()
            records = list((tex.CACHE / 'requests').glob('*.json'))
            self.assertEqual(len(records), 1)
            self.assertNotEqual(json.loads(records[0].read_text())['id'], 'one')

    def test_sumatra_refresh_mapping_and_explicit_reopen(self):
        exe = self.root / 'windows/SumatraPDF.exe'
        exe.parent.mkdir()
        exe.touch()
        sync = self.pdf.with_suffix('.synctex.gz')
        sync.write_bytes(gzip.compress(('SyncTeX Version:1\nInput:1:' + self.data['source'] + '\n').encode()))
        def winpath(path):
            return 'C:/Windows cache/' + Path(path).name
        with patch.object(tex, 'windows_path', side_effect=winpath), \
             patch.object(tex, 'windows_cache', return_value=exe.parent / 'viewers'), \
             patch.dict(os.environ, WSL_DISTRO_NAME='Ubuntu', USER='brian'), \
             patch.object(tex.subprocess, 'Popen') as launch:
            viewer = tex.Viewer('sumatra', 'test-server')
            viewer.sumatra = exe
            viewer.update(self.data, self.pdf)
            self.assertEqual(launch.call_count, 1)
            args = launch.call_args.args[0]
            self.assertIn('-forward-search', args)
            self.assertEqual(Path(launch.call_args.kwargs['stdout'].name).name, 'viewer.log')
            self.assertEqual(launch.call_args.kwargs['stderr'], subprocess.STDOUT)
            editor = args[args.index('-inverse-search') + 1]
            self.assertTrue(editor.startswith('wscript.exe //B //Nologo '))
            self.assertIn('C:/Windows cache/sumatra/source-1.tex', args)
            metadata = viewer.folder / 'callback.json'
            record = json.loads(metadata.read_text())
            self.assertEqual(record['host'], 'test-server')
            self.assertEqual(record['windows_sources']['c:/windows cache/sumatra/source-1.tex'], self.data['source'])
            target = exe.parent / 'viewers/sumatra' / self.pdf.name
            script = (target.parent / 'inverse-search.vbs').read_text(encoding='utf-16')
            self.assertIn('shell.Run(command, 0, True)', script)
            self.assertIn('WScript.Arguments(0)', script)
            self.pdf.write_bytes(b'%PDF-1.5\nmanual rebuild\n%%EOF\n')
            viewer.update(self.data, self.pdf)
            self.assertEqual(target.read_bytes(), self.pdf.read_bytes())
            self.assertEqual(launch.call_count, 1)  # Updates cannot reopen a closed window.
            viewer.update(dict(self.data, id='two'), self.pdf)
            self.assertEqual(launch.call_count, 2)
            viewer.update(dict(self.data, id='three', reconnect=True, server='/new/editor.sock'), self.pdf)
            self.assertEqual(launch.call_count, 2)
            self.assertEqual(json.loads(metadata.read_text())['server'], '/new/editor.sock')
            with patch.object(tex.subprocess, 'run') as run:
                tex.callback(metadata, 'C:\\Windows cache\\sumatra\\source-1.tex', 7)
                payload = json.loads(run.call_args.kwargs['input'])
                self.assertEqual(payload['filename'], self.data['source'])

    def test_sumatra_updates_open_window_callback_without_changing_preferences(self):
        folder = self.root / 'Windows viewer'
        folder.mkdir()
        settings = folder / 'SumatraPDF-settings.txt'
        settings.write_text('Theme = Dark\nInverseSearchCmdLine = wsl.exe old\nZoom = 125\n')
        with patch.object(tex, 'windows_path', return_value='C:\\Windows viewer\\inverse-search.vbs'), \
             patch.dict(os.environ, WSL_DISTRO_NAME='Ubuntu', USER='brian'):
            editor = tex.sumatra_editor(folder, self.root / 'callback.json')
        self.assertIn('"C:\\Windows viewer\\inverse-search.vbs"', editor)
        self.assertEqual(settings.read_text(),
                         'Theme = Dark\nInverseSearchCmdLine = ' + editor + '\nZoom = 125\n')


if __name__ == '__main__':
    unittest.main()

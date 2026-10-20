#!/usr/bin/env python3
"""Explicit PDF viewing, with an SSH transport for remote VimTeX projects.

No network listener: the bridge speaks newline-delimited JSON over SSH stdio.
Only explicit view requests launch windows; file updates never do.
"""
import argparse
import base64
import fcntl
import gzip
import hashlib
import json
import os
import re
from pathlib import Path, PureWindowsPath
import shlex
import signal
import subprocess
import sys
import time
import tempfile
import uuid

CACHE = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'tex-view'
SELF = str(Path(sys.argv[0]).absolute())


def directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def atomic(path, data):
    directory(path.parent)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_bytes(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, data):
    atomic(path, json.dumps(data).encode())


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def lock(name):
    handle = (directory(CACHE / 'locks') / name).open('w')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def request(data, resume=False):
    for field in ('pdf', 'root', 'source', 'server'):
        if not isinstance(data.get(field), str) or not data[field]:
            raise ValueError('Missing request field: ' + field)
    path = directory(CACHE / 'requests') / (key(data['pdf']) + '.json')
    if resume and (not path.exists() or not Path(data['pdf']).is_file()):
        return False
    if not Path(data['pdf']).is_file():
        raise ValueError('Compile the document before opening its PDF.')
    if not data.get('remote'):
        require_sumatra()
    data.update(id=uuid.uuid4().hex, time=time.time(), reconnect=resume)
    # Keep every editor that has attached to this document, not only the latest.
    with (directory(CACHE / 'locks') / ('request-' + key(data['pdf']))).open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        previous = json.loads(path.read_text()) if path.exists() else {}
        editors = {entry['server']: entry for entry in previous.get('editors', [previous])
                   if entry.get('server') and Path(entry['server']).exists()}
        editors[data['server']] = {name: data[name] for name in ('server', 'main') if name in data}
        data['editors'] = list(editors.values())
        write_json(path, data)
    if not data.get('remote'):
        with (CACHE / 'local.log').open('ab') as log:
            subprocess.Popen([SELF, 'watch', str(path)], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
    return True


def focus(tmux, pane):
    if tmux and pane:
        prefix = ['tmux', '-S', tmux.split(',')[0]]
        for command in ('select-window', 'select-pane'):
            subprocess.run(prefix + [command, '-t', pane], capture_output=True, timeout=5)


def inverse(data, filename, line):
    # A copied SyncTeX file may resolve relative inputs under the local cache.
    source = Path(filename)
    cache_dir = data.get('cache_dir')
    if cache_dir and source.is_absolute() and source.is_relative_to(cache_dir):
        source = Path(data['root']) / source.relative_to(cache_dir)
    elif not source.is_absolute():
        source = Path(data['root']) / source
    args = json.dumps([str(source), int(line)])
    expression = "luaeval(\"require('neo_tex').inverse(_A)\", json_decode('%s'))" % args.replace("'", "''")
    result = subprocess.run(['nvim', '--server', data['server'], '--remote-expr', expression],
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'The Neovim session has ended.')
    focus(data.get('tmux'), data.get('pane'))


def ssh_options():
    return json.loads(os.environ.get('TEX_VIEW_SSH_OPTIONS', '[]'))


def connection_name(host, options=None):
    options = ssh_options() if options is None else options
    return host + ('\0' + json.dumps(options) if options else '')


def ssh_command(host, command, options=None):
    if host.startswith('-'):
        raise ValueError('Use an SSH host alias, not SSH options.')
    return ['ssh'] + (ssh_options() if options is None else options) + ['-T', '-o', 'RemoteCommand=none',
            '-o', 'PermitLocalCommand=no', '-o', 'BatchMode=yes', '-o', 'ClearAllForwardings=yes',
            '-o', 'ConnectTimeout=8', host,
            'export PATH="$HOME/.local/bin:$HOME/.local/state/nix/profiles/home-manager/home-path/bin:$PATH"; '
            'exec tex-view ' + command]


def callback(path, filename, line):
    data = json.loads(Path(path).read_text())
    if (data.get('windows_sources') and PureWindowsPath(filename).is_absolute()
            and filename.replace('\\', '/').lower() not in data['windows_sources']):
        # Repair commands cached by already-open Sumatra instances as well.
        return sumatra_callback(filename, line)
    # Sumatra's SyncTeX inputs use stable Windows cache paths, even for SSH sources.
    filename = data.get('windows_sources', {}).get(filename.replace('\\', '/').lower(), filename)
    if data.get('host'):
        payload = dict(data=data, filename=filename, line=int(line))
        subprocess.run(ssh_command(data['host'], 'inverse', data.get('ssh_options', [])), input=json.dumps(payload),
                       text=True, check=True, timeout=25)
        focus(data.get('local_tmux'), data.get('local_pane'))
    else:
        inverse(data, filename, line)


def sumatra_callback(filename, line):
    name = filename.replace('\\', '/').lower()
    for metadata in (CACHE / 'viewers').glob('*/callback.json'):
        try:
            data = json.loads(metadata.read_text())
        except (OSError, ValueError):
            continue
        if name in data.get('windows_sources', {}):
            return callback(metadata, filename, line)
    raise ValueError('No LaTeX preview owns this SyncTeX source: ' + filename)


EDITOR_CHECKS = {}


def editor_references(entry):
    server = entry['server']
    if not Path(server).exists():
        return False
    if not entry.get('main'):  # Compatibility with requests from older editors.
        return True
    identity = (server, entry['main'])
    cached = EDITOR_CHECKS.get(identity)
    if cached and time.monotonic() - cached[0] < 5:
        return cached[1]
    lua = ('(function() for _, b in ipairs(vim.api.nvim_list_bufs()) do '
           'local s = vim.b[b].vimtex; '
           'if vim.api.nvim_buf_is_loaded(b) and s and s.tex == _A then return 1 end '
           'end return 0 end)()')
    expression = "luaeval(" + json.dumps(lua) + ", json_decode('" + json.dumps(entry['main']).replace("'", "''") + "'))"
    try:
        result = subprocess.run(['nvim', '--server', server, '--remote-expr', expression],
                                capture_output=True, text=True, timeout=3)
        # A busy editor or unavailable RPC is not evidence that the reference is gone.
        alive = result.returncode != 0 or result.stdout.strip() != '0'
    except (OSError, subprocess.SubprocessError):
        alive = True
    EDITOR_CHECKS[identity] = (time.monotonic(), alive)
    return alive


def serving(data):
    return (time.time() < data.get('drain_until', 0)
            or any(editor_references(entry) for entry in data.get('editors', [data])))


def stop(pdf):
    """Forget a preview subscription without closing its PDF window."""
    path = CACHE / 'requests' / (key(str(pdf.absolute())) + '.json')
    path.unlink(missing_ok=True)


def finish(data):
    """Build saved sources once before editor exit, then allow final output delivery."""
    path = CACHE / 'requests' / (key(data['pdf']) + '.json')
    if not path.exists():
        return
    current = json.loads(path.read_text())
    if current['server'] != data['server']:
        return
    log = CACHE / ('final-build-' + key(data['pdf']) + '.log')
    try:
        with log.open('wb') as output:
            subprocess.run(['/bin/sh', '-c', data['build_command']], cwd=data['root'],
                           stdout=output, stderr=subprocess.STDOUT, timeout=120, check=True)
    finally:
        current = json.loads(path.read_text())
        if current['server'] == data['server']:
            current['drain_until'] = time.time() + 15
            write_json(path, current)


def windows_path(path):
    return subprocess.check_output(['wslpath', '-w', str(path)], text=True).strip()


def windows_cache():
    config = Path.home() / '.config/tex-view/sumatra.json'
    if config.exists():
        settings = json.loads(config.read_text())
        return Path(settings.get('cache', str(Path(settings['executable']).parent / 'viewers')))
    local = subprocess.check_output(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
         '[Environment]::GetFolderPath("LocalApplicationData")'], text=True).strip()
    return Path(subprocess.check_output(['wslpath', '-u', local], text=True).strip()) / 'tex-view/viewers'


def sumatra_executable():
    configured = os.environ.get('TEX_VIEW_SUMATRA')
    if configured:
        if not Path(configured).is_file():
            raise ValueError('TEX_VIEW_SUMATRA must name an existing SumatraPDF.exe in WSL path form.')
        return Path(configured)
    config = Path.home() / '.config/tex-view/sumatra.json'
    if os.environ.get('WSL_DISTRO_NAME') and config.exists():
        exe = Path(json.loads(config.read_text())['executable'])
        if exe.is_file():
            return exe
    return None


def require_sumatra():
    """Fail synchronously before spawning a local watcher or remote bridge."""
    try:
        if os.environ.get('TEX_VIEW_VIEWER', 'sumatra') != 'sumatra':
            raise ValueError('Only SumatraPDF is supported; unset TEX_VIEW_VIEWER.')
        exe = sumatra_executable()
        if exe is None:
            raise ValueError('SumatraPDF was not found. Run ./run.sh install or tex-view install-sumatra in WSL.')
        if not os.access(exe, os.X_OK):
            raise ValueError(f'SumatraPDF is not executable: {exe}')
        with exe.open('rb') as binary:
            if binary.read(2) != b'MZ':
                raise ValueError(f'SumatraPDF is not a Windows executable: {exe}')
        # Check Windows interop and the writable Windows cache before backgrounding.
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', 'exit 0'],
                       check=True, capture_output=True, timeout=10)
        folder = directory(windows_cache())
        windows_path(folder)
        # Render a tiny blank PDF without opening a window to check the actual executable.
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
                   b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                   b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] >>']
        content = b'%PDF-1.4\n'
        offsets = []
        for number, obj in enumerate(objects, 1):
            offsets.append(len(content))
            content += f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n'
        start = len(content)
        content += b'xref\n0 4\n0000000000 65535 f \n'
        content += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets)
        content += f'trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()
        with tempfile.TemporaryDirectory(prefix='.probe-', dir=folder) as temporary:
            probe = Path(temporary) / 'probe.pdf'
            probe.write_bytes(content)
            result = subprocess.run([str(exe), '-bench', windows_path(probe), '1'],
                                    capture_output=True, timeout=10)
            # Sumatra 3.6.1's benchmark exits with its default code of 1.
            if result.returncode not in (0, 1):
                raise RuntimeError(f'SumatraPDF could not run (exit {result.returncode}).')
        return exe
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise RuntimeError(f'PDF preview unavailable: {error}') from error


def install_sumatra():
    """Install the official portable stable release without changing file associations."""
    import io
    import zipfile
    local = subprocess.check_output(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
         '[Environment]::GetFolderPath("LocalApplicationData")'], text=True).strip()
    folder = directory(Path(subprocess.check_output(['wslpath', '-u', local], text=True).strip()) / 'tex-view')
    url = 'https://www.sumatrapdfreader.org/dl/rel/3.6.1/SumatraPDF-3.6.1-64.zip'
    download = subprocess.check_output(['curl', '-fLSs', '--max-time', '60', url])
    archive = zipfile.ZipFile(io.BytesIO(download))
    exe = folder / 'SumatraPDF.exe'
    atomic(exe, archive.read('SumatraPDF-3.6.1-64.exe'))
    exe.chmod(0o755)
    write_json(Path.home() / '.config/tex-view/sumatra.json',
               dict(executable=str(exe), cache=str(folder / 'viewers')))
    print('Installed portable SumatraPDF 3.6.1:', exe)


def sumatra_editor(folder, metadata):
    # wscript is a GUI process; launch WSL hidden from the outset, rather than
    # creating a console and trying to hide it after it has already flashed.
    command = subprocess.list2cmdline([
        'wsl.exe', '-d', os.environ['WSL_DISTRO_NAME'], '-u', os.environ['USER'],
        '--exec', '/usr/bin/env',
        'PATH=' + str(Path.home() / '.local/bin') + ':' +
        str(Path.home() / '.local/state/nix/profiles/home-manager/home-path/bin') + ':/usr/bin:/bin',
        SELF, 'sumatra-callback'])
    script = folder / 'inverse-search.vbs'
    content = ('Option Explicit\r\n'
               'Dim command, shell, result\r\n'
               'If WScript.Arguments.Count <> 2 Then WScript.Quit 2\r\n'
               'command = "' + command.replace('"', '""') + ' " & Chr(34) & '
               'WScript.Arguments(0) & Chr(34) & " " & CLng(WScript.Arguments(1))\r\n'
               'Set shell = CreateObject("WScript.Shell")\r\n'
               'result = shell.Run(command, 0, True)\r\n'
               'WScript.Quit result\r\n')
    atomic(script, b'\xff\xfe' + content.encode('utf-16le'))
    editor = subprocess.list2cmdline(['wscript.exe', '//B', '//Nologo', windows_path(script)]) + ' "%f" %l'
    # -inverse-search is ignored when a command is forwarded to an existing
    # Sumatra instance. Its watched settings file also updates an open window.
    settings = folder / 'SumatraPDF-settings.txt'
    if settings.exists():
        old = settings.read_text(encoding='utf-8')
        lines = old.splitlines(keepends=True)
        replacement = 'InverseSearchCmdLine = ' + editor + '\n'
        for index, line in enumerate(lines):
            if line.lstrip().startswith('InverseSearchCmdLine ='):
                lines[index] = replacement
                break
        else:
            lines.append('\n' + replacement)
        updated = ''.join(lines)
        if updated != old:
            atomic(settings, updated.encode('utf-8'))
    return editor


# Read tab names without activating windows or sending input to the user's desktop.
SUMATRA_WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$ae = [System.Windows.Automation.AutomationElement]
$scope = [System.Windows.Automation.TreeScope]
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Collections.Generic;
public static class NeoTexTabs {
 delegate bool EnumProc(IntPtr hwnd, IntPtr data);
 [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr data);
 [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent, EnumProc callback, IntPtr data);
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder text, int max);
 [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, IntPtr wp, IntPtr lp, uint flags, uint timeout, out IntPtr result);
 [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
 [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr VirtualAllocEx(IntPtr process, IntPtr address, UIntPtr size, uint type, uint protect);
 [DllImport("kernel32.dll")] static extern bool VirtualFreeEx(IntPtr process, IntPtr address, UIntPtr size, uint type);
 [DllImport("kernel32.dll", SetLastError=true)] static extern bool WriteProcessMemory(IntPtr process, IntPtr address, byte[] bytes, UIntPtr size, out UIntPtr done);
 [DllImport("kernel32.dll", SetLastError=true)] static extern bool ReadProcessMemory(IntPtr process, IntPtr address, byte[] bytes, UIntPtr size, out UIntPtr done);
 public static string[] Read(int pid) {
  if(IntPtr.Size!=8) throw new Exception("A 64-bit PowerShell is required");
  var controls=new List<IntPtr>();
  EnumWindows((window,unused)=>{
   uint owner; GetWindowThreadProcessId(window,out owner);
   if(owner==(uint)pid) EnumChildWindows(window,(child,ignored)=>{
    var name=new StringBuilder(256); GetClassName(child,name,256);
    if(name.ToString()=="SysTabControl32") controls.Add(child);
    return true;
   },IntPtr.Zero);
   return true;
  },IntPtr.Zero);
  if(controls.Count==0) return new string[0];
  IntPtr process=OpenProcess(0x438,false,pid);
  if(process==IntPtr.Zero) throw new Exception("Cannot read SumatraPDF tabs");
  IntPtr memory=IntPtr.Zero;
  try {
   memory=VirtualAllocEx(process,IntPtr.Zero,(UIntPtr)32832,0x3000,4);
   if(memory==IntPtr.Zero) throw new Exception("Cannot allocate tab query buffer");
   var names=new List<string>();
   foreach(var control in controls) {
    IntPtr count;
    if(SendMessageTimeout(control,0x1304,IntPtr.Zero,IntPtr.Zero,2,1000,out count)==IntPtr.Zero) throw new Exception("Tab query timed out");
    if(count.ToInt64()<0 || count.ToInt64()>1000) throw new Exception("Invalid tab count");
    for(int index=0;index<count.ToInt32();index++) {
     // TCITEMW for the 64-bit SumatraPDF installed by neo-tex.
     var item=new byte[40]; item[0]=1;
     Array.Copy(BitConverter.GetBytes(memory.ToInt64()+64),0,item,16,8);
     Array.Copy(BitConverter.GetBytes(16384),0,item,24,4);
     UIntPtr done;
     if(!WriteProcessMemory(process,memory,item,(UIntPtr)item.Length,out done)) throw new Exception("Cannot prepare tab query");
     IntPtr result;
     if(SendMessageTimeout(control,0x133c,(IntPtr)index,memory,2,1000,out result)==IntPtr.Zero || result==IntPtr.Zero) throw new Exception("Cannot read tab label");
     var text=new byte[32768];
     if(!ReadProcessMemory(process,new IntPtr(memory.ToInt64()+64),text,(UIntPtr)text.Length,out done)) throw new Exception("Cannot read tab text");
     names.Add(Encoding.Unicode.GetString(text).Split('\0')[0]);
    }
   }
   return names.ToArray();
  } finally {if(memory!=IntPtr.Zero) VirtualFreeEx(process,memory,UIntPtr.Zero,0x8000); CloseHandle(process);}
 }
}
'@

$records = @(Get-CimInstance Win32_Process -Filter "Name='SumatraPDF.exe'" | ForEach-Object {
    $proc = $_
    $nativeTabs = @([NeoTexTabs]::Read([int]$proc.ProcessId))
    $condition = New-Object System.Windows.Automation.PropertyCondition($ae::ProcessIdProperty, [int]$proc.ProcessId)
    $windows = @($ae::RootElement.FindAll($scope::Children, $condition) | ForEach-Object {
        $window = $_
        $tabsCondition = New-Object System.Windows.Automation.PropertyCondition($ae::ControlTypeProperty, [System.Windows.Automation.ControlType]::TabItem)
        $tabs = @($window.FindAll($scope::Descendants, $tabsCondition) | ForEach-Object { $_.Current.Name })
        @{title=$window.Current.Name; tabs=@($tabs + $nativeTabs)}
    })
    @{command=$proc.CommandLine; windows=$windows}
})
ConvertTo-Json -Depth 5 -Compress -InputObject $records
"""
WINDOW_CHECK = None


def sumatra_windows():
    global WINDOW_CHECK
    if WINDOW_CHECK and time.monotonic() - WINDOW_CHECK[0] < 5:
        return WINDOW_CHECK[1]
    try:
        encoded = base64.b64encode(SUMATRA_WINDOWS_SCRIPT.encode('utf-16le')).decode()
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                capture_output=True, text=True, check=True, timeout=5)
        records = json.loads(result.stdout)
        if not isinstance(records, list):
            raise ValueError('Invalid SumatraPDF window snapshot')
    except (OSError, ValueError, subprocess.SubprocessError):
        records = None
    WINDOW_CHECK = (time.monotonic(), records)
    return records


def pdf_is_open(folder, filename):
    records = sumatra_windows()
    if records is None:
        return None
    profile = windows_path(folder).casefold()
    found = []
    for record in records:
        command = record.get('command')
        if command is None:
            return None  # Inaccessible process: don't assume its PDFs are closed.
        match = re.search(r'(?:^|\s)-appdata\s+(?:"([^"]+)"|(\S+))', command, re.I)
        if match and (match[1] or match[2]).casefold() == profile:
            found.extend(record['windows'])
    expected = filename.casefold()
    unknown = False
    for window in found:
        tabs = window['tabs']
        if any(PureWindowsPath(name).name.casefold() == expected for name in tabs):
            return True
        title = window['title'].casefold()
        if title.startswith(expected + ' - ') or title == expected:
            return True
        if not tabs and title not in ('sumatrapdf', ''):
            unknown = True  # Custom title/provider: preserve the subscription.
    return None if unknown else False


class Viewer:
    def __init__(self, ident, host=None):
        self.folder = directory(CACHE / 'viewers' / ident)
        self.host = host
        self.process = None
        self.last_request = None
        self.sumatra = sumatra_executable()
        if self.sumatra is None:
            raise RuntimeError('PDF preview unavailable: SumatraPDF was not found.')
        self.windows_stamp = None
        self.windows_folder = None
        self.launchers = []
        self.started = time.monotonic()
        self.closed_since = None

    def reap(self):
        self.launchers = [process for process in self.launchers if process.poll() is None]

    def unused(self, editor_active, filename, request_id=None):
        if request_id is not None and request_id != self.last_request:
            self.started = time.monotonic()
            self.closed_since = None
            return False
        if editor_active or time.monotonic() - self.started < 10:
            self.closed_since = None
            return False
        try:
            folder = self.windows_folder or (windows_cache() / self.folder.name)
            opened = pdf_is_open(folder, filename)
        except (OSError, ValueError, subprocess.SubprocessError):
            opened = None
        if opened is not False:
            self.closed_since = None
            return False
        if self.closed_since is None:
            self.closed_since = time.monotonic()
        return time.monotonic() - self.closed_since >= 5

    def update_sumatra(self, data, pdf, record, metadata):
        # Keep PDFs on NTFS: Windows file watching and SyncTeX path handling then
        # work identically for local and remote builds, including stable 3.6.x.
        if self.windows_folder is None:
            self.windows_folder = directory(windows_cache() / self.folder.name)
        folder = self.windows_folder
        target = folder / pdf.name
        stamp = fingerprint([pdf, pdf.with_suffix('.synctex.gz')])
        if stamp == self.windows_stamp and data['id'] == self.last_request:
            return
        windows_folder = windows_path(folder)
        sources = {}
        forward = None
        sync = pdf.with_suffix('.synctex.gz')
        if sync.exists():
            lines = gzip.decompress(sync.read_bytes()).decode('utf-8', errors='surrogateescape').splitlines(keepends=True)
            for index, line in enumerate(lines):
                if not line.startswith('Input:'):
                    continue
                _, tag, name = line.rstrip('\r\n').split(':', 2)
                source = Path(name)
                if not source.is_absolute():
                    source = Path(data['root']) / source
                source = Path(os.path.normpath(source))
                stub = folder / ('source-' + tag + '.tex')
                stub.touch(exist_ok=True)
                mapped = windows_folder.replace('\\', '/') + '/' + stub.name
                sources[mapped.lower()] = str(source)
                lines[index] = f'Input:{tag}:{mapped}\n'
                if source == Path(os.path.normpath(data['source'])):
                    forward = mapped
            sync_content = gzip.compress(''.join(lines).encode('utf-8', errors='surrogateescape'))
        else:
            sync_content = None
        record['windows_sources'] = sources
        write_json(metadata, record)
        if stamp != self.windows_stamp:
            publish(target, [pdf.read_bytes(), sync_content])
            self.windows_stamp = stamp
        if data['id'] == self.last_request:
            return
        if data.get('reconnect'):
            self.last_request = data['id']
            return
        editor = sumatra_editor(folder, metadata)
        args = [str(self.sumatra), '-appdata', windows_folder, '-reuse-instance',
                '-inverse-search', editor]
        if forward:
            args += ['-forward-search', forward, str(data['line'])]
        with (self.folder / 'viewer.log').open('ab') as log:
            self.process = subprocess.Popen(args + [windows_path(target)], stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=subprocess.STDOUT)
        self.launchers.append(self.process)
        self.last_request = data['id']

    def update(self, data, pdf):
        self.reap()
        record = dict(data)
        if pdf != Path(data['pdf']):
            record['cache_dir'] = str(pdf.parent)
        if self.host:
            record.update(host=self.host,
                          ssh_options=ssh_options(),
                          local_tmux=os.environ.get('TEX_VIEW_TMUX'), local_pane=os.environ.get('TEX_VIEW_PANE'))
        metadata = self.folder / 'callback.json'
        self.update_sumatra(data, pdf, record, metadata)


def watch(path):
    require_sumatra()
    guard = lock(key(str(path)))
    # A former editor's watcher may be exiting just as a reopened editor resumes.
    for _ in range(10):
        if guard:
            break
        time.sleep(0.1)
        guard = lock(key(str(path)))
    if not guard:
        return
    with guard:
        viewer = Viewer(key(str(path)))
        previous = published = None
        while True:
            viewer.reap()
            try:
                data = json.loads(path.read_text())
            except FileNotFoundError:
                return
            if viewer.unused(serving(data), Path(data['pdf']).name, data['id']):
                return
            source = Path(data['pdf'])
            paths = [source, source.with_suffix('.synctex.gz')]
            pdf = viewer.folder / source.name
            try:
                stamp = fingerprint(paths)
                if stamp == previous and stamp != published and stamp[0]:
                    contents = [p.read_bytes() if p.exists() else None for p in paths]
                    if fingerprint(paths) == stamp and complete_pdf(contents[0]):
                        publish(pdf, contents)
                        published = stamp
                previous = stamp
                if published is not None:
                    viewer.update(data, pdf)
            except OSError as error:
                print(error, file=sys.stderr, flush=True)
            time.sleep(0.5)


def complete_pdf(content):
    return content and content.startswith(b'%PDF-') and b'%%EOF' in content[-1024:]


def publish(pdf, contents):
    sync = pdf.with_suffix('.synctex.gz')
    if contents[1] is not None:
        atomic(sync, contents[1])
    else:
        sync.unlink(missing_ok=True)
    atomic(pdf, contents[0])


def fingerprint(paths):
    return [[p.stat().st_mtime_ns, p.stat().st_size] if p.exists() else None for p in paths]


def poll(known, pending, ignored=()):
    result = []
    for path in directory(CACHE / 'requests').glob('*.json'):
        try:
            data = json.loads(path.read_text())
            # A connected bridge keeps subscribed documents fresh after editor exit.
            # Fresh bridges still ignore abandoned requests from former editors.
            if (data['id'] in ignored or not data.get('remote')
                    or (not serving(data) and path.stem not in known)):
                continue
            pdf = Path(data['pdf'])
            paths = [pdf, pdf.with_suffix('.synctex.gz')]
            stamp = fingerprint(paths)
            if not stamp[0]:
                continue
            item = dict(key=path.stem, request=data, stamp=stamp, editor_active=serving(data))
            # Wait for two matching polls, then verify again after reading both.
            if stamp != known.get(path.stem) and pending.get(path.stem) == stamp:
                contents = [p.read_bytes() if p.exists() else None for p in paths]
                if fingerprint(paths) == stamp and complete_pdf(contents[0]):
                    item['files'] = [base64.b64encode(c).decode() if c is not None else None for c in contents]
            pending[path.stem] = stamp
            result.append(item)
        except (OSError, ValueError):
            continue
    return result


def serve():
    pending = {}
    for line in sys.stdin:
        message = json.loads(line)
        print(json.dumps(poll(message.get('known', {}), pending, message.get('ignored', []))), flush=True)


def bridge(host, managed=False):
    require_sumatra()
    guard = lock('bridge-' + key(connection_name(host)))
    if not guard:
        raise RuntimeError('A PDF bridge for this server is already running.')
    ignored = None
    known, viewers = {}, {}
    print(f'PDF bridge connected to {host}; use :VimtexView in remote Neovim.', flush=True)
    while True:
        if managed and not live_sessions(host):
            return
        process = None
        try:
            process = subprocess.Popen(ssh_command(host, 'serve'), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, text=True)
            while True:
                if managed and not live_sessions(host):
                    return
                for viewer in viewers.values():
                    viewer.reap()
                process.stdin.write(json.dumps(dict(known=known, ignored=list(ignored or []))) + '\n')
                process.stdin.flush()
                line = process.stdout.readline()
                if not line:
                    raise ConnectionError('SSH bridge disconnected')
                items = json.loads(line)
                if ignored is None:
                    ignored = {item['request']['id'] for item in items}
                for item in items:
                    data = item['request']
                    if data['id'] in ignored:
                        continue
                    ident = key(connection_name(host) + ':' + item['key'])
                    viewer = viewers.setdefault(ident, Viewer(ident, host))
                    if viewer.unused(item.get('editor_active', True), Path(data['pdf']).name, data['id']):
                        ignored.add(data['id'])
                        known.pop(item['key'], None)
                        viewers.pop(ident, None)
                        continue
                    pdf = viewer.folder / Path(data['pdf']).name
                    if 'files' in item:
                        # Publish SyncTeX before the PDF triggers the viewer reload.
                        publish(pdf, [base64.b64decode(c) if c is not None else None for c in item['files']])
                        known[item['key']] = item['stamp']
                    if pdf.exists() and item['key'] in known:
                        viewer.update(data, pdf)
                time.sleep(0.5)
        except (OSError, ValueError, ConnectionError) as error:
            print(f'{error}; reconnecting in 3 seconds', file=sys.stderr, flush=True)
            time.sleep(3)
        finally:
            if process:
                process.terminate()
                process.wait()


def process_identity(pid):
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except (OSError, IndexError):
        return None


def session_directory(host, options=None):
    return directory(CACHE / 'ssh-sessions' / key(connection_name(host, options)))


def live_sessions(host, options=None):
    alive = []
    for path in session_directory(host, options).glob('*.lease'):
        try:
            owner = json.loads(path.read_text())
            if owner.get('start') and process_identity(owner['pid']) == owner['start']:
                alive.append(path)
            else:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue
    return alive


def stop_managed(state):
    pid = state['pid']
    if not state.get('start') or process_identity(pid) != state['start']:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while process_identity(pid) == state['start'] and time.monotonic() < deadline:
            time.sleep(.02)
        if process_identity(pid) == state['start']:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def managed_ssh(host, args, options=None):
    try:
        require_sumatra()
    except RuntimeError as error:
        print(f'tex-view: {error}; SSH will continue without a PDF bridge.', file=sys.stderr)
        return subprocess.call(args)
    options = [] if options is None else options
    folder = session_directory(host, options)
    lease = folder / (uuid.uuid4().hex + '.lease')
    state_path = folder / 'bridge.json'
    owned = None
    with (folder / 'manager.lock').open('a') as manager:
        fcntl.flock(manager, fcntl.LOCK_EX)
        write_json(lease, dict(pid=os.getpid(), start=process_identity(os.getpid())))
        state = json.loads(state_path.read_text()) if state_path.exists() else None
        if not state or not state.get('start') or process_identity(state['pid']) != state['start']:
            probe = lock('bridge-' + key(connection_name(host, options)))
            if probe:
                probe.close()
                environment = os.environ.copy()
                environment['TEX_VIEW_SSH_OPTIONS'] = json.dumps(options)
                for target, source in [('TEX_VIEW_TMUX', 'TMUX'), ('TEX_VIEW_PANE', 'TMUX_PANE')]:
                    if source in environment:
                        environment[target] = environment[source]
                with (CACHE / ('bridge-' + key(connection_name(host, options)) + '.log')).open('ab') as log:
                    owned = subprocess.Popen([SELF, 'bridge', host, '--managed'], stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=log, start_new_session=True, env=environment)
                write_json(state_path, dict(pid=owned.pid, start=process_identity(owned.pid)))
    try:
        return subprocess.call(args)
    finally:
        with (folder / 'manager.lock').open('a') as manager:
            fcntl.flock(manager, fcntl.LOCK_EX)
            lease.unlink(missing_ok=True)
            if not live_sessions(host, options) and state_path.exists():
                stop_managed(json.loads(state_path.read_text()))
                state_path.unlink(missing_ok=True)
        if owned and owned.poll() is not None:
            owned.wait()


def ssh_session(host, args):
    return managed_ssh(host, ['ssh', host] + args)


def ssh_config(args):
    result = subprocess.run(['ssh', '-G'] + args, capture_output=True, text=True, timeout=10)
    config = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            name, separator, value = line.partition(' ')
            if separator:
                config.setdefault(name.lower(), []).append(value)
    return config


def connect(args):
    direct = ['ssh'] + args
    if (not sys.stdin.isatty() or not sys.stdout.isatty()
            or any(arg in {'-G', '-V', '-Q', '-O'} for arg in args)):
        return subprocess.call(direct)
    config = ssh_config(args)
    enabled = 'TEX_VIEW_BRIDGE=1' in ' '.join(config.get('setenv', [])).split()
    if (not enabled or config.get('sessiontype', ['default'])[0] != 'default'
            or config.get('forkafterauthentication') == ['yes']):
        return subprocess.call(direct)
    host = config['host'][0]
    baseline = ssh_config([host])
    options = []
    # Preserve connection overrides without carrying interactive forwarding/TTY
    # options into the separate batch-mode transport or inverse-search callback.
    for name in ('hostname', 'user', 'port', 'proxyjump', 'proxycommand', 'identitiesonly',
                 'identityagent', 'hostkeyalias', 'userknownhostsfile', 'stricthostkeychecking'):
        if name in config and config[name] != baseline.get(name):
            options += ['-o', name + '=' + config[name][0]]
    if config.get('identityfile') != baseline.get('identityfile'):
        for identity in config.get('identityfile', []):
            options += ['-i', identity]
    return managed_ssh(host, direct, options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('request')
    sub.add_parser('resume')
    sub.add_parser('finish')
    sub.add_parser('serve')
    sub.add_parser('inverse')
    sub.add_parser('install-sumatra')
    p = sub.add_parser('watch'); p.add_argument('path', type=Path)
    p = sub.add_parser('stop'); p.add_argument('pdf', type=Path)
    p = sub.add_parser('bridge'); p.add_argument('host'); p.add_argument('--managed', action='store_true')
    p = sub.add_parser('connect'); p.add_argument('args', nargs=argparse.REMAINDER)
    p = sub.add_parser('ssh'); p.add_argument('host'); p.add_argument('args', nargs=argparse.REMAINDER)
    p = sub.add_parser('callback'); p.add_argument('path'); p.add_argument('filename'); p.add_argument('line', type=int)
    p = sub.add_parser('sumatra-callback'); p.add_argument('filename'); p.add_argument('line', type=int)
    args = parser.parse_args()
    if args.command == 'request':
        request(json.load(sys.stdin))
    elif args.command == 'resume':
        print('1' if request(json.load(sys.stdin), resume=True) else '0')
    elif args.command == 'finish':
        finish(json.load(sys.stdin))
    elif args.command == 'watch':
        watch(args.path)
    elif args.command == 'stop':
        stop(args.pdf)
    elif args.command == 'bridge':
        bridge(args.host, managed=args.managed)
    elif args.command == 'connect':
        return connect(args.args[1:] if args.args[:1] == ['--'] else args.args)
    elif args.command == 'serve':
        serve()
    elif args.command == 'inverse':
        data = json.load(sys.stdin)
        inverse(data['data'], data['filename'], data['line'])
    elif args.command == 'callback':
        callback(args.path, args.filename, args.line)
    elif args.command == 'sumatra-callback':
        sumatra_callback(args.filename, args.line)
    elif args.command == 'ssh':
        return ssh_session(args.host, args.args)
    elif args.command == 'install-sumatra':
        install_sumatra()
    return 0


if __name__ == '__main__':
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'tex-view: {error}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)

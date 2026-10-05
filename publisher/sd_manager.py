"""PlaneSlate USB SD manager. Offline, explicit package uploads; no account or server."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import io
import json
import queue
import re
import stat
import threading
import time
import zipfile
from PIL import Image


def sha(data):
    return hashlib.sha256(data).hexdigest()


def compact(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=True)


class Package:
    """Validate all members and manifest references before sending anything to a board."""
    def __init__(self, path, progress=lambda message: None):
        self.path = Path(path)
        self.archive = zipfile.ZipFile(path)
        try:
            self._validate(progress)
        except BaseException:
            self.archive.close()
            raise

    def close(self):
        self.archive.close()

    def _validate(self, progress):
        entries = self.archive.infolist()
        if len(entries) > 110000 or sum(e.file_size for e in entries) > 2 * 1024**3:
            raise ValueError('Das Paket ist zu gross.')
        self.members = {}
        folded = set()
        for entry in entries:
            name = entry.filename
            if entry.is_dir():
                name = name.rstrip('/')
            parts = PurePosixPath(name).parts
            if (not parts or len(name) > 192 or not re.fullmatch(r'[A-Za-z0-9_./-]+', name)
                    or name.startswith('/') or any(p in ('.', '..') or p.startswith('.') for p in parts)
                    or str(PurePosixPath(name)) != name or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('Unsicherer Dateipfad im Paket.')
            if entry.is_dir():
                continue
            if name.lower() in folded or entry.file_size > 64 * 1024**2 or entry.flag_bits & 1:
                raise ValueError('Doppelte, verschluesselte oder zu grosse Datei.')
            folded.add(name.lower())
            self.members[name] = entry
        roots = [k for k in ('library', 'maps') if f'{k}/manifest.json' in self.members]
        if len(roots) != 1:
            raise ValueError('Bitte genau ein Livery- oder Kartenpaket auswaehlen.')
        self.kind = roots[0]
        self.manifest_raw = self.read(f'{self.kind}/manifest.json', 4096)
        self.manifest = json.loads(self.manifest_raw)
        self.version = self.manifest.get('version', '')
        if self.manifest.get('schema') != 1 or not re.fullmatch(r'[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,46}[A-Za-z0-9_-])?', self.version):
            raise ValueError('Unbekanntes Paketformat.')
        prefix = f'{self.kind}/generations/{self.version}/'
        required = {}

        def asset(path, meta, dimensions=None):
            if not isinstance(path, str) or not path.startswith(prefix) or path not in self.members:
                raise ValueError('Referenzierte Datei fehlt im Paket.')
            size, digest = meta.get('bytes'), meta.get('sha256')
            if type(size) is not int or size < 1 or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                raise ValueError('Ungueltige Datei-Pruefsumme.')
            if path in required:
                if required[path] != (size, digest, dimensions):
                    raise ValueError('Widerspruechliche Dateiangaben.')
                return
            data = self.read(path, 64*1024**2 if dimensions is None else 512*1024)
            if len(data) != size or sha(data) != digest:
                raise ValueError(f'Pruefsumme stimmt nicht: {path}')
            if dimensions is not None:
                if not path.endswith('.png'):
                    raise ValueError('PNG-Datei erwartet.')
                with Image.open(io.BytesIO(data)) as image:
                    if image.format != 'PNG' or image.size != dimensions or image.width*image.height > 1024*1024:
                        raise ValueError('Ungueltige Bildgroesse.')
                    if self.kind == 'library' and image.mode not in ('RGB', 'RGBA'):
                        raise ValueError('Livery muss als RGB/RGBA vorliegen.')
                    image.verify()
            required[path] = (size, digest, dimensions)
            if len(required) % 100 == 0:
                progress(f'Paket pruefen: {len(required)} Dateien')

        if self.kind == 'library':
            if not re.fullmatch(r'[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,46}[A-Za-z0-9_-])?', self.manifest.get('mapping_version', '')):
                raise ValueError('Mapping-Version fehlt.')
            index = self.manifest['index']
            asset(index['path'], index)
            data = self.read(index['path'], 64*1024**2)
            lines = data.splitlines()
            if not data.endswith(b'\n') or len(lines) != index['entries'] or not 1 <= len(lines) <= 100000:
                raise ValueError('Livery-Index ist unvollstaendig.')
            keys = set()
            for line in lines:
                if len(line) > 1024:
                    raise ValueError('Livery-Eintrag ist zu lang.')
                e = json.loads(line)
                key = (e['type'], e['airline'], e['layout'])
                if (not re.fullmatch('[A-Z0-9]{1,4}', key[0]) or not re.fullmatch(r'[A-Z]{3}|\*', key[1])
                        or key[2] not in ('compact', 'large') or key in keys):
                    raise ValueError('Ungueltiger oder doppelter Livery-Eintrag.')
                keys.add(key)
                dims = (e['width'], e['height'])
                if any(type(n) is not int or n < 1 or n > 1024 for n in dims):
                    raise ValueError('Ungueltige Bildabmessungen.')
                asset(e['path'], e, dims)
        else:
            m = self.manifest
            if (type(m.get('min_zoom')) is not int or type(m.get('max_zoom')) is not int
                    or not 0 <= m['min_zoom'] <= m['max_zoom'] <= 16 or not m.get('attribution')
                    or len(m['attribution']) > 120 or not str(m.get('license_url', '')).startswith('https://')):
                raise ValueError('Kartenmanifest oder Quellenangabe fehlt.')
            count = 0
            for name in self.members:
                if not name.startswith(prefix) or not name.endswith('.png'):
                    continue
                match = re.fullmatch(re.escape(prefix) + r'(\d+)/(\d+)/(\d+)\.png', name)
                if not match:
                    raise ValueError('Ungueltiger Kartenpfad.')
                z, x, y = map(int, match.groups())
                if not m['min_zoom'] <= z <= m['max_zoom'] or x >= 2**z or y >= 2**z:
                    raise ValueError('Ungueltige Kartenkoordinaten.')
                metadata_path = name[:-4] + '.json'
                metadata = self.read(metadata_path, 512)
                asset(name, json.loads(metadata), (256, 256))
                asset(metadata_path, {'bytes': len(metadata), 'sha256': sha(metadata)})
                count += 1
            if type(m.get('tiles')) is not int or count != m['tiles'] or not 1 <= count <= 10000:
                raise ValueError('Kartenpaket ist unvollstaendig.')
        allowed = set(required) | {f'{self.kind}/manifest.json', 'MAP_LICENSE.txt'}
        if set(self.members) - allowed:
            raise ValueError('Paket enthaelt unerwartete Dateien.')
        self.files = [(path, *required[path][:2]) for path in sorted(required)]
        self.total = sum(size for _, size, _ in self.files)
        self.plan = sha(''.join(f'{path}\t{size}\t{digest}\n' for path, size, digest in self.files).encode())

    def read(self, name, limit):
        entry = self.members.get(name)
        if entry is None or entry.file_size > limit:
            raise ValueError(f'Datei fehlt oder ist zu gross: {name}')
        return self.archive.read(entry)  # ZIP CRC is checked by Python as well.


class Client:
    def __init__(self, port):
        import serial
        self.port = serial.Serial()
        self.port.port = port
        self.port.baudrate = 115200
        self.port.timeout = .2
        self.port.write_timeout = 5
        self.port.dtr = self.port.rts = False
        self.port.open()
        self.sequence = 0
        self.buffer = bytearray()

    def close(self):
        self.port.close()

    def call(self, op, **fields):
        self.sequence += 1
        message = compact(dict(id=self.sequence, op=op, **fields)).encode()
        if len(message) > 10000:
            raise ValueError('USB-Nachricht ist zu gross.')
        self.port.write(b'@PS1 ' + message + b'\n')
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self.buffer.extend(self.port.read(max(1, min(self.port.in_waiting, 16384))))
            while b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n')
                self.buffer = bytearray(rest)
                if not line.startswith(b'@PS1 '):
                    continue
                reply = json.loads(line[5:])
                if reply.get('id') != self.sequence:
                    continue
                if not reply.get('ok'):
                    raise RuntimeError(reply.get('error', 'USB-Fehler'))
                return reply
            if len(self.buffer) > 20000:
                self.buffer.clear()
        raise TimeoutError('Keine Antwort. USB-Verbindung und Firmware pruefen.')


def upload(client, package, progress=lambda done, total: None, cancelled=lambda: False):
    info = client.call('info')
    if info.get('protocol') != 1 or not info.get('sd'):
        raise RuntimeError('Keine verwendbare SD-Karte im Board.')
    client.call('start', kind=package.kind, version=package.version, files=len(package.files),
                plan_sha256=package.plan, manifest_sha256=sha(package.manifest_raw))
    done = 0
    try:
        for path, size, digest in package.files:
            if cancelled():
                raise InterruptedError('Abgebrochen; bisheriges Paket bleibt aktiv.')
            reply = client.call('put', path=path, bytes=size, sha256=digest)
            if not reply['skip']:
                with package.archive.open(path) as file:
                    offset = 0
                    while data := file.read(4096):
                        if cancelled():
                            raise InterruptedError('Abgebrochen; bisheriges Paket bleibt aktiv.')
                        response = client.call('chunk', offset=offset, hex=data.hex())
                        offset += len(data)
                        if response.get('offset') != offset:
                            raise RuntimeError('Unerwartete USB-Bestaetigung.')
                        progress(done + offset, package.total)
                client.call('finish')
            done += size
            progress(done, package.total)
        if cancelled():
            raise InterruptedError('Abgebrochen; bisheriges Paket bleibt aktiv.')
        return client.call('activate', manifest=package.manifest_raw.decode('utf-8'))
    except BaseException:
        try:
            client.call('abort')
        except Exception:
            pass  # Firmware also ends an abandoned transfer after 60 seconds.
        raise


def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from serial.tools import list_ports
    root = tk.Tk()
    root.title('PlaneSlate · SD-Karte verwalten')
    root.geometry('660x420')
    root.minsize(600, 390)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Liveries und Karten per USB', font=('Segoe UI', 18)).pack(anchor='w')
    ttk.Label(frame, text='SD-Karte im Display lassen. Board per USB anschliessen.').pack(anchor='w', pady=(8, 16))
    row = ttk.Frame(frame)
    row.pack(fill='x')
    port = ttk.Combobox(row, width=17, state='readonly')
    port.pack(side='left')
    def refresh():
        devices = list(list_ports.comports())
        devices.sort(key=lambda p: p.vid != 0x303a)
        port['values'] = [p.device for p in devices]
        if devices:
            port.current(0)
    refresh_button = ttk.Button(row, text='USB suchen', command=refresh)
    refresh_button.pack(side='left', padx=8)
    selected = tk.StringVar()
    def choose():
        name = filedialog.askopenfilename(title='PlaneSlate-Paket auswaehlen', filetypes=[('PlaneSlate-Paket', '*.zip')])
        if name:
            selected.set(name)
    choose_button = ttk.Button(frame, text='Livery- oder Kartenpaket auswaehlen …', command=choose)
    choose_button.pack(anchor='w', pady=(16, 4))
    ttk.Label(frame, textvariable=selected, wraplength=600).pack(anchor='w')
    status = tk.StringVar(value='Bereit. Es werden keine Dateien automatisch heruntergeladen.')
    ttk.Label(frame, textvariable=status, wraplength=600).pack(anchor='w', pady=14)
    bar = ttk.Progressbar(frame, maximum=100)
    bar.pack(fill='x')
    actions = ttk.Frame(frame)
    actions.pack(fill='x', pady=12)
    messages = queue.Queue()
    cancel = threading.Event()
    busy = False

    def run_task(action):
        nonlocal busy
        if busy or not port.get():
            return
        if action == 'upload' and not selected.get():
            messagebox.showinfo('Paket auswaehlen', 'Bitte zuerst ein Paket auswaehlen.')
            return
        chosen_port, chosen_path = port.get(), selected.get()
        busy = True
        cancel.clear()
        for button in (start, inspect, refresh_button, choose_button):
            button.configure(state='disabled')
        cancel_button.configure(state='normal' if action == 'upload' else 'disabled')
        status.set('Paket pruefen …' if action == 'upload' else 'Board abfragen …')
        def worker():
            package = client = None
            try:
                if action == 'upload':
                    package = Package(chosen_path, lambda s: messages.put(('status', s)))
                    if cancel.is_set():
                        raise InterruptedError('Abgebrochen.')
                client = Client(chosen_port)
                if package:
                    messages.put(('status', f'{package.version}: {len(package.files)} Dateien uebertragen …'))
                    upload(client, package, lambda d, t: messages.put(('progress', 100*d/max(t, 1))), cancel.is_set)
                    messages.put(('done', f'Fertig: {package.version} ist installiert. USB kann getrennt werden.'))
                else:
                    info = client.call('info')
                    messages.put(('done', f"SD: {'bereit' if info.get('sd') else 'nicht verfuegbar'} · Liveries: {info.get('library', '—')} · Karten: {info.get('maps', '—')}"))
            except Exception as error:
                messages.put(('done', str(error)))
            finally:
                if client:
                    client.close()
                if package:
                    package.close()
        threading.Thread(target=worker, daemon=True).start()

    inspect = ttk.Button(actions, text='SD-Status', command=lambda: run_task('info'))
    inspect.pack(side='left')
    start = ttk.Button(actions, text='Paket installieren', command=lambda: run_task('upload'))
    start.pack(side='left', padx=8)
    cancel_button = ttk.Button(actions, text='Abbrechen', command=cancel.set, state='disabled')
    cancel_button.pack(side='right')
    def poll():
        nonlocal busy
        for _ in range(min(messages.qsize(), 300)):
            kind, value = messages.get_nowait()
            if kind == 'progress':
                bar['value'] = value
                status.set(f'Uebertragen und pruefen: {value:.1f} % — USB verbunden lassen.')
            else:
                status.set(value)
            if kind == 'done':
                busy = False
                for button in (start, inspect, refresh_button, choose_button):
                    button.configure(state='normal')
                cancel_button.configure(state='disabled')
        root.after(100, poll)
    def close():
        if busy:
            cancel.set()
            status.set('Abbruch angefordert. Bitte auf die Bestaetigung warten.')
        else:
            root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    refresh()
    poll()
    root.mainloop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port')
    parser.add_argument('--package', type=Path)
    parser.add_argument('--validate', type=Path)
    args = parser.parse_args()
    if args.validate:
        package = Package(args.validate)
        print(compact(dict(kind=package.kind, version=package.version, files=len(package.files), bytes=package.total)))
        package.close()
    elif args.port:
        client = Client(args.port)
        try:
            if args.package:
                package = Package(args.package)
                try:
                    print(compact(upload(client, package)))
                finally:
                    package.close()
            else:
                print(compact(client.call('info')))
        finally:
            client.close()
    else:
        gui()

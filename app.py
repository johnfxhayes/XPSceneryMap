"""Open a local map of X-Plane 12 custom airports.

The scan only reads the X-Plane install. Results and the remembered folder
are written inside this project, never into X-Plane or a scenery pack.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import winreg
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from scanner import (
    CONFIG_FILE,
    OUTPUT_FILE,
    WEB_DIR,
    app_write_path,
    format_summary,
    installed_pack_names,
    is_xplane_root,
    scan,
    write_output,
)

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

_scan_lock = threading.Lock()
_config_lock = threading.Lock()


class MapHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format, *args):
        return

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/airports":
            self._send_airports()
            return
        if path == "/api/pack-changes":
            self._send_pack_changes()
            return
        if path in ("/", "/index.html"):
            self._send_index()
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/rescan":
            self._rescan()
            return
        if path == "/api/xplane":
            self._change_root()
            return
        if path == "/api/theme":
            self._set_theme()
            return
        if path == "/api/filters":
            self._set_filters()
            return
        if path == "/api/pack-changes":
            self._acknowledge_pack_changes()
            return
        self.send_error(404)

    def _send_index(self):
        theme = saved_theme()
        try:
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        except OSError as error:
            payload = json.dumps({"error": str(error)}).encode("utf-8")
            self._send_bytes(500, payload, "application/json; charset=utf-8")
            return
        html = html.replace('data-theme="dark"', f'data-theme="{theme}"', 1)
        if theme == "light":
            html = html.replace('aria-pressed="true"', 'aria-pressed="false"', 1)
            html = html.replace('aria-label="Light mode"', 'aria-label="Dark mode"', 1)
        html = _apply_saved_filters(html, saved_filters())
        html = _apply_hidden_packs(html, saved_hidden_packs())
        html = _apply_package_list_height(html, saved_package_list_height())
        self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _set_theme(self):
        data = self._read_json()
        theme = data.get("theme")
        if theme not in ("light", "dark"):
            message = b'{"error":"Choose light or dark."}'
            self._send_bytes(400, message, "application/json; charset=utf-8")
            return
        try:
            update_config(theme=theme)
        except OSError as error:
            payload = json.dumps({"error": str(error)}).encode("utf-8")
            self._send_bytes(500, payload, "application/json; charset=utf-8")
            return
        payload = json.dumps({"theme": theme}).encode("utf-8")
        self._send_bytes(200, payload, "application/json; charset=utf-8")

    def _set_filters(self):
        data = self._read_json()
        incoming = data.get("filters")
        if not isinstance(incoming, dict):
            message = b'{"error":"The filter settings could not be read."}'
            self._send_bytes(400, message, "application/json; charset=utf-8")
            return
        filters = saved_filters()
        for name in _FILTER_DEFAULTS:
            if isinstance(incoming.get(name), bool):
                filters[name] = incoming[name]
        updates = {"filters": filters}
        if "hidden_packs" in data:
            hidden = clean_hidden_packs(data.get("hidden_packs"))
            if hidden is not None:
                updates["hidden_packs"] = hidden
        if "package_list_height" in data:
            height = clean_package_list_height(data.get("package_list_height"))
            if height is not None:
                updates["package_list_height"] = height
        try:
            update_config(**updates)
        except OSError as error:
            payload = json.dumps({"error": str(error)}).encode("utf-8")
            self._send_bytes(500, payload, "application/json; charset=utf-8")
            return
        payload = json.dumps(updates).encode("utf-8")
        self._send_bytes(200, payload, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return {}
        if length <= 0 or length > 65536:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _load_scan(self) -> dict | None:
        try:
            data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _send_pack_changes(self):
        data = self._load_scan()
        if data is None:
            message = b'{"added":[],"removed":[],"error":"No scan results"}'
            self._send_bytes(404, message, "application/json; charset=utf-8")
            return
        payload = json.dumps(pack_changes_for(data)).encode("utf-8")
        self._send_bytes(200, payload, "application/json; charset=utf-8")

    def _acknowledge_pack_changes(self):
        data = self._load_scan()
        if data is None:
            message = b'{"error":"No scan results"}'
            self._send_bytes(404, message, "application/json; charset=utf-8")
            return
        try:
            acknowledge_packs(data)
        except OSError as error:
            payload = json.dumps({"error": str(error)}).encode("utf-8")
            self._send_bytes(500, payload, "application/json; charset=utf-8")
            return
        self._send_bytes(200, b'{"ok":true}', "application/json; charset=utf-8")

    def _send_airports(self):
        try:
            payload = OUTPUT_FILE.read_bytes()
        except OSError:
            message = b'{"airports":[],"skipped":[],"error":"No scan results"}'
            self._send_bytes(404, message, "application/json; charset=utf-8")
            return
        self._send_bytes(200, payload, "application/json; charset=utf-8")

    def _rescan(self):
        root = saved_xplane_root()
        if root is None:
            message = b'{"error":"The saved X-Plane folder could not be found."}'
            self._send_bytes(400, message, "application/json; charset=utf-8")
            return
        if not _scan_lock.acquire(blocking=False):
            message = b'{"error":"A scan is already running."}'
            self._send_bytes(409, message, "application/json; charset=utf-8")
            return
        try:
            data = scan(root)
            write_output(data)
        except (OSError, ValueError) as error:
            payload = json.dumps({"error": str(error)}).encode("utf-8")
            self._send_bytes(500, payload, "application/json; charset=utf-8")
            return
        finally:
            _scan_lock.release()
        self._send_airports()

    def _change_root(self):
        initial = saved_xplane_root() or default_initial_dir()
        if not _scan_lock.acquire(blocking=False):
            message = b'{"error":"A scan is already running."}'
            self._send_bytes(409, message, "application/json; charset=utf-8")
            return
        try:
            root = choose_installed_root(initial)
            if root is None:
                message = b'{"cancelled":true}'
                self._send_bytes(200, message, "application/json; charset=utf-8")
                return
            remember_root(root)
            data = scan(root)
            write_output(data)
        except Exception as error:
            payload = json.dumps({"error": _error_text(error)}).encode("utf-8")
            self._send_bytes(500, payload, "application/json; charset=utf-8")
            return
        finally:
            _scan_lock.release()
        self._send_airports()

    def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def unblock_downloaded_files(root: Path | None = None) -> None:
    """Remove the mark Windows adds to every file in a downloaded zip.

    .NET refuses to load Python.Runtime.dll while that mark is present, which
    stops the map from opening. The mark is cleared from this program's own
    files before that library is loaded.
    """
    if os.name != "nt":
        return
    if root is None:
        if not getattr(sys, "frozen", False):
            return
        root = Path(sys.executable).resolve().parent
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        zone = Path(str(path) + ":Zone.Identifier")
        try:
            zone.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> None:
    silence_missing_stdio()
    unblock_downloaded_files()
    parser = argparse.ArgumentParser(
        description="Show installed X-Plane 12 airports on a map. Reads scenery only."
    )
    parser.add_argument(
        "--xplane",
        type=Path,
        help="X-Plane 12 folder. Skips the folder prompt.",
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    if args.xplane:
        root = args.xplane.expanduser()
        if not is_xplane_root(root):
            report_and_exit(f"Not an X-Plane 12 folder (needs X-Plane.exe and Custom Scenery): {root}")
    else:
        root = saved_xplane_root()
        if root is None:
            root = detect_xplane_root()
        if root is None:
            root = choose_xplane_root(default_initial_dir())
            if root is None:
                raise SystemExit(0)

    remember_root(root)
    print(f"Scanning {root}", flush=True)
    print("Reading scenery only. Nothing in X-Plane will be changed.", flush=True)
    status = show_status("Reading your scenery…")
    try:
        data = scan(root)
    except (OSError, ValueError) as error:
        status.destroy()
        report_and_exit(str(error))
    status.destroy()
    written = write_output(data)
    print(format_summary(data), flush=True)
    print(f"Wrote {written}", flush=True)

    server = bind_server(args.port)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"Map: {url}", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        try:
            open_map_window(url)
        except Exception as error:
            report_and_exit(str(error))
    finally:
        server.shutdown()
        server.server_close()


def silence_missing_stdio() -> None:
    """A windowed program has no console. print() needs a real stream."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def report_and_exit(message: str) -> None:
    import tkinter as tk
    from tkinter import messagebox

    window = tk.Tk()
    window.withdraw()
    window.attributes("-topmost", True)
    try:
        messagebox.showerror("X-Plane Scenery Map", message, parent=window)
    finally:
        window.destroy()
    raise SystemExit(1)


def show_status(message: str):
    import tkinter as tk

    window = tk.Tk()
    window.title("X-Plane Scenery Map")
    window.resizable(False, False)
    window.configure(padx=28, pady=22)
    tk.Label(window, text=message, font=("Segoe UI", 12)).pack()
    window.update_idletasks()
    width = window.winfo_width()
    height = window.winfo_height()
    x = (window.winfo_screenwidth() - width) // 2
    y = (window.winfo_screenheight() - height) // 2
    window.geometry(f"+{x}+{y}")
    window.update()
    return window


def open_map_window(url: str) -> None:
    import webview

    webview.create_window(
        "X-Plane Scenery Map",
        url,
        width=1440,
        height=900,
        min_size=(960, 640),
    )
    webview.start()


def _error_text(error: Exception) -> str:
    message = str(error).strip()
    return message.splitlines()[0] if message else type(error).__name__


class MapServer(ThreadingHTTPServer):
    """One listener per port, so an old copy cannot steal the folder request."""

    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bind_server(port: int) -> ThreadingHTTPServer:
    last_error = None
    start = port if port else 8765
    for candidate in range(start, start + 20):
        try:
            return MapServer(("127.0.0.1", candidate), MapHandler)
        except OSError as error:
            last_error = error
    sys.exit(f"Could not listen on 127.0.0.1:{start}: {last_error}")


_SETUP_KEYS = (
    (winreg.HKEY_CURRENT_USER, r"Software\Laminar Research\X-Plane\12.0"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Laminar Research\X-Plane\12.0"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Laminar Research\X-Plane\12.0"),
    (winreg.HKEY_CURRENT_USER, r"Software\WOW6432Node\Laminar Research\X-Plane\12.0"),
)
_STEAM_KEYS = (
    (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam", "InstallPath"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam", "InstallPath"),
)
_STEAM_APP_ID = "2014780"
_VDF_PATH = re.compile(r'"path"\s+"([^"]+)"')
_ACF_DIR = re.compile(r'"installdir"\s+"([^"]+)"')


def detect_xplane_root() -> Path | None:
    """Find X-Plane 12 when this program has not saved a folder yet.

    The Laminar installer stores the folder in SetupPath. A Steam copy is
    read from that library's app manifest. C:\\X-Plane 12 is the last guess.
    """
    for candidate in (*_registry_setup_paths(), *_steam_xplane_paths(), Path(r"C:\X-Plane 12")):
        if is_xplane_root(candidate):
            return candidate
    return None


def _registry_setup_paths():
    for hive, subkey in _SETUP_KEYS:
        raw = _reg_string(hive, subkey, "SetupPath")
        if raw:
            yield Path(raw)


def _steam_xplane_paths():
    seen: set[str] = set()
    for steam in _steam_roots():
        for library in _steam_libraries(steam):
            installdir = _acf_installdir(library / "steamapps" / f"appmanifest_{_STEAM_APP_ID}.acf")
            names = [installdir, "X-Plane 12"] if installdir else ["X-Plane 12"]
            for name in names:
                path = library / "steamapps" / "common" / name
                key = os.path.normcase(str(path))
                if key in seen:
                    continue
                seen.add(key)
                yield path


def _steam_roots():
    seen: set[str] = set()
    for hive, subkey, value in _STEAM_KEYS:
        raw = _reg_string(hive, subkey, value)
        if not raw:
            continue
        path = Path(raw)
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        yield path


def _steam_libraries(steam: Path):
    yield steam
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8")
    except OSError:
        return
    for match in _VDF_PATH.finditer(text):
        raw = match.group(1).replace("\\\\", "\\")
        if raw:
            yield Path(raw)


def _acf_installdir(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _ACF_DIR.search(text)
    if not match:
        return None
    name = match.group(1).strip()
    return name or None


def _reg_string(hive: int, subkey: str, name: str) -> str | None:
    try:
        with winreg.OpenKey(hive, subkey) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def saved_xplane_root() -> Path | None:
    remembered = remembered_root()
    if remembered is not None and is_xplane_root(remembered):
        return remembered
    return None


def default_initial_dir() -> Path:
    preferred = Path(r"C:\X-Plane 12")
    if is_xplane_root(preferred):
        return preferred
    remembered = remembered_root()
    if remembered is not None and remembered.is_dir():
        return remembered
    return Path("C:/")


def read_config() -> dict:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def update_config(**updates) -> None:
    with _config_lock:
        data = read_config()
        data.update(updates)
        path = app_write_path(CONFIG_FILE)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def saved_theme() -> str:
    theme = read_config().get("theme")
    if theme in ("light", "dark"):
        return theme
    return "dark"


_FILTER_DEFAULTS = {
    "land": True,
    "heli": True,
    "sea": True,
    "ortho": True,
    "xpme": False,
}


def clean_hidden_packs(value) -> list[str] | None:
    """Folder names the map should leave off. Empty means show every pack."""
    if not isinstance(value, list):
        return None
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or len(name) > 300 or name in seen:
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= 500:
            break
    return names


def clean_package_list_height(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    height = int(value)
    if height < 64 or height > 2000:
        return None
    return height


def saved_package_list_height() -> int | None:
    return clean_package_list_height(read_config().get("package_list_height"))


def _apply_package_list_height(html: str, height: int | None) -> str:
    blob = "null" if height is None else str(height)
    marker = '<script id="package-list-height" type="application/json">null</script>'
    return html.replace(
        marker,
        f'<script id="package-list-height" type="application/json">{blob}</script>',
        1,
    )


def pack_changes_for(data: dict) -> dict:
    """Names added or removed since the user last acknowledged the library.

    The first scan only remembers the library. It does not announce every
    package that was already installed.
    """
    current = installed_pack_names(data)
    saved = read_config().get("known_packs")
    if not isinstance(saved, list):
        update_config(known_packs=current)
        return {"added": [], "removed": []}
    previous = {name.strip() for name in saved if isinstance(name, str) and name.strip()}
    now = set(current)
    return {
        "added": sorted(now - previous, key=str.casefold),
        "removed": sorted(previous - now, key=str.casefold),
    }


def acknowledge_packs(data: dict) -> None:
    update_config(known_packs=installed_pack_names(data))


def saved_hidden_packs() -> list[str]:
    cleaned = clean_hidden_packs(read_config().get("hidden_packs"))
    return cleaned if cleaned is not None else []


def _apply_hidden_packs(html: str, names: list[str]) -> str:
    blob = json.dumps(names, ensure_ascii=True)
    marker = '<script id="hidden-packs" type="application/json">[]</script>'
    return html.replace(marker, f'<script id="hidden-packs" type="application/json">{blob}</script>', 1)


def saved_filters() -> dict:
    """Checkbox state. XPME starts off until the user turns it on."""
    saved = read_config().get("filters")
    filters = dict(_FILTER_DEFAULTS)
    if isinstance(saved, dict):
        for name in _FILTER_DEFAULTS:
            if isinstance(saved.get(name), bool):
                filters[name] = saved[name]
    return filters


def _apply_saved_filters(html: str, filters: dict) -> str:
    for name, enabled in filters.items():
        checked = f'value="{name}" checked'
        plain = f'value="{name}"'
        if enabled and checked not in html:
            html = html.replace(plain, checked, 1)
        elif not enabled:
            html = html.replace(checked, plain, 1)
    return html


def remembered_root() -> Path | None:
    raw = read_config().get("xplane_root")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw)


def remember_root(root: Path) -> None:
    update_config(xplane_root=str(root))


_FOLDER_TITLE = "Select X-Plane 12 Folder"


def choose_installed_root(initial: Path) -> Path | None:
    """Ask for an X-Plane 12 folder while the map window is open."""
    import webview

    if webview.windows:
        return _on_map_thread(webview.windows[0], lambda: _pick_from_map(webview.windows[0], initial))
    return choose_xplane_root(initial)


def _on_map_thread(window, action):
    """WinForms ignores a folder dialog started from the web server thread."""
    from System import Func
    from webview.platforms.winforms import BrowserView

    form = BrowserView.instances.get(window.uid)
    if form is None or not form.InvokeRequired:
        return action()
    holder: dict = {}

    def _run() -> str:
        holder["value"] = action()
        return ""

    form.Invoke(Func[str](_run))
    return holder.get("value")


def _pick_from_map(window, initial: Path) -> Path | None:
    from webview.platforms.winforms import BrowserView

    parent = BrowserView.instances.get(window.uid)
    while True:
        directory = str(initial) if initial.is_dir() else ""
        selected = _show_folder_picker(parent, directory, _FOLDER_TITLE)
        if not selected:
            return None
        path = Path(selected[0])
        if is_xplane_root(path):
            return path
        _tell_user(parent, "That folder does not contain X-Plane.exe and Custom Scenery.")
        if path.is_dir():
            initial = path


def _tell_user(parent, message: str) -> None:
    import System.Windows.Forms as WinForms

    WinForms.MessageBox.Show(
        parent,
        message,
        "X-Plane 12",
        WinForms.MessageBoxButtons.OK,
        WinForms.MessageBoxIcon.Error,
    )


def _show_folder_picker(parent, directory: str, title: str):
    """Windows folder picker. The title is the words in the window header."""
    from System import Array, Object, UInt32
    from webview.platforms.winforms import OpenFolderDialog, WinForms

    dialog = WinForms.OpenFileDialog()
    dialog.InitialDirectory = directory or None
    dialog.Title = title
    dialog.Filter = OpenFolderDialog.foldersFilter
    dialog.AddExtension = False
    dialog.CheckFileExists = False
    dialog.DereferenceLinks = True
    dialog.Multiselect = False
    dialog.RestoreDirectory = True

    native = OpenFolderDialog.createVistaDialogMethodInfo.Invoke(dialog, [])
    OpenFolderDialog.onBeforeVistaDialogMethodInfo.Invoke(dialog, [native])
    options = OpenFolderDialog.getOptionsMethodInfo.Invoke(dialog, [])
    options = options.op_BitwiseOr(OpenFolderDialog.fosPickFoldersBitFlag)
    OpenFolderDialog.setOptionsMethodInfo.Invoke(native, [options])
    set_title = OpenFolderDialog.iFileDialogType.GetMethod("SetTitle")
    if set_title is not None:
        set_title.Invoke(native, [title])

    cookie_args = Array[Object](
        [OpenFolderDialog.vistaDialogEventsConstructorInfo.Invoke([dialog]), UInt32(0)]
    )
    OpenFolderDialog.adviseMethodInfo.Invoke(native, cookie_args)
    cookie = cookie_args.GetValue(1)
    try:
        result = OpenFolderDialog.showMethodInfo.Invoke(
            native, [parent.Handle if parent else None]
        )
        if result == 0:
            return tuple(dialog.FileNames)
        return None
    finally:
        OpenFolderDialog.unadviseMethodInfo.Invoke(native, [UInt32(cookie)])


def choose_xplane_root(initial: Path) -> Path | None:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    window = tk.Tk()
    window.withdraw()
    window.attributes("-topmost", True)
    try:
        while True:
            chosen = filedialog.askdirectory(
                title="Select X-Plane 12 Folder",
                initialdir=str(initial),
                mustexist=True,
                parent=window,
            )
            if not chosen:
                return None
            path = Path(chosen)
            if is_xplane_root(path):
                return path
            messagebox.showerror(
                "X-Plane 12",
                "That folder does not contain X-Plane.exe and Custom Scenery.",
                parent=window,
            )
            initial = path
    finally:
        window.destroy()


if __name__ == "__main__":
    main()

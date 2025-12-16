#!/usr/bin/env python3
from __future__ import annotations
import sys
import os
import time
import tempfile
import zipfile
import shutil
import subprocess
import requests
import json
import signal
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Iterable, Optional

UA = "OffInstaller/1.0"
MODRINTH_API = "https://api.modrinth.com/v2/project/optifine-for-fabric/version"
FABRIC_INSTALLER_VERSION = "0.11.2"
FABRIC_INSTALLER_URL = (
    f"https://maven.fabricmc.net/net/fabricmc/fabric-installer/{FABRIC_INSTALLER_VERSION}/"
    f"fabric-installer-{FABRIC_INSTALLER_VERSION}.jar"
)
DEFAULT_DOWNLOADS = str(Path.home() / "Downloads")
LOG_DIR = Path.home() / "offinstaller-logs"

try:
    import textual
    from textual.app import App, ComposeResult
    from textual.widgets import Static, ListView, ListItem, Label, Input, Footer
    from textual.reactive import reactive
    from textual.events import Key
    TEXTUAL_AVAILABLE = True
except Exception:
    TEXTUAL_AVAILABLE = False

try:
    from rich.progress import (
        Progress,
        TextColumn,
        BarColumn,
        DownloadColumn,
        TransferSpeedColumn,
        TimeRemainingColumn,
        SpinnerColumn,
    )
    RICH_AVAILABLE = True
except Exception:
    RICH_AVAILABLE = False

ANSI = {
    "CRITICAL": "\033[91m",
    "ERROR": "\033[38;5;208m",
    "WARN": "\033[93m",
    "COMMENT": "\033[97m",
    "USER": "\033[96m",
    "OK": "\033[92m",
    "RESET": "\033[0m",
}
SYMBOL = {
    "CRITICAL": "✖",
    "ERROR": "✘",
    "WARN": "⚠",
    "COMMENT": "•",
    "USER": "➜",
    "OK": "✔",
}

LOG: List[Tuple[str, str, str]] = []

def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def now_filename_ts() -> str:
    return datetime.now().strftime("%m-%d-%y_%H-%M.%S")

def log(level: str, message: str) -> None:
    LOG.append((now_ts(), level, message))

def mb(n: int) -> float:
    return n / 1024.0 / 1024.0

def fmt_size(n: int) -> str:
    return f"{mb(n):6.2f} MB"

def fmt_speed(bps: float) -> str:
    return f"{bps/1024/1024:6.2f} MB/s"

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def sanitize_name(name: str) -> str:
    return "".join(c for c in name if c not in r'<>:"/\|?*').strip()

def pluralize(count: int, singular: str, plural: Optional[str] = None) -> str:
    if count == 1:
        return singular
    return plural if plural is not None else singular + "s"

def format_received_message(success: int, total: int, item_name: str) -> List[str]:
    msgs: List[str] = []
    msgs.append(f"Received {success}/{total} {pluralize(total, item_name)}")
    failed = total - success
    if failed > 0:
        msgs.append(f"Failed to receive {failed} {pluralize(failed, item_name)}")
    return msgs

def format_copied_message(success: int, total: int, item_name: str) -> List[str]:
    msgs: List[str] = []
    msgs.append(f"Copied {success}/{total} {pluralize(total, item_name)}")
    failed = total - success
    if failed > 0:
        msgs.append(f"Failed to copy {failed} {pluralize(failed, item_name)}")
    return msgs

def format_modpack_version(version_number: str) -> str:
    if not version_number:
        return "unknown"
    if "+" in version_number:
        return version_number.split("+")[-1].strip()
    return version_number.strip()

def build_display_name(version_number: str, game_versions: str, version_type: str) -> str:
    modver = format_modpack_version(version_number)
    vt = (version_type or "").lower()
    tag = ""
    if vt == "beta":
        tag = " [Beta]"
    elif vt == "alpha":
        tag = " [Alpha]"
    name = f"OptiFine for Fabric {modver} [Minecraft {game_versions}]"
    if tag:
        name = f"{name}{tag}"
    return sanitize_name(name)

def iter_manifest_files(manifest: dict, base_dest: str) -> Iterable[Tuple[str, str]]:
    for f in manifest.get("files", []):
        url = None
        if isinstance(f.get("downloads"), list) and f["downloads"]:
            dl = f["downloads"][0]
            url = dl.get("url") if isinstance(dl, dict) else dl
        if not url:
            url = f.get("url")
        if not url:
            continue
        rel = f.get("path") or f.get("filename") or os.path.basename(url)
        rel = rel.lstrip("/\\")
        yield url, os.path.join(base_dest, rel)

def load_manifest_from_td(td: str) -> Optional[dict]:
    for root, _, files in os.walk(td):
        if "modrinth.index.json" in files:
            return json.load(open(os.path.join(root, "modrinth.index.json")))
    return None

def find_mrpack_url(selection: dict) -> Optional[str]:
    for f in selection.get("files", []):
        if f.get("filename", "").endswith(".mrpack"):
            return f.get("url")
    return None

def launcher_present(mc_dir: str) -> bool:
    candidates = [
        os.path.join(mc_dir, "launcher_profiles.json"),
        os.path.join(mc_dir, "launcher_accounts.json"),
        os.path.join(mc_dir, "launcher_profiles.json.old"),
    ]
    return any(os.path.exists(p) for p in candidates)

# Textual UI components
if TEXTUAL_AVAILABLE:
    class ChoiceApp(App):
        CSS = """
        .ok { color: green; } .err { color: red; } .step { color: cyan; } .info { color: white; }
        .stable { color: #7CFC00; } .beta { color: orange; } .alpha { color: red; }
        Screen { layout: vertical; padding: 1; }
        #title { height: 3; content-align: left middle; }
        ListView { height: 1fr; border: round red; }
        Footer { height: 1; }
        """
        selection = reactive(None)
        def __init__(self, title: str, options: List[str], opt_class: str = "info", **kw):
            super().__init__(**kw)
            self._title = title
            self._options = options
            self._opt_class = opt_class
        def compose(self) -> ComposeResult:
            yield Static(self._title, id="title", classes="step")
            self.list = ListView()
            yield self.list
            yield Footer()
        async def on_mount(self) -> None:
            items = []
            for opt in self._options:
                it = ListItem(Label(opt, classes=self._opt_class))
                it.data = opt
                items.append(it)
            await self.list.extend(items)
            self.list.index = 0
            self.set_focus(self.list)
        async def on_list_view_selected(self, event) -> None:
            self.selection = event.item.data
            await self.action_quit()
        async def on_key(self, event: Key) -> None:
            if event.key == "ctrl+c":
                await self.action_quit()

    class InputApp(App):
        CSS = ChoiceApp.CSS
        value = reactive(None)
        def __init__(self, title: str, placeholder: str = "", default: str = "", **kw):
            super().__init__(**kw)
            self._title = title
            self._placeholder = placeholder
            self._default = default
        def compose(self) -> ComposeResult:
            yield Static(self._title, id="title", classes="step")
            self.input = Input(placeholder=self._placeholder, value=self._default)
            yield self.input
            yield Footer()
        def on_mount(self) -> None:
            self.set_focus(self.input)
        async def on_input_submitted(self, event) -> None:
            self.value = event.value.strip()
            await self.action_quit()
        async def on_key(self, event: Key) -> None:
            if event.key == "ctrl+c":
                await self.action_quit()

    class VersionPicker(App):
        CSS = ChoiceApp.CSS + """
        Input { border: round $accent; }
        """
        selection = reactive(None)
        def __init__(self, versions: List[dict], **kw):
            super().__init__(**kw)
            self.versions = versions
            self._items = []
        def compose(self) -> ComposeResult:
            yield Static("Type to search; Enter to select; Esc to clear", id="title", classes="step")
            self.search = Input(placeholder="Search by channel, game version, or version number")
            yield self.search
            self.list = ListView()
            yield self.list
            yield Footer()
        async def on_mount(self) -> None:
            await self._populate(self.versions)
            self.set_focus(self.search)
        async def _populate(self, versions: List[dict]) -> None:
            items = []
            for v in versions:
                vt = v.get("version_type", "").lower()
                tag = "Stable" if vt == "release" else ("Beta" if vt == "beta" else "Alpha")
                game = ",".join(v.get("game_versions", []))
                ver = v.get("version_number", "")
                display_ver = ver.split("+")[-1].strip() if "+" in ver else ver
                label = f"{tag:<6}  {game:<14}  {display_ver:<12}"
                cls = "stable" if tag == "Stable" else ("beta" if tag == "Beta" else "alpha")
                it = ListItem(Label(label, classes=cls))
                it.data = v
                items.append(it)
            await self.list.extend(items)
            self.list.index = 0
        async def on_input_changed(self, event) -> None:
            q = (event.value or "").strip().lower()
            self.list.clear()
            if not q:
                await self._populate(self.versions)
                return
            filtered = []
            for v in self.versions:
                vt = v.get("version_type", "").lower()
                tag = "stable" if vt == "release" else ("beta" if vt == "beta" else "alpha")
                game = ",".join(v.get("game_versions", []))
                ver = v.get("version_number", "")
                display_ver = ver.split("+")[-1].strip() if "+" in ver else ver
                hay = f"{tag} {game} {display_ver}".lower()
                if q in hay:
                    filtered.append(v)
            await self._populate(filtered)
        async def on_list_view_selected(self, event) -> None:
            self.selection = event.item.data
            await self.action_quit()
        async def on_key(self, event: Key) -> None:
            if event.key == "ctrl+c":
                await self.action_quit()
            if event.key == "escape":
                self.search.value = ""
                await self.on_input_changed(type("E", (), {"value": ""}))

def _sigint(signum, frame):
    print(); print(f"{ANSI['CRITICAL']}{now_ts()} {SYMBOL['CRITICAL']} Cancelled.{ANSI['RESET']}")
    sys.exit(1)
signal.signal(signal.SIGINT, _sigint)

# --- Download helpers ---
def download_with_cumulative_fallback(urls: List[str], targets: List[str]) -> None:
    sizes, total = [], 0
    for u in urls:
        try:
            h = requests.head(u, headers={"User-Agent": UA}, allow_redirects=True, timeout=5)
            s = int(h.headers.get("content-length") or 0)
        except Exception:
            s = 0
        sizes.append(s); total += s

    success = 0
    failed = 0
    start = time.time()
    for u, t, s in zip(urls, targets, sizes):
        ensure_dir(os.path.dirname(t))
        try:
            with requests.get(u, stream=True, headers={"User-Agent": UA}) as r:
                r.raise_for_status()
                with open(t, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        f.write(chunk)
            success += 1
        except Exception:
            failed += 1
    elapsed = max(0.001, time.time() - start)
    msgs = format_received_message(success, success + failed, "file")
    for m in msgs:
        log("OK" if failed == 0 else "WARN", m)
    log("COMMENT", f"Total download size approx {fmt_size(total)}; elapsed {elapsed:.1f}s")

def download_with_rich(urls: List[str], targets: List[str], description: str) -> None:
    if not RICH_AVAILABLE:
        download_with_cumulative_fallback(urls, targets)
        return

    total_bytes = 0
    sizes = []
    for u in urls:
        try:
            h = requests.head(u, headers={"User-Agent": UA}, allow_redirects=True, timeout=5)
            s = int(h.headers.get("content-length") or 0)
        except Exception:
            s = 0
        sizes.append(s)
        total_bytes += s

    log("COMMENT", f"Starting {description}: {len(urls)} {pluralize(len(urls), 'file')}, approx {fmt_size(total_bytes)}")

    success = 0
    failed = 0
    start = time.time()

    # flush terminal after Textual returns so Rich can render reliably
    print("", flush=True)

    if total_bytes > 0:
        progress = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=None, complete_style="blue"),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            transient=True,
        )
        with progress:
            task_total = progress.add_task(description, total=total_bytes)
            for u, t, s in zip(urls, targets, sizes):
                ensure_dir(os.path.dirname(t))
                try:
                    with requests.get(u, stream=True, headers={"User-Agent": UA}) as r:
                        r.raise_for_status()
                        with open(t, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                if not chunk:
                                    continue
                                f.write(chunk)
                                progress.update(task_total, advance=len(chunk))
                    success += 1
                except Exception:
                    failed += 1
            progress.update(task_total, completed=total_bytes)
    else:
        progress = Progress(
            SpinnerColumn(style="blue"),
            TextColumn("[bold blue]{task.description}"),
            transient=True,
        )
        with progress:
            task = progress.add_task(description, total=None)
            for u, t, s in zip(urls, targets, sizes):
                ensure_dir(os.path.dirname(t))
                try:
                    with requests.get(u, stream=True, headers={"User-Agent": UA}) as r:
                        r.raise_for_status()
                        with open(t, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                if not chunk:
                                    continue
                                f.write(chunk)
                    success += 1
                except Exception:
                    failed += 1
            time.sleep(0.05)
            try:
                progress.remove_task(task)
            except Exception:
                pass

    elapsed = max(0.001, time.time() - start)
    msgs = format_received_message(success, success + failed, "file")
    for m in msgs:
        log("OK" if failed == 0 else "WARN", m)
    # mark overall download as completed (OK) when no failures
    log("OK" if failed == 0 else "WARN", f"{description} finished; elapsed {elapsed:.1f}s; approx {fmt_size(total_bytes)}")

# --- Parsing/moving with progress (aggregated copy logs) ---
def apply_overrides_with_rich(td: str, dest: str) -> None:
    src = os.path.join(td, "overrides")
    if not os.path.isdir(src):
        log("COMMENT", "No overrides directory present")
        return

    files_to_copy: List[Tuple[str, str, str]] = []
    for root, _, files in os.walk(src):
        for item in files:
            s = os.path.join(root, item)
            rel = os.path.relpath(s, src)
            t = os.path.join(dest, rel)
            files_to_copy.append((s, t, rel))

    total = len(files_to_copy)
    log("COMMENT", f"Parsing and moving modpack: {total} {pluralize(total, 'file')}")

    copied = 0
    failed = 0

    # flush terminal after Textual returns so Rich can render reliably
    print("", flush=True)

    if RICH_AVAILABLE:
        if total > 0:
            progress = Progress(
                TextColumn("[bold blue]{task.description}"),
                BarColumn(bar_width=None, complete_style="blue"),
                TextColumn("{task.completed}/{task.total}"),
                transient=True,
            )
            with progress:
                task = progress.add_task("Parsing and moving modpack", total=total)
                for s, t, rel in files_to_copy:
                    try:
                        ensure_dir(os.path.dirname(t))
                        shutil.copy2(s, t)
                        copied += 1
                    except Exception:
                        failed += 1
                    progress.update(task, advance=1)
        else:
            progress = Progress(
                SpinnerColumn(style="blue"),
                TextColumn("[bold blue]{task.description}"),
                transient=True,
            )
            with progress:
                task = progress.add_task("Parsing and moving modpack", total=None)
                time.sleep(0.15)
                try:
                    progress.remove_task(task)
                except Exception:
                    pass
    else:
        for s, t, rel in files_to_copy:
            try:
                ensure_dir(os.path.dirname(t))
                shutil.copy2(s, t)
                copied += 1
            except Exception:
                failed += 1

    msgs = format_copied_message(copied, copied + failed, "file")
    for m in msgs:
        log("OK" if failed == 0 else "WARN", m)

# --- High-level placement ---
def download_and_place(manifest: dict, dest: str, td: str, overwrite: bool = False) -> None:
    urls, targets = [], []
    for url, target in iter_manifest_files(manifest, dest):
        if not overwrite and os.path.exists(target):
            log("USER", f"Skipping existing {os.path.relpath(target, dest)}")
            continue
        urls.append(url); targets.append(target)
    if urls:
        log("COMMENT", f"Downloading {len(urls)} {pluralize(len(urls), 'file')} to {dest}")
        # Use rich for downloads when available (keeps loading bars)
        download_with_rich(urls, targets, description=f"Downloading {len(urls)} files")
    apply_overrides_with_rich(td, dest)

# --- Fabric installer ---
def run_fabric_installer(mc: str, loader_version: str, mc_version: Optional[str]) -> None:
    installer_path = os.path.join(tempfile.gettempdir(), f"fabric-installer-{FABRIC_INSTALLER_VERSION}.jar")
    log("USER", f"Downloading Fabric installer to {installer_path}")
    try:
        download_with_rich([FABRIC_INSTALLER_URL], [installer_path], description="Downloading Fabric installer")
    except Exception as e:
        log("ERROR", f"Failed to download Fabric installer: {e}")
        return
    cmd = ["java", "-jar", installer_path, "client", "-dir", mc, "-loader", loader_version]
    if mc_version:
        cmd += ["-mcversion", mc_version]
    log("USER", "Running Fabric installer")

    # flush terminal after Textual returns so Rich can render reliably
    print("", flush=True)

    if RICH_AVAILABLE:
        progress = Progress(
            SpinnerColumn(style="blue"),
            TextColumn("[bold blue]Installing Fabric loader"),
            transient=True,
        )
        with progress:
            task = progress.add_task("install", total=None)
            try:
                subprocess.check_call(cmd)
                log("OK", "Fabric loader installed successfully")
            except subprocess.CalledProcessError as e:
                log("ERROR", f"Fabric installer failed (exit {e.returncode})")
            finally:
                try:
                    progress.remove_task(task)
                except Exception:
                    pass
    else:
        try:
            subprocess.check_call(cmd)
            log("OK", "Fabric loader installed successfully")
        except subprocess.CalledProcessError as e:
            log("ERROR", f"Fabric installer failed (exit {e.returncode})")

# --- Final summary and flows ---
def final_summary_and_save() -> None:
    print("\n\033[1m\033[4mInstallation Summary\033[0m\n")
    for ts, level, msg in LOG:
        color = ANSI.get(level, ANSI["COMMENT"])
        sym = SYMBOL.get(level, "•")
        print(f"{color}{ts} {sym} {msg}{ANSI['RESET']}")
    ensure_dir(str(LOG_DIR))
    fname = LOG_DIR / f"OFFinstaller-[{now_filename_ts()}].txt"
    with open(fname, "w", encoding="utf-8") as f:
        for ts, level, msg in LOG:
            f.write(f"{ts}\t{level}\t{msg}\n")
    print(f"{ANSI['OK']}{now_ts()} {SYMBOL['OK']} Saved log to: {fname}{ANSI['RESET']}")
    sys.exit(0)

def prepare_destination(dest: str, prompt_title: str = "Destination folder") -> None:
    if os.path.exists(dest):
        app = ChoiceApp(prompt_title + f": {dest}\nOverwrite?", ["Yes, overwrite", "No, use existing"])
        app.run()
        ans = app.selection
        log("USER", f"Destination prompt answered: {ans}")
        if ans == "Yes, overwrite":
            shutil.rmtree(dest, ignore_errors=True)
            ensure_dir(dest)
            log("OK", f"Destination prepared: {dest}")
    else:
        ensure_dir(dest)
        log("OK", f"Destination created: {dest}")

def parse_and_download_flow(versions: List[dict]) -> None:
    log("COMMENT", "Opening version selector for Parse & Download")
    vp = VersionPicker(versions)
    vp.run()
    sel = vp.selection
    if not sel:
        log("CRITICAL", "No version selected; aborting parse & download")
        final_summary_and_save()
    ver_name = sel.get("version_number", "unknown")
    ver_type = sel.get("version_type", "unknown")
    game_versions = ",".join(sel.get("game_versions", []))
    # record the user selection explicitly
    log("USER", f"Selected version: {ver_name} ({ver_type}) for {game_versions}")
    display_name = build_display_name(ver_name, game_versions, ver_type)

    file_url = find_mrpack_url(sel)
    if not file_url:
        log("CRITICAL", "No .mrpack found for this version")
        final_summary_and_save()

    # comment the package URL once
    log("COMMENT", f"Downloading package {file_url}")

    tmp_pkg = os.path.join(tempfile.gettempdir(), os.path.basename(file_url))
    try:
        # per request: no per-file GET logs here; use aggregated download (still uses Rich)
        download_with_cumulative_fallback([file_url], [tmp_pkg])
        # explicit success entry for the package itself
        log("OK", f"Downloaded package: {os.path.basename(file_url)}")
        log("OK", "Package download complete")
    except Exception as e:
        log("ERROR", f"Download error: {e}")
        final_summary_and_save()
    td = tempfile.mkdtemp()
    log("COMMENT", f"Extracting package to {td}")
    try:
        with zipfile.ZipFile(tmp_pkg) as z:
            z.extractall(td)
        log("OK", "Extraction complete")
    except Exception as e:
        log("ERROR", f"Extract error: {e}")
        final_summary_and_save()
    manifest = load_manifest_from_td(td)
    if not manifest:
        log("CRITICAL", "No manifest found in package")
        final_summary_and_save()
    dest = os.path.join(DEFAULT_DOWNLOADS, display_name)
    prepare_destination(dest, "Destination folder")
    # download_and_place will log the descriptive download start as COMMENT
    download_and_place(manifest, dest, td, overwrite=True)
    log("OK", "All files downloaded and organized")
    log("COMMENT", f"Files are available in: {dest}")
    shutil.rmtree(td, ignore_errors=True)
    try:
        os.remove(tmp_pkg)
    except OSError:
        log("WARN", f"Could not remove temporary package {tmp_pkg}")
    final_summary_and_save()

def step_install_flow(mc: str, versions: List[dict]) -> None:
    log("USER", "Opening version selector for installation")
    vp = VersionPicker(versions)
    vp.run()
    sel = vp.selection
    if not sel:
        log("CRITICAL", "No version selected; aborting installation")
        final_summary_and_save()
    ver_name = sel.get("version_number", "unknown")
    ver_type = sel.get("version_type", "unknown")
    game_versions = ",".join(sel.get("game_versions", []))
    display_name = build_display_name(ver_name, game_versions, ver_type)
    log("USER", f"Selected version: {ver_name} ({ver_type}) for {game_versions}")
    file_url = find_mrpack_url(sel)
    if not file_url:
        log("CRITICAL", "No .mrpack found for this version")
        final_summary_and_save()
    tmp_pkg = os.path.join(tempfile.gettempdir(), os.path.basename(file_url))
    log("COMMENT", f"Downloading package {file_url}")
    try:
        # keep rich download for the package here (installer flow)
        download_with_rich([file_url], [tmp_pkg], description="Downloading package")
        log("OK", f"Downloaded package: {os.path.basename(file_url)}")
    except Exception as e:
        log("ERROR", f"Download error: {e}")
        final_summary_and_save()
    td = tempfile.mkdtemp()
    log("COMMENT", f"Extracting package to {td}")
    try:
        with zipfile.ZipFile(tmp_pkg) as z:
            z.extractall(td)
        log("OK", "Extraction complete")
    except Exception as e:
        log("ERROR", f"Extract error: {e}")
        final_summary_and_save()
    manifest = load_manifest_from_td(td)
    if not manifest:
        log("CRITICAL", "No manifest found in package")
        final_summary_and_save()
    deps = manifest.get("dependencies", {}) or {}
    mc_version = deps.get("minecraft") or deps.get("minecraft_version") or (manifest.get("game_versions") or [None])[0]
    loader_version = deps.get("fabric-loader") or deps.get("fabric_loader") or deps.get("loader")
    if loader_version and launcher_present(mc):
        log("USER", f"Installing Fabric loader {loader_version} for Minecraft {mc_version or 'unknown'}")
        run_fabric_installer(mc, loader_version, mc_version)
    else:
        if not loader_version:
            log("WARN", "No fabric-loader version found in manifest; skipping automatic loader install")
        else:
            log("WARN", "Official launcher not detected; skipping automatic loader install")
    log("USER", "Downloading and installing files into Minecraft directory")
    download_and_place(manifest, mc, td, overwrite=False)
    log("OK", "Installation complete")
    log("OK", f"Launch Minecraft with Fabric loader to use {display_name}")
    shutil.rmtree(td, ignore_errors=True)
    try:
        os.remove(tmp_pkg)
    except OSError:
        log("WARN", f"Could not remove temporary package {tmp_pkg}")
    final_summary_and_save()

def run() -> None:
    if not TEXTUAL_AVAILABLE:
        print("\033[91mTextual is required for this installer. Install textual and rerun.\033[0m")
        sys.exit(1)
    if not RICH_AVAILABLE:
        print("\033[91mRich is required for progress bars. Install rich and rerun.\033[0m")
        sys.exit(1)
    default_mc = os.path.expanduser("~/.minecraft")
    if not os.access(default_mc, os.W_OK):
        default_mc = os.path.expanduser("~/minecraft-offinstaller")
    log("COMMENT", "Starting OptiFine for Fabric installer")
    app = ChoiceApp("Choose action", ["Install to Minecraft Launcher", "Parse and Download Modpack Files"])
    app.run()
    action = app.selection
    log("USER", f"Action chosen: {action}")
    try:
        log("COMMENT", "Fetching available versions from Modrinth")
        versions = fetch_versions()
        log("OK", "Fetched versions from Modrinth")
    except Exception as e:
        log("CRITICAL", f"Network error fetching versions: {e}")
        final_summary_and_save()
    if not versions:
        log("CRITICAL", "No versions found; aborting")
        final_summary_and_save()
    if action == "Parse and Download Modpack Files":
        parse_and_download_flow(versions)
        return
    inp = InputApp("Enter Minecraft directory (leave empty for default):", placeholder=default_mc, default=default_mc)
    inp.run()
    mc = inp.value or default_mc
    log("USER", f"Minecraft directory chosen: {mc}")
    try:
        ensure_dir(mc)
    except Exception as e:
        log("CRITICAL", f"Could not create/access Minecraft directory: {e}")
        final_summary_and_save()
    if not launcher_present(mc):
        log("WARN", f"Official Minecraft launcher not detected in {mc}")
        app2 = ChoiceApp("Launcher not detected. Choose an action:", ["Change Minecraft directory", "Download and parse modpack instead"])
        app2.run()
        choice = app2.selection
        log("USER", f"Launcher-missing choice: {choice}")
        if choice == "Change Minecraft directory":
            inp2 = InputApp("Enter new Minecraft directory (leave empty to cancel):", placeholder=default_mc, default=default_mc)
            inp2.run()
            new_mc = inp2.value
            log("USER", f"New Minecraft directory input: {new_mc}")
            if new_mc:
                mc = new_mc
                try:
                    ensure_dir(mc)
                except Exception as e:
                    log("CRITICAL", f"Could not create/access Minecraft directory: {e}")
                    final_summary_and_save()
                if not launcher_present(mc):
                    app3 = ChoiceApp("Launcher still not detected. Choose:", ["Download and parse modpack instead", "Continue and install anyway"])
                    app3.run()
                    fallback = app3.selection
                    log("USER", f"Fallback choice: {fallback}")
                    if fallback == "Download and parse modpack instead":
                        parse_and_download_flow(versions)
                        return
                    else:
                        log("WARN", "Continuing with installation despite missing launcher")
            else:
                log("COMMENT", "No directory chosen; switching to Parse & Download flow")
                parse_and_download_flow(versions)
                return
        else:
            parse_and_download_flow(versions)
            return
    log("OK", "Official Minecraft launcher detected; proceeding with installation")
    step_install_flow(mc, versions)

def fetch_versions() -> List[dict]:
    r = requests.get(MODRINTH_API, headers={"User-Agent": UA})
    r.raise_for_status()
    return r.json()

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print(f"{ANSI['CRITICAL']}{now_ts()} {SYMBOL['CRITICAL']} Cancelled.{ANSI['RESET']}")
        sys.exit(1)

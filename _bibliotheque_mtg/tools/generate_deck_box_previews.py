"""Generate the chooser WebPs with an installed Chromium browser."""

import argparse
import base64
from functools import partial
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic
from threading import Event, Thread


OPS_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = OPS_ROOT / "src/python"
UI_ROOT = SOURCE_ROOT / "bibliotheque_mtg/ui"
BASELINE_PATH = Path(__file__).resolve().with_name("deck_box_preview_sources.json")
STATE_PATH = OPS_ROOT / "state/deck-box-preview-sources.json"

RENDER_SCRIPT = """
async function generatePreviews() {
    const images = {}, errors = {};
    const mesh = await loadDeckBoxMesh();
    const grain = await loadDeckBoxGrain();
    for (const design of designs) {
        const started = performance.now();
        try {
            let finish = deckBoxColors.get(design);
            if (deckBoxPanoramas.has(design)) {
                finish = { texture: await loadDeckBoxTexture(`/deck-box-${design}.webp`) };
            } else if (design !== "standard" && !finish) {
                throw new Error(`Unknown design: ${design}`);
            }
            const source = new Image();
            source.src = createDeckBoxRenderer(mesh, grain, finish).still;
            await source.decode();
            const canvas = document.createElement("canvas");
            canvas.width = 256;
            canvas.height = Math.round(canvas.width * source.height / source.width);
            const context = canvas.getContext("2d");
            context.imageSmoothingQuality = "high";
            context.drawImage(source, 0, 0, canvas.width, canvas.height);
            const url = canvas.toDataURL("image/webp", .88);
            if (!url.startsWith("data:image/webp;base64,")) throw new Error("WebP encoding unavailable");
            images[design] = { data: url.split(",")[1], ms: Math.round(performance.now() - started) };
        } catch (error) {
            errors[design] = String(error);
        }
    }
    return { images, errors };
}
generatePreviews()
    .catch(error => ({ error: String(error) }))
    .then(result => fetch(resultPath, { method: "POST", body: JSON.stringify(result) }));
"""


class PreviewHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.send_error(403)
            return
        if self.path == "/":
            page = self.server.page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif self.path in self.server.textures:
            content = self.server.textures[self.path]
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/deck-box.js":
            super().do_GET()
        elif re.fullmatch(r"/deck-box-[a-z-]+\.(?:json|png|webp)", self.path):
            self.path = "/assets" + self.path
            super().do_GET()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.headers.get("Origin") != f"http://127.0.0.1:{self.server.server_port}":
            self.send_error(403)
            return
        if self.path != self.server.result_path:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4 * 1024 * 1024:
                self.send_error(413)
                return
            self.connection.settimeout(3)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
        except (OSError, ValueError):
            self.send_error(400)
            return
        self.server.payload = payload
        self.send_response(204)
        self.end_headers()
        self.server.finished.set()


def render_previews(browser_path, designs=None, textures=None):
    design_expression = (
        json.dumps(designs) if designs is not None else "deckBoxDesigns.map(([design]) => design)"
    )
    handler = partial(PreviewHandler, directory=UI_ROOT)
    with (
        TemporaryDirectory(prefix="deck-box-preview-", ignore_cleanup_errors=True) as profile,
        ThreadingHTTPServer(("127.0.0.1", 0), handler) as server,
    ):
        server.finished = Event()
        server.result_path = f"/result/{token_urlsafe(24)}"
        server.textures = textures or {}
        server.page = (
            '<!doctype html><meta charset="utf-8"><script src="/deck-box.js"></script>'
            f"<script>const resultPath = {json.dumps(server.result_path)};\n"
            f"const designs = {design_expression};\n{RENDER_SCRIPT}</script>"
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            browser = subprocess.Popen([
                str(browser_path), "--headless", f"--user-data-dir={profile}", "--no-first-run",
                "--disable-background-networking",
                f"http://127.0.0.1:{server.server_port}/",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = monotonic() + 30
                while not server.finished.wait(timeout=0.2):
                    if browser.poll() is not None:
                        raise RuntimeError(f"Chromium exited with status {browser.returncode}.")
                    if monotonic() >= deadline:
                        raise RuntimeError("Chromium did not return preview images within 30 seconds.")
                payload = server.payload
            finally:
                if browser.poll() is None:
                    browser.kill()
                    browser.wait(timeout=3)
        finally:
            server.shutdown()
            thread.join()

    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload


def find_browser():
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if root := os.environ.get(variable):
            for relative in (
                "Google/Chrome/Application/chrome.exe",
                "Microsoft/Edge/Application/msedge.exe",
                "Chromium/Application/chrome.exe",
            ):
                candidate = Path(root) / relative
                if candidate.is_file():
                    return candidate
    return None


def atomic_write(path, content):
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def save_preview(design, image):
    from PIL import Image

    if not re.fullmatch(r"[a-z-]+", design):
        raise ValueError("Invalid deck box design ID")
    content = base64.b64decode(image["data"], validate=True)
    with Image.open(BytesIO(content)) as decoded:
        if decoded.format != "WEBP" or decoded.width != 256:
            raise ValueError("Invalid preview WebP")
        decoded.load()
    path = UI_ROOT / "assets" / f"deck-box-preview-{design}.webp"
    atomic_write(path, content)
    print(f"{path.name} ({len(content):,} bytes, {image['ms']} ms)")


def load_fingerprints(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        print(f"{path.name}: {error}", file=sys.stderr)
    return {}


def refresh_previews(designs, browser_path=None):
    baseline = load_fingerprints(BASELINE_PATH)
    state = load_fingerprints(STATE_PATH)
    assets = UI_ROOT / "assets"
    pending = {}
    failed = False
    for design in sorted(designs):
        source = assets / f"deck-box-{design}.webp"
        try:
            data = source.read_bytes()
        except OSError as error:
            print(f"{source.name}: {error}", file=sys.stderr)
            failed = True
            continue
        fingerprint = sha256(data).hexdigest()
        preview = assets / f"deck-box-preview-{design}.webp"
        if fingerprint != state.get(design, baseline.get(design)) or not preview.is_file():
            pending[design] = (source, data, fingerprint)

    if not pending:
        return not failed

    browser_path = browser_path or find_browser()
    if browser_path is None:
        print("No installed Chromium browser found for deck box previews.", file=sys.stderr)
        return False

    textures = {f"/{source.name}": data for source, data, _ in pending.values()}
    try:
        payload = render_previews(browser_path, list(pending), textures)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(error, file=sys.stderr)
        return False

    updated = {}
    for design, (source, _, fingerprint) in pending.items():
        try:
            if design in payload["errors"]:
                raise ValueError(payload["errors"][design])
            if sha256(source.read_bytes()).hexdigest() != fingerprint:
                raise ValueError("The texture changed during rendering.")
            save_preview(design, payload["images"][design])
            updated[design] = fingerprint
        except (KeyError, OSError, ValueError) as error:
            print(f"{design}: {error}", file=sys.stderr)
            failed = True

    if updated:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        current_state = load_fingerprints(STATE_PATH)
        current_state.update(updated)
        atomic_write(STATE_PATH, (json.dumps(current_state, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return not failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Regenerate stale or missing panorama previews")
    parser.add_argument("browser", type=Path, nargs="?", help="Path to Chrome, Chromium or Edge")
    parser.add_argument("designs", nargs="*", help="Design IDs to render manually (default: all)")
    args = parser.parse_args()
    if any(not re.fullmatch(r"[a-z-]+", design) for design in args.designs):
        parser.error("Invalid deck box design ID")
    if args.refresh and args.designs:
        parser.error("--refresh uses the registered panoramic designs")
    if not args.refresh and args.browser is None:
        parser.error("A browser path is required for manual generation")

    try:
        if args.refresh:
            sys.path.insert(0, str(SOURCE_ROOT))
            from bibliotheque_mtg.personal_decks import PANORAMIC_BOX_DESIGNS

            return 0 if refresh_previews(PANORAMIC_BOX_DESIGNS, args.browser) else 1

        payload = render_previews(args.browser, args.designs or None)
        for design, image in payload["images"].items():
            save_preview(design, image)
        for design, error in payload["errors"].items():
            print(f"{design}: {error}", file=sys.stderr)
        return 1 if payload["errors"] else 0
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

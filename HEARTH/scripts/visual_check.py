"""Real Chromium/Playwright smoke checks against a running local HTTP server."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/visual"))
    parser.add_argument("--bridge", action="store_true", help="Render local assets and bridge fetch through Python when browser navigation is blocked")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    results = []
    with tempfile.TemporaryDirectory(prefix="hearth-visual-") as temporary:
        directory = Path(temporary)
        credentials = directory / "credentials.json"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root / "src")
        with (args.output / "server.log").open("w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "hearth.cli", "serve", "--db", str(directory / "edge.db"),
                 "--credentials", str(credentials), "--port", str(port)], cwd=root, env=env,
                stdout=log, stderr=log)
            try:
                url = f"http://127.0.0.1:{port}"
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("server exited before readiness")
                    try:
                        with urllib.request.urlopen(url + "/healthz", timeout=1) as response:
                            if response.status == 200 and credentials.exists():
                                break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("server readiness timeout")
                key = json.loads(credentials.read_text())["operator"]
                with sync_playwright() as playwright:
                    options = {"headless": True}
                    executable = os.environ.get("CHROMIUM_EXECUTABLE") or shutil.which("chromium")
                    if executable:
                        options["executable_path"] = executable
                    browser = playwright.chromium.launch(**options)
                    try:
                        for width, height, scenario in [(1440, 1000, "normal"), (390, 844, "outage"),
                                                        (320, 768, "sensor_fault")]:
                            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
                            errors = []
                            page.on("pageerror", lambda error: errors.append(str(error)))
                            if args.bridge:
                                # Browser policy may block loopback navigation. Render the local
                                # assets without navigation; Python calls the same real HTTP API.
                                # This is a renderer+HTTP-bridge check, not native browser networking.
                                def http_bridge(path, options):
                                    if path != "/v1/demo/run":
                                        raise ValueError("unexpected bridged route")
                                    request = urllib.request.Request(url + path, data=options["body"].encode(),
                                                                     headers=options["headers"], method=options["method"])
                                    with urllib.request.urlopen(request, timeout=30) as response:
                                        return {"status": response.status, "body": response.read().decode()}
                                page.expose_function("hearthBridge", http_bridge)
                                html = (root / "src/hearth/dashboard.html").read_text()
                                html = html.replace('<link rel="stylesheet" href="/assets/dashboard.css">', "")
                                html = html.replace('<script src="/assets/dashboard.js" defer></script>', "")
                                page.set_content(html)
                                page.add_style_tag(path=str(root / "src/hearth/dashboard.css"))
                                page.evaluate("() => { window.fetch = async (path, options) => { const r = await window.hearthBridge(path, options); return {ok: r.status >= 200 && r.status < 300, status:r.status, json:async()=>JSON.parse(r.body)}; }; }")
                                page.add_script_tag(path=str(root / "src/hearth/dashboard.js"))
                            else:
                                page.goto(url, wait_until="networkidle")
                            page.locator("#api-key").fill(key)
                            page.locator("#scenario").select_option(scenario)
                            page.locator("#run").click()
                            page.wait_for_function("document.querySelector('#message').textContent.startsWith('Completed')", timeout=30000)
                            assert page.locator("#ledger").inner_text() == "Verified"
                            assert page.locator("#final-state").inner_text() == "RED"
                            assert page.locator("#plot path").count() == 2
                            assert page.locator(".transition").count() >= 3
                            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                            if not args.bridge:
                                assert page.evaluate("Object.keys(localStorage).length === 0 && Object.keys(sessionStorage).length === 0")
                            boxes = [page.locator(selector).bounding_box() for selector in ("#api-key", "#scenario", "#run")]
                            for index, first in enumerate(boxes):
                                assert first is not None and first["height"] >= 44
                                for second in boxes[index + 1:]:
                                    assert second is not None
                                    overlap = (first["x"] < second["x"] + second["width"] and second["x"] < first["x"] + first["width"]
                                               and first["y"] < second["y"] + second["height"] and second["y"] < first["y"] + first["height"])
                                    assert not overlap, "interactive controls overlap"
                            page.screenshot(path=str(args.output / f"{width}-{scenario}.png"), full_page=True)
                            assert not errors, errors
                            results.append({"viewport": [width, height], "scenario": scenario, "passed": True,
                                            "transport": "python-http-bridge" if args.bridge else "native-browser-http",
                                            "javascript_errors": errors, "horizontal_overflow": False})
                            page.close()
                    finally:
                        browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

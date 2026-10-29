"""Dashboard screenshots with real device emulation via the Chrome DevTools Protocol (standard library only).

Headless Chrome's --window-size cannot go below ~500 px, so a 390 px phone layout must be emulated:
Emulation.setDeviceMetricsOverride(width, height, deviceScaleFactor, mobile=True) + setTouchEmulationEnabled.

    python scripts/screenshot_cdp.py <chrome.exe> <out.png> <width> <height> <mobile 0|1> <url>
The URL is normally a single-use login link (`trading-engine dashboard login-link`); the page is captured full length.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request


class WS:
    """Minimal RFC 6455 client: text frames, client masking, no extensions (enough for CDP on localhost)."""

    def __init__(self, url):
        assert url.startswith("ws://")
        hostport, path = url[5:].split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.s.recv(4096)
        assert b" 101 " in resp.split(b"\r\n")[0], resp[:200]
        self.buf = resp.split(b"\r\n\r\n", 1)[1]
        self.n = 0

    def send(self, obj):
        data = json.dumps(obj).encode()
        hdr = bytearray([0x81])
        ln = len(data)
        if ln < 126:
            hdr.append(0x80 | ln)
        elif ln < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", ln)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", ln)
        mask = os.urandom(4)
        self.s.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(1 << 20)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        msg = b""
        while True:
            b0, b1 = self._read(2)
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            msg += self._read(ln)
            if b0 & 0x80:
                return json.loads(msg.decode())

    def call(self, method, **params):
        self.n += 1
        self.send({"id": self.n, "method": method, "params": params})
        while True:
            m = self.recv()
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})


def main(chrome, out, width, height, mobile, url):
    width, height, mobile = int(width), int(height), mobile == "1"
    prof = tempfile.mkdtemp(prefix="cdp-")
    port = 9333
    p = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
                          f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        ws = WS(page["webSocketDebuggerUrl"])
        ws.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                deviceScaleFactor=2 if mobile else 1, mobile=mobile)
        if mobile:
            ws.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        ws.call("Page.enable")
        ws.call("Page.navigate", url=url)
        time.sleep(6)                                    # login redirect + data fetches
        h = ws.call("Runtime.evaluate", expression="Math.max(document.body.scrollHeight, "
                    "document.documentElement.scrollHeight)", returnByValue=True)["result"]["value"]
        sw = ws.call("Runtime.evaluate", expression="document.documentElement.scrollWidth",
                     returnByValue=True)["result"]["value"]
        ws.call("Emulation.setDeviceMetricsOverride", width=width, height=int(h), deviceScaleFactor=2 if mobile else 1,
                mobile=mobile)
        time.sleep(1)
        shot = ws.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        with open(out, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print(json.dumps({"out": out, "css_width": width, "page_height": h, "document_scroll_width": sw,
                          "horizontal_overflow": sw > width}))
    finally:
        p.terminate()


if __name__ == "__main__":
    main(*sys.argv[1:])

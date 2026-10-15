"""A fake network camera for dev tests (stands in for the cameras until they exist; P5.2, P4.2).

FFmpeg encodes a synthetic test pattern (or loops a clip, an image or a folder of images as a
slideshow) to H.264 RTP on a local UDP port, and this script serves it over RTSP with RTP
interleaved in the TCP connection, which is what `rtsp:` sources and `parking record` ask for
(`rtsp_transport tcp`). Several clients can watch at once; a client that joins waits for the
next keyframe like with a real camera. Every encoded frame is capped at --max-frame-kb (120 KB),
since a bigger one (a 5 MP keyframe) overflows the local UDP port's buffer and breaks the stream.

    python3 scripts/fake_rtsp.py --port 8554 --fps 10 --size 640x360
    # -> rtsp://127.0.0.1:8554/sub

With `--snapshot-port` it also answers HTTP GETs on any path with a JPEG, like a camera's
snapshot URL: the slideshow's current image as it is on disk (a clip or the test pattern: its
first frame). With `--auth user:pass` (or FAKE_CAMERA_AUTH) both need that login: basic auth
for RTSP, digest for HTTP (`--http-auth basic` for basic), as many cameras do. With `--cover IMG`,
SIGUSR1 "covers the lens": snapshots return IMG until the next SIGUSR1 (P4.4's lens test;
`docker kill -s USR1 <container>`). The RTSP stream isn't covered.

    python3 scripts/fake_rtsp.py --input ../data/replay/ground-feed --size native --fps 2 \
        --hold-s 5 --snapshot-port 8080 --auth admin:secret
    # -> rtsp://admin:secret@127.0.0.1:8554/sub and http://admin:secret@127.0.0.1:8080/snap.jpg

Needs the `ffmpeg` binary (the vision image has it). Stop with Ctrl-C. Dev only: TCP only,
one stream; never expose it outside the machine.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import itertools
import os
import re
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

IMAGE_SUFFIXES = {".jpg", ".jpeg"}


class Relay:
    """Receives FFmpeg's RTP packets on UDP and copies them to every playing client."""

    def __init__(self, port: int):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.clients: dict[int, tuple[socket.socket, threading.Lock]] = {}
        self.lock = threading.Lock()
        self.packets = 0
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, sock: socket.socket, lock: threading.Lock) -> None:
        with self.lock:
            self.clients[id(sock)] = (sock, lock)

    def remove(self, sock: socket.socket) -> None:
        with self.lock:
            self.clients.pop(id(sock), None)

    def _run(self) -> None:
        while True:
            pkt = self.sock.recv(65535)
            self.packets += 1
            frame = b"$\x00" + len(pkt).to_bytes(2, "big") + pkt  # channel 0 = RTP
            with self.lock:
                clients = list(self.clients.values())
            for sock, lock in clients:
                try:
                    with lock:
                        sock.sendall(frame)
                except OSError:
                    self.remove(sock)


class Handler(socketserver.StreamRequestHandler):
    server: Server

    def handle(self) -> None:
        sock: socket.socket = self.request
        send_lock = threading.Lock()
        session = None
        try:
            while True:
                first = self.rfile.read(1)
                if not first:
                    return
                if first == b"$":  # interleaved RTCP from the client: skip it
                    hdr = self.rfile.read(3)
                    self.rfile.read(int.from_bytes(hdr[1:3], "big"))
                    continue
                lines = [first + self.rfile.readline()]
                while lines[-1].strip():
                    lines.append(self.rfile.readline())
                method, url, *_ = lines[0].decode(errors="replace").split()
                headers = {}
                for raw in lines[1:]:
                    k, _, v = raw.decode(errors="replace").partition(":")
                    if v:
                        headers[k.strip().lower()] = v.strip()
                if n := int(headers.get("content-length", 0)):
                    self.rfile.read(n)
                cseq = headers.get("cseq", "0")
                extra: dict[str, str] = {}
                body = b""
                status = "200 OK"
                expected = self.server.basic_header
                if (
                    method not in ("OPTIONS", "TEARDOWN")
                    and expected
                    and (headers.get("authorization") != expected)
                ):
                    status = "401 Unauthorized"
                    extra["WWW-Authenticate"] = 'Basic realm="fake-camera"'
                elif method == "OPTIONS":
                    extra["Public"] = "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"
                elif method == "DESCRIBE":
                    body = self.server.sdp.encode()
                    extra["Content-Type"] = "application/sdp"
                    extra["Content-Base"] = url.rstrip("/") + "/"
                elif method == "SETUP":
                    if "TCP" not in headers.get("transport", "").upper():
                        status = "461 Unsupported Transport"
                    else:
                        session = session or f"{next(self.server.ids):08d}"
                        extra["Transport"] = "RTP/AVP/TCP;unicast;interleaved=0-1"
                elif method == "PLAY":
                    extra["Range"] = "npt=0.000-"
                elif method == "TEARDOWN":
                    pass
                elif method != "GET_PARAMETER":
                    status = "405 Method Not Allowed"
                if session and method != "OPTIONS":
                    extra["Session"] = f"{session};timeout=60"
                head = f"RTSP/1.0 {status}\r\nCSeq: {cseq}\r\n"
                head += "".join(f"{k}: {v}\r\n" for k, v in extra.items())
                head += f"Content-Length: {len(body)}\r\n\r\n"
                with send_lock:
                    sock.sendall(head.encode() + body)
                if method == "PLAY" and status.startswith("200"):
                    self.server.relay.add(sock, send_lock)
                if method == "TEARDOWN":
                    return
        except (OSError, ValueError):
            return
        finally:
            self.server.relay.remove(sock)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, sdp: str, relay: Relay, auth: str | None = None):
        super().__init__(("0.0.0.0", port), Handler)
        self.sdp = sdp
        self.relay = relay
        self.ids = itertools.count(10000001)
        self.basic_header = "Basic " + base64.b64encode(auth.encode()).decode() if auth else None


class Stills:
    """The JPEG a snapshot returns: the slideshow's current image (same clock as FFmpeg's)."""

    def __init__(self, files: list[Path], hold_s: float, cover: Path | None = None):
        self.files = files
        self.hold_s = hold_s
        self.t0 = time.monotonic()
        self.cover = cover
        self.covered = False

    def toggle_cover(self) -> None:
        if self.cover is not None:
            self.covered = not self.covered
            state = "covered" if self.covered else "uncovered"
            print(f"lens {state} at {time.strftime('%Y-%m-%dT%H:%M:%S%z')}", flush=True)

    def current(self) -> bytes:
        if self.covered and self.cover is not None:
            return self.cover.read_bytes()
        i = int((time.monotonic() - self.t0) / self.hold_s) % len(self.files)
        return self.files[i].read_bytes()


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


class SnapshotHandler(http.server.BaseHTTPRequestHandler):
    server: SnapshotServer

    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        if not self._authorized():
            self.send_response(401)
            if self.server.scheme == "digest":
                nonce = secrets.token_hex(16)
                self.server.nonces.add(nonce)
                challenge = f'Digest realm="fake-camera", nonce="{nonce}", qop="auth"'
            else:
                challenge = 'Basic realm="fake-camera"'
            self.send_header("WWW-Authenticate", challenge)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.server.stills.current()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        auth = self.server.auth
        if not auth:
            return True
        given = self.headers.get("Authorization", "")
        if self.server.scheme == "basic":
            return given == "Basic " + base64.b64encode(auth.encode()).decode()
        if not given.startswith("Digest "):
            return False
        p = {k: a or b for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', given)}
        user, _, password = auth.partition(":")
        if p.get("username") != user or p.get("nonce") not in self.server.nonces:
            return False
        ha1 = _md5(f"{user}:fake-camera:{password}")
        ha2 = _md5(f"GET:{p.get('uri', '')}")
        want = _md5(f"{ha1}:{p['nonce']}:{p.get('nc', '')}:{p.get('cnonce', '')}:auth:{ha2}")
        return p.get("uri") == self.path and secrets.compare_digest(p.get("response", ""), want)

    def log_message(self, format, *args) -> None:  # noqa: A002 (quiet, like the RTSP side)
        pass


class SnapshotServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, stills: Stills, auth: str | None, scheme: str):
        super().__init__(("0.0.0.0", port), SnapshotHandler)
        self.stills = stills
        self.auth = auth
        self.scheme = scheme
        self.nonces: set[str] = set()


def image_files(path: Path) -> list[Path]:
    """The images an --input stands for: a folder's JPEGs in name order, or the image itself."""
    if path.is_dir():
        return sorted(f for f in path.iterdir() if f.suffix.lower() in IMAGE_SUFFIXES)
    return [path] if path.suffix.lower() in IMAGE_SUFFIXES else []


def ffmpeg_command(args: argparse.Namespace, rtp_port: int, sdp_file: Path) -> list[str]:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-re"]
    images = image_files(args.input) if args.input else []
    if images:  # a slideshow: each image for --hold-s seconds, round and round
        cmd += ["-loop", "1", "-framerate", f"1/{args.hold_s:g}"]
        if args.input.is_dir():
            pattern = str(args.input / ("*" + images[0].suffix))
            cmd += ["-pattern_type", "glob", "-i", pattern]
        else:
            cmd += ["-i", str(args.input)]
    elif args.input:
        cmd += ["-stream_loop", "-1", "-i", str(args.input)]
    else:
        src = f"testsrc2=size={args.size}:rate={args.fps}"
        cmd += ["-f", "lavfi", "-i", src]
    cmd += ["-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]
    cmd += ["-pix_fmt", "yuv420p", "-r", str(args.fps), "-g", str(int(args.fps * args.gop_s))]
    # A frame's packets arrive in one burst on the local UDP port; a 5 MP keyframe (1-2 MB)
    # overflows the socket's buffer (net.core.rmem_max, ~200 KB) and breaks the stream (P4.3).
    # Cap every frame's size (VBV).
    vbv_kbit = int(args.max_frame_kb * 8)
    cmd += ["-maxrate", f"{max(1, int(vbv_kbit * args.fps / 2))}k", "-bufsize", f"{vbv_kbit}k"]
    if args.input and args.size != "native":
        w, h = args.size.split("x")
        cmd += ["-vf", f"scale={w}:{h}"]
    # SPS/PPS before every keyframe, like a camera, so a client joining mid-stream decodes
    cmd += ["-bsf:v", "dump_extra=freq=keyframe"]
    cmd += ["-f", "rtp", "-sdp_file", str(sdp_file), f"rtp://127.0.0.1:{rtp_port}?pkt_size=1200"]
    return cmd


def rtsp_sdp(ffmpeg_sdp: str) -> str:
    """FFmpeg's RTP SDP, turned into a DESCRIBE answer (no address, a track control URL)."""
    out = []
    for line in ffmpeg_sdp.splitlines():
        if line.startswith("c="):
            line = "c=IN IP4 0.0.0.0"
        elif line.startswith("m=video"):
            parts = line.split()
            parts[1] = "0"
            line = " ".join(parts)
        out.append(line)
        if line.startswith("m=video"):
            out.append("a=control:trackID=0")
    return "\r\n".join(out) + "\r\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, default=8554)
    p.add_argument("--fps", type=float, default=10)
    p.add_argument("--size", default="640x360")
    p.add_argument("--gop-s", type=float, default=2.0, help="keyframe interval in seconds")
    p.add_argument(
        "--input", type=Path, help="a clip to loop, or an image / folder of JPEGs for a slideshow"
    )
    p.add_argument(
        "--max-frame-kb",
        type=float,
        default=120,
        help="largest encoded frame in KB (more is lost on the local UDP hop)",
    )
    p.add_argument("--hold-s", type=float, default=5.0, help="seconds per image of a slideshow")
    p.add_argument("--snapshot-port", type=int, help="also serve JPEG snapshots over HTTP here")
    p.add_argument(
        "--auth",
        default=os.environ.get("FAKE_CAMERA_AUTH") or None,
        help="user:pass required by RTSP and HTTP (default: $FAKE_CAMERA_AUTH; none: no login)",
    )
    p.add_argument("--http-auth", choices=("digest", "basic"), default="digest")
    p.add_argument("--cover", type=Path, help="JPEG snapshots return after SIGUSR1 (lens covered)")
    args = p.parse_args()
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found (run this in the vision container)", file=sys.stderr)
        return 1
    if args.size == "native" and not args.input:
        p.error("--size native needs an --input")
    if args.auth is not None and ":" not in args.auth:
        p.error("--auth must be user:pass")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        rtp_port = probe.getsockname()[1] & ~1 or 50000  # RTP wants an even port
    relay = Relay(rtp_port)
    tmp = Path(tempfile.mkdtemp(prefix="fake-rtsp-"))
    sdp_file = tmp / "stream.sdp"
    ff = subprocess.Popen(ffmpeg_command(args, rtp_port, sdp_file))
    for _ in range(100):
        if sdp_file.is_file() and "m=video" in sdp_file.read_text():
            break
        if ff.poll() is not None:
            print("ffmpeg exited", file=sys.stderr)
            return 1
        time.sleep(0.1)
    else:
        ff.terminate()
        print("ffmpeg wrote no SDP", file=sys.stderr)
        return 1

    server = Server(args.port, rtsp_sdp(sdp_file.read_text()), relay, args.auth)
    snap = None
    if args.snapshot_port:
        files = image_files(args.input) if args.input else []
        if not files:  # a clip or the test pattern: its first frame
            still = tmp / "still.jpg"
            src = ["-i", str(args.input)] if args.input else []
            src = src or ["-f", "lavfi", "-i", f"testsrc2=size={args.size}:rate=1"]
            cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *src]
            subprocess.run([*cmd, "-frames:v", "1", "-q:v", "3", str(still)], check=True)
            files = [still]
        stills = Stills(files, args.hold_s, args.cover)
        signal.signal(signal.SIGUSR1, lambda *_: stills.toggle_cover())
        snap = SnapshotServer(args.snapshot_port, stills, args.auth, args.http_auth)
        threading.Thread(target=snap.serve_forever, daemon=True).start()
    print(f"serving rtsp://127.0.0.1:{args.port}/sub ({args.size} @ {args.fps:g} fps)", flush=True)
    if snap:
        print(f"serving http://127.0.0.1:{args.snapshot_port}/snap.jpg", flush=True)

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
        if snap:
            threading.Thread(target=snap.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        ff.terminate()
        try:
            ff.wait(5)
        except subprocess.TimeoutExpired:
            ff.kill()
        for f in tmp.iterdir():
            f.unlink()
        os.rmdir(tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())

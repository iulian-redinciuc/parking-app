"""A fake RTSP camera for dev tests (stands in for Camera A until it exists; P5.2).

FFmpeg encodes a synthetic test pattern (or loops a clip) to H.264 RTP on a local UDP port,
and this script serves it over RTSP with RTP interleaved in the TCP connection, which is
what `rtsp:` sources and `parking record` ask for (`rtsp_transport tcp`). Several clients
can watch at once; a client that joins waits for the next keyframe like with a real camera.

    python3 scripts/fake_rtsp.py --port 8554 --fps 10 --size 640x360
    # -> rtsp://127.0.0.1:8554/sub

Needs the `ffmpeg` binary (the vision image has it). Stop with Ctrl-C. Dev only: no auth,
TCP only, one stream; never expose it outside the machine.
"""

from __future__ import annotations

import argparse
import itertools
import os
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
                if method == "OPTIONS":
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

    def __init__(self, port: int, sdp: str, relay: Relay):
        super().__init__(("0.0.0.0", port), Handler)
        self.sdp = sdp
        self.relay = relay
        self.ids = itertools.count(10000001)


def ffmpeg_command(args: argparse.Namespace, rtp_port: int, sdp_file: Path) -> list[str]:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-re"]
    if args.input:
        cmd += ["-stream_loop", "-1", "-i", str(args.input)]
    else:
        src = f"testsrc2=size={args.size}:rate={args.fps}"
        cmd += ["-f", "lavfi", "-i", src]
    cmd += ["-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]
    cmd += ["-pix_fmt", "yuv420p", "-r", str(args.fps), "-g", str(int(args.fps * args.gop_s))]
    if args.input:
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
    p.add_argument("--input", type=Path, help="a clip to loop instead of the test pattern")
    args = p.parse_args()
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found (run this in the vision container)", file=sys.stderr)
        return 1

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

    server = Server(args.port, rtsp_sdp(sdp_file.read_text()), relay)
    print(f"serving rtsp://127.0.0.1:{args.port}/sub ({args.size} @ {args.fps:g} fps)", flush=True)

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

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

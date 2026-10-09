"""Recording a camera's stream and checking a source's frame rate (P5.2).

`parking record` saves a camera's RTSP stream to MP4 with **FFmpeg stream copy** (no
re-encoding, almost no CPU): `ffmpeg -rtsp_transport tcp -i URL -map 0:v:0 -c copy -t S`.
Only the video goes in (no audio). The MP4 is fragmented, so a recording cut short (camera
gone, process killed) still plays up to the cut. FFmpeg's messages can hold the URL, so
everything it prints passes through `redact()`; the URL is never logged.

`measure_stream` reads from any `FrameSource` for a while and reports the rate of **new**
frames (by `Frame.seq`) per window, the longest gap and whether that is steady.
"""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from parking.vision.sources import FrameSource

RTSP_TIMEOUT_US = 5_000_000  # FFmpeg's RTSP socket timeout: a dead camera ends the recording
MP4_FLAGS = "+frag_keyframe+empty_moov+default_base_moof"
STOP_GRACE_S = 30.0  # past the duration before FFmpeg is stopped
SHORT_FRACTION = 0.95  # a recording shorter than this share of the request counts as cut short
STEADY_TOLERANCE = 0.2  # every window within ±20% of the median rate
STEADY_MAX_GAP_S = 1.0  # and no gap between new frames longer than this


class RecordError(RuntimeError):
    pass


def ffmpeg_tool(name: str = "ffmpeg") -> str:
    path = shutil.which(name)
    if path is None:
        raise RecordError(
            f"{name} not found: install FFmpeg, or run this in the vision container "
            "(docker compose ... run --rm --entrypoint parking vision-occupancy record ...)"
        )
    return path


def record_command(ffmpeg: str, url: str, seconds: float, out: Path) -> list[str]:
    """The FFmpeg command line: stream copy of the first video stream into fragmented MP4."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if urlsplit(url).scheme in ("rtsp", "rtsps"):
        cmd += ["-rtsp_transport", "tcp", "-timeout", str(RTSP_TIMEOUT_US)]
    cmd += ["-i", url, "-map", "0:v:0", "-c", "copy", "-t", f"{seconds:g}"]
    cmd += ["-movflags", MP4_FLAGS, "-f", "mp4", "-n", str(out)]
    return cmd


def redact(text: str, url: str) -> str:
    """`text` with the URL and its password (plain and percent-encoded) blanked out."""
    parts = urlsplit(url)
    secrets = [url]
    if parts.password:
        secrets += [parts.password, unquote(parts.password), quote(unquote(parts.password))]
    for s in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(s, "<camera url>" if s == url else "***")
    return text


@dataclass
class VideoInfo:
    codec: str | None
    width: int | None
    height: int | None
    fps: float | None  # average frame rate
    frames: int | None  # video packets (= frames for H.264/H.265)
    duration_s: float | None


def _rate(value: str | None) -> float | None:
    if not value or "/" not in value:
        return None
    num, den = value.split("/", 1)
    try:
        return float(num) / float(den) if float(den) else None
    except ValueError:
        return None


def probe(path: Path, ffprobe: str | None = None) -> VideoInfo:
    """Codec, size, frame rate, frame count and duration of a video file (no decoding)."""
    cmd = [ffprobe or ffmpeg_tool("ffprobe"), "-v", "error", "-select_streams", "v:0"]
    cmd += ["-count_packets", "-show_entries"]
    cmd += ["stream=codec_name,width,height,avg_frame_rate,nb_read_packets:format=duration"]
    cmd += ["-of", "json", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if res.returncode != 0:
        raise RecordError(f"ffprobe can't read {path}: {res.stderr.strip() or res.returncode}")
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams") or [{}]
    st = streams[0]
    duration = data.get("format", {}).get("duration")
    frames = st.get("nb_read_packets")
    return VideoInfo(
        codec=st.get("codec_name"),
        width=st.get("width"),
        height=st.get("height"),
        fps=_rate(st.get("avg_frame_rate")),
        frames=int(frames) if frames is not None else None,
        duration_s=float(duration) if duration is not None else None,
    )


@dataclass
class Recording:
    path: Path
    requested_s: float
    returncode: int
    errors: str  # FFmpeg's messages, redacted
    info: VideoInfo | None = None

    @property
    def complete(self) -> bool:
        d = self.info.duration_s if self.info else None
        return d is not None and d >= self.requested_s * SHORT_FRACTION


def record(
    url: str,
    seconds: float,
    out: Path,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    run: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> Recording:
    """Record `seconds` of the stream to `out` and probe the result."""
    ffmpeg = ffmpeg or ffmpeg_tool()
    ffprobe = ffprobe or ffmpeg_tool("ffprobe")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = record_command(ffmpeg, url, seconds, out)
    proc = run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, err = proc.communicate(timeout=seconds + STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.terminate()  # FFmpeg finishes the file on SIGTERM
        try:
            _, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
        err = (err or "") + f"\nstopped: still running {STOP_GRACE_S:g} s past the duration"
    except BaseException:  # Ctrl-C: let FFmpeg close the file, then pass it on
        proc.terminate()
        proc.wait(timeout=10)
        raise
    rec = Recording(out, seconds, proc.returncode, redact((err or "").strip(), url))
    if out.is_file() and out.stat().st_size > 0:
        try:
            rec.info = probe(out, ffprobe)
        except RecordError as e:
            rec.errors = (rec.errors + "\n" + redact(str(e), url)).strip()
    return rec


@dataclass
class StreamStats:
    seconds: float
    frames: int  # new frames seen
    repeats: int  # reads that returned a frame already seen
    misses: int  # reads that returned nothing
    windows: list[float] = field(default_factory=list)  # new frames per second, per window
    max_gap_s: float = 0.0
    fps: float = 0.0  # frames / measured span

    @property
    def steady(self) -> bool:
        """Every full window within ±20% of the median and no gap over 1 s."""
        if not self.windows or self.frames < 2:
            return False
        med = statistics.median(self.windows)
        if med <= 0:
            return False
        ok = all(abs(w - med) <= STEADY_TOLERANCE * med for w in self.windows)
        return ok and self.max_gap_s <= STEADY_MAX_GAP_S

    def to_dict(self) -> dict:
        return asdict(self) | {"steady": self.steady}


def measure_stream(
    src: FrameSource,
    seconds: float,
    window: float = 10.0,
    start_timeout: float = 20.0,
    poll_s: float = 0.005,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_window: Callable[[float, float], None] | None = None,
) -> StreamStats:
    """Read from `src` for `seconds` (timed from the first frame) and count new frames.

    A frame is new when its `seq` differs from the previous one (sources without `seq` count
    every frame). `on_window(elapsed, fps)` is called after each full window. Stops early when
    the source is exhausted (end of a recording) or no frame came within `start_timeout`.
    """
    stats = StreamStats(seconds=0.0, frames=0, repeats=0, misses=0)
    t0 = clock()
    first = last = win_start = 0.0
    last_seq: int | None = None
    win_frames = 0
    while not getattr(src, "exhausted", False):
        now = clock()
        if stats.frames and now - first >= seconds:
            break
        if not stats.frames and now - t0 >= start_timeout:
            break
        frame = src.read()
        now = clock()
        if frame is None or (frame.seq is not None and frame.seq == last_seq):
            if frame is None:
                stats.misses += 1
            else:
                stats.repeats += 1
            sleep(poll_s)
            continue
        last_seq = frame.seq
        if stats.frames:
            stats.max_gap_s = max(stats.max_gap_s, now - last)
        else:
            first = win_start = now
        last = now
        stats.frames += 1
        win_frames += 1
        if now - win_start >= window:
            rate = (win_frames - 1) / (now - win_start)
            stats.windows.append(rate)
            if on_window is not None:
                on_window(now - first, rate)
            win_start, win_frames = now, 1
    if last > first:
        stats.seconds = last - first
        stats.fps = (stats.frames - 1) / stats.seconds
    return stats

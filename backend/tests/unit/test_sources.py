"""Frame sources (config.md "Source URI formats"): file, folder replay, URI parsing."""

import cv2
import numpy as np
import pytest

from parking.vision.sources import FileSource, FolderReplaySource, FrameSource, make_source


def write_img(path, value):
    cv2.imwrite(str(path), np.full((8, 12, 3), value, np.uint8))
    return path


def values(source, n):
    out = []
    for _ in range(n):
        f = source.read()
        out.append(None if f is None else int(f.image[0, 0, 0]))
    return out


def test_file_source_same_image_every_time(tmp_path):
    src = make_source(f"file:{write_img(tmp_path / 'a.png', 77)}")
    assert isinstance(src, FileSource) and isinstance(src, FrameSource) and src.replay
    a, b = src.read(), src.read()
    assert a.image.shape == (8, 12, 3) and np.array_equal(a.image, b.image)
    assert a.ts.tzinfo is not None and b.ts >= a.ts
    a.image[:] = 0  # callers may draw on the frame without changing the next one
    assert src.read().image[0, 0, 0] == 77
    src.close()


def test_file_source_missing_or_unreadable_returns_none(tmp_path):
    assert make_source(f"file:{tmp_path / 'nope.jpg'}").read() is None
    (tmp_path / "bad.jpg").write_bytes(b"not an image")
    assert make_source(f"file:{tmp_path / 'bad.jpg'}").read() is None


def test_relative_paths_resolve_against_root(tmp_path):
    write_img(tmp_path / "a.png", 5)
    assert make_source("file:a.png", root=tmp_path).read().image[0, 0, 0] == 5
    src = make_source("folder:.", root=tmp_path)
    assert src.folder == tmp_path / "."


def test_folder_sorted_and_loops(tmp_path):
    for name, v in [("b.png", 2), ("a.jpg", 1), ("c.PNG", 3)]:
        write_img(tmp_path / name, v)
    (tmp_path / "notes.txt").write_text("skip me")
    (tmp_path / "sub.png").mkdir()
    src = make_source(f"folder:{tmp_path}")
    assert isinstance(src, FolderReplaySource) and src.interval == 5 and src.loop
    got = values(src, 7)
    # JPEG is lossy, so allow ±2 on the jpg
    assert [abs(g - e) <= 2 for g, e in zip(got, [1, 2, 3, 1, 2, 3, 1], strict=True)] == [True] * 7


def test_folder_picks_up_new_files_on_next_pass(tmp_path):
    write_img(tmp_path / "1.png", 10)
    src = make_source(f"folder:{tmp_path}?interval=0.5")
    assert src.interval == 0.5
    assert values(src, 1) == [10]
    write_img(tmp_path / "2.png", 20)
    assert values(src, 3) == [10, 20, 10]
    (tmp_path / "1.png").unlink()
    assert values(src, 2) == [20, 20]


def test_folder_no_loop_stops_at_end(tmp_path):
    write_img(tmp_path / "1.png", 10)
    write_img(tmp_path / "2.png", 20)
    src = make_source(f"folder:{tmp_path}?loop=false&interval=1")
    assert not src.loop
    assert values(src, 2) == [10, 20] and not src.exhausted
    assert src.read() is None and src.exhausted
    write_img(tmp_path / "3.png", 30)
    assert src.read() is None


def test_folder_empty_or_missing_returns_none_then_recovers(tmp_path):
    src = make_source(f"folder:{tmp_path / 'later'}")
    assert src.read() is None and not src.exhausted
    (tmp_path / "later").mkdir()
    assert src.read() is None
    write_img(tmp_path / "later" / "x.png", 9)
    assert values(src, 1) == [9]


def test_folder_unreadable_file_returns_none_and_moves_on(tmp_path):
    (tmp_path / "1.jpg").write_bytes(b"broken")
    write_img(tmp_path / "2.png", 20)
    assert values(make_source(f"folder:{tmp_path}"), 3) == [None, 20, None]


@pytest.mark.parametrize(
    "uri,msg",
    [
        ("nothing", "expected '<scheme>:<target>'"),
        ("file:", "expected '<scheme>:<target>'"),
        ("folder:x?interval=0", "interval must be > 0"),
        ("folder:x?interval=abc", "could not convert"),
        ("folder:x?loop=maybe", "not a boolean"),
        ("folder:x?speed=2", "unknown option(s) speed"),
    ],
)
def test_bad_uris(uri, msg):
    with pytest.raises(ValueError, match=msg.replace("(", r"\(").replace(")", r"\)")):
        make_source(uri)


def test_unknown_scheme_does_not_echo_credentials():
    with pytest.raises(ValueError, match="unknown source scheme 'http'") as e:
        make_source("http://user:secret@10.0.0.1/snap.jpg")
    assert "secret" not in str(e.value)


def test_video_rejects_urls_without_echoing_them():
    with pytest.raises(ValueError, match="use 'rtsp:'") as e:
        make_source("video:rtsp://user:secret@cam/x")
    assert "secret" not in str(e.value)


@pytest.mark.parametrize(
    ("uri", "msg"),
    [
        ("snapshot:rtsp://user:secret@cam/x", "expected an http"),
        ("rtsp:http://user:secret@cam/x", "expected an rtsp"),
    ],
)
def test_camera_sources_check_the_url_scheme(uri, msg):
    with pytest.raises(ValueError, match=msg) as e:
        make_source(uri)
    assert "secret" not in str(e.value)

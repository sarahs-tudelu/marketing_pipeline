"""Regressions for overlapping media work and file-sized memory copies."""

import hashlib
import io
import threading
import tracemalloc
from types import SimpleNamespace

import numpy as np
import pytest

from social.drive_import import DriveClient
from social.real_edit import video_frames, wav
from social.renditions import audio_samples
from social.runway import Runway
from social.storage import source_files
from social.video_rules import has_sound
from tests.test_real_edit import footage


class ChunkResponse:
    status_code = 200
    is_redirect = False

    def __init__(self, size):
        self.size = size
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def raise_for_status(self):
        pass

    def iter_bytes(self, chunk_size):
        for start in range(0, self.size, chunk_size):
            # Fresh chunks, as a real socket supplies, not references to one shared object.
            yield b"x" * min(chunk_size, self.size - start)

    iter_content = iter_bytes


@pytest.mark.parametrize("provider", ["drive", "runway"])
def test_large_download_does_not_keep_two_file_sized_copies(settings, monkeypatch, provider):
    size = 48 * 1024 * 1024
    response = ChunkResponse(size)
    checksum = hashlib.md5(usedforsecurity=False)
    for chunk in response.iter_bytes(65536):
        checksum.update(chunk)
    if provider == "drive":
        client = DriveClient(settings, SimpleNamespace(get=lambda *a, **k: response))
        file = {"id": "large-file", "name": "large.mp4", "size": str(size), "md5Checksum": checksum.hexdigest()}
        def download():
            return client.download(file, size)
    else:
        monkeypatch.setattr("social.runway.public_url", lambda url: None)
        client = Runway(settings, SimpleNamespace(stream=lambda *a, **k: response))
        def download():
            return client.download("https://output.example/large.mp4")
    tracemalloc.start()
    try:
        data = download()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(data) == size and hashlib.md5(data, usedforsecurity=False).hexdigest() == checksum.hexdigest()
    assert response.closed
    # Includes buffer growth/headroom; the old bytearray/list + bytes/join path needs >2x.
    assert peak < size * 1.6, (provider, peak, size)


@pytest.mark.parametrize("failure", [None, "checksum", "size", "render"])
def test_bucket_sources_stream_to_disk_and_cleanup_on_success_or_failure(settings, monkeypatch, failure):
    from social import storage

    data = b"source bytes" * 100000
    bodies, requested = [], []
    closed = []

    class Body(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            requested.append(size)
            return super().read(size)

    def open_body(key):
        body = Body(data)
        bodies.append(body)
        return {"Body": body}

    store = SimpleNamespace(open=open_body, client=SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(storage, "storage", lambda _: store)
    checksum = "bad" if failure == "checksum" else hashlib.sha256(data).hexdigest()
    asset = SimpleNamespace(id="a", storage_key="original.mp4", checksum=checksum)
    if failure == "size":
        settings.max_upload_bytes = len(data) - 1

    def render():
        with source_files(settings, [asset, asset]) as files:
            assert files["a"].is_relative_to(settings.media_dir)
            assert files["a"].read_bytes() == data
            assert len(bodies) == 1  # Repeated clips share a staged original.
            if failure == "render":
                raise ValueError("render failed")

    if failure:
        with pytest.raises(ValueError, match={
            "checksum": "integrity", "size": "MAX_UPLOAD_BYTES", "render": "render failed",
        }[failure]):
            render()
    else:
        render()
    assert requested and all(body.closed for body in bodies) and closed == [True]
    assert list((settings.media_dir / ".processing").iterdir()) == []


def test_local_sources_are_verified_without_loading_or_removing_the_original(settings, monkeypatch):
    from social.storage import LocalStore

    data = b"original bytes"
    store = LocalStore(settings)
    store.put("original.mp4", data, "video/mp4")
    asset = SimpleNamespace(id="a", storage_key="original.mp4", checksum=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(LocalStore, "read", lambda *args: pytest.fail("Must not buffer the original"))
    with source_files(settings, [asset]) as files:
        assert files["a"] == store.path(asset.storage_key)
        assert files["a"].read_bytes() == data
    assert store.path(asset.storage_key).read_bytes() == data
    store.path(asset.storage_key).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="integrity"):
        with source_files(settings, [asset]):
            pytest.fail("Must not render altered source bytes")


def test_render_import_and_description_jobs_cannot_overlap(settings, monkeypatch):
    from social import availability, drive_import, media_search, usage, worker

    settings.worker_interval_seconds = 3600
    release, entered, all_done, light_done = (threading.Event() for _ in range(4))
    lock = threading.Lock()
    running, peak, finished = 0, 0, 0
    failures, started, light = [], [], set()

    def media_job(name):
        def run(*args):
            nonlocal running, peak, finished
            with lock:
                started.append(name)
                running += 1
                peak = max(peak, running)
                entered.set()
            if not release.wait(10):
                failures.append("release timed out")
            with lock:
                running -= 1
                finished += 1
                if finished == 3:
                    all_done.set()
        return run

    def light_job(name):
        def run(*args):
            with lock:
                light.add(name)
                if len(light) == 2:
                    light_done.set()
        return run

    monkeypatch.setattr(worker, "tick", media_job("render"))
    monkeypatch.setattr(drive_import, "tick", media_job("import"))
    monkeypatch.setattr(media_search, "describe_library", media_job("describe"))
    monkeypatch.setattr(availability, "tick", light_job("availability"))
    monkeypatch.setattr(usage, "tick", light_job("usage"))
    scheduler = worker.make_scheduler(None, settings)
    thread = threading.Thread(target=scheduler.start, daemon=True)
    thread.start()
    try:
        assert entered.wait(5) and light_done.wait(5)
        assert len(started) == 1 and peak == 1
        # Queued jobs must still run after APScheduler's normal one-second misfire grace.
        assert not all_done.wait(1.1)
        release.set()
        assert all_done.wait(5)
        assert set(started) == {"render", "import", "describe"} and peak == 1 and not failures
    finally:
        release.set()
        scheduler.shutdown(wait=True)
        thread.join(timeout=5)


def test_planner_retains_only_small_previews_of_large_video():
    source = footage(seconds=1, size=(1920, 1080), sound=False)
    previews = video_frames(source, [0, 0.4, 0.8])
    assert len(previews) == 3
    assert all(p.size == (768, 432) for p in previews)
    assert previews[0].tobytes() != previews[-1].tobytes()  # Still covers different times.


def test_late_audio_interval_discards_prefix_without_changing_samples(tmp_path):
    rate = 48000
    values = np.sin(np.arange(12 * rate, dtype=np.float32) * (2 * np.pi * 440 / rate)) * 0.2
    data = wav(np.stack([values, values]))
    path = tmp_path / "source.wav"
    path.write_bytes(data)
    expected = audio_samples(data)[:, 9 * rate:10 * rate]
    tracemalloc.start()
    try:
        actual = audio_samples(path, 9, 1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    np.testing.assert_array_equal(actual, expected)
    # A late one-second interval must not retain the preceding nine seconds as arrays.
    assert peak < expected.nbytes * 4
    assert audio_samples(path, 15, 1) is None
    assert has_sound(path)
    quiet = wav(np.zeros((2, rate), dtype=np.float32))
    assert not has_sound(quiet)

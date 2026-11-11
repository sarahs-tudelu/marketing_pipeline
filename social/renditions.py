"""Create separately tracked publishing renditions; never replace source media."""

import io
from fractions import Fraction

import av
import numpy as np
from PIL import Image, ImageOps


def image_rendition(data, width, height):
    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image = ImageOps.pad(image, (width, height), color="#f4f2ed")
        output = io.BytesIO()
        image.save(output, "JPEG", quality=92, optimize=True)
        return output.getvalue()


def audio_samples(data):
    with av.open(io.BytesIO(data)) as source:
        if not source.streams.audio:
            return None
        stream = source.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=48000)
        parts = []
        for frame in source.decode(stream):
            parts.extend(f.to_ndarray() for f in resampler.resample(frame))
        parts.extend(f.to_ndarray() for f in resampler.resample(None))
        return np.concatenate(parts, axis=1) if parts else None


def video_rendition(data, width, height, duration, soundtrack=None):
    """Fit, normalize H.264/AAC and optionally replace the soundtrack, padded to the clip."""
    output = io.BytesIO()
    samples = audio_samples(soundtrack if soundtrack is not None else data)
    with (
        av.open(io.BytesIO(data)) as source,
        av.open(output, "w", format="mp4", options={"movflags": "frag_keyframe+empty_moov"}) as target,
    ):
        video = target.add_stream("libx264", rate=30)
        video.width, video.height, video.pix_fmt = width, height, "yuv420p"
        video.options = {"crf": "21", "preset": "veryfast"}
        audio = target.add_stream("aac", rate=48000) if samples is not None else None
        if audio:
            audio.layout = "stereo"
        next_index = 0
        original = source.streams.video[0]
        fps = float(original.average_rate or 30)
        max_frames = round(duration * 30)
        for index, frame in enumerate(source.decode(original)):
            # Fill each source frame's time interval at a fixed publishing frame rate.
            end = min(max_frames, round((index + 1) * 30 / fps))
            if end <= next_index:
                continue
            fitted = ImageOps.pad(frame.to_image(), (width, height), color="black")
            while next_index < end:
                encoded_frame = av.VideoFrame.from_image(fitted)
                encoded_frame.pts, encoded_frame.time_base = next_index, Fraction(1, 30)
                for packet in video.encode(encoded_frame):
                    target.mux(packet)
                next_index += 1
            if next_index >= max_frames:
                break
        if not next_index:
            raise ValueError("Generated video contains no frames")
        for packet in video.encode():
            target.mux(packet)
        if audio:
            total = next_index * 1600
            for start in range(0, total, 1024):
                length = min(1024, total - start)
                chunk = np.zeros((2, length), dtype=np.float32)
                available = max(0, min(length, samples.shape[1] - start))
                chunk[:, :available] = samples[:, start : start + available]
                frame = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="stereo")
                frame.sample_rate, frame.pts, frame.time_base = 48000, start, Fraction(1, 48000)
                for packet in audio.encode(frame):
                    target.mux(packet)
            for packet in audio.encode():
                target.mux(packet)
    return output.getvalue()

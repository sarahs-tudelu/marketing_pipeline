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


def audio_samples(data, start=0, duration=None):
    with av.open(io.BytesIO(data)) as source:
        if not source.streams.audio:
            return None
        stream = source.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=48000)
        parts = []
        count = 0
        for frame in source.decode(stream):
            converted = [f.to_ndarray() for f in resampler.resample(frame)]
            parts.extend(converted)
            count += sum(p.shape[1] for p in converted)
            if duration is not None and count >= round((start + duration) * 48000):
                break
        parts.extend(f.to_ndarray() for f in resampler.resample(None))
        if not parts:
            return None
        samples = np.concatenate(parts, axis=1)
        return samples[:, round(start * 48000): None if duration is None else round((start + duration) * 48000)]


def video_rendition(data, width, height, duration, soundtrack=None, *, start=0):
    """Fit a source interval without altering the original."""
    return video_sequence([(data, start, duration)], width, height, soundtrack)


def video_sequence(clips, width, height, soundtrack=None, *, require_full_duration=False):
    """Join ordered source intervals, retaining their audio timing or replacing the soundtrack."""
    output = io.BytesIO()
    audio_parts = []
    with av.open(output, "w", format="mp4") as target:
        video = target.add_stream("libx264", rate=30)
        video.width, video.height, video.pix_fmt = width, height, "yuv420p"
        video.gop_size = 60  # Two-second keyframe spacing also fits Facebook Reels' 2-5 second GOP limit.
        video.options = {"crf": "21", "preset": "veryfast"}
        next_index = 0
        # Add the audio stream before writing the MP4 header; absent clips get timed silence.
        has_audio = soundtrack is not None
        if not has_audio:
            for data, _, _ in clips:
                with av.open(io.BytesIO(data)) as source:
                    has_audio = has_audio or bool(source.streams.audio)
        audio = target.add_stream("aac", rate=48000) if has_audio else None
        if audio:
            audio.layout = "stereo"
        for data, start, duration in clips:
            if start < 0 or not 0 < duration <= 90 or next_index + round(duration * 30) > 2700:
                raise ValueError("Video intervals must fit the 90-second finished-video limit")
            clip_start = next_index
            max_frames = round(duration * 30)
            with av.open(io.BytesIO(data)) as source:
                original = source.streams.video[0]
                fps = float(original.average_rate or 30)
                origin = None
                for index, frame in enumerate(source.decode(original)):
                    stamp = float(frame.time) if frame.time is not None else index / fps
                    if origin is None:
                        origin = stamp
                    end = clip_start + min(max_frames, round((stamp - origin + 1 / fps - start) * 30))
                    if end <= next_index:
                        continue
                    fitted = ImageOps.pad(frame.to_image(), (width, height), color="black")
                    while next_index < end:
                        encoded_frame = av.VideoFrame.from_image(fitted)
                        encoded_frame.pts, encoded_frame.time_base = next_index, Fraction(1, 30)
                        for packet in video.encode(encoded_frame):
                            target.mux(packet)
                        next_index += 1
                    if next_index - clip_start >= max_frames:
                        break
            length = next_index - clip_start
            if not length or (require_full_duration and length < max_frames - 1):
                raise ValueError("A video segment is shorter than its planned duration; inspect the saved output")
            if audio and soundtrack is None:
                samples = audio_samples(data, start, length / 30)
                chunk = np.zeros((2, length * 1600), dtype=np.float32)
                if samples is not None:
                    available = min(chunk.shape[1], samples.shape[1])
                    chunk[:, :available] = samples[:, :available]
                audio_parts.append(chunk)
        if not next_index:
            raise ValueError("Generated video contains no frames")
        for packet in video.encode():
            target.mux(packet)
        if audio:
            samples = audio_samples(soundtrack, duration=next_index / 30) if soundtrack is not None else np.concatenate(audio_parts, axis=1)
            if samples is None:
                raise ValueError("The replacement soundtrack contains no decodable audio")
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

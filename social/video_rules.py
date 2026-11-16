"""House rules for every video: it has sound, and it ends with a cut to the Tudelu logo.

Generated videos get Runway music. When nobody asked for any, a calm ambient direction is taken from
AMBIENT_STYLES, choosing the style used least recently so posts don't all sound the same. Real videos
(posted as they are or from the library) keep their own sound. A silent one gets an ambient track
from the asset library (real music tagged MUSIC_TAG, least recently used first), so real media never
carries AI-made sound and isn't AI-labelled. Only when the library has no such track does the app
synthesize simple ambient music itself. The logo end card is the uploaded logo, unaltered.
"""

import hashlib
import io
import wave
from datetime import datetime

import numpy as np
from sqlalchemy import select

from .db import now
from .models import Asset, MediaJob

# Library audio tagged with this is real ambient music for real videos.
MUSIC_TAG = "background_music"
# key: (label, direction for Runway's music model)
AMBIENT_STYLES = {
    "warm_pads": (
        "warm pads",
        "Calm ambient instrumental: warm, slowly evolving synth pads over a soft low drone, spacious and "
        "unhurried. No drums, no vocals.",
    ),
    "felt_piano": (
        "felt piano",
        "Calm ambient instrumental: a gentle felt piano melody over soft pads, airy and reflective. "
        "No drums, no vocals.",
    ),
    "glass_bells": (
        "glass bells",
        "Bright ambient instrumental: shimmering glass bells over soft pads, light and open. No drums, no vocals.",
    ),
    "soft_keys": (
        "soft keys",
        "Relaxed ambient instrumental: mellow electric piano chords over warm pads, unhurried. "
        "No drums, no vocals.",
    ),
    "marimba": (
        "marimba",
        "Light ambient instrumental: soft marimba patterns over warm pads, gentle and optimistic. "
        "No drums, no vocals.",
    ),
    "nature": (
        "nature",
        "Peaceful ambient instrumental: soft pads with light birdsong and a gentle breeze, calm and natural. "
        "No vocals.",
    ),
}
SYNTHESIZER = "tudelu-ambient-v1"
NO_LOGO = (
    "Every video ends with the Tudelu logo, and there's no logo in the asset library yet. Upload the logo "
    "as a PNG on Upload media and tick This is the Tudelu logo, then try again."
)
# Below about -54 dBFS RMS a soundtrack is treated as silent.
SILENCE_RMS = 0.002
RATE = 24000


def default_logo(db, *, live=False):
    """The logo every video ends on: the newest asset tagged as the Tudelu logo, if any."""
    from .idea_plan import logo_assets

    found = logo_assets(db.scalars(select(Asset).order_by(Asset.created_at.desc())).all(), live=live)
    return found[0] if found else None


def next_ambient_style(db):
    """The ambient style used least recently by generated music or app-made tracks."""

    def stamp(row):
        return (row.created_at or datetime.min).replace(tzinfo=None)

    last = {}
    jobs = db.scalars(select(MediaJob).order_by(MediaJob.created_at.desc()).limit(40)).all()
    assets = db.scalars(select(Asset).order_by(Asset.created_at.desc()).limit(80)).all()
    uses = [(stamp(j), (j.brief or {}).get("ambient_style")) for j in jobs]
    for a in assets:
        music = (a.tags or {}).get("ambient_music")
        uses.append((stamp(a), music.get("style") if isinstance(music, dict) else None))
    for at, style in sorted((u for u in uses if u[1] in AMBIENT_STYLES), key=lambda u: u[0]):
        last[style] = at
    return min(AMBIENT_STYLES, key=lambda s: (s in last, last.get(s, datetime.min)))


def has_sound(data):
    """True when a video or audio file has audible sound, not just a silent track."""
    from .renditions import audio_samples

    samples = audio_samples(data)
    return samples is not None and samples.size > 0 and float(np.sqrt(np.mean(np.square(samples)))) >= SILENCE_RMS


def ambient_tag(style, seconds):
    return {
        "style": style,
        "label": AMBIENT_STYLES[style][0],
        "seconds": round(seconds, 2),
        "made_by": f"{SYNTHESIZER}: synthesized by the app, not AI",
    }


def track_tag(track, seconds):
    return {
        "asset_id": track.id,
        "name": track.original_name,
        "checksum": track.checksum,
        "seconds": round(seconds, 2),
        "made_by": "Ambient track from the asset library (company audio, not AI)",
    }


def background_music_error(mime, source, generator, prompt, tags):
    """Why a file can't be ambient music for real videos, or "" when it can (or isn't marked as such)."""
    if not (tags or {}).get(MUSIC_TAG):
        return ""
    if not (mime or "").startswith("audio/"):
        return "Only audio files can be ambient music for real videos."
    if source != "company" or (generator or "").strip() or (prompt or "").strip():
        return (
            "AI-made music can't be ambient music for real videos: it would get them labelled as AI. "
            "Use music you own or have licensed, with Source: company media."
        )
    return ""


def music_tracks(db, *, live=False, public=True):
    """Real ambient tracks from the library that a video may use, least recently used first."""
    from .provenance import requires_ai_review

    at = now()
    tracks = [
        a
        for a in db.scalars(
            select(Asset)
            .where(Asset.source == "company", Asset.tags[MUSIC_TAG].as_boolean().is_(True))
            .order_by(Asset.created_at)
        ).all()
        if a.media.get("mime", "").startswith("audio/")
        and not requires_ai_review(a)
        and not (a.rights_expire_at and a.rights_expire_at <= at)
        and not (live and a.demo_only)
        and not (public and a.rights == "not_public")
    ]
    last = {}
    for a in reversed(db.scalars(select(Asset).order_by(Asset.created_at.desc()).limit(200)).all()):
        used = (a.tags or {}).get("ambient_music")
        if isinstance(used, dict) and used.get("asset_id"):
            last[used["asset_id"]] = (a.created_at or datetime.min).replace(tzinfo=None)
    return sorted(tracks, key=lambda t: (t.id in last, last.get(t.id, datetime.min)))


def fit_track(data, seconds, *, crossfade=2.0, fade_out=1.5):
    """A stereo WAV of a music track fitted to `seconds`: looped with a crossfade when short, faded out."""
    from .renditions import audio_samples

    rate = 48000
    source = audio_samples(data, duration=seconds)
    if source is None or not source.shape[1]:
        raise ValueError("The ambient track has no sound that can be decoded; replace it in the asset library")
    need = max(1, round(seconds * rate))
    track = source
    overlap = min(round(crossfade * rate), source.shape[1] // 3)
    while track.shape[1] < need:
        if overlap:
            ramp = np.linspace(0, 1, overlap, dtype=np.float32)
            joined = track[:, -overlap:] * (1 - ramp) + source[:, :overlap] * ramp
            track = np.concatenate([track[:, :-overlap], joined, source[:, overlap:]], axis=1)
        else:
            track = np.concatenate([track, source], axis=1)
    track = track[:, :need].astype(np.float64)
    fade = min(round(fade_out * rate), need // 3)
    if fade:
        track[:, need - fade :] *= np.linspace(1, 0, fade)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as file:
        file.setparams((2, 2, rate, need, "NONE", "not compressed"))
        file.writeframes((np.clip(track, -1, 1) * 32767).astype("<i2").T.tobytes())
    return stream.getvalue()


def video_rule_errors(assets):
    """Why the post's video breaks the house rules, or [] when it has sound and ends on the logo."""
    reasons = []
    for a in assets:
        if not a.media["mime"].startswith("video/"):
            continue
        if not a.media.get("audio_codecs"):
            reasons.append("The video has no sound.")
        if not (a.tags or {}).get("end_card"):
            reasons.append("The video doesn't end with the Tudelu logo.")
    return reasons


def seed_for(*parts):
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:8], 16)


# --- The app's own ambient tracks: soft chord pads with a style-specific layer, no samples or AI. ---

_MAJOR = (0, 2, 4, 5, 7, 9, 11)
_PROGRESSIONS = ((0, 4, 5, 3), (0, 3, 5, 4), (5, 3, 0, 4), (0, 5, 3, 4), (3, 4, 0, 5), (0, 2, 3, 4))
_KEYS = (48, 50, 51, 53, 55, 45, 46)  # Chord roots around C3.
_PIANO = [(h * (1 + 0.0004 * h * h) ** 0.5, 0.9 / h**1.4, 1.8 / h**0.8) for h in range(1, 6)]
_BELL = [(1, 1.0, 2.6), (2.0, 0.3, 1.7), (2.76, 0.45, 1.3), (5.40, 0.2, 0.7), (8.93, 0.1, 0.4)]
_MARIMBA = [(1, 1.0, 0.65), (3.93, 0.28, 0.16), (9.24, 0.07, 0.05)]


def _hz(note):
    return 440.0 * 2 ** ((note - 69) / 12)


def _note(key, degree, octave=0):
    up, index = divmod(degree, 7)
    return key + 12 * (up + octave) + _MAJOR[index]


def _envelope(n, attack, release):
    env = np.ones(n)
    a = min(int(attack * RATE), n // 2)
    r = min(int(release * RATE), n - a)
    if a:
        env[:a] = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, a))
    if r:
        env[n - r :] *= 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, r))
    return env


def _pad(out, start, seconds, freqs, rng, *, gain, harmonics=2, attack=1.4, release=1.8):
    begin = int(start * RATE)
    n = min(int(seconds * RATE), out.shape[1] - begin)
    if n <= 0:
        return
    t = np.arange(n) / RATE
    env = _envelope(n, attack, release) * gain
    for f in freqs:
        # A slightly detuned voice per side makes the pad wide and soft.
        for side, detune in ((0, -0.0025), (1, 0.0025)):
            phase = rng.uniform(0, 2 * np.pi)
            voice = sum(np.sin(2 * np.pi * f * (1 + detune) * h * t + phase * h) / h**1.6 for h in range(1, harmonics + 1))
            out[side, begin : begin + n] += env * voice


def _strike(out, start, f, partials, gain, pan, *, attack=0.006):
    begin = int(start * RATE)
    n = min(int(max(d for _, _, d in partials) * 4 * RATE), out.shape[1] - begin)
    if n <= 0:
        return
    t = np.arange(n) / RATE
    tone = np.zeros(n)
    for ratio, amp, decay in partials:
        if f * ratio < RATE * 0.45:
            tone += amp * np.sin(2 * np.pi * f * ratio * t) * np.exp(-t / decay)
    edge = max(1, min(n, int(attack * RATE)))
    tone[:edge] *= np.linspace(0, 1, edge)
    tail = min(n, int(0.03 * RATE))
    tone[n - tail :] *= np.linspace(1, 0, tail)
    out[0, begin : begin + n] += gain * np.cos(pan * np.pi / 2) * tone
    out[1, begin : begin + n] += gain * np.sin(pan * np.pi / 2) * tone


def _electric_piano(out, start, f, gain, pan):
    begin = int(start * RATE)
    n = min(int(3.0 * RATE), out.shape[1] - begin)
    if n <= 0:
        return
    t = np.arange(n) / RATE
    tone = np.sin(2 * np.pi * f * t + 1.4 * np.exp(-t / 0.35) * np.sin(2 * np.pi * f * t)) * np.exp(-t / 1.1)
    edge, tail = int(0.004 * RATE), min(n, int(0.05 * RATE))
    tone[:edge] *= np.linspace(0, 1, edge)
    tone[n - tail :] *= np.linspace(1, 0, tail)
    out[0, begin : begin + n] += gain * np.cos(pan * np.pi / 2) * tone
    out[1, begin : begin + n] += gain * np.sin(pan * np.pi / 2) * tone


def _breeze(out, rng, gain):
    n = out.shape[1]
    kernel = np.hanning(64)
    kernel /= kernel.sum()
    noise = np.array([np.convolve(c, kernel, mode="same") for c in rng.standard_normal((2, n))])
    noise /= max(float(noise.std()), 1e-9)
    t = np.arange(n) / RATE
    swell = 0.55 + 0.45 * np.sin(2 * np.pi * 0.07 * t + rng.uniform(0, 6)) * np.sin(2 * np.pi * 0.031 * t + rng.uniform(0, 6))
    out += gain * noise * swell


def _birds(out, start, rng, gain):
    pan = rng.uniform(0.2, 0.8)
    at = start
    for _ in range(int(rng.integers(2, 5))):
        seconds = rng.uniform(0.06, 0.13)
        begin, n = int(at * RATE), int(seconds * RATE)
        if begin + n > out.shape[1]:
            return
        t = np.arange(n) / RATE
        low, high = rng.uniform(2600, 3600), rng.uniform(3600, 5000)
        sweep = 2 * np.pi * np.cumsum(low + (high - low) * t / seconds) / RATE
        chirp = np.sin(sweep + 0.6 * np.sin(2 * np.pi * rng.uniform(25, 45) * t)) * np.sin(np.pi * t / seconds) ** 2
        out[0, begin : begin + n] += gain * np.cos(pan * np.pi / 2) * chirp
        out[1, begin : begin + n] += gain * np.sin(pan * np.pi / 2) * chirp
        at += seconds + rng.uniform(0.05, 0.12)


def _reverb(dry, rng, seconds=2.4, mix=0.3):
    """Convolution with decaying noise, block by block to keep memory small."""
    size_ir = int(seconds * RATE)
    t = np.arange(size_ir) / RATE
    ir = rng.standard_normal((2, size_ir)) * np.exp(-t * 6.9 / seconds)
    kernel = np.hanning(9)
    ir = np.array([np.convolve(c, kernel / kernel.sum(), mode="same") for c in ir])
    ir[:, : int(0.015 * RATE)] = 0
    ir /= np.sqrt(np.square(ir).sum(axis=1, keepdims=True))
    n, block = dry.shape[1], 1 << 15
    size = 1 << int(np.ceil(np.log2(block + size_ir - 1)))
    response = np.fft.rfft(ir, size, axis=1)
    wet = np.zeros((2, n + size))
    for begin in range(0, n, block):
        part = np.fft.irfft(np.fft.rfft(dry[:, begin : begin + block], size, axis=1) * response, size, axis=1)
        wet[:, begin : begin + size] += part
    return (1 - mix) * dry + mix * wet[:, :n]


def ambient_track(style, seconds, seed=0):
    """A stereo WAV of calm ambient music in the given style, exactly `seconds` long."""
    if style not in AMBIENT_STYLES:
        raise ValueError(f"Unknown ambient style {style}")
    rng = np.random.default_rng(seed)
    n = max(1, int(round(seconds * RATE)))
    out = np.zeros((2, n))
    key = _KEYS[int(rng.integers(len(_KEYS)))]
    progression = _PROGRESSIONS[int(rng.integers(len(_PROGRESSIONS)))]
    chord_seconds = float(rng.uniform(4.0, 5.5))
    pad_gain = 0.1 if style in {"warm_pads", "nature"} else 0.055
    for index in range(int(np.ceil(seconds / chord_seconds)) + 1):
        start = index * chord_seconds
        degree = progression[index % len(progression)]
        chord = [_note(key, degree + step) for step in (0, 2, 4, 8)]
        freqs = [_hz(m) for m in chord] + [_hz(chord[0] - 12)]
        _pad(out, start, chord_seconds + 1.6, freqs, rng, gain=pad_gain, harmonics=3 if style == "warm_pads" else 2)
        if style == "warm_pads":
            _pad(out, start, chord_seconds + 1.6, [_hz(chord[1] + 12), _hz(chord[3] + 12)], rng, gain=0.025, harmonics=1, attack=2.5)
        elif style == "felt_piano":
            notes = [_note(key, degree + step, 1) for step in (0, 4, 2, 7, 4, 9)]
            for k, note in enumerate(notes[: int(rng.integers(3, 7))]):
                at = start + k * chord_seconds / 6 + rng.uniform(0, 0.08)
                _strike(out, at, _hz(note), _PIANO, rng.uniform(0.11, 0.16), rng.uniform(0.3, 0.7), attack=0.008)
        elif style == "glass_bells":
            for k in range(int(rng.integers(1, 4))):
                note = _note(key, degree + int(rng.choice([0, 2, 4, 7, 9])), 2)
                at = start + rng.uniform(0, chord_seconds * 0.85)
                _strike(out, at, _hz(note), _BELL, rng.uniform(0.05, 0.08), rng.uniform(0.15, 0.85), attack=0.002)
        elif style == "soft_keys":
            for k, note in enumerate(chord):
                _electric_piano(out, start + 0.03 * k, _hz(note + 12), 0.07, 0.35 + 0.1 * k)
            for k in range(int(rng.integers(1, 3))):
                note = _note(key, degree + int(rng.choice([2, 4, 7])), 2)
                _electric_piano(out, start + chord_seconds * (0.5 + 0.2 * k), _hz(note), 0.06, rng.uniform(0.3, 0.7))
        elif style == "marimba":
            steps = (0, 2, 4, 7, 4, 2, 4, 9)
            for k, step in enumerate(steps):
                if rng.uniform() < 0.25:
                    continue
                at = start + k * chord_seconds / len(steps)
                _strike(out, at, _hz(_note(key, degree + step, 1)), _MARIMBA, rng.uniform(0.1, 0.15), 0.3 + 0.4 * (k % 2), attack=0.002)
        elif style == "nature" and rng.uniform() < 0.7:
            _birds(out, start + rng.uniform(0.5, chord_seconds - 0.5), rng, 0.025)
    if style == "nature":
        _breeze(out, rng, 0.03)
    out = _reverb(out, rng)
    out -= out.mean(axis=1, keepdims=True)
    out *= _envelope(n, min(0.8, seconds / 4), min(2.0, seconds / 3))
    rms = float(np.sqrt(np.mean(np.square(out))))
    out = np.tanh(out * (0.14 / max(rms, 1e-9)) * 1.2) / 1.2
    stream = io.BytesIO()
    with wave.open(stream, "wb") as file:
        file.setparams((2, 2, RATE, n, "NONE", "not compressed"))
        file.writeframes((np.clip(out, -1, 1) * 32767).astype("<i2").T.tobytes())
    return stream.getvalue()

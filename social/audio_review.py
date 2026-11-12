"""Full-track audio observations, cached by bytes; fresh factual review at every gate."""

import hashlib
import io
import wave

import av
from pydantic import ValidationError

from .db import now
from .facts import active
from .provider_errors import ProviderError
from .providers import AudioObservation
from .storage import storage

SCHEMA_VERSION = 1
RATE = 24000
MAX_SECONDS = 90.1  # Allow a final codec padding frame at the 90-second video boundary.
MAX_TRACKS = 4


def extract_tracks(data):
    """Decode every audio stream to PCM WAV, preserving both stereo channels."""
    with av.open(io.BytesIO(data)) as source:
        count = len(source.streams.audio)
    if count > MAX_TRACKS:
        raise ValueError("Audio review supports up to four tracks; export a final mixed soundtrack")
    tracks = []
    for index in range(count):
        with av.open(io.BytesIO(data)) as source:
            stream = source.streams.audio[index]
            channels = len(stream.codec_context.layout.channels)
            if channels not in (1, 2):
                raise ValueError("Audio review requires mono or stereo; export a final soundtrack")
            resampler = av.AudioResampler(format="s16", layout="mono" if channels == 1 else "stereo", rate=RATE)
            output, samples = io.BytesIO(), 0
            with wave.open(output, "wb") as wav:
                wav.setparams((channels, 2, RATE, 0, "NONE", "not compressed"))

                def write(frame):
                    nonlocal samples
                    samples += frame.samples
                    if samples / RATE > MAX_SECONDS:
                        raise ValueError("Audio exceeds the 90-second review limit; export a shorter soundtrack")
                    wav.writeframesraw(frame.to_ndarray().astype("<i2").tobytes())

                for frame in source.decode(stream):
                    for converted in resampler.resample(frame):
                        write(converted)
                for converted in resampler.resample(None):
                    write(converted)
            if not samples:
                raise ValueError("An audio stream could not be decoded; it cannot be treated as silence")
            data_wav = output.getvalue()
            tracks.append({
                "track_index": index, "channels": channels, "duration_seconds": samples / RATE,
                "start_seconds": float((stream.start_time or 0) * (stream.time_base or 0)),
                "wav_checksum": hashlib.sha256(data_wav).hexdigest(), "wav": data_wav,
            })
    return tracks


def observe_audio(db, settings, asset, providers):
    store = storage(settings)
    try:
        data = store.read(asset.storage_key)
    finally:
        if hasattr(store, "client"):
            store.client.close()
    if hashlib.sha256(data).hexdigest() != asset.checksum:
        raise ValueError("Media integrity failed before audio review; restore or replace the original file")
    tracks = [] if asset.media["mime"].startswith("image/") else extract_tracks(data)
    model = "fixture/audio-observation-v1" if settings.ai_mode == "fixture" else settings.openai_audio_review_model
    saved = asset.media.get("audio_analysis", {})
    cache_ok = isinstance(saved, dict) and all((
        saved.get("schema_version") == SCHEMA_VERSION,
        saved.get("asset_checksum") == asset.checksum,
        saved.get("model") == model,
        saved.get("simulated") == (settings.ai_mode == "fixture"),
    ))
    previous = saved.get("tracks", []) if cache_ok else []
    result = {
        "schema_version": SCHEMA_VERSION, "asset_checksum": asset.checksum, "model": model,
        "simulated": settings.ai_mode == "fixture", "created_at": now().isoformat(),
        "coverage": "all_audio_streams", "track_count": len(tracks), "tracks": [],
        "status": "no_audio" if not tracks else "incomplete",
        "limitations": ["Machine listening can miss sounds or words; it does not verify music licensing."],
    }
    for track in tracks:
        cached = next((t for t in previous if isinstance(t, dict)
                       and t.get("track_index") == track["track_index"]
                       and t.get("wav_checksum") == track["wav_checksum"]), None)
        observation = None
        if cached:
            try:
                observation = AudioObservation.model_validate(cached.get("observation"))
            except ValidationError:
                pass
        if observation is None:
            observation = providers.listen(track["wav"])
            cached = None
        result["tracks"].append({k: v for k, v in track.items() if k != "wav"} | {
            "observation": observation.model_dump(),
            "observed_at": cached.get("observed_at", now().isoformat()) if cached else now().isoformat(),
        })
        # A later track/provider failure can resume already-observed tracks after the gate commits.
        asset.media = asset.media | {"audio_analysis": result | {"tracks": list(result["tracks"])}}
        db.flush()
    result["status"] = "observed" if tracks else "no_audio"
    asset.media = asset.media | {"audio_analysis": result}
    db.flush()
    return result


def review_audio(db, settings, assets, context, facts, at, providers):
    records, errors = [], []
    for asset in assets:
        try:
            records.append({"asset_id": asset.id, **observe_audio(db, settings, asset, providers)})
        except Exception as exc:
            # Never expose raw decoder, storage, or upstream response bodies in the UI.
            reason = str(exc) if isinstance(exc, ProviderError) else (
                str(exc) if type(exc) is ValueError else "Audio could not be completely decoded or assessed; recheck after correcting the file/provider."
            )
            errors.append({"asset_id": asset.id, "reason": reason, "error_type": type(exc).__name__})
    details = {"assets": records, "errors": errors, "reviewed_at": at.isoformat(),
               "rights_basis": "Declared asset rights include the soundtrack; licensing is not inferred by listening."}
    if errors:
        return False, details
    parts = []
    uncertain = []
    for record in records:
        for track in record["tracks"]:
            observation = track["observation"]
            if observation["uncertain"]:
                uncertain.append({"asset_id": record["asset_id"], "track_index": track["track_index"]})
            for segment in observation["segments"]:
                parts.append({"part_index": len(parts), "asset_id": record["asset_id"],
                              "track_index": track["track_index"], **segment})
    details.update(audio_parts=parts, uncertain_tracks=uncertain)
    if uncertain:
        return False, details | {"reason": "Some audio is unclear or incompletely understood; clarify or replace the soundtrack."}
    if not any(r["track_count"] for r in records):
        return True, details | {"reason": "No audio streams are present in the supplied media."}
    allowed_products = {"company", context["product"]}
    for asset in assets:
        family = asset.tags.get("product_family") or asset.tags.get("product")
        allowed_products.update(family if isinstance(family, list) else [family] if isinstance(family, str) else [])
    approved = [f for f in facts.facts if active(f, at) and f.product in allowed_products]
    approved_ids = {f.id for f in approved}
    review = providers.review_audio(context | {
        "audio_observations": records, "audio_parts": parts,
        "approved_audio_facts": [f.model_dump(mode="json") for f in approved],
    })
    complete = [c.part_index for c in review.part_checks] == list(range(len(parts))) and all(
        c.supported and c.reasons and set(c.fact_ids) <= approved_ids for c in review.part_checks
    )
    banned = [phrase for phrase in facts.banned_phrases
              if phrase.casefold() in "\n".join(p["text"] for p in parts).casefold()]
    return bool(complete and not banned and not review.unsupported_claims and review.score >= settings.review_min_score), (
        details | {"review": review.model_dump(), "review_model": settings.anthropic_model if settings.ai_mode == "live" else "fixture",
                   "complete_transcript_review": complete, "banned_phrases": banned}
    )

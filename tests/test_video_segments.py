import hashlib
import io
from fractions import Fraction

import av
import numpy as np
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from social.config import profiles
from social.facts import load_facts
from social.media_studio import CreativeBrief, process_jobs, queue_jobs, resume_job
from social.models import Asset, Post
from social.provider_errors import ProviderError
from social.renditions import audio_samples, video_sequence
from social.storage import ingest, storage
from social.video_segments import edit_reference, plan_segments


def clip(seconds, color, *, tone=False, tracks=1):
    output = io.BytesIO()
    with av.open(output, 'w', format='mp4') as container:
        video = container.add_stream('libx264', rate=30)
        video.width = video.height = 64
        video.pix_fmt = 'yuv420p'
        video.options = {'preset': 'ultrafast'}
        audios = [container.add_stream('aac', rate=48000) for _ in range(tracks)] if tone else []
        for audio in audios:
            audio.layout = 'stereo'
        for n in range(seconds * 30):
            pixels = np.zeros((64, 64, 3), dtype=np.uint8)
            pixels[:, :, color] = 200
            frame = av.VideoFrame.from_ndarray(pixels, format='rgb24')
            frame.pts, frame.time_base = n, Fraction(1, 30)
            for packet in video.encode(frame):
                container.mux(packet)
        for packet in video.encode():
            container.mux(packet)
        for audio in audios:
            for n in range(0, seconds * 48000, 1024):
                size = min(1024, seconds * 48000 - n)
                samples = .1 * np.sin(2 * np.pi * 440 * (np.arange(size) + n) / 48000)
                frame = av.AudioFrame.from_ndarray(np.tile(samples.astype(np.float32), (2, 1)), format='fltp', layout='stereo')
                frame.sample_rate, frame.pts, frame.time_base = 48000, n, Fraction(1, 48000)
                for packet in audio.encode(frame):
                    container.mux(packet)
            for packet in audio.encode():
                container.mux(packet)
    return output.getvalue()


def brief(db, settings, **overrides):
    return CreativeBrief(**(dict(campaign='Long video', prompt='A calm architectural sequence', platforms=['linkedin'],
                                facts_id=load_facts(db, settings.facts_path).id, rights='public', website=False,
                                operation='video', duration=90) | overrides))


def test_every_duration_has_complete_valid_intervals():
    for maximum in (10, 30):
        for duration in range(3, 91):
            plan = plan_segments(duration, maximum)
            assert sum(p['duration'] for p in plan) == duration
            assert all(2 <= p['duration'] <= maximum for p in plan)
            assert [p['start'] for p in plan] == [sum(s['duration'] for s in plan[:i]) for i in range(len(plan))]
    assert len(plan_segments(90, 10)) == 9
    assert len(plan_segments(90, 30)) == 3


def test_90_second_join_preserves_order_audio_and_exact_duration():
    sources = [clip(30, 0, tone=True), clip(30, 1), clip(30, 2, tone=True)]
    output = video_sequence([(s, 0, 30) for s in sources], 64, 64, require_full_duration=True)
    with av.open(io.BytesIO(output)) as media:
        assert media.duration / av.time_base == 90
        frames = list(media.decode(video=0))
        assert len(frames) == 2700 and frames[-1].time == pytest.approx(89 + 29/30)
        for position, channel in ((0, 0), (899, 0), (900, 1), (1799, 1), (1800, 2), (2699, 2)):
            assert frames[position].to_ndarray(format='rgb24').mean(axis=(0, 1)).argmax() == channel
    samples = audio_samples(output)
    assert np.abs(samples[:, 48000:48000*29]).mean() > .02
    assert np.abs(samples[:, 48000*31:48000*59]).max() < .001
    assert np.abs(samples[:, 48000*61:48000*89]).mean() > .02


def test_short_segment_cannot_be_stretched_to_fill_requested_length():
    with pytest.raises(ValueError, match='shorter than its planned'):
        video_sequence([(clip(2, 0), 0, 10)], 64, 64, require_full_duration=True)


def test_full_pipeline_joins_saved_clips_and_soundtrack_once(db, settings, monkeypatch):
    # Keep the real duration/frame path while avoiding HD encode cost in the test.
    p = profiles(settings)
    p['linkedin'] = p['linkedin'].model_copy(update={'video_width': 64, 'video_height': 64, 'min_width': 64, 'min_height': 64})
    monkeypatch.setattr('social.media_studio.profiles', lambda _: p)
    monkeypatch.setattr('social.seed.seed_media', lambda duration=4: (b'', clip(duration, 0)))
    job = queue_jobs(db, settings, brief(db, settings, audio_prompt='Ambient music'))[0]
    db.commit()
    for _ in range(12):
        process_jobs(db, settings)
    assert job.state == 'complete', job.error
    assert len(job.source_asset_ids) == 10
    assert len(job.brief['video_segments']) == 9
    assert all(s.get('prompt') and s.get('output_asset_id') for s in job.brief['video_segments'])
    assert len(set(s['prompt'] for s in job.brief['video_segments'])) == 9
    result = db.get(Asset, job.output_asset_id)
    assert result.media['duration'] == 90
    assert result.media['audio_codecs'] == ['aac']
    assert result.tags['generated_audio'] and len(result.tags['video_sequence']) == 9
    assert len(db.scalars(select(Post)).all()) == 1
    for segment in result.tags['video_sequence']:
        source = db.get(Asset, segment['output_asset_id'])
        assert source.media['duration'] <= 10
        assert hashlib.sha256(storage(settings).read(source.storage_key)).hexdigest() == source.checksum


def test_failure_on_later_clip_resumes_without_buying_completed_clips_again(db, settings, monkeypatch):
    monkeypatch.setattr('social.media_studio.write_prompt', lambda *a: {'generation_prompt': 'A pergola at dusk', 'prompt_writer': {}, 'depicted_system': 'Pergolas'})
    job = queue_jobs(db, settings, brief(db, settings, duration=12, create_posts=False))[0]
    job.brief = job.brief | {'execution_mode': 'live'}
    db.commit()
    settings.ai_mode, settings.runway_api_key = 'live', SecretStr('mock')

    class Runway:
        submissions = []
        failed = False

        def submit(self, endpoint, payload):
            self.submissions.append(payload)
            assert payload['duration'] == 6
            return f'task-{len(self.submissions)}'

        def task(self, task_id):
            return {'status': 'SUCCEEDED', 'output': [task_id]}

        def download(self, task_id):
            if task_id == 'task-2' and not self.failed:
                self.failed = True
                raise ProviderError('Temporary download failure', provider='Runway', category='download')
            return clip(6, 0)

    provider = Runway()
    for _ in range(4):
        process_jobs(db, settings, provider)
    assert job.state == 'failed' and job.task_id == 'task-2'
    saved = job.brief['video_segments'][0]['output_asset_id']
    resume_job(job)
    db.commit()
    process_jobs(db, settings, provider)
    assert job.state == 'rendering' and len(provider.submissions) == 2
    assert job.brief['video_segments'][0]['output_asset_id'] == saved
    assert len(job.source_asset_ids) == 2


def test_long_edit_splits_original_into_immutable_reusable_intervals(db, settings):
    data = video_sequence([(clip(30, c), 0, 30) for c in (0, 1, 2)], 64, 64)
    original = ingest(db, settings, data, 'original.mp4', rights='public')
    job = queue_jobs(db, settings, brief(db, settings, operation='video_edit', reference_ids=[original.id]))[0]
    db.commit()
    for index in range(3):
        part = edit_reference(db, settings, job)
        assert part.id != original.id and part.media['duration'] == 30
        assert edit_reference(db, settings, job).id == part.id
        with av.open(io.BytesIO(storage(settings).read(part.storage_key))) as media:
            frame = next(media.decode(video=0))
            assert frame.to_ndarray(format='rgb24').mean(axis=(0, 1)).argmax() == index
        assert part.tags['source_checksum'] == original.checksum
        assert part.tags['edit_interval'] == {'start': index * 30, 'duration': 30}
        job.brief = job.brief | {'video_segments': [s | {'output_asset_id': part.id} if s['index'] == index else s for s in job.brief['video_segments']]}
        db.commit()
    assert storage(settings).read(original.storage_key) == data


def test_invalid_lengths_rejected_before_paid_work(db, settings):
    with pytest.raises(ValidationError):
        brief(db, settings, duration=91)
    original = ingest(db, settings, clip(3, 0), 'short.mp4', rights='public')
    with pytest.raises(ValueError, match='longer than'):
        queue_jobs(db, settings, brief(db, settings, operation='video_edit', reference_ids=[original.id]))


def test_live_edit_submits_only_the_saved_30_second_input(db, settings, monkeypatch):
    monkeypatch.setattr('social.media_studio.write_prompt', lambda *a: {'generation_prompt': 'Warm lighting', 'prompt_writer': {}, 'depicted_system': 'Pergolas'})
    original = ingest(db, settings, clip(90, 0), 'long-source.mp4', rights='public')
    job = queue_jobs(db, settings, brief(db, settings, operation='video_edit', reference_ids=[original.id]))[0]
    job.brief = job.brief | {'execution_mode': 'live'}
    db.commit()
    settings.ai_mode, settings.runway_api_key = 'live', SecretStr('mock')

    class Runway:
        def reference(self, asset):
            assert asset.id != original.id and asset.media['duration'] == 30
            assert asset.id == job.brief['video_segments'][0]['input_asset_id']
            return 'runway://split-input'

        def submit(self, endpoint, payload):
            assert endpoint == 'video_to_video' and payload['videoUri'] == 'runway://split-input'
            assert 'duration' not in payload
            return 'edit-task'

    process_jobs(db, settings, Runway())
    assert job.state == 'running' and job.task_id == 'edit-task'


def test_configured_lower_platform_duration_is_enforced_before_generation(db, settings, monkeypatch):
    p = profiles(settings)
    p['linkedin'] = p['linkedin'].model_copy(update={'max_duration': 60})
    monkeypatch.setattr('social.media_studio.profiles', lambda _: p)
    with pytest.raises(ValueError, match='at most 60'):
        queue_jobs(db, settings, brief(db, settings))


def test_audio_reviews_every_stream_and_resumes_completed_track(db, settings):
    from social.audio_review import extract_tracks, observe_audio
    from social.providers import AudioObservation, Providers

    data = clip(2, 0, tone=True, tracks=2)
    assert len(extract_tracks(data)) == 2
    asset = ingest(db, settings, data, 'two-tracks.mp4', rights='public')
    provider, calls = Providers(settings), []

    def listen(wav):
        calls.append(wav)
        if len(calls) == 2:
            raise ValueError('temporary failure')
        return AudioObservation(summary='Instrumental tone', has_speech=False, has_lyrics=False, has_music=True,
                                uncertain=False, segments=[], limitations=[])

    provider.listen = listen
    with pytest.raises(ValueError, match='temporary failure'):
        observe_audio(db, settings, asset, provider)
    db.commit()
    assert asset.media['audio_analysis']['status'] == 'incomplete'
    result = observe_audio(db, settings, asset, provider)
    assert result['track_count'] == 2 and len(result['tracks']) == 2 and len(calls) == 3

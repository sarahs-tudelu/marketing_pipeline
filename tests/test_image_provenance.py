import hashlib
import io

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from social.adapters import ADAPTERS
from social.adapters.base import PublishError
from social.gate import check_disclosure, evaluate
from social.image_provenance import NS, TYPES, declaration_xmp, inspect_xmp, read_image_provenance
from social.providers import Providers
from social.renditions import image_rendition
from social.storage import ingest, storage


@pytest.mark.parametrize('source', ['ai_generated', 'ai_edited'])
def test_generated_rendition_embeds_standard_marker_without_changing_pixels(db, settings, assets, source):
    original = storage(settings).read(assets[0].storage_key)
    ordinary = image_rendition(original, 1080, 1080)
    marked = image_rendition(original, 1080, 1080, source=source, generator='runway/gen4_image')
    evidence = read_image_provenance(marked)
    assert evidence['digital_source_type'] == TYPES[source]
    assert evidence['ai_system'] == 'runway/gen4_image'
    assert evidence['creator_tool'] == 'Tudelu Social Studio'
    assert evidence['derived_from'] == 'urn:sha256:' + hashlib.sha256(original).hexdigest()
    assert evidence['signature_verified'] is False
    with Image.open(io.BytesIO(ordinary)) as before, Image.open(io.BytesIO(marked)) as after:
        assert before.tobytes() == after.tobytes()
        assert 'xmp' in after.info and b'DigitalSourceType' in after.info['xmp']
    assert storage(settings).read(assets[0].storage_key) == original
    a = ingest(db, settings, marked, 'delivered.jpg', source=source, generator='runway/gen4_image', prompt='Architecture', rights='public')
    assert a.media['embedded_provenance'] == evidence


def test_existing_png_ai_declaration_survives_jpeg_conversion():
    output = io.BytesIO()
    metadata = PngInfo()
    metadata.add_itxt('XML:com.adobe.xmp', declaration_xmp('ai_edited', 'Original generator', 'a'*64).decode())
    Image.new('RGB', (32, 32), 'red').save(output, 'PNG', pnginfo=metadata)
    result = image_rendition(output.getvalue(), 64, 64)
    assert read_image_provenance(result)['digital_source_type'] == TYPES['ai_edited']
    assert read_image_provenance(result)['ai_system'] == 'Original generator'


@pytest.mark.parametrize('form', ['attribute', 'text', 'resource'])
def test_recognizes_standard_xmp_property_forms(form):
    uri = TYPES['ai_generated']
    attr = f'e:DigitalSourceType="{uri}"' if form == 'attribute' else ''
    child = '' if form == 'attribute' else (f'<e:DigitalSourceType rdf:resource="{uri}"/>' if form == 'resource'
                                           else f'<e:DigitalSourceType>{uri}</e:DigitalSourceType>')
    xml = f'<rdf:RDF xmlns:rdf="{NS["rdf"]}" xmlns:e="{NS["Iptc4xmpExt"]}"><rdf:Description {attr}>{child}</rdf:Description></rdf:RDF>'
    assert inspect_xmp({'xmp': xml.encode()})['ai_declared']


@pytest.mark.parametrize('xml', [
    b'broken XML',
    b'<!DOCTYPE x [<!ENTITY word "value">]><x/>',
    b'\xff\xfe<\x00x\x00/\x00>',
    b'x' * 256001,
    f'<rdf:RDF xmlns:rdf="{NS["rdf"]}" xmlns:e="{NS["Iptc4xmpExt"]}"><rdf:Description e:DigitalSourceType="{TYPES["ai_generated"]}"><e:DigitalSourceType>digitalCapture</e:DigitalSourceType></rdf:Description></rdf:RDF>'.encode(),
])
def test_invalid_ambiguous_or_unsafe_xmp_cannot_establish_disclosure(xml):
    result = inspect_xmp({'xmp': xml})
    assert not result['ai_declared'] and result.get('error')


def test_unrelated_metadata_and_wrong_namespace_cannot_pass():
    assert not inspect_xmp({'comment': TYPES['ai_generated'], 'jfif': 1})['ai_declared']
    xml = f'<rdf:RDF xmlns:rdf="{NS["rdf"]}" xmlns:e="https://example.org/"><rdf:Description e:DigitalSourceType="{TYPES["ai_generated"]}"/></rdf:RDF>'
    assert not inspect_xmp({'xmp': xml})['ai_declared']


def test_embedded_ai_cannot_be_reclassified_as_company_media(db, settings, assets):
    data = image_rendition(storage(settings).read(assets[0].storage_key), 720, 720,
                           source='ai_generated', generator='runway/gen4_image')
    with pytest.raises(ValueError, match='IPTC AI declaration'):
        ingest(db, settings, data, 'marked.jpg', rights='public')
    with pytest.raises(ValueError, match='cannot be removed'):
        image_rendition(data, 720, 720, source='company')


@pytest.mark.parametrize('platform', ['facebook', 'linkedin'])
def test_live_metadata_check_reads_bytes_not_forged_database_fields(db, settings, post, assets, platform):
    asset = assets[0]
    asset.source, asset.generator, asset.prompt = 'ai_generated', 'runway/gen4_image', 'A facade'
    post.platform, post.ai_disclosure = platform, True
    asset.media = asset.media | {'metadata_keys': ['xmp'], 'embedded_provenance': {'ai_declared': True}}
    passed, detail = check_disclosure(post, assets, live=True, settings=settings)
    assert not passed and not detail['metadata_verified']
    assert detail['embedded_evidence'][0]['byte_verified'] is False
    data = storage(settings).read(asset.storage_key)
    marked = image_rendition(data, 720, 720, source=asset.source, generator=asset.generator)
    corrected = ingest(db, settings, marked, 'marked.jpg', source=asset.source, generator=asset.generator, prompt=asset.prompt, rights='public')
    passed, detail = check_disclosure(post, [corrected], live=True, settings=settings)
    assert passed and detail['metadata_verified']
    # Detect stripped/replaced objects even when a cached metadata record says they are marked.
    storage(settings).path(corrected.storage_key).write_bytes(data)
    passed, detail = check_disclosure(post, [corrected], live=True, settings=settings)
    assert not passed and 'checksum' in detail['reasons'][0]


def test_native_flags_still_work_without_iptc_and_dry_runs_report_the_gap(db, settings, post, assets):
    asset = assets[0]
    asset.source, asset.generator, asset.prompt = 'ai_generated', 'runway/gen4_image', 'A facade'
    post.ai_disclosure = True
    for platform in ['instagram', 'x']:
        post.platform = platform
        assert check_disclosure(post, assets, live=True, settings=settings)[0]
    post.platform = 'facebook'
    passed, details = check_disclosure(post, assets, settings=settings)
    assert passed and not details['metadata_verified'] and details['metadata_required_for_live']
    assert not check_disclosure(post, assets, live=True)[0]  # Storage verification cannot be skipped.


def test_disclosure_evidence_and_verdict_reach_independent_reviewer(db, settings, post, assets):
    asset = assets[0]
    asset.source, asset.generator, asset.prompt = 'ai_generated', 'runway/gen4_image', 'A facade'
    post.platform, post.ai_disclosure = 'facebook', True
    seen = []

    class Capture(Providers):
        def review(self, context):
            seen.append(context['renderer_context']['disclosure_check'])
            return super().review(context)

    evaluate(db, settings, post, providers=Capture(settings), live=True)
    assert seen[0]['passed'] is False and seen[0]['embedded_evidence'][0]['byte_verified'] is False


def test_adapter_revalidates_metadata_before_any_public_request(db, settings, post, assets):
    asset = assets[0]
    asset.source, asset.generator, asset.prompt = 'ai_generated', 'runway/gen4_image', 'A facade'
    post.platform, post.ai_disclosure = 'facebook', True
    adapter = ADAPTERS['facebook'](settings, db, post)
    adapter.dry = False
    with pytest.raises(PublishError, match='IPTC'):
        adapter.validate_payload(post)

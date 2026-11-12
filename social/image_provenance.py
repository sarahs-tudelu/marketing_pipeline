"""IPTC AI declarations in delivered image bytes, distinct from signed C2PA provenance."""

import hashlib
import io
import re
import xml.etree.ElementTree as ET

from PIL import Image

NS = {
    "x": "adobe:ns:meta/", "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "Iptc4xmpExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
    "xmp": "http://ns.adobe.com/xap/1.0/", "xmpMM": "http://ns.adobe.com/xap/1.0/mm/",
    "stRef": "http://ns.adobe.com/xap/1.0/sType/ResourceRef#",
}
TYPES = {
    "ai_generated": "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia",
    "ai_edited": "http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia",
}
for prefix, namespace in NS.items():
    ET.register_namespace(prefix, namespace)


def tag(prefix, name):
    return "{" + NS[prefix] + "}" + name


def inspect_xmp(info):
    raw = info.get("xmp") or info.get("XML:com.adobe.xmp")
    result = {"format": "IPTC XMP", "xmp_present": bool(raw), "ai_declared": False,
              "digital_source_type": None, "signature_verified": False}
    if not raw:
        return result
    try:
        if len(raw) > 256_000:
            raise ValueError("Oversized XMP")
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        # Only bounded UTF-8 XML; no DTDs, entities, external resolution or UTF-16 bypasses.
        if "\x00" in text or re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
            raise ValueError("Unsafe XMP")
        root = ET.fromstring(text)
        rdf = root if root.tag == tag("rdf", "RDF") else root.find("rdf:RDF", NS)
        if rdf is None:
            raise ValueError("Missing RDF")
        descriptions = rdf.findall("rdf:Description", NS)

        def values(prefix, name):
            key = tag(prefix, name)
            found = []
            for description in descriptions:
                if key in description.attrib:
                    found.append(description.attrib[key].strip())
                for child in description.findall(key):
                    found.append((child.get(tag("rdf", "resource")) or child.text or "").strip())
            return found

        types = values("Iptc4xmpExt", "DigitalSourceType")
        if len(types) > 1:
            raise ValueError("Ambiguous DigitalSourceType")
        value = types[0] if types else None
        if value and len(value) > 1024:
            raise ValueError("Invalid DigitalSourceType")
        result.update(digital_source_type=value, ai_declared=value in TYPES.values())
        result["ai_system"] = next(iter(values("Iptc4xmpExt", "AISystemUsed")), "")[:200]
        result["creator_tool"] = next(iter(values("xmp", "CreatorTool")), "")[:200]
        derived = rdf.find("rdf:Description/xmpMM:DerivedFrom/stRef:documentID", NS)
        result["derived_from"] = (derived.text or "")[:100] if derived is not None else ""
    except (ET.ParseError, ValueError, TypeError, UnicodeError):
        result["error"] = "Image XMP could not be safely or unambiguously read"
    return result


def read_image_provenance(data):
    with Image.open(io.BytesIO(data)) as image:
        return inspect_xmp(image.info)


def declaration_xmp(source, generator, source_checksum):
    if source not in TYPES or not generator or not re.fullmatch(r"[a-f0-9]{64}", source_checksum):
        raise ValueError("An AI source, generator and original checksum are required for image disclosure")
    root = ET.Element(tag("x", "xmpmeta"))
    rdf = ET.SubElement(root, tag("rdf", "RDF"))
    description = ET.SubElement(rdf, tag("rdf", "Description"), {tag("rdf", "about"): ""})
    ET.SubElement(description, tag("Iptc4xmpExt", "DigitalSourceType")).text = TYPES[source]
    ET.SubElement(description, tag("Iptc4xmpExt", "AISystemUsed")).text = generator[:200]
    ET.SubElement(description, tag("xmp", "CreatorTool")).text = "Tudelu Social Studio"
    derived = ET.SubElement(description, tag("xmpMM", "DerivedFrom"), {tag("rdf", "parseType"): "Resource"})
    ET.SubElement(derived, tag("stRef", "documentID")).text = "urn:sha256:" + source_checksum
    return ET.tostring(root, encoding="utf-8")


def verify_asset_metadata(asset, settings):
    """Re-read the stored file. Database flags or cached metadata never constitute byte evidence."""
    from .storage import storage

    evidence = {"asset_id": asset.id, "asset_checksum": asset.checksum, "byte_verified": False}
    if not asset.media["mime"].startswith("image/"):
        return evidence | {"reason": "Embedded video provenance is not verified by the image metadata checker"}
    if settings is None:
        return evidence | {"reason": "Storage settings are required to verify embedded image provenance"}
    store = storage(settings)
    try:
        data = store.read(asset.storage_key)
        if hashlib.sha256(data).hexdigest() != asset.checksum:
            return evidence | {"reason": "File checksum differs from the asset record"}
        observed = read_image_provenance(data)
        valid = observed["ai_declared"] and observed["digital_source_type"] == TYPES.get(asset.source)
        return evidence | observed | {"byte_verified": valid,
                                     "reason": "IPTC AI declaration verified in delivered bytes" if valid else
                                     "Missing, invalid or inconsistent IPTC AI-source declaration in image bytes"}
    except Exception:
        return evidence | {"reason": "Embedded image provenance could not be read; check storage and retry"}
    finally:
        if hasattr(store, "client"):
            store.client.close()

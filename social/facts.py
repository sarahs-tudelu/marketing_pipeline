import re
from datetime import datetime
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .models import FactSheet
from .security import digest


class Fact(BaseModel):
    id: str
    kind: Literal["claim", "price", "offer", "testimonial"] = "claim"
    product: str
    text: str = Field(min_length=1)
    variants: list[str] = []
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    amount: str | None = None
    currency: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def dated_offers(self):
        if self.kind in {"price", "offer"} and (not self.valid_from or not self.valid_until):
            raise ValueError("Prices/offers require validity dates")
        if self.kind == "price" and (not self.amount or not self.currency or self.amount not in self.text):
            raise ValueError("Price requires matching literal amount and currency")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("Invalid validity interval")
        return self


class Facts(BaseModel):
    version: str
    demo_only: bool = False
    products: list[dict] = []
    brand_voice: str
    banned_phrases: list[str] = []
    required_disclaimers: list[str] = []
    canonical_urls: list[str] = []
    hashtags: dict[str, list[str]]
    facts: list[Fact]

    @model_validator(mode="after")
    def unique_ids(self):
        if len({f.id for f in self.facts}) != len(self.facts):
            raise ValueError("Fact IDs must be unique")
        return self


def save_facts(db, document):
    facts = Facts.model_validate(document)
    doc = facts.model_dump(mode="json")
    key = digest(doc)
    existing = db.get(FactSheet, key)
    if existing:
        return existing
    from sqlalchemy import select

    if db.scalar(select(FactSheet).where(FactSheet.version == facts.version)):
        raise ValueError("Facts versions are immutable; use a new version")
    sheet = FactSheet(id=key, version=facts.version, document=doc)
    db.add(sheet)
    db.flush()
    return sheet


def load_facts(db, path):
    return save_facts(db, yaml.safe_load(path.read_text(encoding="utf-8")))


def active(fact, at):
    # Normalize explicit offsets rather than discarding them.
    from datetime import timezone

    def utc(value):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value

    return (not fact.valid_from or utc(fact.valid_from) <= at) and (
        not fact.valid_until or at < utc(fact.valid_until)
    )


def composed(parts):
    return any(isinstance(part, dict) and "kind" in part for part in parts)


def part_fact_ids(part):
    if not isinstance(part, dict):
        raise ValueError("Caption parts must be objects")
    ids = part.get("fact_ids", []) if "kind" in part else [part.get("fact_id")]
    if not isinstance(ids, list) or any(not isinstance(id, str) or not id for id in ids):
        raise ValueError("Caption citations must be a list of fact IDs")
    return list(dict.fromkeys(ids))


def caption_fact_ids(parts):
    if not isinstance(parts, list):
        raise ValueError("Caption parts must be a list")
    return list(dict.fromkeys(id for part in parts for id in part_fact_ids(part)))


def number_tokens(value):
    return set(re.findall(r"(?<!\w)\d+(?:[.,]\d+)*(?:%)?", value))


def validate_parts(parts, facts):
    if not isinstance(parts, list) or not parts or len(parts) > 12:
        raise ValueError("A caption needs between one and twelve source-linked parts")
    by_id = {f.id: f for f in facts.facts}
    creative = composed(parts)
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("Caption parts must be objects")
        text = part.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise ValueError("Caption parts need nonempty text within the supported length")
        ids = part_fact_ids(part)
        if any(id not in by_id for id in ids):
            raise ValueError("Caption cites an unknown fact")
        if not creative:
            fact = by_id[ids[0]]
            if text not in [fact.text, *fact.variants]:
                raise ValueError("Legacy caption parts must exactly match an approved fact/variant")
            continue
        if part.get("kind") not in {"claim", "copy"} or "fact_id" in part or "fact_ids" not in part:
            raise ValueError("Composed caption parts need kind, text and fact_ids")
        if (part["kind"] == "claim") != bool(ids):
            raise ValueError("Claims require fact IDs; nonfactual copy must use an empty citation list")
        cited = [by_id[id] for id in ids]
        # Financial terms, offers and real customer quotations keep their approved wording.
        if any(f.kind in {"price", "offer", "testimonial"} for f in cited):
            if len(cited) != 1 or text not in [cited[0].text, *cited[0].variants]:
                raise ValueError("Prices, offers and testimonials must keep exact approved wording")
        evidence = " ".join(wording for f in cited for wording in [f.text, *f.variants])
        if not number_tokens(text) <= number_tokens(evidence):
            raise ValueError("Caption contains a quantity absent from its cited facts")
    if not caption_fact_ids(parts):
        raise ValueError("Caption needs approved fact content")


def render(parts, profile, facts, platform, settings):
    validate_parts(parts, facts)
    by_id = {f.id: f for f in facts.facts}
    texts = [part["text"] for part in parts]
    # Existing saved captions keep their original rendering. New copy has its own opening and rhythm.
    lines = texts if composed(parts) else [profile.lead_in, " ".join(texts)]
    if profile.allow_links and (platform != "x" or settings.x_include_links):
        links = [by_id[id].url for id in caption_fact_ids(parts) if by_id[id].url]
        lines.extend(dict.fromkeys(links))
    lines.extend(facts.required_disclaimers)
    tags = facts.hashtags.get(platform, [])[: profile.hashtag_limit]
    if tags:
        lines.append(" ".join("#" + t.lstrip("#") for t in tags))
    return "\n\n".join(lines)


def content_digest(post, assets):
    return digest(
        {
            "caption": post.caption,
            "parts": post.parts,
            "facts": post.facts_id,
            "platform": post.platform,
            "assets": [
                (a.id, a.checksum, a.source, a.rights, a.rights_expire_at, a.generator, a.prompt, a.tags,
                 a.owner, a.customer_permission, a.permission_reference)
                for a in assets
            ],
            "ai": post.ai_disclosure,
            "campaign": post.campaign,
            "product": post.product,
        }
    )

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


def render(parts, profile, facts, platform, settings):
    by_id = {f.id: f for f in facts.facts}
    texts = []
    for part in parts:
        fact = by_id.get(part.get("fact_id"))
        text = part.get("text", "")
        if not fact or text not in [fact.text, *fact.variants]:
            raise ValueError("Every caption sentence must exactly match an approved fact/variant")
        texts.append(text)
    if not texts:
        raise ValueError("Caption needs approved fact content")
    lines = [profile.lead_in, " ".join(texts)]
    if profile.allow_links and (platform != "x" or settings.x_include_links):
        links = [by_id[p["fact_id"]].url for p in parts if by_id[p["fact_id"]].url]
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
            "assets": [(a.id, a.checksum, a.source, a.rights, a.rights_expire_at) for a in assets],
            "ai": post.ai_disclosure,
            "campaign": post.campaign,
            "product": post.product,
        }
    )

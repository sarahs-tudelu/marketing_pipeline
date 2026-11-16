"""Versioned Builder product guidance shared by generation and visual review."""

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuleSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    repository: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProductRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    instruction: str = Field(min_length=1, max_length=2000)
    source_ids: list[str] = Field(min_length=1)


class ProductProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    name: str
    aliases: list[str] = []
    prompt_cue: str = Field(min_length=1, max_length=420)
    rules: list[ProductRule] = Field(min_length=1)


class VisualGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    reviewed_on: str
    purpose: str
    sources: list[RuleSource] = Field(min_length=1)
    global_rules: list[ProductRule] = Field(min_length=1)
    products: list[ProductProfile] = Field(min_length=1)

    @model_validator(mode="after")
    def source_links(self):
        sources = {s.id for s in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("Visual guidance source IDs must be unique")
        rules = self.global_rules + [r for p in self.products for r in p.rules]
        if len({r.id for r in rules}) != len(rules):
            raise ValueError("Visual guidance rule IDs must be unique")
        if any(set(r.source_ids) - sources for r in rules):
            raise ValueError("Visual guidance rules must cite a recorded source")
        owners = {}
        for p in self.products:
            for name in [p.product_id, p.name, *p.aliases]:
                key = normalize(name)
                if not key or (key in owners and owners[key] != p.product_id):
                    raise ValueError("Visual product names must resolve to one family")
                owners[key] = p.product_id
        if len({p.product_id for p in self.products}) != len(self.products):
            raise ValueError("Visual product IDs must be unique")
        return self


def normalize(name):
    return re.sub(r"[^\w]+", " ", str(name).casefold().replace("_", " ")).strip()


def profile_for(guidance, name):
    if not guidance:
        return None
    key = normalize(name)
    for p in guidance.products:
        if key in {normalize(n) for n in [p.product_id, p.name, *p.aliases]}:
            return p
    raise ValueError(
        f"'{name}' has no approved Builder visual profile in this facts version. "
        "Choose a supported product before generating media."
    )


def context_for(guidance):
    return guidance.model_dump(mode="json") if guidance else None


def constrain_prompt(prompt, profile):
    """Reserve space for product identity; never truncate away its physical cues."""
    from .prompting import PROMPT_LIMIT, utf16_trim

    if profile is None:
        return utf16_trim(prompt)
    cue = profile.prompt_cue
    prompt = " ".join(prompt.split())
    if prompt.endswith(cue):
        prompt = prompt[: -len(cue)].strip()
    remaining = PROMPT_LIMIT - len(cue.encode("utf-16-le")) // 2 - 1
    return (utf16_trim(prompt, remaining) + " " + cue).strip()


def enforce_job_prompt(db, job):
    """Also cover edited/retried prompts before submission, preserving their audit record."""
    from .facts import Facts
    from .models import FactSheet
    from .video_segments import current_segment

    if job.stage != "visual" or job.brief["operation"] == "audio":
        return
    guidance = Facts.model_validate(db.get(FactSheet, job.brief["facts_id"]).document).visual_guidance
    if guidance is None:
        return
    segment = current_segment(job)
    system = (segment or {}).get("system") or (segment or {}).get("shot", {}).get("system")
    system = system or job.brief.get("depicted_system") or job.brief["product"]
    profile = profile_for(guidance, system)
    if segment and segment.get("prompt"):
        segments = [
            s | {"prompt": constrain_prompt(s["prompt"], profile)} if s["index"] == segment["index"] else s
            for s in job.brief["video_segments"]
        ]
        job.brief = job.brief | {"video_segments": segments}
    else:
        job.brief = job.brief | {
            "generation_prompt": constrain_prompt(job.brief["generation_prompt"], profile)
        }
    job.brief = job.brief | {"visual_guidance_version": guidance.version}
    db.commit()

"""Owner-requested reference workflow; product dimensions still come from the Builder."""

VERSION = "tudelu-reference-rendering-2026-11-16-v1"

RULES = [
    "Use actual matching company photographs from the Public asset library, including Google Drive imports, "
    "and the product images on tudelu.com. A description or a previous AI generation is not a visual reference.",
    "Current owner corrections take precedence, then the AI Builder drawing guide, then compatible reference "
    "images, then creative direction. Website illustrations are appearance references, not verified installations "
    "or new caption facts. Reject references that contradict the selected product's Builder geometry.",
    "Choose references separately for every product scene, including later clips and regenerated clips. "
    "Match the product family, roof, support arrangement, finish and selected accessories. Prefer real company "
    "photos when equally suitable. Do not combine incompatible structures or invent missing attachments.",
    "Keep the reference's structural arrangement, straight fascia cross-sections, connected members, roof type, "
    "support count, attachment points and finish consistent throughout a scene. Preserve stainless rod fittings. "
    "Wood Look is a finish on aluminum, not timber. Add only selected, Builder-supported accessories.",
    "For rod-hung canopies show the continuous rods above the roof, connecting the outer fascia to higher wall "
    "anchors. Choose framing that makes that connection visible. Do not crop the critical connection or orbit "
    "behind a structure whose unseen geometry would have to be invented.",
    "Animate a verified starting image with restrained camera motion. Fixed pergola and sunshade louvers remain "
    "stationary; MUTE walls retract vertically overhead without a floor track. Show product operation only when "
    "the reference and Builder guide support it. Otherwise keep the product stationary and move the camera.",
    "Use one coherent configuration per scene and carry it across clips of that scene. Multi-product stories "
    "need a matching reference for each family. A requested new setting never justifies changing the product.",
    "If no compatible reference is available, stop with the missing product/configuration named. Never silently "
    "fall back to text-only product generation. Existing usage permissions and independent publication review apply.",
]


def policy():
    return {"version": VERSION, "rules": RULES}

"""Owner-requested reference workflow; product dimensions still come from the Builder."""

VERSION = "tudelu-reference-rendering-2026-11-16-v2"

DEFAULT_VISUAL_DIRECTION = (
    "Photorealistic architectural film, natural soft light, balanced exposure, restrained contrast and "
    "a coordinated neutral color grade. Consistent lens character, subject scale and slow camera pacing; "
    "match composition and movement across adjacent cuts. Follow explicitly requested lighting and mood."
)

RULES = [
    "Use actual matching company photographs from the Public asset library, including Google Drive imports, "
    "and the product images on tudelu.com. A description or a previous AI generation is not a visual reference.",
    "Current owner corrections and the AI Builder drawing guide define product geometry and supported options. "
    "The user's prompt controls the creative direction. References supply product identity, not a locked scene, "
    "background or color. Website illustrations are not verified installations or new caption facts.",
    "Choose references separately for every product scene, including later clips and regenerated clips. "
    "Match the product family and use its visible construction as evidence. Prefer real company photos when "
    "equally suitable. Different locations, architecture, straight-sided footprints, Builder-supported structures, "
    "finishes, colors and accessories may be generated to follow the prompt. A different background or finish "
    "is not a reference mismatch. Surrounding architecture may vary in brick, stone, glass, wood or other "
    "requested materials; the product itself keeps its Builder-supported materials. Do not combine "
    "incompatible structures or invent missing attachments.",
    "Keep each generated scene's selected structural arrangement, straight fascia cross-sections, connected "
    "members, roof type, supports, attachment points and finish consistent within that scene. Preserve stainless rod fittings. "
    "Wood Look is a finish on aluminum, not timber. Add only selected, Builder-supported accessories.",
    "For rod-hung canopies show the continuous rods above the roof, connecting the outer fascia to higher wall "
    "anchors. Choose framing that makes that connection visible. Do not crop the critical connection or orbit "
    "behind a structure whose unseen geometry would have to be invented.",
    "For video, first generate a new scene image from the prompt using company images as product references, "
    "then check it and animate it. Do not animate an unchanged library background when a new scene is requested. "
    "Fixed pergola and sunshade louvers remain "
    "stationary; MUTE walls retract vertically overhead without a floor track. Show product operation only when "
    "the reference and Builder guide support it. Otherwise keep the product stationary and move the camera.",
    "Use one coherent configuration across clips of the same installation. A montage may mix locations, "
    "product families and supported finishes/configurations as directed. Keep a shared visual direction across "
    "the whole film: compatible lighting, exposure, palette, lens character, framing and camera speed/direction. "
    "Use adjacent compositions for natural match cuts; avoid abrupt changes of rendering style or random "
    "day/night jumps. Variety does not require every clip to look identical. A prior generated scene can guide "
    "style only, never establish product facts or override the current shot's product and requested differences.",
    "If no compatible reference is available, stop with the missing product/configuration named. Never silently "
    "fall back to text-only product generation. Existing usage permissions and independent publication review apply.",
]


def policy():
    return {"version": VERSION, "rules": RULES}

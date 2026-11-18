"""Owner-requested reference workflow; product dimensions still come from the Builder."""

VERSION = "tudelu-reference-rendering-2026-11-18-v5"

DEFAULT_VISUAL_DIRECTION = (
    "Photorealistic architectural film, natural soft light, balanced exposure, restrained contrast and "
    "a coordinated neutral color grade. Consistent lens character, subject scale and slow camera pacing; "
    "match composition and movement across adjacent cuts. Follow explicitly requested lighting and mood."
)

# Prompt guidance is separate from the persisted rendering-policy digest: changing the writer
# must not invalidate a paid scene or an in-flight task that uses an older saved plan.
SELECTION_VERSION = "action-and-doorways-2026-11-18-v3"
STRUCTURE_SELECTION = (
    "Use the matching reference as the starting point for construction details the approved shot leaves "
    "open, and adapt it where the shot's configuration or the situation reasonably calls for a supported "
    "change, such as a panel or glass roof for rain cover, an extra post or beam for a longer span, or a "
    "different finish. Generated prose and earlier reviews are drafts, not user specifications: an "
    "unrequested post count, dimension, removed beam or span that only they mention is not mandatory. "
    "Builder dimensions and post-count examples apply to their stated configuration, not every scene. A "
    "single roof plane may contain multiple louver bays and a connected intermediate beam; that is not a "
    "stacked roof. Seat louver ends in the perimeter or intermediate supporting frame. Keep ordinary "
    "perspective overlap distinct from physical interpenetration or disconnected members."
)

# What happens in a clip. Shared by the prompt writer, the scene planner and the visual check. A prompt
# that only moved the camera over a "completely still" scene came back from Veo as a frozen picture.
MOTION_GUIDANCE = (
    "A clip shows something happening, not a still photo with a moving camera. Describe what moves, in order: "
    "what each person in the scene does, as one or two simple continuous actions that fit the clip's seconds "
    "(such as walking in, sitting down, pouring a drink or talking), natural movement in the setting such as "
    "leaves, water, steam, rain or flickering light, and one slow camera move: a push-in, a lateral truck or "
    "a pedestal. Hold the camera still only when people or the setting carry the movement. The Tudelu "
    "structure itself stays rigid; only a MUTE partition may move, and only as its rules allow."
)
DOORWAY_GUIDANCE = (
    "People pass only through real openings. When someone goes through a door, gate or glass wall, either the "
    "scene starts with it already open wide enough to walk through, or the clip shows them open it by hand, "
    "or automatic doors slide apart, before they step through. Nobody passes through a closed door, glass, a "
    "wall, a railing or furniture."
)


RULES = [
    "Use actual matching company photographs from the Public asset library, including Google Drive imports, "
    "and the product images on tudelu.com. A description or a previous AI generation is not a visual reference.",
    "Current owner corrections and the AI Builder drawing guide describe Tudelu's products and supported "
    "options; customize within them to fit the scene, as a product brief would. The approved plan controls "
    "the creative direction. References supply product identity, not a locked scene, "
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
    # Builder PRODUCT-RULES.md (no-overlap rule, seated posts, panel grid) at the snapshot commit.
    "Every product piece is solid: no member passes through, merges into, hides inside or overlaps another "
    "member, the fascia, a wall, the floor or an attachment, and connections touch flush or seat into each other. "
    "Posts stand plumb and straight from the ground to the frame, seated fully under the fascia's bottom "
    "perimeter unless the selected design places them outside. The roof is one flat plane resting on the frame "
    "(two only for a selected double roof), with evenly spaced panel seams aligned to the frame. Louvers in one "
    "run are parallel, evenly spaced and the same profile, and each end is seated in the frame.",
    "Within a clip the structure is rigid and unchanged: the same number of posts, louvers, roof layers, rods "
    "and joints in every frame. Members never appear, vanish, duplicate, bend, stretch, slide or change thickness "
    "as the camera moves.",
    "Compose scenes so the generator doesn't have to invent structure: the whole structure, or a deliberate "
    "close detail, seen at eye level or slightly above, usually from a three-quarter view, with the roof edge "
    "and most supports in frame. People, furniture and plants may overlap parts of it. Avoid cropping a "
    "structure partway along a post or roof edge, mirror-like reflections of it, and rows of identical structures receding into the distance. "
    "Surrounding signs, awnings and walls are plain and unmarked.",
    "Animate each clip with what happens in it: the people's actions and the setting's natural movement, with "
    "one slow, steady camera move (a push-in, a gentle lateral truck or a slow pedestal), while the structure "
    "stays rigid. Hold the camera still only when people or the setting carry the movement. Avoid orbits and "
    "arcs around the structure, pull-back or crane reveals of parts the scene image doesn't show, fast moves "
    "and zooms through the structure; they make the model invent geometry.",
    "People move like people and pass only through real openings: a door, gate or glass wall is open, or is "
    "opened by hand or slides open automatically, before anyone steps through it.",
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
    "Weather, seasons and people are part of the setting. Choose the supported roof a weather scene needs, "
    "such as a panel or glass pergola roof when people stay dry in rain.",
    "If no compatible reference is available, stop with the missing product/configuration named. Never silently "
    "fall back to text-only product generation. Existing usage permissions and independent publication review apply.",
]


def policy():
    return {"version": VERSION, "rules": RULES}

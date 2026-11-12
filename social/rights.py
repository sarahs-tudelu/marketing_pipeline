"""Usage-rights choices for assets and creative briefs.

New entries choose Public or Not public; the owner is a separate optional field. Rows saved
before this choice existed keep their original free-text rights and are shown as unclassified.
"""

RIGHTS = {"public": "Public", "not_public": "Not public"}
OWNER_MAX = 200


def usage_rights(value):
    """Validate a usage-rights choice from a form, manifest or API request."""
    value = str(value or "").strip()
    if value not in RIGHTS:
        raise ValueError("Choose usage rights: Public or Not public")
    return value


def owner_value(value):
    value = " ".join(str(value or "").split())
    if len(value) > OWNER_MAX:
        raise ValueError(f"Owner must be {OWNER_MAX} characters or fewer")
    return value


def is_public(rights):
    return rights == "public"


def rights_label(rights):
    if rights in RIGHTS:
        return RIGHTS[rights]
    return f"Unclassified: {rights}" if rights else "Missing"


def combined_rights(chosen, sources):
    """Media derived from a Not public source cannot become Public."""
    if any(source.rights == "not_public" for source in sources):
        return "not_public"
    return chosen

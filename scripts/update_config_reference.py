"""Regenerate the checked-in reference and verify .env.example covers every setting."""

import json
from pathlib import Path

from pydantic import SecretStr

from social.config import Profile, Settings

ROOT = Path(__file__).resolve().parents[1]
comments = []
descriptions = {}
for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
    if line.startswith("#"):
        comments.append(line.lstrip("# "))
    elif "=" in line:
        key = line.split("=", 1)[0]
        descriptions[key] = " ".join(comments) if comments else "Related configuration in the group above."
        comments = []

expected = {name.upper() for name in Settings.model_fields}
assert set(descriptions) == expected | {"POSTGRES_PASSWORD"}, "Update .env.example before this reference"


def display(value):
    if isinstance(value, SecretStr):
        return "(unset secret)"
    if value == "":
        return "(unset)"
    if isinstance(value, Path):
        return str(value).replace("\\", "/")
    return json.dumps(value, ensure_ascii=False)


lines = [
    "# Configuration reference",
    "",
    "Generated from `social/config.py` and `.env.example`. All variables are optional in offline development; blank values use defaults. Production/live startup requires authenticated administration, a session secret and an encryption key. Values are loaded from environment variables first, then the ignored local `.env`.",
    "",
    "Actual client IDs, API keys, tokens, passwords and origin domains are deliberately unset. Version/model settings are pinned defaults, not credentials. `LIVE_PLATFORMS` is an explicit additional switch; missing connections still remain dry. Restart web and worker after changes.",
    "",
    "| Environment variable | Default | Purpose / group note |",
    "|---|---|---|",
]
for name, field in Settings.model_fields.items():
    key = name.upper()
    lines.append(f"| `{key}` | `{display(field.default)}` | {descriptions[key].replace('|', '/')} |")
lines.extend(
    [
        "| `POSTGRES_PASSWORD` | (unset secret) | Compose-only PostgreSQL password; use a random alphanumeric value. |",
        "",
        "## Platform profile fields",
        "",
        "`PROFILES_PATH` is YAML keyed by `linkedin`, `instagram`, `facebook`, `x`, `tiktok`. The following defaults apply unless a platform overrides them. Required fields have no default. Caps, caption length, asset counts and supported media envelopes are validated against conservative code ceilings; lower limits are configurable. Expanding the supported format envelope requires another official-documentation review.",
        "",
        "| Field | Default |",
        "|---|---|",
    ]
)
for name, field in Profile.model_fields.items():
    value = "required" if field.is_required() else display(field.default)
    lines.append(f"| `{name}` | `{value}` |")
lines.extend(
    [
        "",
        "Windows are same-day local clock pairs such as `[[09:00, 12:00], [14:00, 17:00]]`; quote time values in YAML. Overnight windows must be split into same-day ranges. Use IANA time zones. Targets are calendar-day preferences; hard caps use rolling 24-hour windows. Promotional mix uses a seven-day trailing history rounded up for small samples.",
        "",
        "Default hard caps: LinkedIn 3, Instagram 5, Facebook 3, X 3, TikTok 3. All targets are 1/day. Platform YAML overrides include Instagram JPEG-only images, TikTok 360-pixel minimum width/height, image-only carousel counts, and distinct platform lead-ins. Native account-specific limits may be lower and always take precedence.",
        "",
        "## Provider gateway and credentials",
        "",
        "Claude uses official Messages with adaptive thinking and `output_config.effort=xhigh`. OpenAI uses official image generation, Responses vision and embeddings. `AI_MODE=fixture` is offline; live publication additionally requires live AI checks and semantic embeddings. Optional video gateway protocol and all connection steps are in `README.md`.",
        "",
        "Meta tokens may be imported from ignored environment variables or entered through Connections; all persisted tokens and live response checkpoints use Fernet encryption. X, LinkedIn and TikTok tokens come from OAuth callbacks. Do not place access/refresh tokens in the YAML profiles or facts files. Secret rotation accepts comma-separated Fernet keys, newest first; run `tudelu rotate-tokens` before removing an old key.",
        "",
    ]
)
(ROOT / "docs/configuration.md").write_text("\n".join(lines), encoding="utf-8")
print(f"Documented {len(expected)} application settings and {len(Profile.model_fields)} profile fields.")

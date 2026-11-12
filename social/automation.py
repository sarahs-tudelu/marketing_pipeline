from .models import Control


def can_automate(db, settings, *, requested=False):
    """Explicit Create now workflows can proceed while routine automation is off."""
    control = db.get(Control, 1)
    db.refresh(control)
    return (requested or settings.automates_posts(control)) and not control.killed

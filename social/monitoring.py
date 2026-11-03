import json
import logging
import smtplib
from email.message import EmailMessage

import httpx
from sqlalchemy import select

from .db import now
from .models import Alert, Control

log = logging.getLogger("social")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        try:
            values = json.loads(record.getMessage())
        except (ValueError, TypeError):
            values = {"event": "log", "message": record.getMessage()}
        if not isinstance(values, dict):
            values = {"event": "log", "message": values}
        if record.exc_info:
            values["error_type"] = record.exc_info[0].__name__
        return json.dumps(
            {"at": now().isoformat(), "level": record.levelname, "logger": record.name, **values}, default=str
        )


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for name in ("", "uvicorn", "uvicorn.error"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(JsonFormatter())
    # Avoid URLs with auth codes or signed upload parameters in dependency logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def event(name, **values):
    log.info(json.dumps({"event": name, "at": now().isoformat(), **values}, default=str))


def alert(db, kind, message, key):
    if not db.scalar(select(Alert.id).where(Alert.dedupe_key == key)):
        db.add(Alert(kind=kind, message=message, dedupe_key=key))
        event("alert", kind=kind, message=message)


def set_kill(db, enabled, actor):
    control = db.get(Control, 1)
    control.killed = enabled
    if enabled:
        alert(db, "kill_switch", f"Global kill switch activated by {actor}", "kill:" + now().isoformat())
    event("kill_switch", enabled=enabled, actor=actor)
    db.commit()


def deliver_alerts(db, settings):
    for item in db.scalars(select(Alert).where(Alert.delivered.is_(False), Alert.attempts < 5)).all():
        if not settings.alert_slack_webhook.get_secret_value() and not settings.alert_email_to:
            continue  # Visible in admin until a channel is configured.
        try:
            if settings.alert_slack_webhook.get_secret_value():
                with httpx.Client(timeout=15) as client:
                    response = client.post(
                        settings.alert_slack_webhook.get_secret_value(),
                        json={"text": f"Tudelu [{item.kind}] {item.message}"},
                    )
                    response.raise_for_status()
            if settings.alert_email_to:
                message = EmailMessage()
                message["From"], message["To"] = settings.smtp_from, settings.alert_email_to
                message["Subject"] = f"Tudelu publishing: {item.kind}"
                message.set_content(item.message)
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
                    if settings.smtp_starttls:
                        smtp.starttls()
                    if settings.smtp_username:
                        smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
                    smtp.send_message(message)
            item.delivered = True
        except Exception as exc:
            event("alert_delivery_failed", alert_id=item.id, error_type=type(exc).__name__)
        item.attempts += 1
        db.commit()

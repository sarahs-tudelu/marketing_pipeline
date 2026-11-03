import argparse
import json
from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import Settings
from .db import make_engine, sessions
from .monitoring import setup_logging


def main():
    parser = argparse.ArgumentParser(description="Tudelu official-API publishing")
    parser.add_argument(
        "command", choices=["init", "seed", "demo", "worker", "analytics", "import-meta", "rotate-tokens"]
    )
    args = parser.parse_args()
    settings = Settings()
    setup_logging()
    if args.command == "worker":
        from .worker import run

        run()
        return
    if args.command == "init":
        command.upgrade(Config("alembic.ini"), "head")
    engine = make_engine(settings.database_url)
    with sessions(engine)() as db:
        from .pipeline import bootstrap

        bootstrap(db)
        if args.command in {"init", "seed"}:
            from .seed import seed

            facts, assets = seed(db, settings)
            print(json.dumps({"facts_version": facts.version, "assets": [a.id for a in assets]}))
        elif args.command == "demo":
            from .seed import demo

            result = demo(db, settings)
            Path("data").mkdir(exist_ok=True)
            Path("data/demo-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result, indent=2))
        elif args.command == "import-meta":
            from .connections import save_meta

            save_meta(
                db,
                settings,
                settings.meta_system_user_token.get_secret_value(),
                settings.meta_page_id,
                settings.instagram_user_id,
            )
            print("Meta connections validated and encrypted.")
        elif args.command == "analytics":
            from .analytics import collect, report

            if settings.analytics_enabled:
                collect(db, settings)
            print(json.dumps(report(db, settings), indent=2, default=str))
        elif args.command == "rotate-tokens":
            from sqlalchemy import select

            from .models import Account, OAuthState, RequestRecord
            from .security import Vault

            vault = Vault(settings)
            for account in db.scalars(select(Account)):
                if account.credentials:
                    account.credentials = vault.encrypt(vault.decrypt(account.credentials))
            for state in db.scalars(select(OAuthState)):
                state.verifier = vault.encrypt(vault.decrypt(state.verifier))
            for record in db.scalars(select(RequestRecord).where(RequestRecord.dry_run.is_(False))):
                if record.response.get("encrypted"):
                    record.response = {
                        "encrypted": vault.encrypt(vault.decrypt(record.response["encrypted"]))
                    }
            db.commit()
            print("Encrypted records rotated to first configured key.")


if __name__ == "__main__":
    main()

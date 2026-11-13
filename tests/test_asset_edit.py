import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from social.asset_edit import AssetEditConflict, edit_asset, form_values, revision
from social.db import now
from social.facts import content_digest
from social.gate import evaluate, run_gate
from social.models import Asset, AssetEdit, DriveImport, Transition
from social.storage import LocalStore, ingest


def values(asset, **changes):
    return (
        {
            key: getattr(asset, key)
            for key in (
                "source",
                "rights",
                "owner",
                "rights_expire_at",
                "generator",
                "prompt",
                "tags",
                "customer_permission",
                "permission_reference",
            )
        }
        | {"rights": "public"}
        | changes
    )


def save(db, asset, **changes):
    result = edit_asset(
        db,
        asset,
        values(asset, **changes),
        original_name=asset.original_name,
        expected_revision=revision(asset),
        actor="test-editor",
    )
    db.commit()
    return result


def test_permissions_and_owner_are_editable_without_rewriting_media(db, settings, assets):
    asset = assets[0]
    original = (asset.id, asset.storage_key, asset.checksum, LocalStore(settings).read(asset.storage_key))
    save(db, asset, rights="not_public", owner="Tudelu", rights_expire_at=now() + timedelta(days=30))
    assert (asset.rights, asset.owner) == ("not_public", "Tudelu")
    assert (
        asset.id,
        asset.storage_key,
        asset.checksum,
        LocalStore(settings).read(asset.storage_key),
    ) == original
    history = db.scalar(select(AssetEdit))
    assert history.before["rights"] == "Owned by company"
    assert history.after["rights"] == "not_public" and history.actor == "test-editor"
    assert history.asset_id == asset.id


def test_old_browser_form_cannot_overwrite_new_metadata(db, assets):
    asset = assets[0]
    old = revision(asset)
    save(db, asset, owner="First editor")
    with pytest.raises(AssetEditConflict, match="changed after"):
        edit_asset(
            db,
            asset,
            values(asset, owner="Second editor"),
            original_name=asset.original_name,
            expected_revision=old,
            actor="second",
        )
    db.rollback()
    assert asset.owner == "First editor"
    assert len(db.scalars(select(AssetEdit)).all()) == 1


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"source": "ai_generated", "generator": "", "prompt": ""}, "require generator"),
        ({"source": "company", "generator": "a model"}, "AI creation details"),
        ({"customer_permission": True, "permission_reference": ""}, "evidence"),
        ({"rights": "made up"}, "usage rights"),
        ({"tags": []}, "JSON object"),
        ({"tags": {"source_asset_ids": ["forged"]}}, "cannot be changed"),
    ],
)
def test_invalid_metadata_does_not_change_the_asset(db, assets, changes, message):
    asset = assets[0]
    before = revision(asset)
    with pytest.raises(ValueError, match=message):
        save(db, asset, **changes)
    db.rollback()
    assert revision(asset) == before
    assert not db.scalar(select(AssetEdit))


def test_ai_metadata_cannot_be_relabelled_company(db, assets):
    asset = assets[0]
    asset.media = asset.media | {"embedded_provenance": {"ai_declared": True}}
    db.commit()
    with pytest.raises(ValueError, match="Embedded metadata"):
        save(db, asset)
    db.rollback()


@pytest.mark.parametrize("status", ["generated", "approved", "scheduled"])
def test_edit_invalidates_pending_posts_and_records_reason(db, settings, assets, post, status):
    if status != "generated":
        run_gate(db, settings, post)
    if status == "scheduled":
        post.status, post.scheduled_at = status, now() + timedelta(hours=2)
        db.commit()
    old_checks = post.approved_digest
    save(db, assets[0], rights="not_public")
    assert post.status == "held" and not post.approved_digest
    assert post.scheduled_at is None
    change = db.scalars(
        select(Transition).where(Transition.post_id == post.id).order_by(Transition.id.desc())
    ).first()
    assert "Media details changed" in change.reason and change.actor == "test-editor"
    if status != "generated":
        assert old_checks


def test_edit_cannot_race_with_publication(db, assets, post):
    post.status = "publishing"
    db.commit()
    with pytest.raises(AssetEditConflict, match="being published"):
        save(db, assets[0], rights="not_public")
    db.rollback()
    assert assets[0].rights == "Owned by company"
    assert post.status == "publishing"


def test_published_posts_keep_their_status_and_history(db, assets, post):
    post.status, post.remote_id, post.approved_digest = "published", "remote-123", "original-approval"
    db.commit()
    save(db, assets[0], rights="not_public")
    assert (post.status, post.remote_id, post.approved_digest) == (
        "published",
        "remote-123",
        "original-approval",
    )


def test_source_rights_apply_to_derived_media_and_cannot_be_unlinked(db, settings, assets, post):
    parent = assets[0]
    parent.rights = "public"
    derived = ingest(
        db,
        settings,
        LocalStore(settings).read(parent.storage_key),
        "rendition.jpg",
        rights="public",
        tags={"source_asset_ids": [parent.id]},
    )
    post.asset_ids, post.status = [derived.id], "scheduled"
    db.commit()
    save(db, parent, rights="not_public")
    assert post.status == "held"
    checks = {c.name: c for c in evaluate(db, settings, post, live=True)[0]}
    assert not checks["rights"].passed
    assert checks["rights"].details["not_public_for_live"] == [parent.id]
    db.rollback()
    save(db, derived, tags={"campaign": "updated"})
    assert derived.tags["source_asset_ids"] == [parent.id]


def test_customer_permission_is_in_approval_digest_and_live_rights(db, settings, assets, post):
    asset = assets[0]
    prior = content_digest(post, assets)
    asset.tags = asset.tags | {"real_customer_content": True}
    asset.customer_permission, asset.permission_reference = True, "release-1"
    assert content_digest(post, assets) != prior
    db.commit()
    save(db, asset, customer_permission=False)
    checks = {c.name: c for c in evaluate(db, settings, post, live=True)[0]}
    assert checks["rights"].details["missing_customer_permission"] == [asset.id]


def test_name_only_edit_preserves_approval_and_original_extension(db, settings, assets, post):
    asset = assets[0]
    save(db, asset)
    post.status = "generated"
    db.commit()
    run_gate(db, settings, post)
    approved = post.approved_digest
    edit_asset(
        db,
        asset,
        values(asset),
        original_name="Showroom.jpg",
        expected_revision=revision(asset),
        actor="editor",
    )
    db.commit()
    assert asset.original_name == "Showroom.jpg"
    assert post.status == "approved" and post.approved_digest == approved
    with pytest.raises(ValueError, match="extension"):
        edit_asset(
            db,
            asset,
            values(asset),
            original_name="Showroom.mp4",
            expected_revision=revision(asset),
            actor="editor",
        )


def test_form_save_validation_csrf_and_history(db, settings, assets):
    from social.app import create_app

    asset = assets[0]
    url = f"/admin/assets/{asset.id}/edit"
    action = f"/forms/assets/{asset.id}/save-media"
    with TestClient(create_app(settings)) as client:
        assert "Edit details" in client.get("/admin/assets").text
        assert client.get(url).status_code == 200
        token = client.get("/api/session").json()["csrf"]
        form = form_values(asset) | {"rights": "public", "owner": "Tudelu", "csrf": token}
        assert client.post(action, data={}).status_code == 403
        failed = client.post(action, data=form | {"tags": "bad json"})
        assert failed.status_code == 400 and "bad json" in failed.text
        response = client.post(action, data=form)
        assert response.status_code == 200 and "Media details saved." in response.text
        assert "Change history" in response.text and "local-developer" in response.text
        db.expire_all()
        assert asset.rights == "public" and asset.owner == "Tudelu"
        assert client.post(action, data=form | {"owner": "Stale"}).status_code == 409
        assert client.get("/admin/assets/missing/edit").status_code == 404
        assert client.post("/forms/assets/missing/save-media", data={"csrf": token}).status_code == 404


def test_drive_scan_preserves_local_permission_edits(db, settings):
    from test_drive_import import FakeDrive, configure, due, file_info

    from social.drive_import import poll

    configure(db)
    drive = FakeDrive([file_info("imported")])
    poll(db, settings, drive)
    record = db.get(DriveImport, "imported")
    asset = db.get(Asset, record.asset_id)
    save(db, asset, rights="public", owner="Tudelu")
    due(db)
    poll(db, settings, drive)
    assert asset.rights == "public" and asset.owner == "Tudelu"
    assert db.get(DriveImport, "imported").asset_id == asset.id
    assert drive.downloads == ["imported"]


def test_display_tags_hide_system_records_but_keep_editable_tags(db, assets):
    asset = assets[0]
    asset.tags = {
        "source_asset_ids": ["original"],
        "google_drive_file_id": "drive-id",
        "product": "demo",
        "brand_logo": True,
    }
    fields = form_values(asset)
    assert json.loads(fields["tags"]) == {"product": "demo"}
    assert fields["brand_logo"] == "on"

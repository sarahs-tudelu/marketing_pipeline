"""The asset library: filters and sorting, the library check, deleting and restoring files, media pickers,
and the Google Drive and library-search fixes that keep real videos available to real project posts."""

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_drive_import import FakeDrive, configure, due, file_info
from test_idea_plans import facts, logo  # noqa: F401
from test_real_edit import footage, jpeg
from test_real_ideas import plan, plan_form

from social import media_search
from social.app import create_app
from social.db import now
from social.drive_import import poll
from social.library import delete_assets, restore_assets
from social.media_search import SEARCH_LIMIT, library_media, shortlist
from social.media_studio import CreativeBrief, validate_sources
from social.models import Asset, AssetEdit, DriveImport, MediaCampaign
from social.pipeline import draft
from social.storage import LocalStore, ingest


def library(db, settings):
    video = ingest(db, settings, footage(), "crew-install.mp4", rights="public",
                   tags={"product": "Retractable Partition Walls", "google_drive_file_id": "d-1",
                         "google_drive_folder_id": "projects"})
    terrace = ingest(db, settings, jpeg((180, 160, 140)), "pergola-terrace.jpg", rights="not_public",
                     tags={"product": "Pergola"})
    concept = ingest(db, settings, jpeg((90, 90, 90)), "concept.jpg", source="ai_generated",
                     generator="runway/gen4_image", prompt="A pergola", rights="public")
    copy = ingest(db, settings, jpeg((20, 30, 40)), "instagram-copy.jpg", rights="public",
                  tags={"rendition": {"platform": "instagram", "original_asset_id": terrace.id},
                        "source_asset_ids": [terrace.id]})
    for index, asset in enumerate([video, terrace, concept, copy]):
        asset.created_at = now() - timedelta(days=4 - index)
    db.commit()
    return video, terrace, concept, copy


def names(html):
    """File names in the library table, in order."""
    table = html.split('class="library-table"', 1)[1]
    return re.findall(r'aria-label="Choose ([^"]+)"', table)


def test_library_filters_sort_and_checks_what_is_there(db, settings):
    video, terrace, concept, copy = library(db, settings)
    with TestClient(create_app(settings)) as client:
        page = client.get("/admin/assets").text
        # Library files, newest first; the post copy only under Everything.
        assert names(page) == ["concept.jpg", "pergola-terrace.jpg", "crew-install.mp4"]
        assert names(client.get("/admin/assets?source=all").text)[0] == "instagram-copy.jpg"
        assert names(client.get("/admin/assets?type=video").text) == ["crew-install.mp4"]
        assert names(client.get("/admin/assets?type=image/jpeg&sort=oldest").text) == [
            "pergola-terrace.jpg", "concept.jpg"]
        assert names(client.get("/admin/assets?product=pergola").text) == ["pergola-terrace.jpg"]
        assert names(client.get("/admin/assets?product=none").text) == ["concept.jpg"]
        assert names(client.get("/admin/assets?source=drive").text) == ["crew-install.mp4"]
        assert names(client.get("/admin/assets?source=ai_generated").text) == ["concept.jpg"]
        assert names(client.get("/admin/assets?rights=not_public").text) == ["pergola-terrace.jpg"]
        assert names(client.get("/admin/assets?q=partition").text) == ["crew-install.mp4"]
        assert names(client.get("/admin/assets?sort=name").text) == [
            "concept.jpg", "crew-install.mp4", "pergola-terrace.jpg"]
        assert "No files match these filters" in client.get("/admin/assets?q=nothing-like-this").text
        # Unknown values fall back to the defaults rather than failing.
        assert names(client.get("/admin/assets?type=x&source=y&sort=z&page=abc").text) == names(page)
        # The library check counts library files by type and source.
        check = page.split('id="library-check"', 1)[1].split("</section>", 1)[0]
        assert re.search(r"Videos</a></th>\s*<td class=\"num\">1</td><td class=\"num\">1</td>", check)
        assert "1 copy the app made for posts" in check


def test_library_pages_keep_their_filters(db, settings, monkeypatch):
    monkeypatch.setattr("social.library.PAGE_SIZE", 2)
    library(db, settings)
    with TestClient(create_app(settings)) as client:
        first = client.get("/admin/assets?source=all").text
        assert names(first) == ["instagram-copy.jpg", "concept.jpg"] and "Page 1 of 2" in first
        assert 'href="/admin/assets?source=all&amp;page=2#library"' in first
        assert names(client.get("/admin/assets?source=all&page=2").text) == ["pergola-terrace.jpg", "crew-install.mp4"]
        assert names(client.get("/admin/assets?source=all&page=9").text) == ["pergola-terrace.jpg", "crew-install.mp4"]


def test_deleting_an_unused_file_removes_it_and_its_bytes(db, settings):
    video, terrace, concept, copy = library(db, settings)
    db.add(AssetEdit(asset_id=concept.id, actor="admin", before={}, after={}))
    db.add(DriveImport(file_id="d-9", folder_id="projects", name="concept.jpg", fingerprint="x",
                       asset_id=concept.id, status="imported"))
    db.commit()
    stored, concept_id = LocalStore(settings).path(concept.storage_key), concept.id
    assert stored.exists()
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        response = client.post("/forms/assets/delete-files", data={
            "csrf": token, "asset_ids": [concept.id], "back": "type=image&q=x&evil=1"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/assets?type=image&q=x#library"
        page = client.get(response.headers["location"]).text
        assert "Deleted 1 file." in page
    db.expire_all()
    assert db.get(Asset, concept_id) is None and not stored.exists()
    assert not db.scalars(select(AssetEdit)).all()
    record = db.get(DriveImport, "d-9")
    assert record.status == "deleted" and record.asset_id is None


def test_a_file_posts_use_is_kept_for_them_but_leaves_the_library(db, settings, facts):  # noqa: F811
    video, terrace, concept, copy = library(db, settings)
    used = ingest(db, settings, jpeg((60, 120, 90), (1080, 1080)), "partition-wall-office.jpg", rights="public",
                  tags={"product": "Retractable Partition Walls"})
    db.commit()
    post = draft(db, "instagram", "Office", [used.id], facts.id)
    campaign = MediaCampaign(name="Weekly projects", brief={"reference_ids": [used.id, video.id], "end_card_asset_id": ""})
    db.add(campaign)
    db.commit()
    result = delete_assets(db, settings, [used.id, terrace.id], "admin")
    # The terrace photo has a copy made from it, so it's kept too.
    assert result == {"deleted": 0, "kept": 2, "campaigns": ["Weekly projects"], "storage_failed": 0}
    db.expire_all()
    kept = db.get(Asset, used.id)
    assert kept.removed_at and kept.removed_by == "admin" and LocalStore(settings).path(kept.storage_key).exists()
    assert db.get(MediaCampaign, campaign.id).brief["reference_ids"] == [video.id]
    assert used.id in post.asset_ids
    # Never chosen for new posts.
    assert used.id not in [a.id for a in library_media(db, product="company")]
    brief = CreativeBrief(campaign="x", prompt="Our office walls", operation="real", platforms=["instagram"],
                          facts_id=facts.id, rights="public", reference_ids=[used.id])
    with pytest.raises(ValueError, match="was deleted from the asset library"):
        validate_sources(db, brief)
    with TestClient(create_app(settings)) as client:
        assert "partition-wall-office.jpg" not in names(client.get("/admin/assets").text)
        assert "partition-wall-office.jpg" not in client.get("/admin/create").text
        deleted = client.get("/admin/assets?source=deleted").text
        assert names(deleted) == ["partition-wall-office.jpg", "pergola-terrace.jpg"]
        assert "kept for the posts that use it" in deleted and "Restore ticked files" in deleted
    assert restore_assets(db, [used.id]) == 1
    db.expire_all()
    assert db.get(Asset, used.id).removed_at is None
    assert used.id in [a.id for a in library_media(db, product="company")]


def test_editors_cannot_delete_library_files(db, settings, monkeypatch):
    video, *_ = library(db, settings)
    settings.auth_mode = "crm"

    async def identity(settings, cookie):
        return "crm:7", "Editor", "editor"

    monkeypatch.setattr("social.crm_access.identity", identity)
    with TestClient(create_app(settings), cookies={settings.crm_session_cookie: "editor"}) as client:
        response = client.post("/forms/assets/delete-files", data={"asset_ids": [video.id]})
        assert response.status_code == 403
    assert db.get(Asset, video.id) is not None


def test_drive_import_reports_why_files_failed_and_names_videos_without_extensions(db, settings):
    configure(db, rights="public")
    clip = footage()
    huge = file_info("huge", size=str(settings.max_upload_bytes + 1))
    files = [
        file_info("clip", clip, name="Install day 1", mimeType="video/quicktime"),
        file_info("heic", b"not really heic", name="IMG_0001.HEIC", mimeType="image/heic"),
        huge,
    ]
    drive = FakeDrive(files, bodies={"clip": clip, "heic": b"not really heic"})
    original = drive.download

    def download(file, limit):
        if int(file["size"]) > limit:
            from social.drive_import import DriveError, too_large

            raise DriveError(too_large(int(file["size"]), limit))
        return original(file, limit)

    drive.download = download
    poll(db, settings, drive)
    imported = db.get(Asset, db.get(DriveImport, "clip").asset_id)
    assert imported.original_name == "Install day 1.mov" and imported.media["mime"] == "video/quicktime"
    assert "HEIC photos aren't supported" in db.get(DriveImport, "heic").error
    assert "over the import size limit of 250.0 MB" in db.get(DriveImport, "huge").error
    with TestClient(create_app(settings)) as client:
        page = client.get("/admin/assets").text
        assert "1 video" in page and "Files that couldn't be imported (2)" in page
        assert "HEIC photos aren&#39;t supported" in page


def test_a_deleted_drive_file_is_not_imported_again_until_asked(db, settings):
    configure(db, rights="public")
    drive = FakeDrive([file_info("a")])
    poll(db, settings, drive)
    asset_id = db.get(DriveImport, "a").asset_id
    delete_assets(db, settings, [asset_id], "admin")
    due(db)
    poll(db, settings, drive)
    assert db.get(DriveImport, "a").status == "deleted" and drive.downloads == ["a"]
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        assert "Deleted from the library (1)" in client.get("/admin/assets").text
        response = client.post("/forms/google-drive/files/a/import-again", data={"csrf": token},
                               follow_redirects=False)
        assert response.status_code == 303
        assert client.post("/forms/google-drive/files/missing/import-again", data={"csrf": token}).status_code == 404
    db.expire_all()
    poll(db, settings, drive)
    record = db.get(DriveImport, "a")
    assert record.status == "imported" and db.get(Asset, record.asset_id) is not None


def test_post_copies_never_crowd_real_files_out_of_the_search(db, settings, monkeypatch):
    video, terrace, concept, copy = library(db, settings)
    for index in range(3):
        ingest(db, settings, jpeg((index, 50, 50)), f"copy-{index}.jpg", rights="public",
               tags={"rendition": {"platform": "instagram", "original_asset_id": video.id}})
    db.commit()
    monkeypatch.setattr(media_search, "LIBRARY_LIMIT", 2)
    assert [a.id for a in library_media(db, product="company")] == [video.id]


def test_the_shortlist_keeps_room_for_videos():
    records = [{"asset_id": f"p{i}", "kind": "photo", "name": "wall", "product": "", "description": "",
                "visible_text": []} for i in range(SEARCH_LIMIT + 50)]
    records += [{"asset_id": f"v{i}", "kind": "video", "name": "IMG_0001.MOV", "product": "", "description": "",
                 "visible_text": []} for i in range(5)]
    kept = shortlist("Installing a wall", records)
    assert len(kept) == SEARCH_LIMIT and sum(r["kind"] == "video" for r in kept) == 5


def test_a_video_idea_says_why_there_is_no_footage(db, settings, facts, logo):  # noqa: F811
    ingest(db, settings, jpeg((180, 160, 140)), "partition-wall-finished.jpg", rights="public")
    ingest(db, settings, footage(), "partition-wall-install.mp4", rights="not_public")
    db.commit()
    with TestClient(create_app(settings)) as client:
        page = plan(client, facts, prompt="Installation process of our partition walls").text
    assert "Photos shown as stills, in viewing order" in page
    assert "The asset library has no videos this post can use (1 is marked Not public" in page
    assert "the library has no videos this post can use" in page


def test_library_picks_are_the_media_of_a_real_post(db, settings, facts, logo):  # noqa: F811
    first = ingest(db, settings, jpeg((150, 110, 80)), "deck-one.jpg", rights="public")
    second = ingest(db, settings, jpeg((80, 110, 150)), "deck-two.jpg", rights="public")
    db.commit()
    with TestClient(create_app(settings)) as client:
        create = client.get("/admin/create").text
        assert 'id="idea-library"' in create and "Or choose from the asset library" in create
        assert 'name="reference_ids" value="%s"' % first.id in create
        response = plan(client, facts, prompt="Our new deck", reference_ids=[first.id, second.id], upload_use="real")
        assert response.status_code == 200, response.text
        form = plan_form(response.text)
        assert form["operation"] == ["real"] and set(form["real_media"]) <= {first.id, second.id}
        assert form["reference_ids"] == [first.id, second.id] and form["upload_use"] == ["real"]
        # Without a choice of use, library picks guide AI media as they did before.
        ai = plan(client, facts, prompt="A deck at sunset", format="image", reference_ids=[first.id])
        assert ai.status_code == 200 and plan_form(ai.text)["upload_use"] == ["reference"]
        assert "Adjust anything before generating" in ai.text

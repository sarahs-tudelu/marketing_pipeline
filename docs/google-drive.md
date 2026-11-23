# Automatic Google Drive importing

The Asset library can watch one Google Drive folder, optionally including its subfolders. A separate job in the existing worker copies supported original images, video and audio through the normal ingestion path into the configured local or S3/Spaces storage and creates their library records. It uses the official Drive API with the `drive.readonly` scope. No Drive files are edited or deleted.

## Connect the folder

1. Enable **Google Drive API** in the Google Cloud project used by the importer.
2. Configure a service account credential on the server. Either reuse `GOOGLE_APPLICATION_CREDENTIALS` if that account is appropriate, set `GOOGLE_DRIVE_CREDENTIALS_FILE` to a mounted credentials file, or set `GOOGLE_DRIVE_CREDENTIALS_JSON` to a Google Secret Manager reference containing the dedicated service account JSON. Keep private keys out of the repository and browser forms. For the Droplet, the JSON secret option uses the existing Secret Manager bootstrap and needs no additional volume mount.
3. Share the desired Drive folder with that service account's email as **Viewer**, with downloading permitted. Organization sharing policies must allow this. Domain-wide delegation is not required for a folder shared directly with the account. Shared drives are supported when the account can access the selected folder.
4. Deploy the code, run `alembic upgrade head`, and restart both web and worker. This migration adds `drive_sources` and `drive_imports`; it does not rewrite existing assets.
5. Open **Asset library → Automatic Google Drive imports**. The service account email appears there when its credentials are configured. Paste the folder link, choose the media source, usage rights, owner and optional tags, then select **Save and start importing**. For a folder of one product, tag it, such as `{"product_family": "pergolas"}`, using a product ID (`canopies`, `walkways`, `pergolas`, `sunshades`, `carports`, `drive_through`, `partition_walls`) or another of its Builder names. Campaigns choose media by this tag, and Pinterest sorts Pins into product boards by it.

Example secret reference, with `SECRET_BACKEND=google`:

```dotenv
GOOGLE_DRIVE_CREDENTIALS_JSON=gsm://projects/YOUR_PROJECT_ID/secrets/tudelu-drive-service-account/versions/1
GOOGLE_DRIVE_POLL_SECONDS=300
GOOGLE_DRIVE_BATCH_SIZE=10
```

Credentials are selected in this order: explicit JSON, explicit Drive credentials file, `GOOGLE_APPLICATION_CREDENTIALS`, then application-default credentials. A user OAuth credential can also work if it already has the Drive read-only scope and a refresh token, but this app does not add a browser OAuth connection flow. Credentials for Google Secret Manager alone do not prove Drive access.

Use **Pause imports**, **Resume imports**, or **Check for files** in the library. These controls queue work for the background worker. Reload the library to see its latest progress and file errors. No imports occur until a source is saved and enabled. Google Drive importing is separate from the post automation switch and does not create or publish posts.

After import, choose **Edit details** beside an asset to update its usage rights, owner and other metadata. The importer leaves those edits intact while the Drive file and its import metadata are unchanged. Changed Drive content or folder defaults can produce a separate new asset, using the import settings. Library edits do not change the original Drive file.

## Source details and mixed folders

**Not public** is the default usage-rights choice. Choose Public only for media authorized for publication. Folder-wide source details apply to every imported file. AI sources require a generator/model and the actual generation/edit prompt; use per-file source records when these differ.

For a folder containing mixed company/AI media, choose **Mixed sources (per-file source records)**. Add a JSON file beside each media file with the exact media filename plus `.json`, such as `showroom.jpg.json`. The record overrides folder defaults. Every record must explicitly specify its source. Example:

```json
{
  "source": "company",
  "rights": "not_public",
  "owner": "Tudelu",
  "tags": {"product_family": "partition_walls", "campaign": "showroom"}
}
```

An AI record adds `"source": "ai_generated"` or `"ai_edited"`, plus `generator` and `prompt`. Real customer content needs `tags.real_customer_content: true`, `customer_permission: true` and `permission_reference`. Optional `rights_expire_at` accepts an ISO timestamp. These records contain metadata only: do not add `filename`, storage paths or credential fields. Missing, duplicate, invalid or larger-than-64-KiB records leave their media in an error state until corrected.

## Processing behavior

- Pagination and pending files are saved in the database. Work resumes after restarts, at most `GOOGLE_DRIVE_BATCH_SIZE` entries per worker interval. Completed scans wait `GOOGLE_DRIVE_POLL_SECONDS` before starting again. Large libraries therefore take longer than five minutes for a full pass.
- Each Drive file is tracked by its ID, content fingerprint and source metadata. Unchanged files are not downloaded again. Different Drive file IDs are separate source records even when their bytes match; checksum-named storage reuses identical bytes.
- Changed bytes or source metadata create a new library asset. Existing asset records and posts keep their original media and rights. Renaming a file alone does not create another asset.
- Original bytes are verified against Drive's size and MD5 checksum when supplied, then preserved by the usual ingestion path. `MAX_UPLOAD_BYTES` still applies; original media is not transcoded.
- Documents, folders and shortcuts are not media assets. Subfolders are traversed only when selected; shortcuts are not followed. Unsupported or invalid image/video/audio files appear as import errors. Videos and audio named without an extension in Drive get one from their Drive type (for example `.mov` for QuickTime), and M4V videos are read as MP4. HEIC photos aren't supported; export them as JPEG.
- A failure on one media file does not stop other files; it's tried again on every scan. Access failures and expired listing tokens restart the scan after the configured delay; previously imported files are skipped. Removing files from Drive, pausing imports or changing the watched folder never deletes library assets.
- The Drive section of the asset library shows how many photos, videos and audio files were imported, how far a scan in progress has got, and **Files that couldn't be imported** with each file's reason (sanitized: never provider responses or credentials) and a count per reason. A file over `MAX_UPLOAD_BYTES` (250 MB by default) says how large it is; put a smaller export in the folder or raise the limit. **What's in the library**, above the library list, counts photos, videos and audio by source and shows how many are Not public (left out of public posts) or not described yet.
- Deleting an imported file in the asset library stops the next scans from importing it again. **Deleted from the library** lists those files with **Import again**, which brings one back on the next scan.
- The existing worker singleton lock and per-job `max_instances=1` prevent overlapping import workers. Optimistic source revisions let settings changes and pause actions supersede a worker's older configuration.

Implementation references: [Drive file search](https://developers.google.com/workspace/drive/api/guides/search-files), [original file downloads](https://developers.google.com/workspace/drive/api/guides/manage-downloads), [shared drive support](https://developers.google.com/workspace/drive/api/guides/enable-shareddrives), [service account authorization](https://developers.google.com/identity/protocols/oauth2/service-account).

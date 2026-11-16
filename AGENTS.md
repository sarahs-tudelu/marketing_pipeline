# Repository workflow

Implement and verify changes in the local workspace, commit them, and push to GitHub `origin/main`.

The user owns deployment. Do not pull or copy application code onto the server, build production images, run production migrations, or restart services. Do not deploy unless the user explicitly changes this instruction. The user will pull `main` and update the running services.

When the user requests reconciliation of a dirty server checkout, inspect it and preserve conflicting work in a named stash including untracked files. Reconcile any unique source changes into GitHub from the local workspace. Leave the server's HEAD unchanged and its checkout ready for the user's pull; preserve ignored environment files and runtime data. Do not apply an old server stash over newer source without reviewing the differences.

GitHub `origin/main` is the source of truth for releases. Do not upload a Windows working tree to the server as a release. Follow `.gitattributes` for line endings and keep secrets out of commits.

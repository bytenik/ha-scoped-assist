# Publishing Scoped Assist

The integration is stored at the repository root. Keep `content_in_root: true`
in `hacs.json` so HACS installs it into `custom_components/scoped_assist/` and
existing Git submodule installations continue to use the same layout.

## Release

1. Update `version` in `manifest.json` and document any compatibility changes
   in the README. Tags use the same version with a `v` prefix.
2. Merge the changes into `main` and wait for all jobs in **Validate** to pass:
   HACS, Hassfest, and the Home Assistant smoke test. HACS must pass with no
   ignored checks.
3. Create a full GitHub release from that validated commit, for example:

   ```sh
   gh release create v0.2.1 --target COMMIT_SHA --title "Scoped Assist 0.2.1" --notes-file release-notes.md
   ```

HACS downloads the integration from the release's source tree. A separate ZIP
asset is unnecessary. Never create a tag pointing at an unvalidated commit.

## HACS default catalog

After publishing the release, fork `hacs/default` under the maintainer's personal
GitHub account. Create a branch from `master`, add
`bytenik/ha-scoped-assist` alphabetically to `integration`, and submit an editable
pull request using its checklist. Include links to the new release and the
successful HACS and Hassfest runs.

Follow the [HACS publishing requirements](https://www.hacs.xyz/docs/publish/include/).
Users can install through HACS's custom repositories while catalog review is
pending.

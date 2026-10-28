# COPR to GitHub Release Action

A GitHub Action that uploads COPR-generated RPM assets to GitHub Releases based on git tags.

## Overview

This action monitors [Fedora COPR](https://copr.fedorainfracloud.org/) builds for a given package, waits for them to complete, and uploads the resulting RPM artifacts as GitHub Release assets. It supports multi-architecture builds and can process individual tags or all tags in a repository.

COPR builds are aggregated per chroot across all builds of the same package version: a build that failed in some chroots still contributes the chroots it succeeded in, and resubmitting only the failed chroots completes the version. For each chroot, the most recently finished successful build is used.

A release is never published with missing assets:
- Every chroot a version was built for (succeeded or failed in any of its builds) must have succeeded in at least one build of that version. Chroots that are no longer enabled in the COPR project are not required, so a chroot that cannot be fixed can be disabled there. Skipped chroots are not required.
- Every RPM that a succeeded chroot lists in its results must be downloadable. Results of a just-finished build that are not published yet are re-checked every 30 seconds for up to 5 minutes.

Otherwise that tag is not released (and an existing release is not clobbered) and the action fails, naming the chroots or assets at fault. After resubmitting the failed chroots in COPR, re-run the action with `clobber-assets: true`. COPR download URLs that redirect (e.g. projects migrated to Pulp storage) are followed.

## Testing

```bash
pip install requests
python -m unittest -v
```

## Usage

```yaml
- uses: karellen/copr-to-gh-release@main
  with:
    copr-owner-name: my-copr-user
    copr-project-name: my-project
    copr-package-name: my-package
```

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `copr-owner-name` | yes | | COPR owner name |
| `copr-project-name` | yes | | COPR project name |
| `copr-package-name` | yes | | COPR package name |
| `tag-to-version-regex` | no | `""` | Regex that extracts an RPM version from the tag (group 1) |
| `tag` | no | `""` | Specific tag to process (processes all tags if empty) |
| `fetch-tags` | no | `true` | Fetch tags from remote before processing |
| `clobber-assets` | no | `false` | Re-upload assets for releases that already exist |
| `no-ignore-epoch` | no | `false` | Consider RPM epoch in version matching |
| `wait-build` | no | `true` | Wait for pending/running COPR builds to complete |

## Example

```yaml
name: Release
on:
  push:
    tags:
      - my-package-*
jobs:
  release:
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    steps:
      - uses: karellen/copr-to-gh-release@main
        with:
          copr-owner-name: karellen
          copr-project-name: my-project
          copr-package-name: my-package
          tag-to-version-regex: '^my-package-(\d.+)$'
          tag: ${{ github.ref_name }}
          clobber-assets: true
          wait-build: true
```

## License

Apache License 2.0

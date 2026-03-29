# COPR to GitHub Release Action

A GitHub Action that uploads COPR-generated RPM assets to GitHub Releases based on git tags.

## Overview

This action monitors [Fedora COPR](https://copr.fedorainfracloud.org/) builds for a given package, waits for them to complete, and uploads the resulting RPM artifacts as GitHub Release assets. It supports multi-architecture builds and can process individual tags or all tags in a repository.

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

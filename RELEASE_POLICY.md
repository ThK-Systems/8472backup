# Release Policy

8472backup is in early development and has no usable backup release yet. A release is made only when the project owner approves it and the release criteria below are met.

## Versioning

Use semantic versions in the form `<major>.<minor>.<patch>`:

- **Major:** incompatible changes to supported behavior or persistent formats.
- **Minor:** compatible user-visible functionality.
- **Patch:** compatible bug fixes and documentation corrections.

Release tags use the form `v<major>.<minor>.<patch>`. Tags are immutable. The package version is derived from version-control metadata through `setuptools-scm`; verify the built package version against the intended tag before publishing.

## Branches

`master` is the integration branch. Short-lived feature and fix branches may be used. Create a maintenance branch only when a released version needs fixes while `master` has moved on:

```text
maint/<major>.<minor>
```

Maintenance branches accept focused bug fixes only. Fixes should be applied to `master` as well. Do not merge maintenance branches wholesale into `master`.

## Release criteria

Before tagging a release:

- [ ] The project owner has approved the release contents.
- [ ] The changelog, package metadata, and user-facing documentation identify the intended version.
- [ ] Relevant tests, Ruff checks, and mypy checks pass.
- [ ] Security-sensitive changes and persistent-format changes have received explicit review.
- [ ] The supported installation and upgrade procedures have been verified for the release artifacts being published.
- [ ] Release notes describe important changes, known limitations, and any required operator action.

Publish only artifact formats that have been built and verified for that release. Do not imply that an artifact or automated release workflow exists before it has been implemented and checked.

## Product and licensing policy

The project will remain FOSS under AGPLv3. It will not use a freemium model or gate product functionality behind paid tiers. Commercial licenses or support offerings may be sold; their terms are separate from the release process and must not reduce the rights granted by the project's FOSS license.

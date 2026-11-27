# Contributing to 8472backup

8472backup is an early-stage client/server backup project. Contributions should follow the authoritative requirements in [`spec/10-8472backup-Spec.md`](spec/10-8472backup-Spec.md). Read only the specification parts relevant to your change. The project owner retains responsibility for requirements, architecture, review, and acceptance.

## Before proposing a change

For a substantial change, open a discussion or issue first so its fit with the project goals and unresolved design decisions can be reviewed. Keep pull requests focused and do not silently turn an open design question into a project requirement.

## Development environment

- Use Python 3.12 or later.
- Keep runtime dependencies small. Any external runtime dependency must be available as a Debian 13 (Trixie) package.
- The project does not use `pip` as its installation method. No development dependency installation method is specified here; follow the repository's current development setup.

Run the relevant checks before submitting a change:

```bash
python3.12 -m pytest
ruff check .
ruff format --check .
mypy .
```

Add focused tests for new behavior and relevant failure cases. Document user-visible changes in the `[Unreleased]` section of `CHANGELOG.md`.

## Design and implementation

Prefer small, reviewable changes and simple explicit code. Preserve the architecture and the project's client/server trust boundary. Do not add speculative abstractions, dependencies, cryptographic constructions, persistent formats, or security guarantees without explicit review.

No AI service may be required to build, test, or run the project.

## Commits and pull requests

Use clear, focused commits. Conventional prefixes are encouraged, for example:

- `fix:` for bug fixes
- `feat:` for features
- `docs:` for documentation
- `refactor:` for internal changes
- `test:` for tests

Pull requests should explain what changed, why it is needed, and which checks were run. The project owner reviews and accepts changes.

## Reporting issues

Report ordinary bugs and feature requests through the project's issue tracker. Include the affected commit or version, operating system, Python version, steps to reproduce, and relevant output. Do not include secrets, backup contents, or sensitive personal data.

Do not report security vulnerabilities in public issues. Follow [`SECURITY.md`](SECURITY.md) instead.

## License

The project is licensed under the GNU Affero General Public License, version 3 (AGPLv3). Contributions are provided under the same license.

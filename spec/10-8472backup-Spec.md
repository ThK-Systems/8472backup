# 8472backup — Project Specification for Codex

Version: 0.67 — Use trusted server time for snapshot protection, 2026-11-27

Status: requirements and agreed architectural direction; early design phase. No production-ready implementation is implied.

This file is the versioned entry point for the authoritative 8472backup specification. The specification consists of this entry point and the six companion parts below. Read this file first, then consult the relevant parts for the task at hand.

The numbered chapters in the companion parts retain their original numbering. The parts together define the requirements and agreed architectural direction. This entry point controls the current specification version and the scope of the set; it does not replace or summarize chapter requirements. If requirements conflict, resolve them explicitly with the project owner before implementation. MUST denotes a requirement, SHOULD a recommendation, OPEN an unresolved decision, and DEFERRED work outside the current implementation scope.

## Specification parts

- [Purpose, Scope, and Architecture](10a-PurposeScopeArchitecture.md): chapters 1–6
- [Cryptography, Objects, and Packs](10b-CryptographyObjectsAndPacks.md): chapters 7–8
- [Snapshots, Commit, and Retention](10c-SnapshotsCommitAndRetention.md): chapters 9–12
- [Communication, Configuration, and Operations](10d-CommunicationConfigurationAndOperations.md): chapters 13–17
- [Verification and Development](10e-VerificationAndDevelopment.md): chapters 18–20
- [Future Work and References](10f-FutureWorkAndReferences.md): chapters 21–24

All parts must carry the version declared here. Update this version and the version in every part together when the specification changes. The [English glossary](99-Glossary.md) defines terminology; the [configuration roadmap](70-ConfigurationRoadmap.md) records planned settings and unresolved candidates. Neither replaces or amends this specification set.

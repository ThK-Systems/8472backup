# 8472backup — Project Specification for Codex

Version: 0.34 — Provisional data without a separate staging structure, 2026-11-19

Status: requirements and agreed architectural direction; early design phase. No production-ready implementation is implied.

This file is the versioned entry point for the authoritative 8472backup specification. The specification consists of this entry point and the six companion parts below. Read this file first, then consult the relevant parts for the task at hand.

The numbered chapters in the companion parts retain their original numbering. The parts together define the requirements and agreed architectural direction. This entry point controls the current specification version and the scope of the set; it does not replace or summarize chapter requirements. If requirements conflict, resolve them explicitly with the project owner before implementation. MUST denotes a requirement, SHOULD a recommendation, OPEN an unresolved decision, and DEFERRED work outside the current implementation scope.

## Specification parts

- [Purpose, Scope, and Architecture](01a-PURPOSE-SCOPE-ARCHITECTURE.md): chapters 1–6
- [Cryptography, Objects, and Packs](01b-CRYPTOGRAPHY-OBJECTS-AND-PACKS.md): chapters 7–8
- [Snapshots, Commit, and Retention](01c-SNAPSHOTS-COMMIT-AND-RETENTION.md): chapters 9–12
- [Communication, Configuration, and Operations](01d-COMMUNICATION-CONFIGURATION-AND-OPERATIONS.md): chapters 13–17
- [Verification and Development](01e-VERIFICATION-AND-DEVELOPMENT.md): chapters 18–20
- [Future Work and References](01f-FUTURE-WORK-AND-REFERENCES.md): chapters 21–24

All parts must carry the version declared here. Update this version and the version in every part together when the specification changes. The [English glossary](99-GLOSSARY.md) defines terminology; the [configuration roadmap](03-CONFIGURATION-ROADMAP.md) records planned settings and unresolved candidates. Neither replaces or amends this specification set.

<!-- second-brain-repository-bootstrap:v1 -->

# Repository Agent Guide

This file owns the project's instructions for maintainers, contributors and coding
agents. It must be usable without a maintainer's private repositories, personal
accounts or machine paths. Personal agent configuration may supplement this guide
in its owner's environment; it does not replace project policy or grant access.

## Task-Start Synchronization

Before repository inspection, planning or edits:

1. Run `.codex/bin/repository-preflight` from inside this repository. It uses
   Git only, fetching and fast-forwarding a clean checkout's tracking branch.
2. Preserve dirty, detached, untracked or diverged work. Never reset, clean,
   force, auto-stash or overwrite work to make the preflight pass. Use a clean
   checkout based on the freshly fetched primary branch when appropriate.
3. Read this current guide and the project's contribution/release instructions.
   If working on a feature/release branch, update primary in its clean worktree
   first and follow the project's policy for incorporating it. The preflight
   checks the current branch; it does not prove that primary is incorporated.

## Project Instructions

Maintain the project's purpose, setup, architecture, validation commands,
contribution/review/branch policy, release/deployment authorization, privacy and
source-of-truth rules here or in linked project-owned guides. Document supported
contributor setup and privileged maintainer roles separately. Keep personal reply
preferences, private context and maintainer-specific credential tooling outside
this shared contract. Do not assume direct-to-primary delivery for a shared
project; follow its actual workflow.

## Project

This repository adapts the pinned AirPlay-to-DLNA relay to standalone legacy
Sonos players. The README owns installation, confirmed models, and sync limits.

- Keep relay.env.example synthetic and keep local relay.env, downloaded
  upstream sources and extracted receiver images out of Git.
- Use the pinned relay checksum in install-pi. Review the wrapper's
  compatibility before changing that pin.
- Check shell syntax and Python compilation for code changes. For installer
  changes, test against the pinned ARM64 image on Linux. For playback changes,
  verify WAV/FLAC framing and the standalone-speaker and stream-ownership
  checks before exercising a real speaker.
- Keep timing results tied to the test method. Multiroom timing can vary
  during a session, including across track changes.

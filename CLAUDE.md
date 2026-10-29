# CLAUDE.md: claude-session

claude-session: Runs and supervises Claude Code sessions (terminal-agnostic). It is a Layer 1 (primitive) tool in the awkit system and must work fully
standalone: no harness, bus, shared library or shared contracts package is required.

**Naming.** Tools have no shared prefix; see design guide §1 naming rules.

## Read first

- `../awkit-docs/architecture.md`: this tool's §5.1 entry and its §20 entry (plus the sections
  listed in `docs/design.md`).
- `../awkit-docs/design-guide.md`: conventions every awkit tool follows.
- `docs/design.md`: this tool's design doc, constraints and open topics.

## Rules

- Open source: nothing confidential. No company names, hostnames, ticket project keys, custom-field
  IDs, repo/org names or people's names in code, docs, tests or fixtures. Organization specifics
  belong in the user's local config.
- Never import sibling repos. Read their docs and schemas by path only; copying a sibling's schema
  files into tests, pinned to a version, is allowed.
- No upward awareness: this tool never knows about, or behaves differently for, the tools that call
  it. Behavior may differ only by explicit arguments.
- Interface first: write `docs/interface.md` and `schemas/` before implementation.
- Semver. A breaking interface change is, before 1.0, a `feat` commit whose title names the break (never `!` before 1.0), and from 1.0 on a `!` commit; the title becomes the changelog line (details go in the PR body). Either way, update `awkit-docs/interface-registry.md`. Never hand-edit CHANGELOG.md or the version.
- Run `make verify` before every commit.
- All subprocess and HTTP calls go through `src/claude_session/gateway.py` so tests can substitute fakes.
- Never run destructive git commands (reset, checkout --, clean, restore, forced push) and never
  kill processes by pattern; signal only exact verified PIDs this tool started, plus the process
  groups of hooks this tool spawned.
- Minimize dependencies; justify each one in `docs/design.md`.

## Workflow

1. The design session is done: `docs/design.md` (decisions, spike results), `docs/interface.md` and `schemas/` are the contract.
2. Implement test-first, following `docs/plan.md` and the repo's GitHub issues.
3. Claude Code behavior this tool relies on is covered by `live` tests (`make live`, never in CI); run them after every Claude Code upgrade.

## Repository workflow

- Work in a git worktree on a branch named `<type>/<short-slug>`, never in the main checkout (design guide section 17).
- PR titles are Conventional Commit headers; they become the squash commit, the version bump and the changelog line (architecture section 18.3). Do not use `!` before 1.0 (a breaking change before then is a `feat` whose title names the break).
- Never use `--no-verify`. Check PR titles, bodies and comments with `guard text` where the private-content guard is installed (architecture section 18.7).
- Never merge a PR without the maintainer's explicit instruction for that PR; merges go through `awkit-ci`'s `scripts/pr-merge` (architecture section 18.6).
- Never hand-edit `CHANGELOG.md` or the version in `pyproject.toml`; the release job owns them.

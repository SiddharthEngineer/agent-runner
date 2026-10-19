# Plan: engineerfamily agent folder layout (AGENTS.md, agents/, interactive settings)
**Status:** ❌ Not started
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19; docs and editor settings only)
**Repo:** SiddharthEngineer/engineerfamily (`~/src/engineerfamily`, base `main`, merge commits)
**Depends on:** ef-validate-ci (needs `scripts/validate.sh` and CI)

## Goal
engineerfamily gets the same layout as Strelitzia (design D4): `AGENTS.md` is the auto-loaded repo guide, `agents/` holds the
ROADMAP, plans and DEPLOY doc, and `.claude/` holds only what Claude Code loads (the commands come in ef-runner-config, and
`settings.json` here). After this, new engineerfamily plans live in `engineerfamily/agents/plans/`, not in agent-runner.

## Context
- Task 0 check: `scripts/validate.sh` and `.github/workflows/ci.yml` exist on `origin/main` (ef-validate-ci). If not, `blocked`.
- `CLAUDE.md` (19 lines): Deploys + Rules. `README.md:30-112` ("Release And Deployment Model", "Tagging Rules", "Deploying",
  "How the workflow works") and `README.md:227-245` ("VPS Layout") are the deploy docs to move. `README.md:1-29` stays.
- Strelitzia's versions to mirror (read-only): `~/src/Strelitzia/AGENTS.md` (34 lines: Map, Validate, PIV loop, Rules),
  `agents/ROADMAP.md` header and table columns, `.claude/settings.json` (interactive allow/deny).
- Claude Code auto-loads `CLAUDE.md`. Keep a `CLAUDE.md` whose only content is `@AGENTS.md`, so any tool that reads either file gets the same text.
- The engineerfamily Parts done so far are tracked in agent-runner's `agents/ROADMAP.md` (A5 ef-validate-ci). Don't copy their
  history; link to it.

## Tasks
### Task 1: AGENTS.md
**Runs:** unattended
**Files:** AGENTS.md (new), CLAUDE.md (modify)
**What:** `AGENTS.md`: a one-paragraph summary, Map (services/*, nginx confs, compose files, Makefile, scripts/, agents/), Validate
(`scripts/validate.sh`), the PIV loop (same wording as Strelitzia, base `main`), and Rules: today's `CLAUDE.md` rules, plus "prod is the
user's" and "never edit inside `/srv`". `CLAUDE.md` becomes the single line `@AGENTS.md`. Keep it under 45 lines.
**Validate:** `wc -l AGENTS.md` → ≤ 45; `cat CLAUDE.md` → `@AGENTS.md`; every rule line from the old `CLAUDE.md` appears in `AGENTS.md`
(check with `git show HEAD~1:CLAUDE.md`).
**Status:** ❌

### Task 2: agents/ folder
**Runs:** unattended
**Files:** agents/ROADMAP.md (new), agents/DEPLOY.md (new), agents/plans/done/.gitkeep (new), README.md (modify)
**What:** `agents/DEPLOY.md` gets README's deploy sections (lines in Context) moved verbatim, with headings demoted one level and
a short intro. README keeps a 3-line "Deploying" pointer to it. `agents/ROADMAP.md`: Strelitzia's header and legend, an empty Active
table, Manual steps, Decisions ("2026-10-19 (user): runner programme PRs auto-merge on green CI"), and a line saying earlier
engineerfamily runner Parts are tracked in agent-runner's ROADMAP (link).
**Validate:** `grep -c 'make preprod-deploy' agents/DEPLOY.md` ≥ 1; `grep -n 'agents/DEPLOY.md' README.md` → the pointer; `scripts/validate.sh` → all PASS.
**Status:** ❌

### Task 3: interactive settings
**Runs:** unattended
**Files:** .claude/settings.json (new)
**What:** Allow: `scripts/validate.sh`, read-only git, `git add|commit|switch|fetch|pull`, `git push -u origin HEAD`,
`gh pr create|view|checks`, `docker compose … config -q`, `make prod-changes`, `make preprod-deploy:*`, `agent-runner queue|gate|inbox:*`.
Deny: `make deploy-prod:*`, `make tag-prod:*`, `make strelitzia-deploy:*`, `make deploy-preprod:*`, `git push --force:*`,
`git push origin main:*`, `docker volume:*`, `sudo:*`, `Read(**/.env)`, `Read(services/nginx/.htpasswd*)`.
**Validate:** `python3 -c "import json;d=json.load(open('.claude/settings.json'));assert 'Bash(make deploy-prod:*)' in d['permissions']['deny']"` → no error.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes; the PR's CI is green.

## Human test steps (preprod)
None (low risk; nothing deploys).

## Decisions
1. `CLAUDE.md` stays as an `@AGENTS.md` import, so Claude Code keeps auto-loading the guide.
2. Deploy docs move instead of being copied, so there's one source.

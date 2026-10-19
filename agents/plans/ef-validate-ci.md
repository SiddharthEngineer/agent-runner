# Plan: engineerfamily validate script, PR CI, and `make prod-changes`
**Status:** ❌ Not started
**Risk:** review (adds `.github/` and a Makefile target to the deploy repo)
**Repo:** SiddharthEngineer/engineerfamily (`~/src/engineerfamily`, base `main`, merge commits). This plan lives in agent-runner until engineerfamily has its own `agents/` (Part ef-agents-layout).
**Depends on:** - (useful before the runner exists; design [shared-agent-runner](shared-agent-runner.md) D5)

## Goal
engineerfamily PRs get a green or red check: the same `scripts/validate.sh` runs locally, in agent worktrees and in CI, and checks
that every compose file resolves and every nginx conf passes `nginx -t`. `make prod-changes` shows the user what the next prod
deploy would ship (engineerfamily commits since the last prod tag, Strelitzia commits since what prod runs), each with its PR link.
Once agents merge `review` PRs, this is the human checkpoint before a prod deploy.

## Context
- `CLAUDE.md` "Rules": the two checks run by hand today (compose `config -q` and `nginx -t` in `nginx:alpine`).
- Tried on 2026-10-19 from `~/src/engineerfamily`: `docker compose --env-file .env.template -f docker-compose.base.yml -f docker-compose.<env>.yml config -q`
  passes for local, preprod and prod, even though the build contexts `../Strelitzia-{preprod,prod}` don't exist there.
  `nginx -t` passes for all three confs with `nginx.<env>.conf` mounted at `/etc/nginx/conf.d/default.conf` and an empty file at
  `/etc/nginx/.htpasswd` (preprod's `auth_basic_user_file`). No `ssl_certificate` lines exist (Cloudflare terminates TLS), so no stub cert is needed.
- `docker-compose.preprod.yml:11-15` and `docker-compose.prod.yml:8-11`: the nginx mounts to mirror.
- `.github/workflows/deploy.yml`: dispatch-only. Leave it alone.
- `Makefile:5-13` (`.PHONY`), `Makefile:20-26` (`PROD_DIR`, `STRELITZIA_PROD_DIR`), `Makefile:216-232` (tags: `vX.Y.Z` for prod,
  `vX.Y.Z-preprod` for preprod). Prod tags may not be ancestors of `origin/main` (release-branch sync, PR #37), so find the last prod
  tag with `git tag -l 'v[0-9]*' --sort=-v:refname | grep -v -- '-preprod' | head -1`, not `git describe`.
- Strelitzia history: the dev clone `~/src/Strelitzia` (`origin/trunk`); prod runs `git -C /srv/Strelitzia-prod rev-parse HEAD`.

## Tasks
### Task 1: `scripts/validate.sh`
**Runs:** interactive (needs Docker; the unattended runner doesn't build engineerfamily yet)
**Files:** scripts/validate.sh (new)
**What:** Same shape as Strelitzia's: `step <name> <cmd…>` prints `PASS`/`FAIL` and stops at the first failure. Steps:
`bash -n` on `scripts/*.sh` and `scripts/vps/*` shell files; `compose <env>` for local, preprod and prod with `--env-file .env.template`;
`nginx <env>` for each `services/nginx/nginx.<env>.conf` in `nginx:alpine` (`docker run --rm`, conf mounted read-only, an empty
`mktemp` file mounted as `/etc/nginx/.htpasswd`, removed by a `trap`). Runs from any cwd (`cd "$(dirname "$0")/.."`). `-h` prints usage.
**Validate:** `scripts/validate.sh` → 7+ PASS lines. Then break `nginx.preprod.conf` in a scratch copy (a missing `;`):
`scripts/validate.sh` → `FAIL  nginx preprod`, exit 1. Revert the change.
**Status:** ❌

### Task 2: PR CI
**Runs:** interactive (`.github/`)
**Files:** .github/workflows/ci.yml (new)
**What:** `on: pull_request` and `push: branches: [main]`; one job on `ubuntu-latest` (Docker and Compose v2 are preinstalled):
`actions/checkout@v4`, then `scripts/validate.sh`. `permissions: contents: read`. No secrets.
**Validate:** push the branch and open the PR, then `gh pr checks <pr> --repo SiddharthEngineer/engineerfamily --watch` → `validate` passes.
**Status:** ❌

### Task 3: `make prod-changes`
**Runs:** interactive (reads `/srv/Strelitzia-prod`)
**Files:** scripts/prod-changes.sh (new), Makefile (modify)
**What:** Read-only. `git fetch -q --tags origin` in this repo and in `$STRELITZIA_DEV_DIR` (default `~/src/Strelitzia`). Print
`engineerfamily: <last prod tag>..origin/main` and then `git log --oneline --no-merges` for that range. Print `Strelitzia: <prod
sha>..origin/trunk` and its log. Turn `(#NN)` or `Merge pull request #NN` in a subject into
`https://github.com/SiddharthEngineer/<repo>/pull/NN`. If a range is empty, print `nothing new`. Makefile: `prod-changes` target
calling it with `STRELITZIA_PROD_DIR`, plus `.PHONY` and a `help` line.
**Validate:** `make prod-changes` → two headed sections. The engineerfamily range starts at `v1.3.0` (the newest prod tag on
2026-10-19), and the PR links match `gh pr list --state merged`. With `STRELITZIA_PROD_DIR=/nonexistent` → a clear error, exit 1.
**Status:** ❌

### Task 4: docs
**Runs:** interactive
**Files:** CLAUDE.md (modify), README.md (modify)
**What:** In `CLAUDE.md` "Rules", replace the two hand-run commands with `scripts/validate.sh` (one line saying what it checks). In
README "Deploys", add "before a prod deploy, run `make prod-changes`".
**Validate:** `scripts/validate.sh` → all PASS; `grep -n validate.sh CLAUDE.md` → the new line.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes locally and in the PR's CI.
- `make prod-changes` prints both ranges with PR links.

## Human test steps (preprod)
1. Open the PR on GitHub: the `validate` check is green.
2. On the VPS, run `make -C ~/src/engineerfamily prod-changes`: you should see what the next `deploy-prod`/`strelitzia-deploy` would ship.
Nothing deploys: this changes no running service, so it doesn't need a preprod deploy.

## Decisions
1. Validate against `.env.template`, not `/srv` `.env` files, so CI and agent worktrees can run it without secrets.
2. `prod-changes` is a script called by make, so it can be tested and run without make.
3. The preprod `.htpasswd` is an empty stub: `nginx -t` only needs the file to exist.

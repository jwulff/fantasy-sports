# Releasing

How a `fantasy-sports` release actually gets to PyPI, and the one-time setup
it depends on. See `.github/workflows/release.yml` for the workflow this
document describes, and ADR-0008 for the budgets it enforces before
publishing.

## One-time setup (John, on pypi.org — cannot be done from the repo)

Trusted publishing means **no `PYPI_API_TOKEN` secret ever exists in this
repo.** Authentication is pure OIDC: GitHub Actions proves to PyPI which
repo/workflow/environment is running, and PyPI trusts that identity directly.
The registration for that trust has to happen on PyPI's side, by a PyPI
account with rights to the project name, before the first tag is ever pushed.

1. Sign in to <https://pypi.org> as the account that will own the
   `fantasy-sports` project (create the account/2FA first if needed — PyPI
   requires 2FA for publishing).
2. Go to <https://pypi.org/manage/account/publishing/> ("Publishing" under
   account settings) and add a **pending publisher** — this works even before
   the project exists on PyPI, which is the case today:
   - **PyPI project name:** `fantasy-sports`
   - **Owner:** `jwulff`
   - **Repository name:** `fantasy-sports`
   - **Workflow filename:** `release.yml`
   - **Environment name:** `pypi`
3. Repeat the same steps on **TestPyPI** (<https://test.pypi.org/manage/account/publishing/>,
   separate account/login from pypi.org), with **Environment name: `testpypi`**.
   This is what lets the `workflow_dispatch` dry-run path publish before the
   real release ever runs.
4. In the GitHub repo, under **Settings → Environments**, confirm `pypi` and
   `testpypi` environments now exist (PyPI's trusted-publisher registration
   does not create them — GitHub creates an environment automatically the
   first time a workflow references it, but you can also add optional
   protection rules, e.g. required reviewers, on the `pypi` one here).

Nothing else is required. No secret is ever added to the repo — the
`id-token: write` permission in the workflow is the entire auth mechanism
once the publishers above are registered.

## Dry run before the first real release

Before pushing the first `v*` tag, run the pipeline once against TestPyPI so
the tag-triggered job is not the first time it has ever executed:

```bash
gh workflow run release.yml
```

This runs the full test matrix, the ADR-0008 budget checks, builds the sdist
and wheel, smoke-tests the wheel's console scripts, and publishes to
<https://test.pypi.org/project/fantasy-sports/>. Verify with:

```bash
uv tool install --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ fantasy-sports
fantasy-sports --version
uv tool uninstall fantasy-sports
```

(The `--extra-index-url` is needed because `fantasy-sports`'s own
dependencies — `espn-api`, `typer`, etc. — are not published on TestPyPI.)

## Cutting a real release

1. **Bump the version.** Edit `pyproject.toml`: drop the `.dev0` suffix (or
   bump to the next version and re-add `.dev0` for the next dev cycle,
   depending on where the working tree is). For the first release this is
   `version = "0.1.0.dev0"` → `version = "0.1.0"`.
2. **Update the changelog.** Move the `## [Unreleased]` section in
   `CHANGELOG.md` to a dated `## [X.Y.Z] - YYYY-MM-DD` heading, and start a
   fresh empty `## [Unreleased]` above it.
3. **Commit.**
   ```bash
   git add pyproject.toml CHANGELOG.md
   git commit -m "Release: v0.1.0"
   ```
4. **Tag and push.**
   ```bash
   git tag v0.1.0
   git push origin main
   git push origin v0.1.0
   ```
   Pushing the tag is what fires `.github/workflows/release.yml`'s
   `publish-pypi` job — it does not run on an ordinary `git push origin main`.
5. **Watch the workflow.**
   ```bash
   gh run watch --exit-status
   ```
   or open the Actions tab. The `test` and `budgets` jobs must pass before
   `build` runs; `build` must pass (including the wheel smoke test) before
   `publish-pypi` runs.
6. **Verify the published package** from a clean environment:
   ```bash
   uv tool install fantasy-sports
   fantasy-sports --version   # expect 0.1.0
   fantasy --version          # the second console-script alias
   uv tool uninstall fantasy-sports
   ```
7. **Create the GitHub release** (optional but recommended — the tag alone
   satisfies the workflow trigger):
   ```bash
   gh release create v0.1.0 --generate-notes --title "v0.1.0" \
     --notes-file <(sed -n '/## \[0.1.0\]/,/## \[/p' CHANGELOG.md | sed '$d')
   ```
8. **Start the next dev cycle.** Bump `pyproject.toml` to the next version
   with `.dev0` (e.g. `0.2.0.dev0`) in a follow-up commit, so `main` never
   sits at a released version number.

## What CI actually checks before a release can publish

Same gates as ordinary CI (`.github/workflows/ci.yml`), run again inside
`release.yml` so a release can never skip them even if `main`'s last CI run
happened to be red for an unrelated push:

- Lint and format (`ruff check`, `ruff format --check`)
- Full offline test suite across Python 3.12 / 3.13 / 3.14, coverage enforced
- ADR-0008 budgets: dependency count, wheel size, cold-start timing
  (`scripts/check_budgets.py`, `scripts/check_startup.py`)
- The built wheel actually installs in isolation and both `fantasy-sports`
  and `fantasy` console scripts run (`--version`, `--help`)

Only after all of that passes does the publish job run, and it runs with no
credential in the repo other than the short-lived OIDC token GitHub mints for
that one job.

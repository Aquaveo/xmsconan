# Releasing the XMS libraries

How to release one of the xms C++/Python libraries built with xmsconan, for both
toolchains the products consume:

| Artifact | Destination | Toolchain | Produced by |
|---|---|---|---|
| Conan packages | `aquaveo-stable` | msvc 194 / gcc / clang | the library's CI, on the tag |
| cp310 + cp313 (+ later) wheels | devpi `aquaveo/dev` | msvc 194 (Windows) | the library's CI, on the tag |
| Conan packages | `aquaveo-vs2019` | msvc 192 | the VS2019 bridge track (step 5), by hand |
| VS2019 wheel | devpi `aquaveo/stable` | msvc 192 | the VS2019 bridge track (step 6), by hand |

This page is the *procedure*. The tools it drives are documented in
[USAGE.md](USAGE.md); in particular §10 (generated CI), §13 (`wheel-deploy`),
§16 (`xmsconan vs2019`) and §17 (credentials). It does not repeat their flag
references.

> **The VS2019 bridge is temporary.** It exists so a desktop product can move
> conan1 → conan2 while still on VS2019 (consuming `aquaveo-vs2019`), and then
> VS2019 → VS2022 with no version change (repointing to `aquaveo-stable`) — one
> variable at a time. Retire `aquaveo-vs2019`, steps 5–6 below and this page's
> VS2019 sections once the last consuming product is on VS2022.

---

## The invariant

> **Same git tag + same xmsconan version + same Conan patch series ⇒ same recipe
> revision on both remotes.**

The version number describes **source**. The toolchain lives in the Conan
`package_id` and in *which remote* a package is on — never in the version string.
There is no `7.0.12-vs2019`.

Why the xmsconan version matters: `XmsConan2File.export()` copies
`xms_conan2_file.py` into the export folder, so that file's contents feed the
recipe revision hash. If the tag pipeline generated the recipe with one xmsconan
and your workstation generates it with another whose `xms_conan2_file.py`
differs, the two remotes get **different recipe revisions for the same version**,
and a consumer with both remotes configured resolves inconsistently.

Generated CI installs `xmsconan[ci]>=<floor>` with `--upgrade` (USAGE §10.3), so
the tag pipeline runs **the newest xmsconan on devpi at that moment**. That is the
version your VS2019 build must use — read it from the tag pipeline's install log,
not from the CI file.

## Release order

Each library pins **exact** versions of its xms dependencies, and a VS2019 build
fails at graph resolution if a pinned version has no msvc 192 packages. Release
strictly in dependency order, one library fully finished (all four destinations)
before the next starts:

```
xmscore
└── xmsgrid
    └── xmsinterp
        ├── xmsextractor
        │   └── xmsconstraint
        ├── xmsmesher
        └── xmsstamper
```

- `xmsextractor`, `xmsmesher` and `xmsstamper` are independent of each other;
  `xmsextractor` goes first only because `xmsconstraint` needs it.
- **`xmsconstraint` is on GitLab** (`https://git.aquaveo.com/Aquaveo/xmsconstraint`),
  not GitHub — `gh` does not apply, and its release is a tag push (step 3).
- Releasing only one library? You still need every dependency it pins to already
  exist on **both** remotes.

## Before you start (one-time per workstation)

| Requirement | How |
|---|---|
| VS2019 with the C++ toolset | Checked by `xmsconan vs2019 setup` preflight (USAGE §16.3). |
| One virtualenv **per Python version** you build pybind for | Typically 3.10 and 3.13. The recipe builds against the interpreter running Conan (USAGE §16.8), so a 3.10 wheel needs a 3.10 venv. |
| xmsconan, **pinned and non-editable**, in every venv | `pip install "xmsconan[ci]==<version the tag pipeline used>" -i https://public.aquapi.aquaveo.com/aquaveo/dev/+simple` |
| Conan `~=2.31.0` | Comes with the `[ci]` extra. Preflight rejects anything else. |
| Credential files | `C:\Users\<user>\.config\tokens\conan` (Conan remote password, alone on one line), `C:\Users\<user>\.config\tokens\devpi` (devpi username on line 1, password on line 2), `C:\Users\<user>\.config\tokens\jfrog` (Artifactory access token, for the Artifactory UI/REST API — the steps below do not need it). Never commit or paste their contents. |
| Fresh clones of the libraries, side by side under one `<root>` | `xmsconan vs2019 build --root <root>` expects `<root>/<library>/build.toml`. |
| **Git Bash**, not PowerShell | PowerShell strips the quotes out of `--filter` JSON. |

Confirm the install is what you think it is — it must print a `site-packages`
path, not a source tree:

```bash
python -c "import os, xmsconan; print(xmsconan.__version__, os.path.dirname(xmsconan.__file__))"
```

## Procedure, per library

Two steps publish to shared infrastructure that is hard to un-publish — the Conan
upload in step 5.7 and the devpi upload in step 6.5. **Get a second person's go-ahead before
each**, stating the library, version, remote/index, exactly what will be
uploaded, and what you verified. Creating the release in step 3 triggers CI that
publishes too, so treat it the same way. Building never publishes on its own.

### 1. Prepare `master`

1. Fresh clone, or `git fetch origin` and confirm local `master` fast-forwards to
   `origin/master` (a stale local commit on `master` is a reason to re-clone).
2. Branch from **`master`** — never from a `release-*` branch:
   `git checkout -b bump/<library>-deps origin/master`.
3. In `build.toml`, bump every `xms_dependencies` version to the one just
   released on **both** remotes.
4. Regenerate and check: `xmsconan ci build.toml`, then
   `xmsconan gen --check build.toml` and `xmsconan ci --check build.toml` (USAGE
   §4.1).
5. Read the CI diff. A recently maintained repo should show pin changes only;
   list anything else (action bumps, job rewrites, a Conan pin change — which
   can move `package_id`s) in the PR/MR description.
6. Check `testing_framework`: `gtest/1.17.0` has no msvc 192 build, so a library
   on `gtest` cannot build its VS2019 testing configurations until someone builds
   and uploads gtest for msvc 192.
7. Check what will actually ship: `git log <last tag>..origin/master --oneline`.
8. PR/MR into `master`, wait for green, merge. Then
   `git checkout master && git merge --ff-only origin/master`.

### 2. Write release notes

Plain notes in `notes.md` (outside the repo, or uncommitted). Release titles are
the **bare version number**.

### 3. Cut the release

**GitHub repos — create a Release, not a bare tag:**

```bash
SHA=$(git rev-parse origin/master)        # full 40-char SHA, never a short hash
gh release create <version> --target "$SHA" --title "<version>" --notes-file notes.md
```

**GitLab (`xmsconstraint`) — push an annotated tag; there is no Release object:**

```bash
git tag -a <version> -F notes.md "$(git rev-parse origin/master)"
git push origin <version>
```

Every deploy job is tag-gated and takes its version from the tag. A tag push is
live immediately; the only correction is delete-and-repush.

### 4. Verify CI and record the recipe revision

1. Every job of the tag pipeline is green — check the **job list**, not just the
   pipeline status (an `allow_failure` job can fail inside a "passed" pipeline).
2. GitHub: each platform job attached its `<MATRIX_NAME>.tar.gz` to the Release.
3. Note the xmsconan version the pipeline installed (its `pip install` log line).
4. Record the recipe revision:

   ```bash
   conan list "<library>/<version>#*" -r aquaveo-stable
   ```

If the library's `build.toml` sets `[ci].windows_vs2019 = true` (GitLab only,
USAGE §10.2), the tag pipeline also published the msvc 192 Conan packages: skip
the build, run the two-remote check in step 5.8, then do step 6 if the library
ships a wheel.

### 5. Build and publish the VS2019 Conan packages

1. **Detached checkout of the tag**, clean tree:

   ```bash
   cd <root>/<library>
   git checkout <version>
   git describe --tags --exact-match       # must print <version>
   git status --short                      # must be empty
   ```

2. **Match the tag pipeline's xmsconan** in each venv you will use (see the
   invariant above): `pip install "xmsconan[ci]==<that version>"`.
3. **Put the 3.10 venv first on `PATH`** — preflight shells out to `conan`:
   `export PATH="/path/to/.venv310/Scripts:$PATH"`.
4. **Preflight** (also logs in to `aquaveo-vs2019`; the password goes to Conan
   through the environment, never argv):

   ```bash
   xmsconan vs2019 setup --password-file "$USERPROFILE/.config/tokens/conan"
   ```

   Expect `[OK]` for the VS2019 toolset, the conan client and the remote.

5. **Build.** From the 3.10 venv, then the pybind configuration again from the
   3.13 venv:

   ```bash
   xmsconan vs2019 build --root <root> --only <library> --version <version> \
     --python-versions 3.10 --log-dir <root>/vs2019-logs/<library>-<version>

   export PATH="/path/to/.venv313/Scripts:$PATH"
   xmsconan vs2019 build --root <root> --only <library> --version <version> \
     --python-versions 3.13 --filter '{"options": {"pybind": true}}' \
     --log-dir <root>/vs2019-logs/<library>-<version>
   ```

   **Read the `==> Summary` table** — `succeeded` / `failed` — not only the exit
   code (see Known traps). If configurations failed, do not quietly publish what
   built: decide explicitly, file an issue naming the missing configurations and
   why, and record the gap. To leave out a known-failing group deliberately,
   filter it, e.g. `--filter '{"options": {"testing": false}}'`. A partial set
   still shares the recipe revision; only binary coverage differs.

6. **Verify before publishing:**

   ```bash
   conan list "<library>/<version>#*:*" -c
   ```

   - the expected number of package ids (14 for the default matrix; fewer if
     `[matrix]` narrows it — `--preview` shows the count), every one
     `compiler.version: 192`;
   - **the recipe revision is identical to the one recorded in step 4.4.** If it
     differs, stop: the xmsconan or Conan version does not match the tag
     pipeline's, and publishing would put two recipes on the two remotes;
   - the build logs resolved each xms dependency at the expected
     `<dep>/<version>#<rrev>`.

7. **Publish** (second-person go-ahead first):

   ```bash
   xmsconan vs2019 upload --library <library> --version <version>
   ```

   Only `compiler.version=192` binaries go, and only to `aquaveo-vs2019`
   (USAGE §16.7).

8. **Confirm both remotes carry the same recipe revision:**

   ```bash
   conan list "<library>/<version>#*" -r aquaveo-stable
   conan list "<library>/<version>#*" -r aquaveo-vs2019
   ```

### 6. Build and publish the VS2019 wheel

Only for libraries with pybind bindings.

1. **Decide which Python versions belong on `aquaveo/stable`.** Check what the
   channel already carries for this library and match it (so far: cp310 only;
   CI covers newer ABIs on `aquaveo/dev`).
2. **Extract into a fresh, library-specific directory** — never a shared
   `wheelhouse/`:

   ```bash
   export PATH="/path/to/.venv310/Scripts:$PATH"
   xmsconan vs2019 build --root <root> --only <library> --version <version> \
     --python-versions 3.10 --filter '{"options": {"pybind": true}}' \
     --wheel-dir <root>/wheelhouse-<library>-<version> \
     --log-dir <root>/vs2019-logs/<library>-<version>
   ```

3. **Remove every wheel that is not going to the channel.** Extraction scans the
   whole Conan cache, so the cp313 wheel from step 5.5 lands here too, and
   `wheel-deploy` uploads every `*.whl` in the directory.
4. **Repair**, if the library's `[ci].windows_wheel_repair` is `true` (USAGE
   §12.1). Run it from *outside* the wheel directory:

   ```bash
   cd <root>
   xmsconan wheel-repair --wheel-dir <root>/wheelhouse-<library>-<version> --platform windows
   ```

   Then check where the repaired wheel actually is (see Known traps): normally
   `<wheel-dir>`, or `<wheel-dir>_repaired` if cleanup failed. Confirm exactly one
   `.whl`, the right `.pyd`, and look at what was vendored.
5. **Publish** (second-person go-ahead first). Credentials go in environment
   variables, not on the command line; `aquaveo/dev` is the VS2022 channel and
   must not be the target:

   ```bash
   export AQUAPI_URL=https://public.aquapi.aquaveo.com/aquaveo/stable/
   export AQUAPI_USERNAME="$(sed -n 1p "$USERPROFILE/.config/tokens/devpi" | tr -d '\r')"
   export AQUAPI_PASSWORD="$(sed -n 2p "$USERPROFILE/.config/tokens/devpi" | tr -d '\r')"
   xmsconan wheel-deploy --wheel-dir <the directory that holds the repaired wheel>
   unset AQUAPI_USERNAME AQUAPI_PASSWORD
   ```

   (`--password-file` reads the whole file as the password, so it does not fit a
   username-plus-password file.)

6. **Verify**, bypassing pip's cache:

   ```bash
   pip install --no-cache-dir "<library>==<version>" \
     -i https://public.aquapi.aquaveo.com/aquaveo/stable/+simple
   ```

7. `git checkout master` to leave the detached HEAD.

### 7. Done

The library is released when all four destinations in the table at the top carry
`<version>` and both Conan remotes report the same recipe revision. Only then
start the next library in the order.

---

## Known traps

**Building and publishing**

- **Read the summary table, not just the exit code.** `xmsconan vs2019 build`
  exits 1 when a configuration fails (USAGE §16.4), but a release run once
  reported exit 0 next to a `failed` row
  ([xmsconan#89](https://github.com/Aquaveo/xmsconan/issues/89), closed: the code
  path is correct, and the likely cause was a pipe). Anything piped through
  `tee` reports `tee`'s status — use `set -o pipefail`, and read
  `succeeded` / `failed` before every upload.
- **`wheel-repair` can exit non-zero after it succeeded, and names the wrong
  path.** It writes `<wheel-dir>_repaired`, deletes `<wheel-dir>`, and renames
  `_repaired` back. delvewheel prints `fixed wheel written to …_repaired…`, which
  on success is a path that no longer exists. If anything holds `<wheel-dir>` open
  (e.g. a shell `cd`'d into it), the delete fails with `PermissionError: [WinError
  32]` and the command exits non-zero with a good wheel left in `_repaired`. Run
  it from elsewhere and check which directory holds the wheel before deploying.
- **One virtualenv per Python version.** The pybind build only works from a venv
  of the target version; preflight catches the mismatch (exit 2).
- **Never an editable xmsconan install for releases.** With `pip install -e`,
  `pip show` reports whatever version was last installed while the code that runs
  is whatever the source tree has checked out — nothing records what you built
  with, and the recipe revision follows the source tree.
- **Pin Conan `~=2.31.0`.** A Conan minor bump changes `package_id` computation
  and silently detaches new builds from the binaries already published.
- **Verify a fresh publish with `pip install --no-cache-dir`.** pip caches the
  simple-index page, so a check seconds after upload reports
  `from versions: none` and looks like a failed upload.
- **Extraction scans the whole Conan cache.** `--wheel-dir` picks up every
  pybind wheel of that version in the cache (cp313 included), and
  `collect_dependency_libs` stages DLLs from every package in the cache. Inspect
  the wheel directory and the vendored DLLs before deploying.
- **GitHub: a Release, with a full SHA.** A bare tag push publishes the Conan
  packages and wheels, then fails `Upload Zipped Conan Packages` on every
  platform job (USAGE §10.1). Recover with
  `gh release create <tag> --verify-tag` and re-run the failed jobs.
  `gh release create --target` rejects an abbreviated SHA (`Release.target_commitish
  is invalid`); always use `$(git rev-parse origin/master)`.

**Repository state**

- **Branch and tag from `master`, never from `release-*`.** The current-line
  `release-*` branches carry no commits `master` lacks and fall behind
  immediately; branching from one silently starts you behind. Check with
  `git log --cherry-mark --left-right --no-merges origin/master...origin/release-X.Y`.
  Do not delete `release-*` branches that *do* have unique commits — older ones
  hold work found nowhere else.
- **Work from a fresh clone.** A long-lived checkout can carry an unpushed local
  commit on `master` that blocks the fast-forward.

**Windows shell**

- **Use Git Bash, not PowerShell, for `--filter`.** PowerShell strips the inner
  double quotes, so `'{"options": {"pybind": true}}'` arrives as invalid JSON.
- **`git show origin/master:<path>` fails in Git Bash** — MSYS rewrites the path
  after the colon. Use `MSYS_NO_PATHCONV=1` or `git cat-file -p`.
- **Check `py -0p` resolves.** A registered interpreter whose path no longer
  exists makes `py -3.X -m venv` fail.

**Verifying a change**

- **Compare the set of failures, not the count.** A change checked only on a
  platform that already had 21 failing tests hid a new failure inside the same
  count of 21. Verify on a green platform, or diff the failing test names.
- **Debug builds no pybind; Release does.** A Debug-green / Release-red split
  usually points at the Python layer, not the compiler.
- **Before blaming toolchain divergence, diff the outputs pairwise.** A baseline
  mismatch that looked like msvc 192 vs 194 floating-point drift was a stale
  baseline: the three CI outputs were byte-identical to each other.

**Maintaining the VS2019 track**

- **Conan 2.31 silently ignores an option value for an option the recipe does not
  declare** (`-o pkg/*:opt=val`, exit 0). A misspelled or undeclared option does
  not fail the build; confirm the option appears in the package's info.
- **Use `conan export-pkg`, not `conan create`, for binary-only shims** (the
  republished third-party packages such as `boost/1.74.0.3`). Under `conan
  create` the recipe runs from the cache export folder, which has no payload, so
  it "succeeds" with `WARN: No files in this package!` and ships an empty
  package.
- **Stacked PRs merge deepest first** — or retarget the child onto `master`
  before merging the parent. A child merged into a parent branch that was already
  merged is marked "merged" while its commits never reach `master`.

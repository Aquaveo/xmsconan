# Releasing the XMS libraries

How to release one of the xms C++/Python libraries built with xmsconan, for both
toolchains the products consume:

| Artifact | Destination | Toolchain | Produced by |
|---|---|---|---|
| Conan packages | remote `aquaveo` (Artifactory repo `aquaveo-stable`) | msvc 194 / gcc / clang | the library's CI, on the tag |
| Linux, macOS and Windows wheels, one per `[ci]` Python version | devpi `aquaveo/dev` | gcc / clang / msvc 194 | the library's CI, on the tag |
| Conan packages | remote `aquaveo-vs2019` | msvc 192 | the VS2019 bridge track (step 5), by hand |
| VS2019 wheel | devpi `aquaveo/stable` | msvc 192 | the VS2019 bridge track (step 6), by hand |

This page is the *procedure*. The tools it drives are documented in
[USAGE.md](USAGE.md); in particular §10 (generated CI), §13 (`wheel-deploy`),
§16 (`xmsconan vs2019`) and §17 (credentials). It does not repeat their flag
references.

> **The VS2019 bridge is temporary.** It exists so a desktop product can move
> conan1 → conan2 while still on VS2019 (consuming `aquaveo-vs2019`), and then
> VS2019 → VS2022 with no version change (repointing to the `aquaveo` remote, Artifactory repo `aquaveo-stable`) — one
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

Generated CI installs `xmsconan[ci]>=<floor>,<<next major>` with `--upgrade`
(USAGE §10.3), so the tag pipeline runs **the newest xmsconan below the next
major on devpi at that moment**. That is the
version your VS2019 build must use — read it from the tag pipeline's install log,
not from the CI file.

## Release order

Each library's `xms_dependencies` name exact versions — unless its
`[vs2019_dependency_overrides]` table replaces one with a version or range for
msvc 192 only (USAGE §7.4) — and a VS2019 build fails at graph resolution if the
version it resolves has no msvc 192 packages. Release
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
- The `xmsconan vs2019` driver also lists `xmsgridtrace` and `xmssnap` (USAGE
  §16.5). They are outside this release order: nothing above depends on them, and
  this procedure has not been run for either.

## Before you start (one-time per workstation)

| Requirement | How |
|---|---|
| VS2019 with the C++ toolset | Checked by `xmsconan vs2019 setup` preflight (USAGE §16.3). |
| One virtualenv **per Python version** you build pybind for | Typically 3.10 and 3.13. The recipe builds against the interpreter running Conan (USAGE §16.8), so a 3.10 wheel needs a 3.10 venv. |
| xmsconan, **pinned and non-editable**, in every venv | `pip install "xmsconan[ci]==<version the tag pipeline used>" -i https://public.aquapi.aquaveo.com/aquaveo/dev/+simple` |
| Conan `~=2.31.0` | Comes with the `[ci]` extra. Preflight rejects anything else. |
| Credential files | `C:\Users\<user>\.config\tokens\conan` (Artifactory password for both Conan remotes, alone on one line), `C:\Users\<user>\.config\tokens\devpi` (devpi username on line 1, password on line 2), `C:\Users\<user>\.config\tokens\jfrog` (Artifactory access token, for the Artifactory UI/REST API — the steps below do not need it). Never commit or paste their contents. |
| Conan logins | `xmsconan conan-setup --login --username <user> --password-file "$USERPROFILE/.config/tokens/conan"` for the `aquaveo` remote, and `vs2019 setup` (step 5.4) for `aquaveo-vs2019`. The two resolve the username differently. `conan-setup` takes `--username`, then `~/.xmsconan.toml`, and nothing else; if neither names one, it hands Conan no credentials at all — the password file is ignored and Conan prompts — so **`--username` is required whenever you pass `--password-file` to `conan-setup`**. `vs2019 setup` takes `--username`, then `$CONAN_LOGIN_USERNAME`, then `~/.xmsconan.toml`, then falls back to user `aquaveo`, so pass `--username` there too if your account is personal. |
| Fresh clones of the libraries, side by side under one `<root>` | `xmsconan vs2019 build --root <root>` expects `<root>/<library>/build.toml`. |
| **Git Bash**, not PowerShell | PowerShell strips the quotes out of `--filter` JSON. |

Confirm the install is what you think it is — it must print a `site-packages`
path, not a source tree:

```bash
python -c "import os, xmsconan; print(xmsconan.__version__, os.path.dirname(xmsconan.__file__))"
```

## Procedure, per library

Two steps publish to shared infrastructure that is hard to un-publish — the Conan
upload in step 5.8 and the devpi upload in step 6.5. **Get a second person's go-ahead before
each**, stating the library, version, remote/index, exactly what will be
uploaded, and what you verified. Creating the release in step 3 triggers CI that
publishes too, so treat it the same way. Building never publishes on its own.

### 1. Prepare `master`

1. Fresh clone, or `git fetch origin` and confirm local `master` fast-forwards to
   `origin/master` (a stale local commit on `master` is a reason to re-clone).
2. Branch from **`master`** — never from a `release-*` branch:
   `git checkout -b bump/<library>-deps origin/master`.
3. In `build.toml`, bump every `xms_dependencies` version to the one just
   released on **both** remotes. If the library has a
   `[vs2019_dependency_overrides]` table (USAGE §7.4), check each entry too: it
   decides what msvc 192 resolves, so it must still name a version or range that
   exists on `aquaveo-vs2019` — or be removed if the override is no longer wanted.
4. Regenerate the CI file: `xmsconan ci build.toml` (`xmsconan ci --check
   build.toml` beforehand shows whether it had drifted, USAGE §4.1). The build
   files `xmsconan gen` writes are gitignored, so there is nothing of theirs to
   commit.
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

**GitLab (`xmsconstraint`) — push an annotated tag; the pipeline needs no
Release object:**

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
   conan list "<library>/<version>#*" -r aquaveo
   ```

If the library's `build.toml` sets `[ci].windows_vs2019 = true` (GitLab only,
USAGE §10.2), the tag pipeline also published the msvc 192 Conan packages. Do
steps 5.1–5.4, skip 5.5–5.8, run the two-remote check in 5.9, then step 6 if the
library ships a wheel.

### 5. Build and publish the VS2019 Conan packages

1. **Detached checkout of the tag**, clean tree:

   ```bash
   cd <root>/<library>
   git checkout <version>
   git describe --tags --exact-match       # must print <version>
   git status --short                      # must be empty
   ```

2. **Match the tag pipeline's xmsconan** in each venv you will use (see the
   invariant above): `pip install "xmsconan[ci]==<that version>" -i https://public.aquapi.aquaveo.com/aquaveo/dev/+simple`.
3. **Put the 3.10 venv first on `PATH`** — preflight shells out to `conan`:
   `export PATH="/path/to/.venv310/Scripts:$PATH"`.
4. **Preflight** (also logs in to `aquaveo-vs2019`; the password goes to Conan
   through the environment, never argv):

   ```bash
   xmsconan vs2019 setup --username <user> --password-file "$USERPROFILE/.config/tokens/conan"
   ```

   Expect `[OK]` for the VS2019 toolset, the conan client and the remote.

5. **Clear msvc 194 pybind packages of this version from the local cache**
   before building. Wheel extraction stages **one** wheel per Python version,
   chosen by recipe-revision timestamp and build type; an msvc 194 and an msvc
   192 Release pybind package of the same recipe revision tie on both, so either
   can be the one staged, and the filename and log line are identical. An msvc
   194 wheel would then *replace* the msvc 192 one, and nothing after extraction
   can tell. On a machine that also builds this library for VS2022:

   ```bash
   conan list "<library>/<version>:*" -p "compiler.version=194" -c
   conan remove "<library>/<version>:*" -p "compiler.version=194" -c   # if the list is not empty
   ```

   Only the local cache is touched (no `-r`). Repeat before the step 6.2
   fallback.

6. **Build.** From the 3.10 venv, then the pybind configuration again from the
   3.13 venv. The 3.10 run also stages the wheel for step 6 (`--wheel-dir`; omit
   it for a library without pybind bindings), so the wheel comes from the same
   binary that step 5.8 publishes:

   ```bash
   xmsconan vs2019 build --root <root> --only <library> --version <version> \
     --python-versions 3.10 --log-dir <root>/vs2019-logs/<library>-<version> \
     --wheel-dir <root>/wheelhouse-<library>-<version>

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
   still shares the recipe revision; only binary coverage differs. Wheel
   extraction is skipped when any configuration failed (USAGE §16.8), so a run
   with failures stages no wheel — see step 6.2.

7. **Verify before publishing:**

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
     `<dep>/<version>#<rrev>` — the `xms_dependencies` version, or, where
     `[vs2019_dependency_overrides]` names that dependency, a version the
     override allows.

8. **Publish** (second-person go-ahead first):

   ```bash
   xmsconan vs2019 upload --library <library> --version <version>
   ```

   Only `compiler.version=192` binaries go, and only to `aquaveo-vs2019`
   (USAGE §16.7).

9. **Confirm both remotes carry the same recipe revision:**

   ```bash
   conan list "<library>/<version>#*" -r aquaveo
   conan list "<library>/<version>#*" -r aquaveo-vs2019
   ```

### 6. Build and publish the VS2019 wheel

Only for libraries with pybind bindings.

1. **Decide which Python versions belong on `aquaveo/stable`.** List what the
   channel already carries for this library and match it; newer ABIs are covered
   by CI on `aquaveo/dev`.
2. **The wheel directory** is the fresh, library-specific `--wheel-dir` from
   step 5.6 — never a shared `wheelhouse/`. If that run staged nothing (a
   configuration failed, or the library's CI published the Conan packages so
   step 5.6 was skipped), clear msvc 194 packages as in step 5.5, then stage it
   with a pybind-only run from the 3.10 venv. This **rebuilds** the package with
   `conan create`, giving a new local package revision, so the wheel is not
   byte-for-byte the binary on `aquaveo-vs2019`:

   ```bash
   export PATH="/path/to/.venv310/Scripts:$PATH"
   xmsconan vs2019 build --root <root> --only <library> --version <version> \
     --python-versions 3.10 --filter '{"options": {"pybind": true}}' \
     --wheel-dir <root>/wheelhouse-<library>-<version> \
     --log-dir <root>/vs2019-logs/<library>-<version>
   ```

   The rebuild re-exports the recipe, so confirm
   `conan list "<library>/<version>#*" -c` still shows the recipe revision
   recorded in step 4.4 before going further.

3. **Remove every wheel that is not going to the channel.** Extraction copies a
   wheel for every Python version it finds in the local Conan cache (a cp313
   build, if one is there), and `wheel-deploy` uploads every `*.whl` in the
   directory.
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
  exits 1 when a configuration fails (USAGE §16.4), but a pipeline such as
  `… | tee build.log` reports `tee`'s status, not the build's
  ([xmsconan#89](https://github.com/Aquaveo/xmsconan/issues/89)). Use
  `set -o pipefail`, and read `succeeded` / `failed` before every upload.
- **Compare the set of failures, not the count.** A new failure can hide inside
  an unchanged failure count on a platform that was already red. Verify on a
  green platform, or diff the failing test names.
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
- **Extraction scans the whole Conan cache.** `--wheel-dir` stages one wheel
  per Python version from every pybind package of that version in the cache, and
  does not filter on `compiler.version`: with an msvc 194 package of the same
  recipe revision in the cache, its wheel can be staged *instead of* the msvc 192
  one, under the identical filename. Clear those before building (step 5.5).
  `collect_dependency_libs` likewise stages DLLs from every package in the cache;
  inspect the vendored DLLs before deploying.
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

**Maintaining the VS2019 track**

- **Options a legacy recipe does not declare.** The legacy `boost/1.74.0.3` does
  not declare the conan-center boost options the modern profiles set, which is
  why the driver turns those defaults off; see USAGE §16.6 before adding a
  profile option to the msvc 192 matrix, and confirm in the package's info that
  an option you set actually took effect.
- **Use `conan export-pkg`, not `conan create`, for binary-only shims** (the
  republished third-party packages such as `boost/1.74.0.3`). Under `conan
  create` the recipe runs from the cache export folder, which has no payload, so
  it "succeeds" with `WARN: No files in this package!` and ships an empty
  package.
- **Stacked PRs merge deepest first**, or retarget the child onto `master`
  before merging the parent.

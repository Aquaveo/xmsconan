# Packager per-dependency option support

## Summary

Add support for assigning values to options on dependencies in a `build.toml`
and having them applied to profiles written by
`XmsConanPackager.create_build_profile`. Start by writing tests for the new
functionality and verify their behavior is correct (some should fail, others
might pass). Then implement the new feature, and ensure all the tests pass.
Review the "Cleanup" section below after everything else is done.

## Background

Today the project supports `xms_dependencies` and `xms_dependency_options`
keys in the `build.toml`. The former lists first-party dependency packages,
while the latter assigns options to them. The first-party packages all have
a small set of standard options, so they're hard-coded. It's unclear whether
`xms_dependency_options` actually works at this point, but that's out of scope
for this project.

The hypothetical motivating scenario: a `lidar` package built for Linux with
`pybind=True`, against `boost` with `wchar_t=builtin`, `laslib` with
`shared=True`, and `example` with `test_option="test-value"`. The `build.toml`
for this package might include the following lines:

```toml
[conan_profile_options]
boost.wchar_t="builtin"
laslib.shared=true
example.test_option="test-value"
```

And the profile for the `pybind=True` configuration should include these lines:

```
[options]
&:pybind=True
&:testing=False
boost/*:wchar_t=builtin
laslib/*:shared=True
example/*:test_option=test-value
```


## The xms_dependency_options key

The `build.toml` currently supports an `xms_dependency_options` key which seems
like something that could be imitated for this project, but is actually a bad
example.

The build-toml-driven `xms_dependency_options` key may not actually be
functional at this point in time. It eventually ends up in a generated
`conanfile.py` that uses its information to update its `self.options` dict at
configure time, but the documentation for Conan says configure-time options are
only suggestions and may be silently overridden by various vaguely defined
things. I've confirmed at least one case where this is a problem for the new
project (more details in the next section), so the system shouldn't be
imitated.

It may be the case that the `xms_dependency_options` system is broken.
I haven't confirmed any cases yet, and it isn't important, so fixing it isn't
part of this work.


## Existing extra_dependency_options work

The past five commits attempted to add an `extra_dependency_options` key to
the `build.toml` and thread its information into the right place so that Conan
would apply those options. However, it was discovered to be unreliable. The
Conan documentation says option values in the `configure` method may be
overridden, and I've confirmed that happens silently in the case of Linux
pybind builds. The `*:shared=True` line emitted into the profile overrides all
the `shared` values assigned in the `configure` methods, making
`extra_dependency_options` unsuitable for this job.

Since assigning options at configure time can't be trusted, they need to be
written into the profile instead, which means getting the information about
them from the `build.toml` into `XmsConanPackager.create_build_profile`.

It may be possible to salvage some code from the `extra_dependency_options`
commits, but they should be considered a failed experiment and not included in
the final product.


## Getting information from `build.toml` to the packager

Today, data flows from `build.toml` to the packager through generated files:

1. `build_file_generator.py` reads `build.toml` and renders templates from
   `xmsconan/generator_tools/templates/`. Two of those outputs matter here:
   `conanfile.py` (from `conanfile.py.jinja`) and `build.py` (from
   `build.py.jinja`).
2. The generated `build.py` is the entry point that instantiates the packager.
   It does `import conanfile` and reads module-level constants such as
   `LIBRARY_NAME`, then passes them to `XmsConanPackager`.
3. The packager itself never reads `build.toml`. It receives whatever it needs
   through constructor or method arguments.

The same path works for the new TOML key. The `conan_profile_options` table
becomes a module-level constant in the generated `conanfile.py`, `build.py`
reads it, and forwards it to the packager constructor. Concretely:

**`build_file_generator.py`** — add a default alongside the others
(near line 90):

```python
toml_data.setdefault("conan_profile_options", {})
```

**`conanfile.py.jinja`** — add a module-level constant near `LIBRARY_NAME`:

```jinja
CONAN_PROFILE_OPTIONS = {{ conan_profile_options }}
```

The `setdefault` above guarantees the value is at least `{}`, so the constant
is always defined and the line needs no conditional wrapper. (This differs
from the existing `xms_dependency_options` / `extra_dependency_options` jinja
blocks, which omit the class attribute entirely when empty. Those are class
attributes Conan reads, so an absent attribute is meaningful. `CONAN_PROFILE_OPTIONS`
is consumed unconditionally by `build.py`, so it must always exist.)

**`build.py.jinja`** — pass it through to the packager constructor:

```python
builder = packager.XmsConanPackager(
    conanfile.LIBRARY_NAME,
    conanfile.__file__,
    profile_options=conanfile.CONAN_PROFILE_OPTIONS,
    ...
)
```

**`packager.py`** — accept the new kwarg, store it, and attach it to every
combination at the end of `generate_configurations`, alongside `options` and
`buildenv`:

```python
combination['profile_options'] = self._profile_options
```

`create_build_profile` then reads `configuration['profile_options']` and emits
the dep-qualified lines into the `[options]` section.

This routes the new data through the same generated-file pipeline that
`LIBRARY_NAME` already uses. The conanfile keeps class attributes for things
Conan reads, while module-level constants remain packager metadata that
`build.py` forwards.


## Schema

`create_build_profile` currently takes a dict with multiple keys. A new
`profile_options` key will be added to it:

```python
configuration['profile_options'] = {
    'boost':   {'wchar_t': 'builtin'},
    'laslib':  {'shared': True},
    'example': {'test_option': 'test-value'},
}
```

This is based on the shape of `xms_dependency_options`, which already exists.
The name is similar to the key in the `build.toml`, but not the same. The
`build.toml` includes information about much more than just Conan, so its key
is prefixed with `conan_` to make its purpose clear. The `create_build_profile`
method is clearly part of Conan-related code though, so the prefix would be
redundant there and is omitted.

The new `profile_options` key in `create_build_profile` has a similar name to
the existing `options` key. Both of them provide information that ends up in
the `[options]` section of the profile. The existing `options` key provides
information for XMS-specific options, while the new `profile_options` provides
options for third-party dependencies. `options` might be replaced with
`profile_options` at some point. For now, we'll live with the redundant names.

The key is added per combination *after* the cartesian product in
`generate_configurations`, identical to how `options` and `buildenv` are added
today (packager.py:135-149). It does not participate in the product.

## Profile output

`create_build_profile` writes one line per dep / option pair into the existing
`[options]` section, using conan2's wildcard pattern:

```
[options]
&:wchar_t=builtin
&:pybind=True
&:testing=False
*:shared=False    # only for Linux pybind builds, as today
boost/*:wchar_t=builtin
laslib/*:shared=True
example/*:test_option=test-value
```

Per-dep lines are written AFTER the `*:shared=False` wildcard. Conan2
applies profile options in declaration order with last-wins, so per-dep
overrides must come after any wildcards to take effect. (An earlier
draft of this spec assumed conan resolves by pattern specificity; CI
testing showed it doesn't.) If `profile_options` is absent or empty, no
extra lines are emitted and behavior is unchanged.

## Tests

All tests are written upfront, before any implementation. 

Each one is expected
to fail when added; the implementation is complete when all tests pass. None
are marked `xfail` — plain red tests are the cleaner TDD entry point, and the
design has fixed every observable surface they assert against, so there is no
information the implementation would reveal that we don't already have.

The tests:

1. **`tests/test_packager.py` — `test_create_build_profile_writes_profile_options`.**
   Exercises the `create_build_profile` end of the pipeline.
   1. Build a packager named `"lidar"`.
   2. Call `generate_configurations(system_platform="linux")`.
   3. Filter to a configuration with `pybind=True`.
   4. Attach `profile_options = {'boost': {'wchar_t': 'builtin'},
      'laslib': {'shared': True}, 'example': {'test_option': 'test-value'}}`
      to that configuration.
   5. Call `create_build_profile`.
   6. Assert the resulting profile file contains:
      - `os=Linux` (settings)
      - `&:pybind=True` (consumer option)
      - `boost/*:wchar_t=builtin`
      - `laslib/*:shared=True`
      - `example/*:test_option=test-value`

   Follows the pattern of `test_create_build_profile_writes_settings_and_options`
   already in the file. Uses `@patch_env(clear=True)` for environment isolation,
   matching the surrounding tests.

2. **`tests/test_packager.py` — `test_packager_applies_profile_options_to_configurations`.**
   `XmsConanPackager(..., profile_options=...)` stores the value, and
   `generate_configurations` attaches it to every combination as
   `combination['profile_options']`.

3. **`tests/test_packager.py` — `test_create_build_profile_with_no_profile_options`.**
   `create_build_profile` emits no `pkg/*:` lines in the `[options]` section
   when `profile_options` is `{}`. Guards the no-regression path so existing
   projects with no `conan_profile_options` in their `build.toml` don't change.
   

4. **`tests/test_build_file_generator.py` — `test_conan_profile_options_reaches_template_context`.**
   `conan_profile_options` from `build.toml` reaches the template render
   context. Mirrors the existing `extra_dependency_options` tests at lines
   264-290.

5. **`tests/test_build_file_generator.py` — `test_generates_conanfile_with_profile_options`.**
   The generated `conanfile.py` contains a `CONAN_PROFILE_OPTIONS = {...}` line
   at module scope with appropriate values in it when `conan_profile_options`
   is set in the `build.toml`.

6. **`tests/test_build_file_generator.py` — `test_generates_conanfile_without_profile_options`.**
   The generated `conanfile.py` contains a `CONAN_PROFILE_OPTIONS = {}` line
   at module scope, assigned to an empty dict, when `conan_profile_options` is
   not set in the `build.toml`.


## Cleanup
- Existing `extra_dependency_options` stuff might need to be removed.
- Test 4 might need review. It's hard to tell before implementing.


## Out of scope

- `xms_dependency_options`. See dedicated section above.
- Implementing or fixing `extra_dependency_options`. It was a failed
  experiment.

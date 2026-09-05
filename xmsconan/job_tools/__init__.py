"""``xmsconan job <kind>`` -- a CI job as one tool call.

A generated pipeline used to spell each job out in shell: it exported a
version, chose a matrix leg by interpolating JSON into a quoted argument,
prefixed the build with ``xvfb-run``, and passed six flags derived from
``build.toml`` that the template had to re-derive for every host. All of it
was Jinja, so a mistake surfaced as a red pipeline in a consumer repository
rather than as a failing test here.

The commands in this package take that work back. What is left in a template
is what a tool cannot decide -- the stage graph, ``needs``, images, runners,
``rules``, secrets wiring -- and every job's ``script:`` is an install line
and one ``xmsconan job`` call. See ``docs/DESIGN-ci-job-commands.md``.
"""

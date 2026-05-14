"""End-to-end integration test for the xmsconan coverage runner.

Every other test in this suite mocks out the conan / cmake / gcovr / pytest-cov
boundary. That coverage is good for the Python logic above the mocks, but it
cannot prove that xmsconan and the CLIs it shells out to agree on the wire
format — and every coverage bug that shipped in the 2.14.x line was exactly a
wire-format mismatch. Issue #68 calls for a single no-mock test that drives
``run_coverage`` end-to-end against a real recipe so future shape drift fails
here, in xmsconan CI, instead of an xmscore release later.

The test is gated behind ``XMS_INTEGRATION_TESTS=1`` because it builds a full
pybind+Debug package (boost, zlib, pybind11, the stub library, then a wheel)
and runs pytest-cov + gcovr against it — wall time is measured in minutes,
not seconds. The marker ``integration`` is registered in ``pyproject.toml``
so ``pytest -m integration`` opts in cleanly.
"""
# 1. Standard python modules
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

# 2. Third party modules
import pytest

# 3. Aquaveo modules
from xmsconan.ci_tools.conan_setup import conan_setup
from xmsconan.coverage_tools.coverage_generator import run_coverage


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "coverage_stub"


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "Coverage instrumentation is gcc/clang only; CMakeLists.txt raises a "
            "FATAL_ERROR under MSVC when XMS_COVERAGE is set."
        ),
    ),
    pytest.mark.skipif(
        shutil.which("conan") is None,
        reason="conan is not on PATH; cannot drive an end-to-end coverage build.",
    ),
    pytest.mark.skipif(
        shutil.which("gcovr") is None,
        reason="gcovr is not on PATH; cannot collect C++ coverage.",
    ),
    pytest.mark.skipif(
        not os.environ.get("XMS_INTEGRATION_TESTS"),
        reason=(
            "Slow no-mock coverage integration test; set XMS_INTEGRATION_TESTS=1 to opt in."
        ),
    ),
]


@pytest.fixture
def stub_workspace(tmp_path, monkeypatch):
    """Lay down a fresh copy of the stub recipe with an ephemeral CONAN_HOME.

    Each invocation gets its own ``CONAN_HOME`` under ``tmp_path`` so the
    test never contaminates the developer's main conan cache with a
    transient ``stub/0.0.0`` package. When ``CONAN_REMOTE_URL`` +
    ``CONAN_LOGIN_USERNAME`` + ``CONAN_PASSWORD`` are set in the
    environment (the integration workflow exports them from GitHub
    secrets), the fixture wires the Aquaveo conan remote into the
    ephemeral cache via the same ``conan_setup`` helper CI uses
    elsewhere. ``conan install`` then pulls prebuilt boost/zlib/pybind11
    from Aquaveo and the cold-cache run completes in a couple of
    minutes. Without those env vars conan has no remote with a binary
    matching the packager's boost options (conancenter does not ship
    ``without_stacktrace=True, without_locale=True``), and the install
    fails — local devs who want to run the test without credentials
    should configure their own remote first.
    """
    workspace = tmp_path / "workspace"
    shutil.copytree(_FIXTURE_DIR, workspace)

    conan_home = tmp_path / "conan_home"
    conan_home.mkdir()
    monkeypatch.setenv("CONAN_HOME", str(conan_home))
    # XMS_COVERAGE is set by run_coverage itself for the build subprocess;
    # clear any inherited value so the surrounding pytest process does not
    # see it (the recipe's configure() guards on the env var).
    monkeypatch.delenv("XMS_COVERAGE", raising=False)

    remote_url = os.environ.get("CONAN_REMOTE_URL")
    username = os.environ.get("CONAN_LOGIN_USERNAME")
    password = os.environ.get("CONAN_PASSWORD")
    if remote_url and username and password:
        # ``conan_setup`` runs ``conan profile detect -e`` first, then
        # adds + logs in the aquaveo remote. ``remove_conancenter=False``
        # keeps conancenter as a fallback so dependencies that aren't
        # mirrored on aquaveo still resolve.
        conan_setup(
            remote_url=remote_url, login=True,
            remove_conancenter=False, username=username, password=password,
        )
    else:
        subprocess.run(["conan", "profile", "detect", "--force"], check=True)

    # No ``--build=missing``: Aquaveo ships prebuilt binaries for our
    # boost option set, so a plain ``conan install`` resolves from the
    # remote. Falling through to a source build here would mask a real
    # environment problem — the wire-format canary should fail loudly
    # when the expected binary is missing, not silently rebuild boost.
    subprocess.run(
        [
            "conan", "install",
            "--requires=boost/1.86.0",
            "--requires=zlib/1.3.1",
            "--requires=pybind11/3.0.1",
        ],
        check=True,
    )
    return workspace


class TestRunCoverageEndToEnd:
    """``run_coverage`` against a real stub recipe produces both layer summaries.

    Bundled into a single test because each invocation costs a full
    instrumented build (boost + pybind11 + stub + a wheel + pytest-cov +
    gcovr); splitting across tests would double the wall time without
    isolating distinct behaviors.
    """

    def test_coverage_pipeline_writes_full_two_layer_report(self, stub_workspace):
        """End-to-end pass produces both summary JSONs and both HTML report trees.

        Asserts the C++ percentage is strictly inside (0, 100): the stub
        deliberately leaves ``Subtract`` unexercised so a degenerate
        ``everything 100%`` report fails the bounded check below. That
        catches the "gcovr is running but seeing nothing" failure mode
        which a naive ``line_total > 0`` check would let through.
        """
        build_toml = stub_workspace / "build.toml"

        exit_code = run_coverage(
            str(build_toml), version="0.0.0", output_dir=str(stub_workspace),
        )
        assert exit_code == 0, (
            "run_coverage returned non-zero against thresholds=0 stub; build.py or "
            "a threshold check failed. Inspect the captured subprocess output above."
        )

        cpp_summary = stub_workspace / "cov-cpp-summary.json"
        py_summary = stub_workspace / "cov-py-summary.json"
        assert cpp_summary.exists(), "gcovr did not write cov-cpp-summary.json"
        assert py_summary.exists(), "pytest-cov did not write cov-py-summary.json"

        cpp_data = json.loads(cpp_summary.read_text(encoding="utf-8"))
        py_data = json.loads(py_summary.read_text(encoding="utf-8"))

        # ``_assert_gcovr_collected_data`` already enforces this inside
        # run_coverage, but asserting independently means a future
        # regression that weakens that guard does not silently weaken
        # the canary too.
        assert cpp_data["line_total"] > 0, (
            f"gcovr reported zero instrumented lines: {cpp_data!r}"
        )
        assert 0 < cpp_data["line_percent"] < 100, (
            f"C++ coverage {cpp_data['line_percent']}% is not strictly in (0, 100); "
            "the stub's untested ``Subtract`` should yield a partial result. A 100% "
            "value usually means gcovr is filtering aggressively or mis-attributing."
        )

        assert py_data["totals"]["percent_covered"] > 0, (
            f"pytest-cov reported zero Python coverage: {py_data!r}"
        )

        assert (stub_workspace / "coverage-html-cpp" / "index.html").exists(), (
            "gcovr did not write the C++ HTML report."
        )
        assert (stub_workspace / "coverage-html-py").is_dir(), (
            "pytest-cov did not write the Python HTML report directory."
        )

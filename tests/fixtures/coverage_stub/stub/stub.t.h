#pragma once

#include <cxxtest/TestSuite.h>

#include "stub.h"
#include "testing/test_tools.h"

// CxxTest harness for the coverage-integration stub. Exercises
// ``Subtract`` from the C++ side (the python tests don't call it),
// so combining CxxTest + pytest-cov reaches Add/Subtract/Multiply.
// ``Divide`` is left unexercised on purpose — the integration test
// asserts C++ coverage is strictly inside (0, 100), so something
// must remain uncovered.
//
// The call to ``stub::testing::describe_char`` forces the runner to
// pull ``test_tools.o`` (a testing_sources translation unit), which
// has an unresolved reference to ``CxxTest::charToString``. The
// generated runner.cpp resolves it via ``cxxtest/Root.cpp``. If the
// xmsconan template ever reverts to compiling testing_sources into
// the main static library, the pybind module's link picks up
// ``test_tools.o`` and dlopen fails — which is exactly the failure
// mode #74's canary missed in 2.15.0 because the stub had no
// testing_sources at all.
class StubTestSuite : public CxxTest::TestSuite
{
public:
  void testSubtract()
  {
    TS_ASSERT_EQUALS(stub::Subtract(5, 2), 3);
    TS_ASSERT_EQUALS(stub::Subtract(-1, -1), 0);
  }

  void testAddAgreesWithPybindSide()
  {
    // Redundant with the pytest test on the python side; the point
    // is to demonstrate both layers contributing .gcda data to the
    // same .gcno set.
    TS_ASSERT_EQUALS(stub::Add(2, 3), 5);
  }

  void testDescribeCharRoundtrip()
  {
    // The result string format isn't load-bearing — what matters is
    // that the call site forces test_tools.cpp into the runner's
    // link closure so the canary actually exercises the
    // testing_sources path.
    TS_ASSERT(!stub::testing::describe_char('a').empty());
  }
};

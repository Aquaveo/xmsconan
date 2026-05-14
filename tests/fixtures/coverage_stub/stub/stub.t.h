#pragma once

#include <cxxtest/TestSuite.h>

#include "stub.h"

// CxxTest harness for the coverage-integration stub. Exercises
// ``Subtract`` from the C++ side (the python tests don't call it),
// so combining CxxTest + pytest-cov reaches Add/Subtract/Multiply.
// ``Divide`` is left unexercised on purpose — the integration test
// asserts C++ coverage is strictly inside (0, 100), so something
// must remain uncovered.
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
};

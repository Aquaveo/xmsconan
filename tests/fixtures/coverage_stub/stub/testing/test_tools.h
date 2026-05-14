#pragma once

#include <string>

namespace stub {
namespace testing {

// Format a character using CxxTest's value-traits machinery. The body
// of this function deliberately exercises ``CxxTest::ValueTraits<char>``
// which calls ``CxxTest::charToString`` — declared in
// ``cxxtest/ValueTraits.h`` but defined only in
// ``cxxtest/ValueTraits.cpp``. The .o for this translation unit ends up
// with an unresolved reference to ``CxxTest::charToString`` that only
// the cxxtest runner (whose generated runner.cpp pulls in
// ``cxxtest/Root.cpp``) can satisfy. This mirrors xmscore's
// ``TestTools.cpp`` pattern and is what would have caught the 2.15.0
// pybind-module-link-leak bug in the canary.
std::string describe_char(char c);

}  // namespace testing
}  // namespace stub

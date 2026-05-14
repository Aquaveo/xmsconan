#include "test_tools.h"

#include <cxxtest/ValueTraits.h>

namespace stub {
namespace testing {

std::string describe_char(char c)
{
  // CxxTest::ValueTraits<char>'s constructor (inline in ValueTraits.h)
  // calls CxxTest::charToString — which is declared in the same header
  // but defined only in cxxtest/ValueTraits.cpp. If this translation
  // unit ends up in any link that does not also pull cxxtest's Root.cpp
  // umbrella, the resulting binary has an unresolved reference and
  // dlopen / dlsym fails at runtime.
  return CxxTest::ValueTraits<char>(c).asString();
}

}  // namespace testing
}  // namespace stub

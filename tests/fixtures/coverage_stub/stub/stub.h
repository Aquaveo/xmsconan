#pragma once

namespace stub {

int Add(int a, int b);
int Subtract(int a, int b);
int Multiply(int a, int b);
// Divide is deliberately exposed but never called by either test layer
// — the integration test asserts C++ coverage is strictly inside
// (0, 100) and a fully-exercised stub would push it to 100%. Keep
// Divide untested.
int Divide(int a, int b);

}  // namespace stub

#include <pybind11/pybind11.h>

#include "../stub.h"

PYBIND11_MODULE(_stub, m)
{
  m.def("add", &stub::Add, "Return a + b.");
  m.def("subtract", &stub::Subtract, "Return a - b.");
  m.def("multiply", &stub::Multiply, "Return a * b.");
}

"""Tests that exercise the stub library through its pybind11 bindings.

These tests deliberately call only *some* of the stub's C++ functions —
``Subtract`` is exposed and importable but never invoked. The resulting
non-100% C++ percentage is what lets the integration test assert that
gcovr is actually measuring the call graph rather than reporting a
degenerate all-or-nothing value.
"""
from xms.stub import add, multiply


def test_add_returns_sum():
    """``add`` returns the arithmetic sum of its two integer arguments."""
    assert add(2, 3) == 5


def test_add_handles_negatives():
    """``add`` handles negative operands without sign error."""
    assert add(-4, 1) == -3


def test_multiply_returns_product():
    """``multiply`` returns the arithmetic product."""
    assert multiply(4, 5) == 20


def test_multiply_with_zero():
    """``multiply`` short-circuits to zero on a zero operand."""
    assert multiply(0, 99) == 0

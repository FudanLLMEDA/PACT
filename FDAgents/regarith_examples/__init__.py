"""Reproducible normalized examples for the registered arithmetic IR."""

from .generate import (
    build_all_examples,
    build_fir_recurrence_transport_example,
    build_optical_product_sum_example,
    build_simple_signed_fixed_product_example,
    build_vtr_root_divider_rejection_example,
)

__all__ = [
    "build_all_examples",
    "build_simple_signed_fixed_product_example",
    "build_optical_product_sum_example",
    "build_fir_recurrence_transport_example",
    "build_vtr_root_divider_rejection_example",
]

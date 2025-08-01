# Copyright 2023 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pallas-specific JAX primitives."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from collections.abc import Hashable
import enum
import functools
import string
from typing import Any

import jax
from jax import lax
from jax import tree_util
from jax._src import ad_util
from jax._src import api_util
from jax._src import callback
from jax._src import core as jax_core
from jax._src import dtypes
from jax._src import effects
from jax._src import linear_util as lu
from jax._src import pretty_printer as pp
from jax._src import state
from jax._src import util
from jax._src.interpreters import ad
from jax._src.interpreters import batching
from jax._src.interpreters import partial_eval as pe
from jax._src.pallas import core as pallas_core
from jax._src.state import discharge as state_discharge
from jax._src.state import indexing
from jax._src.state import primitives as sp
from jax._src.state import types as state_types
from jax.interpreters import mlir
import jax.numpy as jnp

partial = functools.partial
Slice = indexing.Slice
NDIndexer = indexing.NDIndexer

map, unsafe_map = util.safe_map, map
zip, unsafe_zip = util.safe_zip, zip

program_id_p = jax_core.Primitive("program_id")
batching.ragged_prop_rules[program_id_p] = batching.ragged_mask_no_op_rule

def program_id(axis: int) -> jax.Array:
  """Returns the kernel execution position along the given axis of the grid.

  For example, with a 2D `grid` in the kernel execution corresponding to the
  grid coordinates `(1, 2)`,
  `program_id(axis=0)` returns `1` and `program_id(axis=1)` returns `2`.

  The returned value is an array of shape `()` and dtype `int32`.

  Args:
    axis: the axis of the grid along which to count the program.
  """
  return program_id_p.bind(axis=axis)

def program_id_bind_with_trace(trace, _, params):
  axis = params.pop("axis")
  grid_env = pallas_core.current_grid_env()
  if grid_env:
    return grid_env[axis].index
  frame = pallas_core.axis_frame()
  # Query the size of the axis to make sure it's a valid axis (and error
  # otherwise).
  _ = frame.size(axis)
  return jax_core.Primitive.bind_with_trace(program_id_p, trace, (), dict(axis=axis))
# TODO(dougalm): figure out how put the grid_env contest on the relevant trace
program_id_p.def_bind_with_trace(program_id_bind_with_trace)

@program_id_p.def_abstract_eval
def _program_id_abstract_eval(**_):
  return jax_core.ShapedArray((), jnp.int32)

num_programs_p = jax_core.Primitive("num_programs")

def num_programs(axis: int) -> int | jax.Array:
  """Returns the size of the grid along the given axis."""
  return num_programs_p.bind(axis=axis)

def _num_programs_bind_with_trace(trace, _, params):
  axis = params.pop("axis")
  # We might be using a local grid env
  grid_env = pallas_core.current_grid_env()
  if grid_env:
    return grid_env[axis].size
  # Otherwise, we look up the size of the grid in the axis env
  frame = pallas_core.axis_frame()
  size = frame.size(axis)
  if size is pallas_core.dynamic_grid_dim:
    return jax_core.Primitive.bind_with_trace(num_programs_p, trace, (), dict(axis=axis))
  return size
num_programs_p.def_bind_with_trace(_num_programs_bind_with_trace)

@num_programs_p.def_abstract_eval
def _num_programs_abstract_eval(**_):
  return jax_core.ShapedArray((), jnp.int32)

class AtomicOpType(enum.Enum):
  XCHG = "xchg"
  ADD = "add"
  MAX = "max"
  MIN = "min"
  AND = "and"
  OR = "or"
  XOR = "xor"

atomic_rmw_p = jax_core.Primitive("atomic_rmw")


def _atomic_rmw_discharge_rule(
    in_avals, out_avals, *args_flat, args_tree, atomic_type: AtomicOpType
):
  del out_avals  # Unused.
  ref, transforms, val, mask = args_tree.unflatten(args_flat)
  *prev_transforms, idx = transforms
  ref = state_discharge.transform_array(ref, prev_transforms)

  if mask is not None:
    raise NotImplementedError

  if atomic_type == AtomicOpType.ADD:
    monoid = lambda x, y: x + y
  elif atomic_type == AtomicOpType.MAX:
    monoid = jnp.maximum
  elif atomic_type == AtomicOpType.MIN:
    monoid = jnp.minimum
  else:
    raise NotImplementedError(atomic_type)

  if all((isinstance(s, Slice) or not s.shape) for s in idx.indices):
    indices = idx.indices
    scalar_dims = [not isinstance(s, Slice) and not s.shape for s in indices]
    slice_starts = [s.start if isinstance(s, Slice) else s for s in indices]
    slice_sizes = tuple(s.size if isinstance(s, Slice) else 1 for s in indices)
    out_ones = lax.dynamic_slice(ref, slice_starts, slice_sizes=slice_sizes)
    val_indexer = tuple(None if scalar else slice(None) for scalar in scalar_dims)
    val = val[val_indexer]
    val = monoid(val, out_ones)
    x_new = lax.dynamic_update_slice(ref, val, start_indices=slice_starts)
    out_indexer = tuple(0 if scalar else slice(None) for scalar in scalar_dims)
    out = out_ones[out_indexer]
  elif all(not isinstance(s, Slice) for s in idx.indices):
    out = ref[idx.indices]
    x_new = ref.at[idx.indices].set(monoid(out, val))
  else:
    raise NotImplementedError
  return (x_new,) + (None,) * (len(in_avals) - 1), out


state_discharge.register_discharge_rule(atomic_rmw_p)(_atomic_rmw_discharge_rule)


@atomic_rmw_p.def_effectful_abstract_eval
def _atomic_abstract_eval(*avals_flat, args_tree, atomic_type: AtomicOpType):
  ref, _, _, _ = args_tree.unflatten(avals_flat)
  if ref.dtype == jnp.dtype("float16") and atomic_type != AtomicOpType.ADD:
    raise ValueError(f"`atomic_{atomic_type.value}` does not support f16.")
  if ref.dtype in {
      jnp.dtype("bool"),
      jnp.dtype("int8"),
      jnp.dtype("int16"),
      jnp.bfloat16,
  }:
    raise ValueError(
        f"`atomic_{atomic_type.value}` does not support {ref.dtype}."
    )
  return _swap_abstract_eval(*avals_flat, args_tree=args_tree)


def _atomic_rmw(x_ref_or_view, idx, val, *, mask: Any | None = None,
                atomic_type: AtomicOpType):
  x_ref, transforms = sp.get_ref_and_transforms(
      x_ref_or_view, idx, "atomic_rmw"
  )
  args_flat, args_tree = tree_util.tree_flatten((x_ref, transforms, val, mask))
  return atomic_rmw_p.bind(
      *args_flat, args_tree=args_tree, atomic_type=atomic_type
  )

def atomic_xchg(x_ref_or_view, idx, val, *, mask: Any | None = None):
  """Atomically exchanges the given value with the value at the given index.

  Args:
    x_ref_or_view: The ref to operate on.
    idx: The indexer to use.
    mask: TO BE DOCUMENTED.

  Returns:
    The value at the given index prior to the aupdate.
  """
  return _atomic_rmw(
      x_ref_or_view, idx, val, mask=mask, atomic_type=AtomicOpType.XCHG
  )


def atomic_add(x_ref_or_view, idx, val, *, mask: Any | None = None):
  """Atomically computes ``x_ref_or_view[idx] += val``.

  Args:
    x_ref_or_view: The ref to operate on.
    idx: The indexer to use.
    mask: TO BE DOCUMENTED.

  Returns:
    The value at the given index prior to the atomic operation.
  """
  return _atomic_rmw(
      x_ref_or_view, idx, val, mask=mask, atomic_type=AtomicOpType.ADD
  )


def atomic_max(x_ref_or_view, idx, val, *, mask: Any | None = None):
  """Atomically computes ``x_ref_or_view[idx] = max(x_ref_or_view[idx], val)``.

  Args:
    x_ref_or_view: The ref to operate on.
    idx: The indexer to use.
    mask: TO BE DOCUMENTED.

  Returns:
    The value at the given index prior to the atomic operation.
  """
  return _atomic_rmw(
      x_ref_or_view, idx, val, mask=mask, atomic_type=AtomicOpType.MAX
  )


def atomic_min(x_ref_or_view, idx, val, *, mask: Any | None = None):
  """Atomically computes ``x_ref_or_view[idx] = min(x_ref_or_view[idx], val)``.

  Args:
    x_ref_or_view: The ref to operate on.
    idx: The indexer to use.
    mask: TO BE DOCUMENTED.

  Returns:
    The value at the given index prior to the atomic operation.
  """
  return _atomic_rmw(
      x_ref_or_view, idx, val, mask=mask, atomic_type=AtomicOpType.MIN
  )


def atomic_and(x_ref_or_view, idx, val, *, mask: Any | None = None):
  """Atomically computes ``x_ref_or_view[idx] &= val``.

  Args:
    x_ref_or_view: The ref to operate on.
    idx: The indexer to use.
    mask: TO BE DOCUMENTED.

  Returns:
    The value at the given index prior to the atomic operation.
  """
  return _atomic_rmw(
      x_ref_or_view, idx, val, mask=mask, atomic_type=AtomicOpType.AND
  )


def atomic_or(x_ref_or_view, idx, val, *, mask: Any | None = None):
  """Atomically computes ``x_ref_or_view[idx] |= val``.

  Args:
    x_ref_or_view: The ref to operate on.
    idx: The indexer to use.
    mask: TO BE DOCUMENTED.

  Returns:
    The value at the given index prior to the atomic operation.
  """
  return _atomic_rmw(
      x_ref_or_view, idx, val, mask=mask, atomic_type=AtomicOpType.OR
  )


def atomic_xor(x_ref_or_view, idx, val, *, mask: Any | None = None):
  """Atomically computes ``x_ref_or_view[idx] ^= val``.

  Args:
    x_ref_or_view: The ref to operate on.
    idx: The indexer to use.
    mask: TO BE DOCUMENTED.

  Returns:
    The value at the given index prior to the atomic operation.
  """
  return _atomic_rmw(
      x_ref_or_view, idx, val, mask=mask, atomic_type=AtomicOpType.XOR
  )

atomic_cas_p = jax_core.Primitive("atomic_cas")

@atomic_cas_p.def_effectful_abstract_eval
def _atomic_cas_abstract_eval(ref_aval, cmp_aval, val_aval):
  if cmp_aval.dtype != val_aval.dtype or cmp_aval.shape != val_aval.shape:
    raise ValueError("cmp and val must have identical dtypes and shapes")
  if ref_aval.shape:
    raise ValueError("ref must be scalar.")
  if cmp_aval.shape:
    raise ValueError("cmp must be scalar.")
  if val_aval.shape:
    raise ValueError("val must be scalar.")
  return jax_core.ShapedArray(val_aval.shape, val_aval.dtype), {state.WriteEffect(0)}


def atomic_cas(ref, cmp, val):
  """Performs an atomic compare-and-swap of the value in the ref with the
  given value.

  Args:
    ref: The ref to operate on.
    cmp: The expected value to compare against.
    val: The value to swap in.

  Returns:
    The value at the given index prior to the atomic operation.
  """
  return atomic_cas_p.bind(ref, cmp, val)

@state_discharge.register_discharge_rule(atomic_cas_p)
def _atomic_cas_discharge_rule(in_avals, out_avals, ref, cmp, val):
  del in_avals, out_avals
  new_val = jnp.where(ref == cmp, val, ref)
  return (new_val, None, None), ref

max_contiguous_p = jax_core.Primitive("max_contiguous")

max_contiguous_p.def_impl(lambda x, **_: x)
mlir.register_lowering(max_contiguous_p, lambda _, x, **__: [x])

def max_contiguous(x, values):
  if not isinstance(values, (list, tuple)):
    values = (values,)
  return max_contiguous_p.bind(x, values=tuple(values))

@max_contiguous_p.def_abstract_eval
def _max_contiguous_abstract_eval(aval, **_):
  return aval

multiple_of_p = jax_core.Primitive("multiple_of")

multiple_of_p.def_impl(lambda x, **_: x)
mlir.register_lowering(multiple_of_p, lambda _, x, **__: [x])

def multiple_of(x: jax.Array, values: Sequence[int] | int) -> jax.Array:
  values = (values,) if isinstance(values, int) else tuple(values)
  return multiple_of_p.bind(x, values=values)

@multiple_of_p.def_abstract_eval
def _multiple_of_abstract_eval(aval, **_):
  return aval

load_p = jax_core.Primitive('masked_load')


@load_p.def_effectful_abstract_eval
def _load_abstract_eval(*avals_flat, args_tree, **_):
  ref, transforms, _, _ = args_tree.unflatten(avals_flat)
  assert transforms is not None and isinstance(transforms[-1], NDIndexer)
  transformed_ref = pallas_core.TransformedRef(ref, transforms)
  return (
      jax_core.ShapedArray(transformed_ref.shape, transformed_ref.dtype),
      {state.ReadEffect(0)},
  )


def _load_pp_rule(eqn, context, settings):
  # Pretty prints `a = load x i` as `x[i] <- a`
  y, = eqn.outvars
  x, transforms, mask, other = tree_util.tree_unflatten(
      eqn.params["args_tree"], eqn.invars
  )
  # TODO(sharadmv): pretty print mask and other
  lhs = jax_core.pp_vars([y], context, print_shapes=settings.print_shapes)
  result = [lhs, pp.text(" <- "), sp.pp_ref_transforms(context, x, transforms)]
  if mask is not None:
    result += [
        pp.text(" "),
        pp.text("mask="),
        pp.text(jax_core.pp_var(mask, context)),
    ]
  if other is not None:
    result += [
        pp.text(" "),
        pp.text("other="),
        pp.text(jax_core.pp_var(other, context)),
    ]
  return pp.concat(result)
jax_core.pp_eqn_rules[load_p] = _load_pp_rule


def _load_jvp(primals, tangents, args_tree, **params):
  ref_primal, transforms, mask, other_primal = args_tree.unflatten(primals)
  ref_tangent, _, _, other_tangent = args_tree.unflatten(tangents)
  if other_tangent is not None:
    other_tangent = ad_util.instantiate(other_tangent)
  return (
      load_p.bind(
          *tree_util.tree_leaves((ref_primal, transforms, mask, other_primal)),
          args_tree=args_tree,
          **params,
      ),
      load_p.bind(
          *tree_util.tree_leaves(
              (ref_tangent, transforms, mask, other_tangent)
          ),
          args_tree=args_tree,
          **params,
      ),
  )


ad.primitive_jvps[load_p] = _load_jvp

def uninitialized_value(shape, dtype):
  if jnp.issubdtype(dtype, jnp.floating):
    return jnp.full(shape, jnp.nan, dtype)
  # Note: Currently semaphore is i16[], meaning this case needs to be
  # handled before the general case for integers.
  # TODO(justinfu): Handle semaphores with a custom extended dtype.
  elif jnp.issubdtype(dtype, pallas_core.SEMAPHORE_INTERPRET_DTYPE):
    return jnp.full(shape, 0, dtype)
  elif jnp.issubdtype(dtype, jnp.integer):
    return jnp.full(shape, jnp.iinfo(dtype).min, dtype)
  elif jnp.issubdtype(dtype, jnp.bool):
    return jnp.full(shape, False, dtype)
  elif jnp.issubdtype(dtype, pallas_core.semaphore_dtype):
    return jnp.full(shape, 0, dtype)
  raise NotImplementedError(dtype)

def _pad_values_to_avoid_dynamic_slice_oob_shift(value,
                                   slice_sizes, unpad=False):
  """
  DynamicSlice and DynamicUpdateSlice adjust the start index in cases where the
  requested slice overruns the bounds of the array. This pads the array with
  uninitialised values such that the requested slice will never overrun.

  For example, if arr is [1.,2.,3.,4.] and a slice of size 4, start index 2 is
  requested then the result will be [3.,4.,NaN,NaN] after padding, rather than
  [1.,2.,3.,4.] from the unpadded array

  unpad=True performs the inverse operation
  """

  padding_config = tuple((0, slice_size, 0) for slice_size in slice_sizes)
  if unpad:
    padding_config = tuple((-low, -high, -interior)
                           for (low, high, interior) in padding_config)
  padding_value = uninitialized_value(shape=(), dtype=value.dtype)
  value = lax.pad(value,
                  padding_config=padding_config,
                  padding_value=padding_value)
  return value

_unpad_values_to_avoid_dynamic_slice_oob_shift = partial(
  _pad_values_to_avoid_dynamic_slice_oob_shift, unpad=True)


@state_discharge.register_discharge_rule(load_p)
def _load_discharge_rule(in_avals, out_avals, *args_flat, args_tree, **_):
  del out_avals  # Unused.
  ref, transforms, mask, other = args_tree.unflatten(args_flat)
  *prev_transforms, idx = transforms
  assert isinstance(idx, NDIndexer)
  ref = state_discharge.transform_array(ref, prev_transforms)
  if all((isinstance(s, Slice) or not s.shape) for s in idx.indices):
    # TODO(ayx): support strided load/store in interpret mode.
    for s in idx.indices:
      if isinstance(s, Slice) and s.stride > 1:
        raise NotImplementedError("Unimplemented stride support.")
    indices = idx.indices
    scalar_dims = [not isinstance(s, Slice) and not s.shape for s in indices]
    slice_starts = [s.start if isinstance(s, Slice) else s for s in indices]
    slice_sizes = tuple(s.size if isinstance(s, Slice) else 1 for s in indices)
    # fixes an inconsistency with lax.dynamic_slice where if the slice goes out
    # of bounds, it will instead move the start_index backwards so the slice
    # will fit in memory.
    ref = _pad_values_to_avoid_dynamic_slice_oob_shift(ref, slice_sizes)
    idx_dtype = dtypes.canonicalize_dtype(jnp.int64)
    out_ones = lax.dynamic_slice(
        ref,
        [jnp.astype(s, idx_dtype) for s in slice_starts],
        slice_sizes=slice_sizes,
    )
    out_indexer = tuple(0 if scalar else slice(None) for scalar in scalar_dims)
    out = out_ones[out_indexer]
  elif all(not isinstance(s, Slice) for s in idx.indices):
    out = ref[idx.indices]
  else:
    raise NotImplementedError
  if mask is not None and other is not None:
    out = jnp.where(mask, out, other)
  return (None,) * len(in_avals), out


swap_p = jax_core.Primitive('masked_swap')


@swap_p.def_effectful_abstract_eval
def _swap_abstract_eval(*avals_flat, args_tree, **_):
  ref, transforms, val, _ = args_tree.unflatten(avals_flat)
  assert transforms is not None and isinstance(transforms[-1], NDIndexer)
  transformed_ref = pallas_core.TransformedRef(ref, transforms)
  expected_output_shape = transformed_ref.shape
  expected_output_dtype = transformed_ref.dtype
  if expected_output_shape != val.shape:
    raise ValueError(
        f"Invalid shape for `swap`. Ref shape: {ref.shape}. "
        f"Value shape: {val.shape}. Transforms: {transforms}. "
    )
  if expected_output_dtype != val.dtype:
    raise ValueError(
        f"Invalid dtype for `swap`. Ref dtype: {expected_output_dtype}. "
        f"Value dtype: {val.dtype}. "
    )
  return (
      jax_core.ShapedArray(expected_output_shape, expected_output_dtype),
      {state.WriteEffect(0)},
  )


def _swap_pp_rule(eqn, context, settings):
  # Pretty prints `a = swap x v i` as `a, x[i] <- x[i], v`
  # or:
  # Pretty prints `_ = swap x v i` as `x[i] <- v`
  y, = eqn.outvars
  x, transforms, val, mask = eqn.params["args_tree"].unflatten(eqn.invars)
  x_i = sp.pp_ref_transforms(context, x, transforms)
  if isinstance(y, jax_core.DropVar):
    return pp.concat([
        x_i,
        pp.text(" <- "), pp.text(jax_core.pp_var(val, context))])
  y = jax_core.pp_vars([y], context, print_shapes=settings.print_shapes)
  result = [
      y,
      pp.text(", "),
      x_i,
      pp.text(" <- "),
      x_i,
      pp.text(", "),
      pp.text(jax_core.pp_var(val, context)),
  ]
  if mask is not None:
    result += [
        pp.text(" "),
        pp.text("mask="),
        pp.text(jax_core.pp_var(mask, context)),
    ]
  return pp.concat(result)
jax_core.pp_eqn_rules[swap_p] = _swap_pp_rule


def _swap_jvp(primals, tangents, *, args_tree, **params):
  ref_primal, transforms, val_primal, mask = args_tree.unflatten(primals)
  ref_tangent, _, val_tangent, _ = args_tree.unflatten(tangents)
  val_tangent = ad_util.instantiate(val_tangent)
  return (
      swap_p.bind(
          *tree_util.tree_leaves((ref_primal, transforms, val_primal, mask)),
          args_tree=args_tree,
          **params,
      ),
      swap_p.bind(
          *tree_util.tree_leaves((ref_tangent, transforms, val_tangent, mask)),
          args_tree=args_tree,
          **params,
      ),
  )


ad.primitive_jvps[swap_p] = _swap_jvp


@state_discharge.register_discharge_rule(swap_p)
def _swap_discharge_rule(in_avals, out_avals, *args_flat, args_tree, **_):
  del out_avals  # Unused.
  ref, transforms, val, mask = args_tree.unflatten(args_flat)
  *prev_transforms, idx = transforms
  assert isinstance(idx, NDIndexer)
  ref = state_discharge.transform_array(ref, prev_transforms)
  if all((isinstance(s, Slice) or not s.shape) for s in idx.indices):
    # TODO(ayx): support strided load/store in interpret mode.
    for s in idx.indices:
      if isinstance(s, Slice) and s.stride > 1:
        raise NotImplementedError("Unimplemented stride support.")
    indices = idx.indices
    scalar_dims = [
        i
        for i, s in enumerate(indices)
        if not isinstance(s, Slice) and not s.shape
    ]
    slice_starts = [s.start if isinstance(s, Slice) else s for s in indices]
    slice_sizes = tuple(s.size if isinstance(s, Slice) else 1 for s in indices)
    # Fixes an inconsistency with lax.dynamic_update_slice where if the slice
    # goes out of bounds, it will instead move the start_index backwards so the
    # slice will fit in memory.
    ref = _pad_values_to_avoid_dynamic_slice_oob_shift(ref, slice_sizes)
    out = lax.dynamic_slice(ref, slice_starts, slice_sizes=slice_sizes)
    out = jnp.squeeze(out, scalar_dims)
    if mask is not None:
      out_ = out
      out = jnp.where(mask, out, val)
      val = jnp.where(mask, val, out_)
    val = jnp.expand_dims(val, scalar_dims)
    x_new = lax.dynamic_update_slice(ref, val, start_indices=slice_starts)
    x_new = _unpad_values_to_avoid_dynamic_slice_oob_shift(x_new, slice_sizes)
  elif all(not isinstance(s, Slice) for s in idx.indices):
    out = ref[idx.indices]
    if mask is not None:
      out_ = out
      out = jnp.where(mask, out, val)
      val = jnp.where(mask, val, out_)
    x_new = ref.at[idx.indices].set(val)
  else:
    raise NotImplementedError
  return (x_new,) + (None,) * (len(in_avals) - 1), out


def load(x_ref_or_view, idx, *, mask=None, other=None, cache_modifier=None,
         eviction_policy=None, volatile=False) -> jax.Array:
  """Returns an array loaded from the given index.

  If neither ``mask`` nor ``other`` is specified, this function has the same
  semantics as ``x_ref_or_view[idx]`` in JAX.

  Args:
    x_ref_or_view: The ref to load from.
    idx: The indexer to use.
    mask: An optional boolean mask specifying which indices to load.
      If mask is ``False`` and ``other`` is not given, no assumptions can
      be made about the value in the resulting array.
    other: An optional value to use for indices where mask is ``False``.
    cache_modifier: TO BE DOCUMENTED.
    eviction_policy: TO BE DOCUMENTED.
    volatile: TO BE DOCUMENTED.
  """
  x_ref, transforms = sp.get_ref_and_transforms(x_ref_or_view, idx, "load")
  args_flat, args_tree = tree_util.tree_flatten(
      (x_ref, transforms, mask, other)
  )
  return load_p.bind(
      *args_flat,
      args_tree=args_tree,
      cache_modifier=cache_modifier,
      eviction_policy=eviction_policy,
      is_volatile=volatile,
  )

def swap(x_ref_or_view, idx, val, *, mask=None, eviction_policy=None,
         _function_name="swap") -> jax.Array:
  """Swaps the value at the given index and returns the old value.

  See :func:`~jax.experimental.pallas.load` for the meaning of the arguments.

  Returns:
    The value stored in the ref prior to the swap.
  """
  x_ref, transforms = sp.get_ref_and_transforms(
      x_ref_or_view, idx, _function_name
  )
  args_flat, args_tree = tree_util.tree_flatten((x_ref, transforms, val, mask))
  return swap_p.bind(
      *args_flat, args_tree=args_tree, eviction_policy=eviction_policy
  )

def store(x_ref_or_view, idx, val, *, mask=None, eviction_policy=None) -> None:
  """Stores a value at the given index.

  See :func:`~jax.experimental.pallas.load` for the meaning of the arguments.
  """
  _ = swap(x_ref_or_view, idx, val, mask=mask, eviction_policy=eviction_policy,
           _function_name="store")

def dot(a, b, trans_a: bool = False, trans_b: bool = False,
        allow_tf32: bool | None = None, precision=None):
  if (a.ndim != 2) or (b.ndim != 2):
    raise ValueError("`a` and `b` must be 2D arrays.")
  lhs_contract_dim = 0 if trans_a else 1
  rhs_contract_dim = 0 if not trans_b else 1
  if allow_tf32 is not None:
    if precision is not None:
      raise ValueError("Only one of allow_tf32 and precision can be specified")
    precision = lax.Precision.HIGH if allow_tf32 else lax.Precision.HIGHEST

  def _handle_f8(dtype: jax.typing.DTypeLike):
    """Ugly workaround to support float8_e4m3b11fnuz in dot."""
    if dtype == jnp.float8_e4m3b11fnuz:
      return jnp.bfloat16
    return dtype

  dtype = jnp.promote_types(_handle_f8(a.dtype), _handle_f8(b.dtype))
  out_dtype = jnp.int32 if jnp.issubdtype(dtype, jnp.integer) else jnp.float32
  return jax.lax.dot_general(
      a,
      b,
      dimension_numbers=(((lhs_contract_dim,), (rhs_contract_dim,)), ((), ())),
      precision=precision,
      preferred_element_type=out_dtype,
  )

reciprocal_p = jax_core.Primitive("reciprocal")


def reciprocal(x, *, approx=False):
  return reciprocal_p.bind(x, approx=approx)


@reciprocal_p.def_abstract_eval
def _reciprocal_abstract_eval(x, *, approx):
  del approx
  return x


def _reciprocal_lowering_rule(
    ctx: mlir.LoweringRuleContext, x, *, approx=False
):
  def _reciprocal(x, *, approx=False):
    if approx:
      return jnp.reciprocal(x.astype(jnp.bfloat16)).astype(jnp.float32)
    return jnp.reciprocal(x)

  return mlir.lower_fun(_reciprocal, multiple_results=False)(
      ctx, x, approx=approx
  )


mlir.register_lowering(reciprocal_p, _reciprocal_lowering_rule)


class PrintEffect(effects.Effect):
  __str__ = lambda self: "Print"


debug_print_effect = PrintEffect()

# TODO(slebedev): Consider making the effect ordered.
effects.lowerable_effects.add_type(PrintEffect)
effects.control_flow_allowed_effects.add_type(PrintEffect)
effects.remat_allowed_effects.add_type(PrintEffect)
effects.custom_derivatives_allowed_effects.add_type(PrintEffect)


debug_print_p = jax_core.Primitive("debug_print")
debug_print_p.multiple_results = True


def debug_print(fmt: str, *args: jax.typing.ArrayLike):
  """Prints values from inside a Pallas kernel.

  Args:
    fmt: A format string to be included in the output. The restrictions on the
      format string depend on the backend:

      * On GPU, when using Triton, ``fmt`` must not contain any placeholders
        (``{...}``), since it is always printed before any of the values.
      * On GPU, when using the experimental Mosaic GPU backend, ``fmt`` must
        contain a placeholder for each value to be printed. Format specs and
        conversions are not supported. All values must be scalars.
      * On TPU, if all inputs are scalars: If ``fmt`` contains placeholders,
        all values must be 32-bit integers. If there are no placeholders, the
        values are printed after the format string.
      * On TPU, if the input is a single vector, the vector is printed after
        the format string. The format string must end with a single placeholder
        ``{}``.
    *args: The values to print.
  """  # fmt: skip
  has_placeholders = False
  if fmt:
    _, field_name, *_ = next(iter(string.Formatter().parse(fmt)))
    has_placeholders = field_name is not None
  return debug_print_p.bind(*args, fmt=fmt, has_placeholders=has_placeholders)


def check_debug_print_format(
    fmt: str, *args: jax.typing.ArrayLike
):
  n_placeholders = 0
  for _, field, spec, conversion in string.Formatter().parse(fmt):
    if field is not None:
      n_placeholders += 1
    if spec or conversion:
      raise ValueError(
          "The format string should not contain any format specs or conversions"
      )
    if field:
      raise ValueError(
          "The format string should not reference arguments by position or name"
      )

  if len(args) != n_placeholders:
    raise TypeError(
        f"The format string expects {n_placeholders} "
        f"argument{'' if n_placeholders == 1 else 's'}, but got {len(args)}"
    )


@debug_print_p.def_impl
def debug_print_impl(*args: Any, fmt: str, has_placeholders: bool):
  if has_placeholders:
    print(fmt.format(*args))
  else:
    print(fmt, *args)
  return ()


@debug_print_p.def_effectful_abstract_eval
def debug_print_abstract_eval(*avals: Any, fmt: str, has_placeholders: bool):
  del avals, fmt, has_placeholders  # Unused.
  return [], {debug_print_effect}


def debug_print_batching_rule(args, dims, **params):
  """Unrolls the print primitive across the mapped axis."""
  axis_size = next(x.shape[i] for x, i in zip(args, dims) if i is not None)

  # TODO(sharadmv): implement in terms of rolled loop unstead of unrolled.
  def get_arg_at_dim(i, dim, arg):
    if dim is batching.not_mapped:
      # Broadcast unmapped argument
      return arg
    return lax.index_in_dim(arg, i, axis=dim, keepdims=False)

  outs = []
  for i in range(axis_size):
    args_idx = map(functools.partial(get_arg_at_dim, i), dims, args)
    outs.append(debug_print_p.bind(*args_idx, **params))
  outs = [jnp.stack(xs) for xs in zip(*outs)]
  return outs, (0,) * len(outs)


batching.primitive_batchers[debug_print_p] = functools.partial(
    debug_print_batching_rule, debug_print_p
)


@functools.partial(mlir.register_lowering, debug_print_p)
def debug_print_lowering_rule(ctx, *args, **params):
  result, _, _ = callback.emit_python_callback(
      ctx,
      functools.partial(debug_print_p.impl, **params),
      None,
      list(args),
      ctx.avals_in,
      ctx.avals_out,
      has_side_effect=True,
  )
  return result


# All of those shenanigans are because we can't make TransformedRef a PyTree,
# because they should appear as atomic JAX values to the users.
# TODO(apaszke): This can be deleted once we make transforms in Mosaic GPU
# inferred by the compiler.
@lu.transformation2
def wrap_with_transforms(f, transforms, *args):
  new_args = tuple(
      state_types.TransformedRef(a, t) if t else a
      for a, t in zip(args, transforms)
  )
  return f(*new_args)


run_scoped_p = jax_core.Primitive("run_scoped")
run_scoped_p.multiple_results = True


def run_scoped(
    f: Callable[..., Any],
    *types: Any,
    collective_axes: Hashable | tuple[Hashable, ...] = (),
    **kw_types: Any,
) -> Any:
  """Calls the function with allocated references and returns the result.

  The positional and keyword arguments describe which reference types
  to allocate for each argument. Each backend has its own set of reference
  types in addition to :class:`jax.experimental.pallas.MemoryRef`.

  When `collective_axes` is specified, the same allocation will be returned for
  all programs that only differ in their program ids along the collective axes.
  It is an error not to call the same `run_scoped` in all programs along that
  axis.
  """
  if not isinstance(collective_axes, tuple):
    collective_axes = (collective_axes,)
  flat_types, in_tree = tree_util.tree_flatten((types, kw_types))
  flat_fun, out_tree_thunk = api_util.flatten_fun(
      lu.wrap_init(f,
                   debug_info=api_util.debug_info("pallas run_scoped",
                                                  f, types, kw_types)),
      in_tree)
  # We allow ref avals to be transformed references.
  ref_avals = [t.get_ref_aval() for t in flat_types]
  avals = [
      t.ref if isinstance(t, state_types.TransformedRef) else t
      for t in ref_avals
  ]
  ref_transforms = tuple(
      t.transforms if isinstance(t, state_types.TransformedRef) else ()
      for t in ref_avals
  )
  flat_fun = wrap_with_transforms(flat_fun, ref_transforms)
  # Turn the function into a jaxpr. The body of run_scoped may have
  # effects (IO) on constvars (i.e. variables inherited from the
  # parent scope). Jax can't reason about effects to references that
  # are not in the invars of an operation so we just put them all
  # there.
  jaxpr, _, consts = pe.trace_to_jaxpr_dynamic(flat_fun, avals)
  out = run_scoped_p.bind(*consts, jaxpr=jaxpr, collective_axes=collective_axes)
  return tree_util.tree_unflatten(out_tree_thunk(), out)


@run_scoped_p.def_effectful_abstract_eval
def _run_scoped_abstract_eval(*args, jaxpr, collective_axes):
  del args, collective_axes
  # jaxpr will have effects for its inputs (Refs that are allocated) and for
  # constvars (closed over Refs). The effects for the allocated Refs are local
  # to the jaxpr and shouldn't propagate out.
  nonlocal_effects = {
      eff
      for eff in jaxpr.effects
      if not (
          isinstance(eff, effects.JaxprInputEffect)
          and eff.input_index >= len(jaxpr.constvars)
      )
  }
  return [v.aval for v in jaxpr.outvars], nonlocal_effects


def _run_scoped_discharge_rule(
    should_discharge,
    in_avals,
    out_avals,
    *args_flat,
    jaxpr,
    collective_axes):
  del out_avals
  if collective_axes:
    raise NotImplementedError(
        "run_scoped discharge does not support collective_axes yet."
    )
  num_consts = len(args_flat)
  # discharge_state only discharges invars, not consts, so in order to
  # discharge the requested refs we need to move them to the invar set.
  jaxpr_noconst = pe.convert_constvars_jaxpr(jaxpr)
  num_return_values = len(jaxpr_noconst.outvars)
  discharged_body, new_consts = state_discharge.discharge_state(
      jaxpr_noconst,
      [],
      should_discharge=should_discharge + [False] * len(jaxpr.invars),
  )
  if new_consts:
    raise NotImplementedError(
        "Cannot handle new consts created by state discharge.")

  # Lowering expects that the jaxpr.consts to be the eqn.invals.
  discharged_body = pe.convert_invars_to_constvars(discharged_body, num_consts)

  # Run_scoped discharged the external variables but the scoped ones
  # are not discharged.
  out = run_scoped_p.bind(
      *args_flat, jaxpr=discharged_body, collective_axes=collective_axes
  )
  # Order of outputs:
  # (1) return values, (2) closed refs, (3) scoped refs.
  return_values = out[:num_return_values]
  ref_outputs = out[num_return_values:]
  # We update all ref values with their updated values from the discharged
  # body. For other values we leave them in place.
  updates = [
      ref_outputs.pop(0) if should and isinstance(aval, state.AbstractRef)
      else None for should, aval in zip(should_discharge, in_avals)]
  assert len(updates) == len(in_avals), f'{len(updates)} != {len(in_avals)}'
  return updates, return_values


state_discharge.register_partial_discharge_rule(run_scoped_p)(
    _run_scoped_discharge_rule)


@functools.partial(mlir.register_lowering, run_scoped_p)
def _run_scoped_lowering_rule(ctx, *args, jaxpr, collective_axes):
  if collective_axes:
    raise ValueError(
        "run_scoped lowering outside of Pallas does not support"
        " collective_axes."
    )
  jaxpr_noconst = pe.convert_constvars_jaxpr(jaxpr)
  num_return_values = len(jaxpr_noconst.outvars)
  discharged_body, new_consts = state_discharge.discharge_state(
      jaxpr_noconst, [], should_discharge=True)
  if new_consts:    raise NotImplementedError(
        "Cannot handle new consts created by state discharge.")

  def _lower_fun(*lower_fun_args):
    # Create inputs filled with uninitialized values to the body.
    num_consts = len(lower_fun_args)
    body_avals = [v.aval for v in discharged_body.invars[num_consts:]]
    init_vals = [uninitialized_value(
        aval.shape, aval.dtype) for aval in body_avals]
    out = jax_core.eval_jaxpr(discharged_body, [], *lower_fun_args, *init_vals)
    return out[:num_return_values]

  return mlir.lower_fun(_lower_fun, multiple_results=True)(ctx, *args)


def _get_ref_and_transforms(ref):
  if isinstance(ref, state.TransformedRef):
    return ref.ref, ref.transforms
  return ref, ()


class DeviceIdType(enum.Enum):
  MESH = "mesh"
  LOGICAL = "logical"


def check_sem_avals(
    sem_aval, sem_transforms_avals, name, allowed_semaphore_types=None
):
  if allowed_semaphore_types is None:
    allowed_semaphore_types = {
        pallas_core.semaphore,
        pallas_core.barrier_semaphore,
        # For interpret mode.
        pallas_core.SEMAPHORE_INTERPRET_DTYPE,
    }
  if not isinstance(sem_aval, state.AbstractRef):
    raise ValueError(f"Cannot {name} on a non-semaphore Ref: {sem_aval}")
  sem_shape = sem_aval.shape
  if sem_transforms_avals:
    sem_shape = sem_transforms_avals[-1].get_indexer_shape()
  if sem_shape:
    raise ValueError(f"Cannot {name} on a non-()-shaped semaphore: {sem_shape}")
  sem_dtype = sem_aval.dtype
  if not any(
      jnp.issubdtype(sem_dtype, sem_type)
      for sem_type in allowed_semaphore_types
  ):
    raise ValueError(
        f"Must {name} semaphores of the following types:"
        f" {allowed_semaphore_types}."
    )


def _transform_semaphore(ref_value, transforms, ref_aval):
  """Helper function for indexing into a semaphore during state_discharge."""
  if ref_value.shape == ref_aval.shape:
    return state_discharge.transform_array(ref_value, transforms)
  elif len(ref_value.shape) == 0:
    return ref_value
  else:
    raise ValueError(
        f"Semaphore value shape {ref_value.shape} does not match aval shape"
        f" {ref_aval.shape}"
    )


semaphore_read_p = jax_core.Primitive("semaphore_read")
semaphore_read_p.multiple_results = False


def semaphore_read(sem_or_view):
  ref, transforms = _get_ref_and_transforms(sem_or_view)
  args = [ref, transforms]
  flat_args, args_tree = tree_util.tree_flatten(args)
  return semaphore_read_p.bind(*flat_args, args_tree=args_tree)

@semaphore_read_p.def_abstract_eval
def _semaphore_read_abstract_eval(
    *avals,
    args_tree,
):
  del avals, args_tree
  return jax_core.ShapedArray((), jnp.dtype("int32"))

def _semaphore_read_discharge_rule(in_avals,
                                   out_avals,
                                   *flat_args,
                                   args_tree):
  del out_avals
  [ref, transforms] = args_tree.unflatten(flat_args)
  sem_value = _transform_semaphore(ref, transforms, in_avals[0])
  sem_value = sem_value.astype(jnp.int32)
  return (None,) * len(in_avals), sem_value
state_discharge.register_discharge_rule(semaphore_read_p)(
    _semaphore_read_discharge_rule
)


semaphore_signal_p = jax_core.Primitive('semaphore_signal')
semaphore_signal_p.multiple_results = True


def semaphore_signal(
    sem_or_view,
    inc: int | jax.Array = 1,
    *,
    device_id: int | jax.Array | None | tuple[int | jax.Array, ...] = None,
    device_id_type: DeviceIdType = DeviceIdType.MESH,
    core_index: int | jax.Array | None = None,
):
  ref, transforms = _get_ref_and_transforms(sem_or_view)
  inc = jnp.asarray(inc, dtype=jnp.int32)
  args = [ref, transforms, inc, device_id, core_index]
  flat_args, args_tree = tree_util.tree_flatten(args)
  semaphore_signal_p.bind(
      *flat_args,
      args_tree=args_tree,
      device_id_type=device_id_type,
  )


# TODO(sharadmv,ivyzheng): remove this once we use axis dicts primarily
class CommsEffect(effects.Effect):
  pass
_comms_effect = CommsEffect()
effects.lowerable_effects.add_type(CommsEffect)
effects.control_flow_allowed_effects.add_type(CommsEffect)
effects.remat_allowed_effects.add_type(CommsEffect)
effects.custom_derivatives_allowed_effects.add_type(CommsEffect)


@semaphore_signal_p.def_effectful_abstract_eval
def _semaphore_signal_abstract_eval(
    *avals,
    args_tree,
    device_id_type: DeviceIdType,
):
  del device_id_type
  (
      sem_aval,
      sem_transforms_avals,
      value_aval,
      device_id_avals,
      core_index_aval,
  ) = tree_util.tree_unflatten(args_tree, avals)
  check_sem_avals(sem_aval, sem_transforms_avals, "signal")
  if value_aval.dtype != jnp.dtype("int32"):
    raise ValueError("Must signal an int32 value.")
  effs : set[effects.Effect] = set()
  if device_id_avals is not None:
    device_id_flat_avals = tree_util.tree_leaves(device_id_avals)
    for aval in device_id_flat_avals:
      if aval.dtype != jnp.dtype("int32"):
        raise ValueError("`device_id`s must be an int32 value.")
    effs.add(_comms_effect)
  return [], effs


def _semaphore_signal_pp_eqn(eqn: jax_core.JaxprEqn,
                             context: jax_core.JaxprPpContext,
                             settings: jax_core.JaxprPpSettings):
  del settings
  invars = eqn.invars
  tree = eqn.params["args_tree"]
  (
      sem,
      sem_transforms,
      value,
      device_ids,
      _,
  ) = tree_util.tree_unflatten(tree, invars)
  out = pp.concat([
      pp.text("semaphore_signal"),
      pp.text(" "),
      sp.pp_ref_transforms(context, sem, sem_transforms),
      pp.text(" "),
      pp.text(jax_core.pp_var(value, context)),
  ])
  if device_ids is not None:
    flat_device_ids = tree_util.tree_leaves(device_ids)
    if not flat_device_ids:
      return out
    device_ids_pp = [pp.text(jax_core.pp_var(flat_device_ids[0], context))]
    for device_id in flat_device_ids[1:]:
      device_ids_pp.append(pp.text(" "))
      device_ids_pp.append(pp.text(jax_core.pp_var(device_id, context)))
    out = pp.concat([out, pp.concat(device_ids_pp)])
  return out
jax_core.pp_eqn_rules[semaphore_signal_p] = _semaphore_signal_pp_eqn


def _semaphore_signal_discharge_rule(in_avals,
                                     out_avals,
                                     *flat_args,
                                     args_tree,
                                     device_id_type):
  del out_avals, device_id_type
  [ref, transforms, inc, device_id, core_index] = args_tree.unflatten(flat_args)
  if device_id is not None:
    raise NotImplementedError("Remote signal not implemented.")
  if core_index is not None:
    raise NotImplementedError("Multiple core support not implemented.")
  sem_value = _transform_semaphore(ref, transforms, in_avals[0])
  inc = inc.astype(pallas_core.SEMAPHORE_INTERPRET_DTYPE)
  _, new_sem_value = state_discharge.transform_swap_array(
      ref, transforms, sem_value + inc
  )
  return (new_sem_value,) + (None,) * (len(in_avals) - 1), ()
state_discharge.register_discharge_rule(semaphore_signal_p)(
    _semaphore_signal_discharge_rule
)


semaphore_wait_p = jax_core.Primitive('semaphore_wait')
semaphore_wait_p.multiple_results = True

def semaphore_wait(sem_or_view, dec: int | jax.Array = 1):
  ref, transforms = _get_ref_and_transforms(sem_or_view)
  dec = jnp.asarray(dec, dtype=jnp.int32)
  args = [ref, transforms, dec]
  flat_args, args_tree = tree_util.tree_flatten(args)
  semaphore_wait_p.bind(*flat_args, args_tree=args_tree)

@semaphore_wait_p.def_abstract_eval
def _semaphore_wait_abstract_eval(*avals, args_tree):
  sem_aval, sem_transforms_avals, value_aval = tree_util.tree_unflatten(
      args_tree, avals
  )
  check_sem_avals(sem_aval, sem_transforms_avals, "wait")
  if value_aval.dtype != jnp.dtype("int32"):
    raise ValueError("Must wait an int32 value.")
  return []

def _semaphore_wait_pp_eqn(eqn: jax_core.JaxprEqn,
                             context: jax_core.JaxprPpContext,
                             settings: jax_core.JaxprPpSettings):
  del settings
  invars = eqn.invars
  tree = eqn.params["args_tree"]
  (
      sem,
      sem_transforms,
      value,
  ) = tree_util.tree_unflatten(tree, invars)
  return pp.concat([
      pp.text("semaphore_wait"),
      pp.text(" "),
      sp.pp_ref_transforms(context, sem, sem_transforms),
      pp.text(" "),
      pp.text(jax_core.pp_var(value, context)),
  ])
jax_core.pp_eqn_rules[semaphore_wait_p] = _semaphore_wait_pp_eqn

def _semaphore_wait_discharge_rule(in_avals,
                                     out_avals,
                                     *flat_args,
                                     args_tree):
  del out_avals
  [ref, transforms, dec] = args_tree.unflatten(flat_args)
  sem_value = _transform_semaphore(ref, transforms, in_avals[0])
  dec = dec.astype(pallas_core.SEMAPHORE_INTERPRET_DTYPE)
  _, new_sem_value = state_discharge.transform_swap_array(
      ref, transforms, sem_value - dec
  )
  return (new_sem_value,) + (None,) * (len(in_avals) - 1), ()
state_discharge.register_discharge_rule(semaphore_wait_p)(
    _semaphore_wait_discharge_rule
)

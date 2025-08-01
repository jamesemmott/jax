# Copyright 2019 The JAX Authors.
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

"""Shared neural network activations and other functions."""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
import operator
import math
import numpy as np
from typing import Any, Literal
import warnings

from jax._src import api
from jax._src import config
from jax._src import core
from jax._src import custom_derivatives
from jax._src import deprecations
from jax._src import dtypes
from jax._src import lax
from jax._src import numpy as jnp
from jax._src import util
from jax._src.core import AxisName
from jax._src.cudnn.fused_attention_stablehlo import (
    dot_product_attention as cudnn_dot_product_attention, MaskType)
from jax._src.cudnn.scaled_matmul_stablehlo import (
    scaled_matmul_wrapper as cudnn_scaled_matmul,
    scaled_dot_general_wrapper as cudnn_scaled_dot_general,
    BlockScaleConfig)
from jax._src.interpreters import batching
from jax._src.interpreters import mlir
from jax._src.numpy import einsum as jnp_einsum
from jax._src.numpy import util as numpy_util
from jax._src.sharding_impls import NamedSharding, PartitionSpec as P
from jax._src.typing import Array, ArrayLike, DType, DTypeLike
from jax._src.ops.special import logsumexp as _logsumexp


# activations
@api.jit
def identity(x: ArrayLike) -> Array:
  r"""Identity activation function.

  Returns the argument unmodified.

  Args:
    x : input array

  Returns:
    The argument `x` unmodified.

  Examples:
    >>> jax.nn.identity(jax.numpy.array([-2., -1., -0.5, 0, 0.5, 1., 2.]))
    Array([-2. , -1. , -0.5, 0. , 0.5, 1. , 2. ], dtype=float32)

  """
  numpy_util.check_arraylike("identity", x)
  return jnp.asarray(x)

@custom_derivatives.custom_jvp
@api.jit
def relu(x: ArrayLike) -> Array:
  r"""Rectified linear unit activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{relu}(x) = \max(x, 0)

  except under differentiation, we take:

  .. math::
    \nabla \mathrm{relu}(0) = 0

  For more information see
  `Numerical influence of ReLU’(0) on backpropagation
  <https://dl.acm.org/doi/10.5555/3540261.3540297>`_.

  Args:
    x : input array

  Returns:
    An array.

  Examples:
    >>> jax.nn.relu(jax.numpy.array([-2., -1., -0.5, 0, 0.5, 1., 2.]))
    Array([0. , 0. , 0. , 0. , 0.5, 1. , 2. ], dtype=float32)

  See also:
    :func:`relu6`

  """
  return jnp.maximum(x, 0)
# For behavior at 0, see https://dl.acm.org/doi/10.5555/3540261.3540297
relu.defjvps(lambda g, ans, x: lax.select(x > 0, g, lax.full_like(g, 0)))

@api.jit
def squareplus(x: ArrayLike, b: ArrayLike = 4) -> Array:
  r"""Squareplus activation function.

  Computes the element-wise function

  .. math::
    \mathrm{squareplus}(x) = \frac{x + \sqrt{x^2 + b}}{2}

  as described in https://arxiv.org/abs/2112.11687.

  Args:
    x : input array
    b : smoothness parameter
  """
  numpy_util.check_arraylike("squareplus", x)
  numpy_util.check_arraylike("squareplus", b)
  x = jnp.asarray(x)
  b = jnp.asarray(b)
  y = x + jnp.sqrt(jnp.square(x) + b)
  return y / 2

@api.jit
def softplus(x: ArrayLike) -> Array:
  r"""Softplus activation function.

  Computes the element-wise function

  .. math::
    \mathrm{softplus}(x) = \log(1 + e^x)

  Args:
    x : input array
  """
  return jnp.logaddexp(x, 0)

@api.jit
def sparse_plus(x: ArrayLike) -> Array:
  r"""Sparse plus function.

  Computes the function:

  .. math::

    \mathrm{sparse\_plus}(x) = \begin{cases}
      0, & x \leq -1\\
      \frac{1}{4}(x+1)^2, & -1 < x < 1 \\
      x, & 1 \leq x
    \end{cases}

  This is the twin function of the softplus activation ensuring a zero output
  for inputs less than -1 and a linear output for inputs greater than 1,
  while remaining smooth, convex, monotonic by an adequate definition between
  -1 and 1.

  Args:
    x: input (float)
  """
  numpy_util.check_arraylike("sparse_plus", x)
  x = jnp.asarray(x)
  return jnp.where(x <= -1.0, 0.0, jnp.where(x >= 1.0, x, (x + 1.0)**2/4))

@api.jit
def soft_sign(x: ArrayLike) -> Array:
  r"""Soft-sign activation function.

  Computes the element-wise function

  .. math::
    \mathrm{soft\_sign}(x) = \frac{x}{|x| + 1}

  Args:
    x : input array
  """
  numpy_util.check_arraylike("soft_sign", x)
  x_arr = jnp.asarray(x)
  return x_arr / (jnp.abs(x_arr) + 1)

@partial(api.jit, inline=True)
def sigmoid(x: ArrayLike) -> Array:
  r"""Sigmoid activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{sigmoid}(x) = \frac{1}{1 + e^{-x}}

  Args:
    x : input array

  Returns:
    An array.

  See also:
    :func:`log_sigmoid`

  """
  return lax.logistic(x)

@api.jit
def sparse_sigmoid(x: ArrayLike) -> Array:
  r"""Sparse sigmoid activation function.

  Computes the function:

  .. math::

    \mathrm{sparse\_sigmoid}(x) = \begin{cases}
      0, & x \leq -1\\
      \frac{1}{2}(x+1), & -1 < x < 1 \\
      1, & 1 \leq x
    \end{cases}

  This is the twin function of the ``sigmoid`` activation ensuring a zero output
  for inputs less than -1, a 1 output for inputs greater than 1, and a linear
  output for inputs between -1 and 1. It is the derivative of ``sparse_plus``.

  For more information, see `Learning with Fenchel-Young Losses (section 6.2)
  <https://arxiv.org/abs/1901.02324>`_.

  Args:
    x : input array

  Returns:
    An array.

  See also:
    :func:`sigmoid`
  """
  return 0.5 * jnp.clip(x + 1.0, 0.0, 2.0)

@api.jit
def silu(x: ArrayLike) -> Array:
  r"""SiLU (aka swish) activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{silu}(x) = x \cdot \mathrm{sigmoid}(x) = \frac{x}{1 + e^{-x}}

  :func:`swish` and :func:`silu` are both aliases for the same function.

  Args:
    x : input array

  Returns:
    An array.

  See also:
    :func:`sigmoid`
  """
  numpy_util.check_arraylike("silu", x)
  x_arr = jnp.asarray(x)
  return x_arr * sigmoid(x_arr)

swish = silu

@api.jit
def mish(x: ArrayLike) -> Array:
  r"""Mish activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{mish}(x) = x \cdot \mathrm{tanh}(\mathrm{softplus}(x))

  For more information, see
  `Mish: A Self Regularized Non-Monotonic Activation Function
  <https://arxiv.org/abs/1908.08681>`_.

  Args:
    x : input array

  Returns:
    An array.
  """
  numpy_util.check_arraylike("mish", x)
  x_arr = jnp.asarray(x)
  return x_arr * jnp.tanh(softplus(x_arr))

@api.jit
def log_sigmoid(x: ArrayLike) -> Array:
  r"""Log-sigmoid activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{log\_sigmoid}(x) = \log(\mathrm{sigmoid}(x)) = -\log(1 + e^{-x})

  Args:
    x : input array

  Returns:
    An array.

  See also:
    :func:`sigmoid`
  """
  numpy_util.check_arraylike("log_sigmoid", x)
  x_arr = jnp.asarray(x)
  return -softplus(-x_arr)

@api.jit
def elu(x: ArrayLike, alpha: ArrayLike = 1.0) -> Array:
  r"""Exponential linear unit activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{elu}(x) = \begin{cases}
      x, & x > 0\\
      \alpha \left(\exp(x) - 1\right), & x \le 0
    \end{cases}

  Args:
    x : input array
    alpha : scalar or array of alpha values (default: 1.0)

  Returns:
    An array.

  See also:
    :func:`selu`
  """
  numpy_util.check_arraylike("elu", x)
  x_arr = jnp.asarray(x)
  return jnp.where(x_arr > 0,
                   x_arr,
                   alpha * jnp.expm1(jnp.where(x_arr > 0, 0., x_arr)))

@api.jit
def leaky_relu(x: ArrayLike, negative_slope: ArrayLike = 1e-2) -> Array:
  r"""Leaky rectified linear unit activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{leaky\_relu}(x) = \begin{cases}
      x, & x \ge 0\\
      \alpha x, & x < 0
    \end{cases}

  where :math:`\alpha` = :code:`negative_slope`.

  Args:
    x : input array
    negative_slope : array or scalar specifying the negative slope (default: 0.01)

  Returns:
    An array.

  See also:
    :func:`relu`
  """
  numpy_util.check_arraylike("leaky_relu", x)
  x_arr = jnp.asarray(x)
  return jnp.where(x_arr >= 0, x_arr, negative_slope * x_arr)

@api.jit
def hard_tanh(x: ArrayLike) -> Array:
  r"""Hard :math:`\mathrm{tanh}` activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{hard\_tanh}(x) = \begin{cases}
      -1, & x < -1\\
      x, & -1 \le x \le 1\\
      1, & 1 < x
    \end{cases}

  Args:
    x : input array

  Returns:
    An array.
  """
  numpy_util.check_arraylike("hard_tanh", x)
  x_arr = jnp.asarray(x)
  return jnp.where(x_arr > 1, 1, jnp.where(x_arr < -1, -1, x_arr))

@api.jit
def celu(x: ArrayLike, alpha: ArrayLike = 1.0) -> Array:
  r"""Continuously-differentiable exponential linear unit activation.

  Computes the element-wise function:

  .. math::
    \mathrm{celu}(x) = \begin{cases}
      x, & x > 0\\
      \alpha \left(\exp(\frac{x}{\alpha}) - 1\right), & x \le 0
    \end{cases}

  For more information, see
  `Continuously Differentiable Exponential Linear Units
  <https://arxiv.org/abs/1704.07483>`_.

  Args:
    x : input array
    alpha : array or scalar (default: 1.0)

  Returns:
    An array.
  """
  return jnp.maximum(x, 0.0) + alpha * jnp.expm1(jnp.minimum(x, 0.0) / alpha)

@api.jit
def selu(x: ArrayLike) -> Array:
  r"""Scaled exponential linear unit activation.

  Computes the element-wise function:

  .. math::
    \mathrm{selu}(x) = \lambda \begin{cases}
      x, & x > 0\\
      \alpha e^x - \alpha, & x \le 0
    \end{cases}

  where :math:`\lambda = 1.0507009873554804934193349852946` and
  :math:`\alpha = 1.6732632423543772848170429916717`.

  For more information, see
  `Self-Normalizing Neural Networks
  <https://arxiv.org/abs/1706.02515>`_.

  Args:
    x : input array

  Returns:
    An array.

  See also:
    :func:`elu`
  """
  alpha = 1.6732632423543772848170429916717
  scale = 1.0507009873554804934193349852946
  return scale * elu(x, alpha)

# TODO(phawkins): this jit was found to change numerics in a test. Debug this.
# @partial(api.jit, static_argnames=("approximate",))
def gelu(x: ArrayLike, approximate: bool = True) -> Array:
  r"""Gaussian error linear unit activation function.

  If ``approximate=False``, computes the element-wise function:

  .. math::
    \mathrm{gelu}(x) = \frac{x}{2} \left(\mathrm{erfc} \left(
      \frac{-x}{\sqrt{2}} \right) \right)

  If ``approximate=True``, uses the approximate formulation of GELU:

  .. math::
    \mathrm{gelu}(x) = \frac{x}{2} \left(1 + \mathrm{tanh} \left(
      \sqrt{\frac{2}{\pi}} \left(x + 0.044715 x^3 \right) \right) \right)

  For more information, see `Gaussian Error Linear Units (GELUs)
  <https://arxiv.org/abs/1606.08415>`_, section 2.

  Args:
    x: input array
    approximate: whether to use the approximate or exact formulation.
  """
  [x_arr] = numpy_util.promote_args_inexact("gelu", x)

  if approximate:
    sqrt_2_over_pi = np.sqrt(2 / np.pi).astype(x_arr.dtype)
    cdf = 0.5 * (1.0 + jnp.tanh(sqrt_2_over_pi * (x_arr + 0.044715 * (x_arr ** 3))))
    return x_arr * cdf
  else:
    sqrt_half = np.sqrt(0.5).astype(x_arr.dtype)
    return jnp.array(
        0.5 * x_arr * (lax.erfc(-x_arr * sqrt_half)), dtype=x_arr.dtype
    )

@partial(api.jit, static_argnames=("axis",))
def glu(x: ArrayLike, axis: int = -1) -> Array:
  r"""Gated linear unit activation function.

  Computes the function:

  .. math::
    \mathrm{glu}(x) =  x\left[\ldots, 0:\frac{n}{2}, \ldots\right] \cdot
      \mathrm{sigmoid} \left( x\left[\ldots, \frac{n}{2}:n, \ldots\right]
        \right)

  where the array is split into two along ``axis``. The size of the ``axis``
  dimension must be divisible by two.

  Args:
    x : input array
    axis: the axis along which the split should be computed (default: -1)

  Returns:
    An array.

  See also:
    :func:`sigmoid`
  """
  numpy_util.check_arraylike("glu", x)
  x_arr = jnp.asarray(x)
  size = x_arr.shape[axis]
  assert size % 2 == 0, "axis size must be divisible by 2"
  x1, x2 = jnp.split(x_arr, 2, axis)
  return x1 * sigmoid(x2)

# other functions

logsumexp = _logsumexp


@partial(api.jit, static_argnames=("axis",))
def log_softmax(x: ArrayLike,
                axis: int | tuple[int, ...] | None = -1,
                where: ArrayLike | None = None) -> Array:
  r"""Log-Softmax function.

  Computes the logarithm of the :code:`softmax` function, which rescales
  elements to the range :math:`[-\infty, 0)`.

  .. math ::
    \mathrm{log\_softmax}(x)_i = \log \left( \frac{\exp(x_i)}{\sum_j \exp(x_j)}
    \right)

  Args:
    x : input array
    axis: the axis or axes along which the :code:`log_softmax` should be
      computed. Either an integer or a tuple of integers.
    where: Elements to include in the :code:`log_softmax`. The output for any
      masked-out element is minus infinity.

  Returns:
    An array.

  Note:
    If any input values are ``+inf``, the result will be all ``NaN``: this reflects the
    fact that ``inf / inf`` is not well-defined in the context of floating-point math.

  See also:
    :func:`softmax`
  """
  numpy_util.check_arraylike("log_softmax", x)
  x_arr = jnp.asarray(x)
  x_max = jnp.max(x_arr, axis, where=where, initial=-np.inf, keepdims=True)
  x_safe = x_arr if where is None else jnp.where(where, x_arr, -np.inf)
  shifted = x_safe - lax.stop_gradient(x_max)
  shifted_logsumexp = jnp.log(
      jnp.sum(jnp.exp(shifted), axis, where=where, keepdims=True))
  result = shifted - shifted_logsumexp
  if where is not None:
    return jnp.where(where, result, -np.inf)
  return result


# TODO(phawkins): this jit was found to change numerics in a test. Debug this.
# @partial(api.jit, static_argnames=("axis",))
def softmax(x: ArrayLike,
            axis: int | tuple[int, ...] | None = -1,
            where: ArrayLike | None = None) -> Array:
  r"""Softmax function.

  Computes the function which rescales elements to the range :math:`[0, 1]`
  such that the elements along :code:`axis` sum to :math:`1`.

  .. math ::
    \mathrm{softmax}(x) = \frac{\exp(x_i)}{\sum_j \exp(x_j)}

  Args:
    x : input array
    axis: the axis or axes along which the softmax should be computed. The
      softmax output summed across these dimensions should sum to :math:`1`.
      Either an integer or a tuple of integers.
    where: Elements to include in the :code:`softmax`. The output for any
      masked-out element is zero.

  Returns:
    An array.

  Note:
    If any input values are ``+inf``, the result will be all ``NaN``: this reflects the
    fact that ``inf / inf`` is not well-defined in the context of floating-point math.

  See also:
    :func:`log_softmax`
  """
  if config.softmax_custom_jvp.value:
    # mypy is confused by the `functools.partial` application in the definition
    # of `_softmax` and incorrectly concludes that `_softmax` returns
    # `ReturnValue` -- the unsubstituted type parameter of `custom_jvp`.
    return _softmax(x, axis, where)
  else:
    return _softmax_deprecated(x, axis, where)

# TODO(mattjj): replace softmax with _softmax when deprecation flag is removed
@partial(custom_derivatives.custom_jvp, nondiff_argnums=(1,))
def _softmax(
    x: ArrayLike,
    axis: int | tuple[int, ...] | None = -1,
    where: ArrayLike | None = None,
    initial: ArrayLike | None = -np.inf) -> Array:
  x_max = jnp.max(x, axis, where=where, initial=initial, keepdims=True)
  x_safe = x if where is None else jnp.where(where, x, initial)
  unnormalized = jnp.exp(x_safe - x_max)
  result = unnormalized / jnp.sum(unnormalized, axis, where=where, keepdims=True)
  if where is not None:
    result = jnp.where(where, result, 0)
  return result

@_softmax.defjvp
def _softmax_jvp(axis, primals, tangents):
  (x, where, initial), (x_dot, _, _) = primals, tangents
  y = _softmax(x, axis, where, initial)
  return y, y * (x_dot - (y * x_dot).sum(axis, where=where, keepdims=True))

def _softmax_deprecated(
    x: ArrayLike,
    axis: int | tuple[int, ...] | None = -1,
    where: ArrayLike | None = None,
    initial: ArrayLike | None = -np.inf) -> Array:
  x_max = jnp.max(x, axis, where=where, initial=initial, keepdims=True)
  x_safe = x if where is None else jnp.where(where, x, initial)
  unnormalized = jnp.exp(x_safe - lax.stop_gradient(x_max))
  result = unnormalized / jnp.sum(unnormalized, axis, where=where, keepdims=True)
  if where is not None:
    result = jnp.where(where, result, 0)
  return result


@partial(api.jit, static_argnames=("axis",))
def standardize(x: ArrayLike,
                axis: int | tuple[int, ...] | None = -1,
                mean: ArrayLike | None = None,
                variance: ArrayLike | None = None,
                epsilon: ArrayLike = 1e-5,
                where: ArrayLike | None = None) -> Array:
  r"""Standardizes input to zero mean and unit variance.

  The standardization is given by:

  .. math::

     x_{std} = \frac{x - \langle x\rangle}{\sqrt{\langle(x - \langle x\rangle)^2\rangle + \epsilon}}

  where :math:`\langle x\rangle` indicates the mean of :math:`x`, and :math:`\epsilon` is
  a small correction factor introduced to avoid division by zero.

  Args:
    x: input array to be standardized.
    axis: integer or tuple of integers representing the axes along which
      to standardize. Defaults to the last axis (``-1``).
    mean: optionally specify the mean used for standardization. If not specified,
      then ``x.mean(axis, where=where)`` will be used.
    variance: optionally specify the variance used for standardization. If not
      specified, then ``x.var(axis, where=where)`` will be used.
    epsilon: correction factor added to variance to avoid division by zero; defaults
      to ``1E-5``.
    where: optional boolean mask specifying which elements to use when computing
      the mean and variance.

  Returns:
    An array of the same shape as ``x`` containing the standardized input.
  """
  numpy_util.check_arraylike("standardize", x)
  numpy_util.check_arraylike_or_none("standardize", mean, variance, where)
  if mean is None:
    mean = jnp.mean(x, axis, keepdims=True, where=where)
  if variance is None:
    # this definition is traditionally seen as less accurate than jnp.var's
    # mean((x - mean(x))**2) but may be faster and even, given typical
    # activation distributions and low-precision arithmetic, more accurate
    # when used in neural network normalization layers
    variance = jnp.mean(
        jnp.square(x), axis, keepdims=True, where=where) - jnp.square(mean)
  return jnp.subtract(x, jnp.asarray(mean)) * lax.rsqrt(jnp.asarray(variance) + epsilon)

# TODO(slebedev): Change the type of `x` to `ArrayLike`.
@partial(api.jit, static_argnames=("num_classes", "dtype", "axis"))
def _one_hot(x: Array, num_classes: int, *,
             dtype: Any, axis: int | AxisName) -> Array:
  num_classes = core.concrete_dim_or_error(
      num_classes,
      "The error arose in jax.nn.one_hot argument `num_classes`.")
  dtype = dtypes.canonicalize_dtype(dtype)
  try:
    output_pos_axis = util.canonicalize_axis(axis, x.ndim + 1)  # type: ignore[arg-type]
  except TypeError:
    axis_size = lax.axis_size(axis)
    if num_classes != axis_size:
      raise ValueError(f"Expected num_classes to match the size of axis {axis}, "
                       f"but {num_classes} != {axis_size}") from None
    axis_idx = lax.axis_index(axis)
    return jnp.asarray(_dot_product_attention_xla == axis_idx, dtype=dtype)
  axis = operator.index(axis)  # type: ignore[arg-type]
  lhs = lax.expand_dims(x, (axis,))
  rhs_shape = [1] * x.ndim
  rhs_shape.insert(output_pos_axis, num_classes)
  # TODO(yashkatariya): Maybe expose `out_sharding` on `one_hot` too?
  rhs_sharding = NamedSharding(x.aval.sharding.mesh, P(*[None] * len(rhs_shape)))  # pytype: disable=attribute-error
  rhs = lax.broadcasted_iota(x.dtype, rhs_shape, output_pos_axis,
                             out_sharding=rhs_sharding)
  return (lhs == rhs).astype(dtype)

# TODO(slebedev): Change the type of `x` to `ArrayLike`.
def one_hot(x: Any, num_classes: int, *,
            dtype: Any = dtypes.float_, axis: int | AxisName = -1) -> Array:
  """One-hot encodes the given indices.

  Each index in the input ``x`` is encoded as a vector of zeros of length
  ``num_classes`` with the element at ``index`` set to one::

    >>> jax.nn.one_hot(jnp.array([0, 1, 2]), 3)
    Array([[1., 0., 0.],
           [0., 1., 0.],
           [0., 0., 1.]], dtype=float32)

  Indices outside the range [0, num_classes) will be encoded as zeros::

    >>> jax.nn.one_hot(jnp.array([-1, 3]), 3)
    Array([[0., 0., 0.],
           [0., 0., 0.]], dtype=float32)

  Args:
    x: A tensor of indices.
    num_classes: Number of classes in the one-hot dimension.
    dtype: optional, a float dtype for the returned values (default :obj:`jnp.float_`).
    axis: the axis or axes along which the function should be
      computed.
  """
  num_classes = core.concrete_dim_or_error(
      num_classes,
      "The error arose in jax.nn.one_hot argument `num_classes`.")
  x_arr = jnp.asarray(x)
  if not dtypes.isdtype(x_arr.dtype, "integral"):
    # Deprecated 2024-12-18
    deprecations.warn(
      'jax-nn-one-hot-float-input',
      f"jax.nn.one_hot input should be integer-typed; got dtype={x_arr.dtype}",
      stacklevel=1)
  return _one_hot(x_arr, num_classes, dtype=dtype, axis=axis)


@custom_derivatives.custom_jvp
@api.jit
def relu6(x: ArrayLike) -> Array:
  r"""Rectified Linear Unit 6 activation function.

  Computes the element-wise function

  .. math::
    \mathrm{relu6}(x) = \min(\max(x, 0), 6)

  except under differentiation, we take:

  .. math::
    \nabla \mathrm{relu}(0) = 0

  and

  .. math::
    \nabla \mathrm{relu}(6) = 0

  Args:
    x : input array

  Returns:
    An array.

  See also:
    :func:`relu`
  """
  return jnp.minimum(jnp.maximum(x, 0), 6.)
relu6.defjvps(lambda g, ans, x:
              lax.select((x > 0) & (x < 6), g, lax.full_like(g, 0)))

@api.jit
def hard_sigmoid(x: ArrayLike) -> Array:
  r"""Hard Sigmoid activation function.

  Computes the element-wise function

  .. math::
    \mathrm{hard\_sigmoid}(x) = \frac{\mathrm{relu6}(x + 3)}{6}

  Args:
    x : input array

  Returns:
    An array.

  See also:
    :func:`relu6`
  """
  return relu6(x + 3.) / 6.

@api.jit
def hard_silu(x: ArrayLike) -> Array:
  r"""Hard SiLU (swish) activation function

  Computes the element-wise function

  .. math::
    \mathrm{hard\_silu}(x) = x \cdot \mathrm{hard\_sigmoid}(x)

  Both :func:`hard_silu` and :func:`hard_swish` are aliases for the same
  function.

  Args:
    x : input array

  Returns:
    An array.

  See also:
    :func:`hard_sigmoid`
  """
  numpy_util.check_arraylike("hard_silu", x)
  x_arr = jnp.asarray(x)
  return x_arr * hard_sigmoid(x_arr)

hard_swish = hard_silu

def _get_large_negative(dtype):
  dtype_max = dtypes.finfo(dtype).max
  return jnp.asarray(-0.7 * dtype_max, dtype=dtype)

def _get_causal_mask(T, S):
  mask = jnp.tril(jnp.ones((T, S), dtype=bool))
  return mask[None, None, :, :]

def _get_window_mask(T: int, S: int, local_window_size: tuple[int, int]):
  query_pos = jnp.array(range(T))
  key_pos = jnp.array(range(S))
  left_window, right_window = local_window_size
  left_mask = query_pos[..., None] <= key_pos[..., None, :] + left_window
  right_mask = query_pos[..., None] >= key_pos[..., None, :] - right_window
  return jnp.logical_and(right_mask, left_mask)[None, None, :, :]

def _get_padding_mask_logits(T, S, q_seqlen, kv_seqlen):
  q_mask = True
  kv_mask = True
  if q_seqlen is not None:
    q_indices = jnp.arange(0, T)[None, :, None]
    q_mask = q_indices < q_seqlen[:, None, None]
  if kv_seqlen is not None:
    kv_indices = jnp.arange(0, S)[None, None, :]
    kv_mask = kv_indices < kv_seqlen[:, None, None]
  mask = jnp.logical_and(q_mask, kv_mask)
  return mask[:, None, :, :]

def _get_padding_mask_encoded(T, q_seqlen):
  q_indices = jnp.arange(0, T)[None, :]
  mask = q_indices < q_seqlen[:, None]
  return mask[:, :, None, None]

def _apply_masks(logits, mask, is_causal, q_seqlen, kv_seqlen,
                 local_window_size):
  if mask is None and not is_causal and q_seqlen is None and kv_seqlen is None:
    return logits

  combined_mask = jnp.ones_like(logits, dtype=bool)
  if mask is not None:
    assert mask.dtype == np.dtype(bool)
    combined_mask = jnp.logical_and(combined_mask, mask)

  T, S = logits.shape[2], logits.shape[3]

  if is_causal:
    mask = _get_causal_mask(T, S)
    combined_mask = jnp.logical_and(combined_mask, mask)

  if local_window_size is not None:
    mask = _get_window_mask(T, S, local_window_size)
    combined_mask = jnp.logical_and(combined_mask, mask)

  if q_seqlen is not None or kv_seqlen is not None:
    mask = _get_padding_mask_logits(T, S, q_seqlen, kv_seqlen)
    combined_mask = jnp.logical_and(combined_mask, mask)

  large_negative_number = _get_large_negative(logits.dtype)
  padded_logits = jnp.where(combined_mask, logits, large_negative_number)
  return padded_logits

def _dot_product_attention_core(query, key, value, bias, mask, is_causal,
                                scale, q_seqlen, kv_seqlen, local_window_size):
  logits_dtype = jnp.promote_types(query.dtype, np.float32)

  # If the query and logits dtypes are different, then the default precision
  # can use inconsistent types in the backwards pass
  # (see https://github.com/jax-ml/jax/issues/24047).
  if query.dtype == dtypes.bfloat16:
    precision = lax.DotAlgorithmPreset.BF16_BF16_F32
  elif query.dtype == np.float16:
    precision = lax.DotAlgorithmPreset.F16_F16_F32
  # TODO(sbodenstein): Implement this fix for all dtypes.
  else:
    precision = None

  # Explicit precision will fail on platforms that don't support it. For example,
  # some GPUs do not support BF16_BF16_F32, and TPU does not support F16_F16_F32.
  # Use the default precision as a fallback in these cases.
  try:
    logits = jnp_einsum.einsum(
        "BTNH,BSNH->BNTS",
        query,
        key,
        precision=precision,
        preferred_element_type=logits_dtype,
    )
  except:  # pylint: disable=bare-except
    logits = jnp_einsum.einsum(
        "BTNH,BSNH->BNTS",
        query,
        key,
        precision=None,
        preferred_element_type=logits_dtype,
    )

  logits *= jnp.array(scale, dtype=logits.dtype)

  if bias is not None:
    logits = (logits + bias).astype(logits.dtype)

  padded_logits = _apply_masks(logits, mask, is_causal, q_seqlen, kv_seqlen,
                               local_window_size)

  # Softmax and it is always carried out in fp32.
  padded_logits = padded_logits.astype(np.float32)
  probs = softmax(padded_logits, axis=-1).astype(key.dtype)

  encoded = jnp_einsum.einsum('BNTS,BSNH->BTNH', probs, value)
  if q_seqlen is not None and kv_seqlen is not None:
    mask = _get_padding_mask_encoded(encoded.shape[1], q_seqlen)
    encoded *= mask.astype(encoded.dtype)
  return encoded

def _dot_product_attention_xla(
    query: Array,
    key: Array,
    value: Array,
    bias: Array | None,
    mask: Array | None,
    is_causal: bool,
    scale: float,
    q_seqlen: Array | None,
    kv_seqlen: Array | None,
    local_window_size: tuple[int, int] | None):

  B, T, N, H = query.shape
  _, S, K, _ = key.shape
  G = N // K

  query = jnp.reshape(query, (B, T, K, G, H))
  def _reshape_to_grouped(t):
    if t is not None:
      tB, tN, tT, tS = t.shape
      if tN == 1:
        t = jnp.broadcast_to(t[:, :, None, :, :], (tB, tN, G, tT, tS))
      else:
        assert tN == N
        t = jnp.reshape(t, (tB, K, G, tT, tS))
    return t
  bias = _reshape_to_grouped(bias)
  mask = _reshape_to_grouped(mask)
  vmapped_fn = api.vmap(
      _dot_product_attention_core,
      in_axes=(3, None, None, 2, 2, None, None, None, None, None),
      out_axes=3,
  )
  encoded = vmapped_fn(query, key, value, bias, mask, is_causal, scale,
                       q_seqlen, kv_seqlen, local_window_size)
  encoded = jnp.reshape(encoded, (B, T, N, H))
  return encoded

def bias_fwd_rule(a, query_head_num):
  return bias_fwd_p.bind(a, query_head_num), a
def bias_bwd_rule(query_head_num, res, g):
  a = res
  if a.shape[0] > 1 or a.shape[-3] != query_head_num:
    raise ValueError("cuDNN only supports bias gradient when the batch size is "
                     f"1 and the head number matches the query, but got "
                     f"B={a.shape[0]}, N={a.shape[-3]}.")
  return (bias_bwd_p.bind(g, a, query_head_num),)

# This function uses two custom primitives, `bias_fwd` and `bias_bwd`, to work
# around a cuDNN issue where bias gradients are only supported when the batch
# size is 1 and the number of heads matches the query.
# TODO(kaixih@nvidia): Remove this workaround once cuDNN resolves the issue.
@partial(custom_derivatives.custom_vjp, nondiff_argnums=(1,))
def check_valid_bias_batch(x, query_head_num):
  output, _ = bias_fwd_rule(x, query_head_num)
  return output
check_valid_bias_batch.defvjp(bias_fwd_rule, bias_bwd_rule)

bias_fwd_p = core.Primitive('bias_fwd')
bias_fwd_p.multiple_results = False
bias_bwd_p = core.Primitive('bias_bwd')
bias_bwd_p.multiple_results = False

def bias_fwd_impl(a, query_head_num):
  return a
def bias_bwd_impl(g, a, query_head_num):
  return g
bias_fwd_p.def_impl(bias_fwd_impl)
bias_bwd_p.def_impl(bias_bwd_impl)

def bias_fwd_abstract_eval(a, query_head_num):
  return core.ShapedArray(a.shape, a.dtype)
def bias_bwd_abstract_eval(g, a, query_head_num):
  return core.ShapedArray(g.shape, g.dtype)
bias_fwd_p.def_abstract_eval(bias_fwd_abstract_eval)
bias_bwd_p.def_abstract_eval(bias_bwd_abstract_eval)

def bias_fwd_lowering(ctx, a, query_head_num):
  return [a]
def bias_bwd_lowering(ctx, g, a, query_head_num):
  return [g]
mlir.register_lowering(bias_fwd_p, bias_fwd_lowering)
mlir.register_lowering(bias_bwd_p, bias_bwd_lowering)

def bias_fwd_batch_rule(batched_args, batch_dims):
  x, query_head_num = batched_args
  a = batch_dims[0]
  output, _ = bias_fwd_rule(x, query_head_num)
  return output, a
def bias_bwd_batch_rule(batched_args, batch_dims):
  g, x, query_head_num = batched_args
  b = batch_dims[0]
  *Bs, _, _, _ = x.shape
  B = math.prod(Bs)
  x = jnp.reshape(x, (B,) + x.shape[-3:])
  output, = bias_bwd_rule(query_head_num, x, g)
  return output, b
batching.primitive_batchers[bias_fwd_p] = bias_fwd_batch_rule
batching.primitive_batchers[bias_bwd_p] = bias_bwd_batch_rule

def dot_product_attention(
    query: ArrayLike,
    key: ArrayLike,
    value: ArrayLike,
    bias: ArrayLike | None = None,
    mask: ArrayLike | None = None,
    *,
    scale: float | None = None,
    is_causal: bool = False,
    query_seq_lengths: ArrayLike | None = None,
    key_value_seq_lengths: ArrayLike | None = None,
    local_window_size: int | tuple[int, int] | None = None,
    implementation: Literal['xla', 'cudnn'] | None = None) -> Array:
  r"""Scaled dot product attention function.

  Computes the attention function on Query, Key, and Value tensors:

  .. math::

    \mathrm{Attention}(Q, K, V)=\mathrm{softmax}(\frac{QK^T}{\sqrt{d_k}})V

  If we define :code:`logits` as the output of :math:`QK^T` and the
  :code:`probs` as the output of :math:`softmax`.

  Throughout this function, we utilize the following uppercase letters to
  represent the shape of array::

    B = batch size
    S = length of the key/value (source)
    T = length of the query (target)
    N = number of attention heads
    H = dimensions of each attention head
    K = number of key/value heads
    G = number of groups, which equals to N // K

  Args:
    query: query array; shape :code:`(BTNH|TNH)`
    key: key array: shape :code:`(BSKH|SKH)`. When `K` equals `N`, multi-headed
      attention (MHA https://arxiv.org/abs/1706.03762) is performed. Otherwise,
      grouped query attention (GQA https://arxiv.org/abs/2305.13245) is
      performed if `N` is a multiple of `K`, and multi-query attention (MQA
      https://arxiv.org/abs/1911.02150) is performed if `K == 1` (a special case
      of GQA).
    value: value array, should have the same shape as the `key` array.
    bias: optional, bias array to be added to logits; The shape must be 4D and
      be broadcastable to :code:`(BNTS|NTS)`.
    mask: optional, mask array used to filter out logits. It is a boolean mask
      where `True` indicates the element should take part in attention. For an
      additive mask, users should pass it to `bias`. The shape must be 4D and be
      broadcastable to :code:`(BNTS|NTS)`.
    scale: scale for the logits. If None, the scale will be set to 1 divided by
      the square root of query's head dimension (i.e. H).
    is_causal: If true, causal attention will be applied. Note, some
      implementations like `xla` will generate a mask tensor and apply it to the
      logits to mask out the non-causal parts of the attention matrix, but other
      implementations like `cudnn` will avoid computing the non-causal regions,
      providing speedups.
    query_seq_lengths: `int32` array of sequence lengths for query; shape
      :code:`(B)`
    key_value_seq_lengths: `int32` array of sequence lengths for key and value;
      shape :code:`(B)`
    local_window_size: Window sizes to make self attention to attend to each
      token's local window. If set, this specifies the (left_window_size,
      right_window_size) for each token. E.g., if local_window_size == (3, 2)
      and the sequence is [0, 1, 2, 3, 4, 5, c, 7, 8, 9], token `c` can attend
      to [3, 4, 5, c, 7, 8]. If a single int is given, it will be interpreted as
      a symmetric window (window_size, window_size).
    implementation: A string to control which implementation backend to use.
      Supported strings are `xla`, `cudnn` (cuDNN flash attention). It defaults
      to `None`, which will automatically select the best available backend.
      Note, `cudnn` supports only a subset of shapes/dtypes, and an exception
      will be thrown if its not supported.

  Returns:
    An array of the attention output with the same shape as :code:`query`.
  """
  output_shape = jnp.asarray(query).shape
  def _ensure_4d(t):
    t = jnp.asarray(t)
    dims_to_add = 4 - t.ndim
    if dims_to_add > 0:
      return jnp.expand_dims(t, axis=tuple(range(dims_to_add)))
    return t

  query_arr = _ensure_4d(query)
  key_arr = _ensure_4d(key)
  value_arr = _ensure_4d(value)
  bias = _ensure_4d(bias) if bias is not None else None
  mask = _ensure_4d(mask) if mask is not None else None
  if query_seq_lengths is not None:
    query_seq_lengths = jnp.asarray(query_seq_lengths)
  if key_value_seq_lengths is not None:
    key_value_seq_lengths = jnp.asarray(key_value_seq_lengths)
  if isinstance(local_window_size, int):
    local_window_size = (local_window_size, local_window_size)

  def _check_shape_and_dtype(t: Array | None, shape: Sequence[int],
                             dtype: DType | None, name: str) -> None:
    if t is None:
      return
    if t.ndim != len(shape):
      raise ValueError(f"{name} ndim should be {len(shape)}, but got {t.ndim}")
    if dtype is not None and t.dtype != dtype:
      raise ValueError(f"{name} dtype should be {dtype}, but got {t.dtype}")
    for i in range(t.ndim):
      if shape[i] != -1 and t.shape[i] != shape[i]:
        raise ValueError(f"{name} shape should be {shape}: but got {t.shape}")

  B, S, K, H = key_arr.shape
  _check_shape_and_dtype(value_arr, [B, S, K, H], key_arr.dtype, 'value')
  _check_shape_and_dtype(query_arr, [B, -1, -1, H], key_arr.dtype, 'query')
  _check_shape_and_dtype(mask, [-1] * 4, np.dtype(bool), 'mask')
  _check_shape_and_dtype(bias, [-1] * 4, None, 'bias')
  _check_shape_and_dtype(query_seq_lengths, [B], np.dtype('int32'),
                         'query_seq_lengths')
  _check_shape_and_dtype(key_value_seq_lengths, [B], np.dtype('int32'),
                         'key_value_seq_lengths')
  if query_arr.shape[-2] % K != 0:
    raise ValueError(f"The number of query heads must be a multiple of "
                     f"key/value heads, but got {query_arr.shape[-2]} vs {K}")

  scale_val = (1.0 / np.sqrt(H)) if scale is None else scale

  match implementation:
    case 'xla':
      out = _dot_product_attention_xla(
          query_arr, key_arr, value_arr, bias, mask, is_causal=is_causal,
          scale=scale_val, q_seqlen=query_seq_lengths,
          kv_seqlen=key_value_seq_lengths,
          local_window_size=local_window_size,
      )
    case 'cudnn':
      if bias is not None:
        bias = check_valid_bias_batch(bias, query_arr.shape[-2])
        bias = jnp.asarray(bias)
      use_padding = (
           query_seq_lengths is not None or key_value_seq_lengths is not None
      )
      if use_padding:
        if query_seq_lengths is None:
          T = query_arr.shape[1]
          query_seq_lengths = jnp.full((B,), T, dtype=np.int32)
        if key_value_seq_lengths is None:
          key_value_seq_lengths = jnp.full((B,), S, dtype=np.int32)

      mask_type = MaskType.NO_MASK
      if use_padding and is_causal:
        mask_type = MaskType.PADDING_CAUSAL
      elif is_causal:
        mask_type = MaskType.CAUSAL
      elif use_padding:
        mask_type = MaskType.PADDING
      # CuDNN supports only the left window with an exclusive boundary when
      # causal mask is enabled.
      sliding_window = None
      if local_window_size is not None:
        l_window, r_window = local_window_size
        if r_window == 0 or mask_type == MaskType.CAUSAL:
          sliding_window = l_window + 1
        else:
          raise ValueError(f"cuDNN doesn't support right window: {r_window} "
                           "when causal mask is not used.")

      out = cudnn_dot_product_attention(
          query_arr, key_arr, value_arr, bias, mask, query_seq_lengths,
          key_value_seq_lengths, scale=scale_val, mask_type=mask_type,
          sliding_window_length=sliding_window,
      )
    case None:
      # TODO(kaixih@nvidia) Defaults to XLA for now. Will automatically select
      # best backend.
      out = _dot_product_attention_xla(
          query_arr, key_arr, value_arr, bias, mask, is_causal=is_causal,
          scale=scale_val, q_seqlen=query_seq_lengths,
          kv_seqlen=key_value_seq_lengths,
          local_window_size=local_window_size,
      )
    case _:
      raise ValueError(f"Unsupported implementation option: {implementation}")

  return jnp.reshape(out, output_shape)

def scaled_matmul(
    lhs: Array,
    rhs: Array,
    lhs_scales: Array,
    rhs_scales: Array,
    preferred_element_type: DTypeLike = np.float32,
) -> Array:
    r"""Scaled matrix multiplication function.

    Performs block-scaled matmul of `a` and `b` using `a_scales` and `b_scales`.
    The last dim is the contracting dim, and block size is inferred.

    Mathematically, this operation is equivalent to::

      a_block_size = a.shape[-1] // a_scales.shape[-1]
      b_block_size = b.shape[-1] // b_scales.shape[-1]
      a_scaled = a * jnp.repeat(a_scales, a_block_size, axis=-1)
      b_scaled = b * jnp.repeat(b_scales, b_block_size, axis=-1)
      jnp.einsum('BMK,BNK->BMN', a_scaled, b_scaled)

    Args:
      lhs (Array): Operand a, shape (B, M, K).
      rhs (Array): Operand b, shape (B, N, K).
      lhs_scales (Array): Shape (B, M, K_a), where `K % K_a == 0`.
      rhs_scales (Array): Shape (B, N, K_b), where `K % K_b == 0`.
      preferred_element_type (DTypeLike, optional): Defaults to `jnp.float32`.

    Returns:
      Array of shape (B, M, N).

    Notes:
      - We currently do not support user-defined `precision` for customizing the
        compute data type. It is fixed to `jnp.float32`.
      - Block size is inferred as `K // K_a` for `a` and `K // K_b` for `b`.
      - To use cuDNN with Nvidia Blackwell GPUs, inputs must match::

          # mxfp8
          a, b: jnp.float8_e4m3fn | jnp.float8_e5m2
          a_scales, b_scales: jnp.float8_e8m0fnu
          block_size: 32
          # nvfp4
          a, b: jnp.float4_e2m1fn
          a_scales, b_scales: jnp.float8_e4m3fn
          block_size: 16

    Examples:

      Basic case:

      >>> a = jnp.array([1, 2, 3]).reshape((1, 1, 3))
      >>> b = jnp.array([4, 5, 6]).reshape((1, 1, 3))
      >>> a_scales = jnp.array([0.5]).reshape((1, 1, 1))
      >>> b_scales = jnp.array([0.5]).reshape((1, 1, 1))
      >>> scaled_matmul(a, b, a_scales, b_scales)  # doctest: +SKIP
      Array([[[8.]]], dtype=float32)

      Using fused cuDNN call on Blackwell GPUs:

      >>> dtype = jnp.float8_e4m3fn
      >>> a = jax.random.normal(jax.random.PRNGKey(1), (3, 128, 64), dtype=dtype)
      >>> b = jax.random.normal(jax.random.PRNGKey(2), (3, 128, 64), dtype=dtype)
      >>> a_scales = jnp.ones((3, 128, 4), dtype=jnp.float8_e8m0fnu)
      >>> b_scales = jnp.ones((3, 128, 4), dtype=jnp.float8_e8m0fnu)
      >>> scaled_matmul(a, b, a_scales, b_scales)  # doctest: +SKIP
    """
    a, b, a_scales, b_scales = lhs, rhs, lhs_scales, rhs_scales
    if not all(x.ndim == 3 for x in (a, b, a_scales, b_scales)):
        raise ValueError(
            "scaled_matmul requires all inputs to be 3-dimensional arrays"
        )

    B_a, M_a, K_a = a.shape
    B_b, N_b, K_b = b.shape
    if K_a != K_b or B_a != B_b:
        raise ValueError(
            "scaled_matmul requires inputs a and b to have matching batch (B) "
            f"and contract (K) dimensions, but got shapes {a.shape} and "
            f"{b.shape}"
        )

    B_as, M_as, K_as = a_scales.shape
    B_bs, N_bs, K_bs = b_scales.shape
    if K_as != K_bs or B_as != B_bs:
        raise ValueError(
            "scaled_matmul requires scales to have matching batch (B) and "
            f"contract (K) dimensions, but got shapes {a_scales.shape} and "
            f"{b_scales.shape}"
        )

    if M_as != M_a or N_bs != N_b:
        raise ValueError(
            "scaled_matmul requires scales to match non-contract dimensions of "
            f"inputs, but got shapes a: {a.shape}, b: {b.shape}, a_scales: "
            f"{a_scales.shape}, b_scales: {b_scales.shape}"
        )

    preferred_element_type = dtypes.canonicalize_dtype(
        np.dtype(preferred_element_type)
    )
    out = cudnn_scaled_matmul(
        a,
        b,
        a_scales,
        b_scales,
        preferred_element_type=preferred_element_type,
    )
    return out

def get_scaled_dot_general_config(mode: Literal['nvfp4', 'mxfp8'],
                                  global_scale: Array | None = None):
    r"""Get quantization configs for scaled_dot_general.

    Create quantization configs for the `jax.nn.scaled_dot_general`.

    See Also:
      - :func:`jax.nn.scaled_dot_general`: Scaled dot general function.
    """

    if mode == 'nvfp4':
        one = jnp.ones((1,), dtype=np.float32)
        return BlockScaleConfig(
            mode='nvfp4',
            block_size=16,
            data_type=dtypes.float4_e2m1fn,
            scale_type=dtypes.float8_e4m3fn,
            global_scale=one if global_scale is None else global_scale,
            infer_only=False
        )
    elif mode == 'mxfp8':
        return BlockScaleConfig(
            mode='mxfp8',
            block_size=32,
            data_type=dtypes.float8_e4m3fn,
            scale_type=dtypes.float8_e8m0fnu,
            global_scale=None,
            infer_only=False
        )
    else:
        raise ValueError(f"Unsupported mode: {mode}")

def scaled_dot_general(
    lhs, rhs,
    dimension_numbers,
    preferred_element_type=np.float32,
    configs: list[BlockScaleConfig] | None = None,
    implementation: Literal['cudnn'] | None = None,
  ):
  r"""Scaled dot general operation.

  Performs a generalized dot product with block-scaled quantization on the
  lhs and rhs inputs. This operation extends `lax.dot_general` to support
  user-defined scaling configurations.

  Essentially, the operation follows::

      a, a_scales = quantize(lhs, configs[0])
      b, b_scales = quantize(rhs, configs[1])
      c = jax.nn.scaled_matmul(a, b, a_scales, b_scales)

  Args:
    lhs (ArrayLike): Input array.
    rhs (ArrayLike): Input array.
    dimension_numbers (DotDimensionNumbers): A tuple of two tuples specifying
      the contraction and batch dimensions:
      `((lhs_contracting_dims, rhs_contracting_dims), (lhs_batch_dims, rhs_batch_dims))`.
    preferred_element_type (DTypeLike, optional): Output data type of the dot
      product. Defaults to `jnp.float32`. Other valid types include
      `jnp.bfloat16` and `jnp.float16`.
    configs (list of BlockScaleConfig, optional): Scaling configurations for
      lhs, rhs, and gradients. Users can obtain valid configurations via
      `jax.nn.get_scaled_dot_general_config`. Currently, `nvfp4` and `mxfp8`
      are supported. If `None`, falls back to `lax.dot_general`.
    implementation: str
      (Deprecated) Backend selector, now ignored. The system chooses the backend
      automatically. Scheduled for removal in future releases.

  Returns:
    Array: The resulting tensor, with batch dimensions first, followed by
    non-contracting/non-batch dimensions of lhs, and then those of rhs.

  See Also:
    - :func:`jax.nn.scaled_matmul`: Scaled matmul function.
    - :func:`jax.lax.dot_general`: General dot product operator.

  Notes:
    - Unlike `nn.scaled_matmul`, which assumes quantized low-precision
      inputs with explicit scaling factors, this operator takes high-precision
      inputs, applies quantization internally, and handles the backward pass.

  Examples:

    Creating config for mxfp8:

    >>> configs = [jax.nn.get_scaled_dot_general_config('mxfp8')] * 3

    Creating config for nvfp4:

    >>> global_scale = jnp.array([0.5], jnp.float32)
    >>> configs = [jax.nn.get_scaled_dot_general_config('nvfp4', global_scale)] * 3

    Using scaled_dot_general with the configs:

    >>> import functools
    >>> scaled_dot_general_fn = functools.partial(jax.nn.scaled_dot_general, configs=configs)
    >>> lhs = jax.random.normal(jax.random.PRNGKey(1), (3, 128, 64))
    >>> rhs = jax.random.normal(jax.random.PRNGKey(2), (3, 128, 64))
    >>> out = scaled_dot_general_fn(lhs, rhs, (((2,), (2,)), ((0,), (0,))))  # doctest: +SKIP
  """
  if implementation is not None:
    warnings.warn("Backend selector, now ignored. The system chooses the "
                  "backend automatically.", DeprecationWarning)

  if configs is None:
    return lax.dot_general(lhs, rhs, dimension_numbers,
                           preferred_element_type=preferred_element_type)

  out = cudnn_scaled_dot_general(
      lhs, rhs, dimension_numbers,
      preferred_element_type=preferred_element_type,
      configs=configs
  )

  return out

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Conv2D operator declaration and schedule registration for VTA."""

import numpy as np

import tvm
from tvm import te
from tvm import autotvm
import topi

from .util import is_packed_layout
from ..environment import get_env
from tvm.relay import op as Op
from tvm.contrib.util import eprint


@autotvm.register_topi_compute("conv2d_packed.vta")
def conv2d_packed(cfg, data, kernel, strides, padding, dilation, layout, out_dtype):
    """ Packed conv2d function."""
    if not is_packed_layout(layout):
        raise topi.InvalidShapeError()
    assert dilation == (1, 1)

    if padding[0]:
        pad_data = topi.nn.pad(data, [0, 0, padding[0], padding[1], 0, 0], name="pad_data")
    else:
        pad_data = data
    assert len(data.shape) == 6
    assert len(kernel.shape) == 6
    oheight = topi.util.get_const_int((pad_data.shape[2] - kernel.shape[2]) // strides[0] + 1)
    owidth = topi.util.get_const_int((pad_data.shape[3] - kernel.shape[3]) // strides[1] + 1)
    oshape = (data.shape[0], kernel.shape[0], oheight, owidth, data.shape[4], kernel.shape[4])

    ishape = topi.util.get_const_tuple(data.shape)
    kshape = topi.util.get_const_tuple(kernel.shape)
    d_i = te.reduce_axis((0, kshape[2]), name='d_i')
    d_j = te.reduce_axis((0, kshape[3]), name='d_j')
    k_o = te.reduce_axis((0, ishape[1]), name='k_o')
    k_i = te.reduce_axis((0, ishape[-1]), name='k_i')
    hstride, wstride = strides
    res = te.compute(
        oshape,
        lambda b_o, c_o, i, j, b_i, c_i: te.sum(
            pad_data[b_o, k_o, i*hstride+d_i, j*wstride+d_j, b_i, k_i].astype(out_dtype) *
            kernel[c_o, k_o, d_i, d_j, c_i, k_i].astype(out_dtype),
            axis=[k_o, d_i, d_j, k_i]),
        name="res", tag="conv2d_dense")

    cfg.add_flop(2 * np.prod(topi.util.get_const_tuple(oshape)) *
                 kshape[2] * kshape[3] * ishape[1] * ishape[-1])

    return res


@autotvm.register_topi_schedule("conv2d_packed.vta")
def schedule_conv2d_packed(cfg, outs):
    """Schedule packed conv2d"""
    assert len(outs) == 1
    output = outs[0]
    const_ops = []
    ewise_inputs = []
    ewise_ops = []
    conv2d_res = []
    assert "int" in output.op.input_tensors[0].dtype

    def _traverse(op):
        if topi.tag.is_broadcast(op.tag):
            if not op.same_as(output.op):
                if not op.axis:
                    const_ops.append(op)
                else:
                    ewise_ops.append(op)
            for tensor in op.input_tensors:
                if isinstance(tensor.op, tvm.te.PlaceholderOp):
                    ewise_inputs.append((op, tensor))
                else:
                    _traverse(tensor.op)
        else:
            assert op.tag == "conv2d_dense"
            conv2d_res.append(op)

    _traverse(output.op)
    assert len(conv2d_res) == 1
    conv2d_stage = conv2d_res[0].output(0)
    s = te.create_schedule(output.op)

    ##### space definition begin #####
    b, c_o, x_i, x_j, _, _ = s[conv2d_stage].op.axis
    c_i, _, _, _ = s[conv2d_stage].op.reduce_axis
    cfg.define_split('tile_b', b, num_outputs=2)
    cfg.define_split('tile_h', x_i, num_outputs=2)
    cfg.define_split('tile_w', x_j, num_outputs=2)
    cfg.define_split('tile_ci', c_i, num_outputs=2)
    cfg.define_split('tile_co', c_o, num_outputs=2)
    cfg.define_knob('oc_nthread', [1, 2])
    cfg.define_knob('h_nthread', [1, 2])
    ###### space definition end ######

    data, kernel = conv2d_stage.op.input_tensors
    if isinstance(data.op, tvm.te.ComputeOp) and "pad" in data.op.tag:
        temp = data.op.input_tensors[0]
        pad_data = data
        data = temp
    else:
        pad_data = None

    env = get_env()

    # setup pad
    if pad_data is not None:
        cdata = pad_data
        s[pad_data].set_scope(env.inp_scope)
    else:
        cdata = s.cache_read(data, env.inp_scope, [conv2d_stage])
    ckernel = s.cache_read(kernel, env.wgt_scope, [conv2d_stage])
    s[conv2d_stage].set_scope(env.acc_scope)

    # cache read input
    cache_read_ewise = []
    for consumer, tensor in ewise_inputs:
        cache_read_ewise.append(
            s.cache_read(tensor, env.acc_scope, [consumer]))

    # set ewise scope
    for op in ewise_ops:
        s[op].set_scope(env.acc_scope)
        s[op].pragma(s[op].op.axis[0], env.alu)

    for op in const_ops:
        s[op].compute_inline()

    # tile
    x_bo, x_co, x_i, x_j, x_bi, x_ci = s[output].op.axis
    x_co0, x_co1 = cfg['tile_co'].apply(s, output, x_co)
    x_i0, x_i1 = cfg['tile_h'].apply(s, output, x_i)
    x_j0, x_j1 = cfg['tile_w'].apply(s, output, x_j)
    s[output].reorder(x_bo, x_i0, x_co0, x_j0, x_co1, x_i1, x_j1, x_bi, x_ci)
    store_pt = x_j0

    # set all compute scopes
    s[conv2d_stage].compute_at(s[output], store_pt)
    for op in ewise_ops:
        s[op].compute_at(s[output], store_pt)

    for tensor in cache_read_ewise:
        s[tensor].compute_at(s[output], store_pt)
        s[tensor].pragma(s[tensor].op.axis[0], env.dma_copy)

    # virtual threading along output channel axes
    if cfg['oc_nthread'].val > 1:
        _, v_t = s[output].split(x_co0, factor=cfg['oc_nthread'].val)
        s[output].reorder(v_t, x_bo)
        s[output].bind(v_t, te.thread_axis("cthread"))

    # virtual threading along spatial rows
    if cfg['h_nthread'].val > 1:
        _, v_t = s[output].split(x_i0, factor=cfg['h_nthread'].val)
        s[output].reorder(v_t, x_bo)
        s[output].bind(v_t, te.thread_axis("cthread"))

    x_bo, x_co, x_i, x_j, x_bi, x_ci = s[conv2d_stage].op.axis
    k_o, d_i, d_j, k_i = s[conv2d_stage].op.reduce_axis
    s[conv2d_stage].reorder(x_bo, k_o, x_j, d_j, d_i, x_co, x_i, x_bi, x_ci, k_i)

    k_o, _ = cfg['tile_ci'].apply(s, conv2d_stage, k_o)
    s[cdata].compute_at(s[conv2d_stage], k_o)
    s[ckernel].compute_at(s[conv2d_stage], k_o)

    # Use VTA instructions
    s[cdata].pragma(s[cdata].op.axis[0], env.dma_copy)
    s[ckernel].pragma(s[ckernel].op.axis[0], env.dma_copy)
    s[conv2d_stage].tensorize(x_bi, env.gemm)
    s[output].pragma(x_co1, env.dma_copy)

    return s


# FIXME(zhanghao): move this code to a proper location
@topi.generic.schedule_add.register(["vta"])
def _schedule_add(outs):
    assert len(outs) == 1

    def is_cast_op(op):
        # return op.same_as(Op.op.get("cast"))
        # FIXME(zhanghao): find a better way to do compare
        return op.name == 'T_cast'

    outs = [outs] if isinstance(outs, te.tensor.Tensor) else outs
    output = outs[0]
    s = te.create_schedule([x.op for x in outs])
    te.schedule.AutoInlineInjective(s)
    # s[output].fuse(s[output].op.axis)

    # only put the int-related ops to vta
    if "int" in output.dtype:
        ewise_inputs = []
        ewise_ops = []
        const_ops = []

        def _traverse(op):
            if topi.tag.is_broadcast(op.tag):
                if not op.same_as(output.op):
                    if not op.axis:
                        const_ops.append(op)
                    elif not is_cast_op(op):
                        ewise_ops.append(op)

                for tensor in op.input_tensors:
                    if isinstance(tensor.op, tvm.te.PlaceholderOp):
                        ewise_inputs.append((op, tensor))
                    elif is_cast_op(tensor.op) and not op.same_as(output.op):
                        ewise_inputs.append((op, tensor))
                    else:
                        _traverse(tensor.op)
            else:
                for tensor in op.input_tensors:
                    if (not isinstance(tensor.op, tvm.te.PlaceholderOp)) \
                            and (not is_cast_op(tensor.op)):
                        _traverse(tensor.op)

        op = output.op
        _traverse(op)
        x_bo, x_co, x_i, x_j, x_bi, x_ci = s[output].op.axis

        x_co_max = topi.util.get_const_int(x_bo.dom.extent)
        x_i_max = topi.util.get_const_int(x_i.dom.extent)
        x_j_max = topi.util.get_const_int(x_j.dom.extent)

        # TODO(zhanghao): auto-tune
        x_co0, x_co1 = s[output].split(x_co, factor=1)

        from functools import reduce
        def factors(n):
            return sorted(set(reduce(list.__add__,
                              ([i, n//i] for i in range(1, int(n**0.5) + 1) if n % i == 0))))

        # FIXME(zhanghao): use auto-tune
        i_factors = factors(x_i_max)
        i_factor = i_factors[-1]
        while i_factor > 28:
            del i_factors[-1]
            i_factor = i_factors[-1]

        j_factors = factors(x_j_max)
        j_factor = j_factors[-1]
        while j_factor > 14:
            del j_factors[-1]
            j_factor = j_factors[-1]

        x_i0, x_i1 = s[output].split(x_i, factor=i_factor)
        x_j0, x_j1 = s[output].split(x_j, factor=j_factor)
        s[output].reorder(x_bo, x_i0, x_co0, x_j0, x_co1, x_i1, x_j1, x_bi, x_ci)
        store_pt = x_j0

        env = get_env()
        for eo in ewise_ops:
            s[eo].set_scope(env.acc_scope)
            s[eo].pragma(s[eo].op.axis[0], env.alu)
            s[eo].compute_at(s[output], store_pt)

        # cache read input
        cache_read_ewise = []
        for consumer, tensor in ewise_inputs:
            cache_read_ewise.append(
                s.cache_read(tensor, env.acc_scope, [consumer]))

        for tensor in cache_read_ewise:
            s[tensor].pragma(s[tensor].op.axis[0], env.dma_copy)
            s[tensor].compute_at(s[output], store_pt)

        for op in const_ops:
            s[op].compute_inline()

        s[output].pragma(x_co1, env.dma_copy)

    return s

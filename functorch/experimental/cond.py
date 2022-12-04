# TODO(zhxchen17) Expose API through functorhc.experimental.control_flow
#                 and rename this file to _cond.py.
from functorch._src.eager_transforms import _unwrap_all_tensors_from_functional, _wrap_all_tensors_to_functional, functionalize
import torch

import torch.utils._pytree as pytree

from torch._C import DispatchKey, DispatchKeySet, ExcludeDispatchKeyGuard
from torch._ops import PyOperator
from torch._subclasses.fake_tensor import FakeTensorMode
from torch.fx.experimental.proxy_tensor import (
    get_isolated_graphmodule,
    get_proxy_slot,
    ProxyTorchDispatchMode,
    make_fx,
    track_tensor_tree,
)
from torch.fx.passes.shape_prop import _extract_tensor_metadata
from torch.utils._python_dispatch import (
    _get_current_dispatch_mode,
    _pop_mode_temporarily,
)
from torch.utils._pytree import tree_flatten


"""
We're going to define a `cond` operation.
In order to do this, we need implementations for each of the dispatch keys.
"""
cond = PyOperator("cond")


def trace_cond(proxy_mode, func_overload, pred, true_fn, false_fn, operands):
    def _unwrap_proxy(e):
        if not isinstance(e, (torch.Tensor, torch.SymInt, torch.SymFloat)):
            return e
        return get_proxy_slot(e, proxy_mode.tracer, e, lambda e: e.proxy)

    assert isinstance(operands, list), "Cond operands must be a list of tensors"
    assert all(isinstance(o, torch.Tensor) for o in operands), "Cond operands must be a list of tensors"

    true_graph = get_isolated_graphmodule(true_fn, operands, {})
    false_graph = get_isolated_graphmodule(false_fn, operands, {})

    true_outs = []
    false_outs = []
    for node in true_graph.graph.nodes:
        if node.op == 'output':
            true_outs.extend(node.args)

    for node in false_graph.graph.nodes:
        if node.op == 'output':
            false_outs.extend(node.args)

    flat_true_outs, _ = pytree.tree_flatten(true_outs)
    flat_false_outs, _ = pytree.tree_flatten(false_outs)
    assert(len(flat_true_outs) == len(flat_false_outs))

    for i in range(0, len(flat_true_outs)):
        true_out = flat_true_outs[i]
        false_out = flat_false_outs[i]
        assert true_out.meta['tensor_meta'] == false_out.meta['tensor_meta']

    # There are probably better ways - I know that create_arg has some self incrementing name
    # magic to it, but since we explicitly have to get the name for register_module,
    # I was not sure how to do that. This kinda simulates it.
    next_name = None
    i = 0
    while not next_name:
        candidate = f"true_graph_{i}"
        if hasattr(proxy_mode.tracer.root, candidate):
            i += 1
        else:
            next_name = candidate

    true_name = next_name
    false_name = f"false_graph_{i}"
    assert(not hasattr(proxy_mode.tracer.root, false_name))

    proxy_mode.tracer.root.register_module(true_name, true_graph)
    proxy_mode.tracer.root.register_module(false_name, false_graph)

    args = (pred, true_graph, false_graph, [operands])

    proxy_args = pytree.tree_map(_unwrap_proxy, args)

    out_proxy = proxy_mode.tracer.create_proxy('call_function', func_overload, proxy_args, {},
                                               name="conditional")

    # At this point, we're *guaranteed* that whether an output came from the
    # true or false branch is indistinguishable. So, as this is just for tracing
    # purposes, choose the true branch.

    # TODO: Uhh.... it shouldn't matter, but changing this to true_fn results in
    # a FakeTensorMode error :
    # `Current active mode <class 'torch._subclasses.fake_tensor.FakeTensorMode'> not registered`
    out = false_fn(*operands)

    return track_tensor_tree(out, out_proxy, constant=None, tracer=proxy_mode.tracer)


@cond.py_impl(DispatchKey.CPU)
def cond_dense(pred, true_fn, false_fn, operands):
    mode = _get_current_dispatch_mode()
    assert (mode is None), "Mode should never be enabled for CPU key"
    if pred:
        print(true_fn, operands)
        return true_fn(*operands)
    else:
        return false_fn(*operands)


@cond.py_impl(DispatchKey.AutogradCPU)
def cond_autograd(pred, true_fn, false_fn, *operands):
    # TODO: support autograd
    flat_operands, _ = tree_flatten([true_fn, false_fn] + [operands])
    assert all([not f.requires_grad for f in flat_operands
                if isinstance(f, torch.Tensor)])

    guard = ExcludeDispatchKeyGuard(DispatchKeySet(DispatchKey.AutogradCPU))
    return cond(pred, true_fn, false_fn, *operands)


@cond.py_impl(ProxyTorchDispatchMode)
def inner(pred, true_fn, false_fn, operands):
    mode = _get_current_dispatch_mode()
    assert (mode is not None), "Mode should always be enabled for python fallback key"
    with _pop_mode_temporarily() as mode:
        res = trace_cond(mode, cond, pred, true_fn, false_fn, operands)
    return res


@cond.py_impl(FakeTensorMode)
def cond_fake_tensor_mode(pred, true_fn, false_fn, operands):
    true_outs = true_fn(*operands)
    flat_true_outs, _ = pytree.tree_flatten(true_outs)
    flat_false_outs, _ = pytree.tree_flatten(false_fn(*operands))
    if len(flat_true_outs) != len(flat_false_outs):
        raise RuntimeError("Unmatched number of outputs from cond() branches.")

    for true_out, false_out in zip(flat_true_outs, flat_false_outs):
        true_meta = _extract_tensor_metadata(true_out)
        false_meta = _extract_tensor_metadata(false_out)
        if true_meta != false_meta:
            raise RuntimeError(
                f"Unmatched tensor metadata from cond() branches.\ntrue branch: {true_meta}, false branch: {false_meta}")
    return true_outs


# We cannot directly call fallthrough here due to issue #89037.
@cond.py_impl(DispatchKey.PythonDispatcher)
def cond_python_dispatcher(*args):
    _ = ExcludeDispatchKeyGuard(DispatchKeySet(DispatchKey.PythonDispatcher))
    return cond(*args)


@cond.py_impl(torch._C._functorch.TransformType.Functionalize)
def cond_functionalize(interpreter, pred, true_fn, false_fn, inputs):
    reapply_views = interpreter.functionalizeAddBackViews()
    mode = 'mutations_and_views' if reapply_views else 'mutations'
    unwrapped_inputs = _unwrap_all_tensors_from_functional(inputs, reapply_views=reapply_views)
    functional_true_fn = make_fx(functionalize(true_fn, remove=mode))(*unwrapped_inputs)
    functional_false_fn = make_fx(functionalize(false_fn, remove=mode))(*unwrapped_inputs)
    _ = ExcludeDispatchKeyGuard(DispatchKeySet(DispatchKey.FuncTorchDynamicLayerFrontMode))
    return _wrap_all_tensors_to_functional(cond(pred, functional_true_fn, functional_false_fn, unwrapped_inputs), level=interpreter.level())

# TODO (tugsbayasgalan) somehow I can't fall through FuncTorchDynamicLayerBackMode
@cond.py_impl(DispatchKey.FuncTorchDynamicLayerBackMode)
def cond_functorch_layer_back_mode(*args):
    _ = ExcludeDispatchKeyGuard(DispatchKeySet(DispatchKey.FuncTorchDynamicLayerBackMode))
    return cond(*args)

# TODO(voz): Make this automatic for keys, this is very ugly atm
cond.fallthrough(DispatchKey.PythonTLSSnapshot)
cond.fallthrough(DispatchKey.ADInplaceOrView)
cond.fallthrough(DispatchKey.BackendSelect)

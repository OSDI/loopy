from __future__ import division

__copyright__ = "Copyright (C) 2012 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


from pytools import Record
import islpy as isl

import numpy as np


# {{{ support code for AST wrapper objects

class GeneratedInstruction(Record):
    """Objects of this type are wrapped around ASTs upon
    return from generation calls to collect information about them.

    :ivar implemented_domains: A map from an insn id to a list of
        implemented domains, with the purpose of checking that
        each instruction's exact iteration space has been covered.
    """
    __slots__ = ["insn_id", "implemented_domain", "ast"]


class GeneratedCode(Record):
    """Objects of this type are wrapped around ASTs upon
    return from generation calls to collect information about them.

    :ivar implemented_domains: A map from an insn id to a list of
        implemented domains, with the purpose of checking that
        each instruction's exact iteration space has been covered.
    """
    __slots__ = ["ast", "implemented_domains"]


def gen_code_block(elements):
    from cgen import Block, Comment, Line, Initializer

    block_els = []
    implemented_domains = {}

    for el in elements:
        if isinstance(el, GeneratedCode):
            for insn_id, idoms in el.implemented_domains.iteritems():
                implemented_domains.setdefault(insn_id, []).extend(idoms)

            if isinstance(el.ast, Block):
                block_els.extend(el.ast.contents)
            else:
                block_els.append(el.ast)

        elif isinstance(el, Initializer):
            block_els.append(el)

        elif isinstance(el, Comment):
            block_els.append(el)

        elif isinstance(el, Line):
            assert not el.text
            block_els.append(el)

        elif isinstance(el, GeneratedInstruction):
            block_els.append(el.ast)
            if el.implemented_domain is not None:
                implemented_domains.setdefault(el.insn_id, []).append(
                        el.implemented_domain)

        else:
            raise ValueError("unrecognized object of type '%s' in block"
                    % type(el))

    if len(block_els) == 1:
        ast, = block_els
    else:
        ast = Block(block_els)

    return GeneratedCode(ast=ast, implemented_domains=implemented_domains)


def wrap_in(cls, *args):
    inner = args[-1]
    args = args[:-1]

    if not isinstance(inner, GeneratedCode):
        raise ValueError("unrecognized object of type '%s' in block"
                % type(inner))

    args = args + (inner.ast,)

    return GeneratedCode(ast=cls(*args),
            implemented_domains=inner.implemented_domains)


def wrap_in_if(condition_codelets, inner):
    from cgen import If

    if condition_codelets:
        return wrap_in(If,
                "\n&& ".join(condition_codelets),
                inner)

    return inner


def add_comment(cmt, code):
    if cmt is None:
        return code

    from cgen import add_comment
    assert isinstance(code, GeneratedCode)

    return GeneratedCode(
            ast=add_comment(cmt, code.ast),
            implemented_domains=code.implemented_domains)

# }}}


# {{{ code generation state

class CodeGenerationState(object):
    def __init__(self, implemented_domain, c_code_mapper):
        """
        :param implemented_domain: The entire implemented domain,
            i.e. all constraints that have been enforced so far.
        :param c_code_mapper: A C code mapper that does not take per-ILP
            assignments into account.
        """
        self.implemented_domain = implemented_domain

        self.c_code_mapper = c_code_mapper

    def copy(self, implemented_domain=None, c_code_mapper=None):
        return CodeGenerationState(
                implemented_domain=implemented_domain or self.implemented_domain,
                c_code_mapper=c_code_mapper or self.c_code_mapper)

    def intersect(self, other):
        new_impl, new_other = isl.align_two(self.implemented_domain, other)
        return CodeGenerationState(
                new_impl & new_other,
                self.c_code_mapper)

    def fix(self, iname, aff):
        new_impl_domain = self.implemented_domain

        impl_space = self.implemented_domain.get_space()
        if iname not in impl_space.get_var_dict():
            new_impl_domain = (new_impl_domain
                    .add_dims(isl.dim_type.set, 1)
                    .set_dim_name(
                        isl.dim_type.set,
                        new_impl_domain.dim(isl.dim_type.set),
                        iname))
            impl_space = new_impl_domain.get_space()

        from loopy.isl_helpers import iname_rel_aff
        iname_plus_lb_aff = iname_rel_aff(impl_space, iname, "==", aff)

        from loopy.symbolic import pw_aff_to_expr
        cns = isl.Constraint.equality_from_aff(iname_plus_lb_aff)
        expr = pw_aff_to_expr(aff)

        new_impl_domain = new_impl_domain.add_constraint(cns)
        return CodeGenerationState(
                new_impl_domain,
                self.c_code_mapper.copy_and_assign(iname, expr))

# }}}


# {{{ cgen overrides

from cgen import POD as PODBase


class POD(PODBase):
    def get_decl_pair(self):
        from pyopencl.tools import dtype_to_ctype
        return [dtype_to_ctype(self.dtype)], self.name

# }}}


# {{{ implemented data info

class ImplementedDataInfo(Record):
    """
    .. attribute:: name

        The expanded name of the array. Note that, for example
        in the case of separate-array-tagged axes, multiple
        implemented arrays may correspond to one user-facing
        array.

    .. attribute:: dtype
    .. attribute:: cgen_declarator

        Declarator syntax tree as a :mod:`cgen` object.

    .. attribute:: arg_class

    .. attribute:: base_name

        The user-facing name of the underlying array.
        May be *None* for non-array arguments.

    .. attribute:: shape
    .. attribute:: strides

        Strides in multiples of ``dtype.itemsize``.

    .. attribute:: offset_for_name
    .. attribute:: stride_for_name_and_axis

        A tuple *(name, axis)* indicating the (implementation-facing)
        name of the array and axis number for which this argument provides
        the strides.

    .. attribute:: allows_offset
    """

    def __init__(self, name, dtype, cgen_declarator, arg_class,
            base_name=None, shape=None, strides=None,
            offset_for_name=None, stride_for_name_and_axis=None,
            allows_offset=None):
        Record.__init__(self,
                name=name,
                dtype=np.dtype(dtype),
                cgen_declarator=cgen_declarator,
                arg_class=arg_class,
                base_name=base_name,
                shape=shape,
                strides=strides,
                offset_for_name=offset_for_name,
                stride_for_name_and_axis=stride_for_name_and_axis,
                allows_offset=allows_offset)

# }}}


# {{{ main code generation entrypoint

def generate_code(kernel, with_annotation=False,
        allow_complex=None):
    if kernel.schedule is None:
        from loopy.schedule import get_one_scheduled_kernel
        kernel = get_one_scheduled_kernel(kernel)

    from loopy.preprocess import infer_unknown_types
    kernel = infer_unknown_types(kernel, expect_completion=True)

    from loopy.check import pre_codegen_checks
    pre_codegen_checks(kernel)

    from cgen import (FunctionBody, FunctionDeclaration,
            Value, Module, Block,
            Line, Const, LiteralLines, Initializer)

    from cgen.opencl import (CLKernel, CLRequiredWorkGroupSize)

    allow_complex = False
    for var in kernel.args + list(kernel.temporary_variables.itervalues()):
        if var.dtype.kind == "c":
            allow_complex = True

    seen_dtypes = set()
    seen_functions = set()

    from loopy.codegen.expression import LoopyCCodeMapper
    ccm = (LoopyCCodeMapper(kernel, seen_dtypes, seen_functions,
        with_annotation=with_annotation,
        allow_complex=allow_complex))

    mod = []

    body = Block()

    # {{{ examine arg list

    from loopy.kernel.data import ImageArg, ValueArg
    from loopy.kernel.array import ArrayBase

    impl_arg_info = []

    for arg in kernel.args:
        if isinstance(arg, ArrayBase):
            impl_arg_info.extend(
                    arg.decl_info(
                        is_written=arg.name in kernel.get_written_variables(),
                        index_dtype=kernel.index_dtype))

        elif isinstance(arg, ValueArg):
            impl_arg_info.append(ImplementedDataInfo(
                name=arg.name,
                dtype=arg.dtype,
                cgen_declarator=Const(POD(arg.dtype, arg.name)),
                arg_class=ValueArg))

        else:
            raise ValueError("argument type not understood: '%s'" % type(arg))

    if any(isinstance(arg, ImageArg) for arg in kernel.args):
        body.append(Initializer(Const(Value("sampler_t", "loopy_sampler")),
            "CLK_NORMALIZED_COORDS_FALSE | CLK_ADDRESS_CLAMP "
                "| CLK_FILTER_NEAREST"))

    # }}}

    from pyopencl.tools import dtype_to_ctype
    mod.extend([
        LiteralLines(r"""
        #define lid(N) ((%(idx_ctype)s) get_local_id(N))
        #define gid(N) ((%(idx_ctype)s) get_group_id(N))
        """ % dict(idx_ctype=dtype_to_ctype(kernel.index_dtype))),
        Line()])

    # {{{ build lmem array declarators for temporary variables

    body.extend(
            idi.cgen_declarator
            for tv in kernel.temporary_variables.itervalues()
            for idi in tv.decl_info(
                is_written=True, index_dtype=kernel.index_dtype))

    # }}}

    initial_implemented_domain = isl.BasicSet.from_params(kernel.assumptions)
    codegen_state = CodeGenerationState(
            initial_implemented_domain, c_code_mapper=ccm)

    from loopy.codegen.loop import set_up_hw_parallel_loops
    gen_code = set_up_hw_parallel_loops(kernel, 0, codegen_state)

    body.append(Line())

    if isinstance(gen_code.ast, Block):
        body.extend(gen_code.ast.contents)
    else:
        body.append(gen_code.ast)

    mod.append(
        FunctionBody(
            CLRequiredWorkGroupSize(
                kernel.get_grid_sizes_as_exprs()[1],
                CLKernel(FunctionDeclaration(
                    Value("void", kernel.name),
                    [iai.cgen_declarator for iai in impl_arg_info]))),
            body))

    # {{{ handle preambles

    for arg in kernel.args:
        seen_dtypes.add(arg.dtype)
    for tv in kernel.temporary_variables.itervalues():
        seen_dtypes.add(tv.dtype)

    preambles = kernel.preambles[:]
    for prea_gen in kernel.preamble_generators:
        preambles.extend(prea_gen(seen_dtypes, seen_functions))

    seen_preamble_tags = set()
    dedup_preambles = []

    for tag, preamble in sorted(preambles, key=lambda tag_code: tag_code[0]):
        if tag in seen_preamble_tags:
            continue

        seen_preamble_tags.add(tag)
        dedup_preambles.append(preamble)

    mod = ([LiteralLines(lines) for lines in dedup_preambles]
            + [Line()] + mod)

    # }}}

    result = str(Module(mod))

    from loopy.check import check_implemented_domains
    assert check_implemented_domains(kernel, gen_code.implemented_domains,
            result)

    return result, impl_arg_info

# }}}


# vim: foldmethod=marker

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import subprocess, os
from common import backend

def get_execution_parallism():
  return 1

def do_native_translation_v2(codeset, **kwargs):
  kernel_name, in_args, out_args, body = codeset

  if backend == 'c-sycl_intel':  # Issue: Data over SYCL Buffer & Accessor is very slow on Intel CPU
    expand_args = ' '.join([f'{x[0]}* {x[1]} = ({x[0]}*)__args[{i}];' for i, x in enumerate(in_args + out_args)])
    expand_accs = expand_ptrs = ''
  else:                          # Using standard SYCL Buffer & Accessor
    expand_args = '\n  '.join([f'auto &__args_{i} = *((cl::sycl::buffer<{x[0]}>*)__args[{i}]);' for i, x in enumerate(in_args + out_args)])
    expand_accs = '\n    '.join([f'auto __accs_{i} = __args_{i}.get_access<cl::sycl::access::mode::{"read" if i < len(in_args) else "discard_write"}>(cgh);' for i, x in enumerate(in_args + out_args)]) + '\n'
    expand_ptrs = '\n      '.join([f'{x[0]}* {x[1]} = ({x[0]}*)__accs_{i}.get_pointer();' for i, x in enumerate(in_args + out_args)]) + '\n'

  def get_extent(key, defval=1):
    str_pat = f'// [thread_extent] {key} = '
    idx = body.find(str_pat)
    if idx >= 0:
      return int(body[idx+len(str_pat):body.index('\n', idx)])
    return defval

  group_shared = []
  parsed_lines, body = [], body.split('\n')
  for line in body:
    simple_line = line.strip()
    if not simple_line.startswith('__shared__ '):
      parsed_lines.append(line)
      continue
    _, type, data = simple_line.split()
    name, size_str = data[:-2].split('[')
    parsed_lines.append(f'{line[0:len(line)-len(simple_line)]}{type}* {name} = __accessor_{name}.get_pointer();');
    group_shared.append(f'sycl::accessor<{type}, 1, sycl::access::mode::read_write, sycl::access::target::local> __accessor_{name}(sycl::range<1>({size_str}), cgh);');
  body = '\n'.join(parsed_lines)
  group_shared = '    \n'.join(group_shared)
  del parsed_lines

  body = body.replace('Idx.', 'Idx_').replace('__syncthreads()', '_item.barrier(cl::sycl::access::fence_space::global_and_local);').replace('\n', '\n    ')
  index_str = 'const int blockIdx_x = _item.get_group(0), blockIdx_y = _item.get_group(1), blockIdx_z = _item.get_group(2), threadIdx_x = _item.get_local_id(0), threadIdx_y = _item.get_local_id(1), threadIdx_z = _item.get_local_id(2);'

  lds = [get_extent('threadIdx_x'), get_extent('threadIdx_y'), get_extent('threadIdx_z')]
  gds = [get_extent('blockIdx_x') * lds[0], get_extent('blockIdx_y') * lds[1], get_extent('blockIdx_z') * lds[2]]

  full_body = f'''#include <math.h>
#include <algorithm>
#include <CL/sycl.hpp>
{kwargs['attrs'].blend}

#ifndef __SYCL_COMMON_MACRO__
#define __SYCL_COMMON_MACRO__

#define make_int4(x, y, z, w)  (int4{{x, y, z, w}})
#define make_int2(x, y)  (int2{{x, y}})

#define USING_NATIVE_VECTELEM

#ifdef USING_NATIVE_VECTELEM

#define __ITEM_0_OF__(v) (v).x()
#define __ITEM_1_OF__(v) (v).y()
#define __ITEM_2_OF__(v) (v).z()
#define __ITEM_3_OF__(v) (v).w()

using namespace cl::sycl;

#else

struct int2 {{ int x, y; }};
struct int4 {{ int x, y, z, w; }};
#define __ITEM_0_OF__(v) (v).x
#define __ITEM_1_OF__(v) (v).y
#define __ITEM_2_OF__(v) (v).z
#define __ITEM_3_OF__(v) (v).w

#define MAKE_VEC4_OP(type) \\
  inline type operator+(const type &l, const type &r) {{ return make_##type(l.x + r.x, l.y + r.y, l.z + r.z, l.w + r.w); }} \\
  inline type operator-(const type &l, const type &r) {{ return make_##type(l.x - r.x, l.y - r.y, l.z - r.z, l.w - r.w); }} \\
  inline type operator*(const type &l, const type &r) {{ return make_##type(l.x * r.x, l.y * r.y, l.z * r.z, l.w * r.w); }} \\
  inline type operator/(const type &l, const type &r) {{ return make_##type(l.x / r.x, l.y / r.y, l.z / r.z, l.w / r.w); }} \\
  inline type operator%(const type &l, const type &r) {{ return make_##type(l.x % r.x, l.y % r.y, l.z % r.z, l.w % r.w); }}
#define MAKE_VEC2_OP(type) \\
  inline type operator+(const type &l, const type &r) {{ return make_##type(l.x + r.x, l.y + r.y); }} \\
  inline type operator-(const type &l, const type &r) {{ return make_##type(l.x - r.x, l.y - r.y); }} \\
  inline type operator*(const type &l, const type &r) {{ return make_##type(l.x * r.x, l.y * r.y); }} \\
  inline type operator/(const type &l, const type &r) {{ return make_##type(l.x / r.x, l.y / r.y); }} \\
  inline type operator%(const type &l, const type &r) {{ return make_##type(l.x % r.x, l.y % r.y); }}

MAKE_VEC4_OP(int4)
MAKE_VEC2_OP(int2)

#endif // USING_NATIVE_VECTELEM

#endif

extern "C" void {kernel_name}(sycl::queue* q, void **__args) {{
  {expand_args}

  using namespace std;

  q->submit([&](auto &cgh) {{
    {group_shared}
    {expand_accs}
    cgh.parallel_for(cl::sycl::nd_range<3>(cl::sycl::range<3>({str(gds)[1:-1]}), cl::sycl::range<3>({str(lds)[1:-1]})), [=](cl::sycl::nd_item<3> _item) {{
      {expand_ptrs}
      {index_str}

      {body}
    }});
  }});
}}
'''
  return full_body

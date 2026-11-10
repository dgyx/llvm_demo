#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_demo.py — MCU demo 一键运行脚本

流程：
  1. gcc 编译模拟器 mcu_sim
  2. 生成随机测试数据（n=64 个 int32 x 2 组，写入 data.bin）
  3. mcu_cc.py 编译 kernel_scalar.c / kernel_simd.c → *.mcu 镜像
     （内部调用 LLVM clang 生成 x86 汇编，再做指令选择 + 寄存器分配）
  4. 模拟器执行两个程序，用 --dump 读回完整 32-bit 结果并比对
  5. 打印 SIMD vs 标量的静态指令数对比
"""

import os
import random
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mcu_cc  # noqa: E402

N = 64
DATA_BASE = 0x8000
RESULT_ADDR = 0xF000  # kernel 把 32-bit 结果 st 到这里


def sh(cmd, check=True):
    """运行命令；check=False 时容忍非零退出码（HALT 退出码 = r0 低 8 位）。"""
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       cwd=HERE)
    if check and p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise RuntimeError("命令失败(%d): %s" % (p.returncode, cmd))
    return p.stdout


def gen_data(path):
    rng = random.Random(42)
    va = [rng.randint(-100, 100) for _ in range(N)]
    vb = [rng.randint(-100, 100) for _ in range(N)]
    with open(path, "wb") as f:
        f.write(struct.pack("<%di" % N, *va))
        f.write(struct.pack("<%di" % N, *vb))
    return va, vb


def s32(u):
    """把 dump 出来的无符号数转成有符号 int32。"""
    return u - 0x100000000 if u >= 0x80000000 else u


def count_dynamic_steps(prog_name, max_steps=2000000):
    """用 --trace 统计动态执行步数（教学：观察 SIMD 减少动态指令数）。"""
    p = subprocess.run(
        "./mcu_sim %s --trace --max-steps %d" % (prog_name, max_steps),
        shell=True, capture_output=True, text=True, cwd=HERE)
    return p.stderr.count("\n") if p.returncode in (0,) or True else -1


def main():
    # 1. 模拟器
    if not os.path.exists(os.path.join(HERE, "mcu_sim")):
        sh("gcc -O2 -Wall -o mcu_sim mcu_sim.c")
    print("[1] mcu_sim 就绪")

    # 2. 测试数据
    data_bin = os.path.join(HERE, "data.bin")
    a, b = gen_data(data_bin)
    expect = sum(x * y for x, y in zip(a, b))
    print("[2] 测试数据 n=%d  参考结果=%d" % (N, expect))

    results = {}
    for name in ("kernel_scalar", "kernel_simd"):
        src = os.path.join(HERE, name + ".c")
        prog = os.path.join(HERE, name + ".mcu")
        # 3. 编译（LLVM clang → .s → MCU 指令选择 → 寄存器分配 → 镜像）
        img = mcu_cc.compile_kernel(
            src, 1, data_path=data_bin, data_base=DATA_BASE,
            args_values=[DATA_BASE, DATA_BASE + 4 * N, N, RESULT_ADDR])
        with open(prog, "wb") as f:
            f.write(img)
        n_insn = (len(img) - 68 - 4 * N * 2) // 4

        # 4. 模拟执行；退出码 = r0 & 0xFF（可能非 0，属正常）
        out = sh("./mcu_sim %s --dump %x:1 --max-steps 2000000"
                 % (os.path.basename(prog), RESULT_ADDR), check=False)
        got = s32(int(out.split()[1]))
        steps = count_dynamic_steps(os.path.basename(prog))
        results[name] = (n_insn, got, steps)
        status = "OK" if got == expect else "FAIL(期望 %d)" % expect
        print("[3] %-14s 静态指令=%-3d 动态步数=%-5d 结果=%-8d %s"
              % (name, n_insn, steps, got, status))
        if got != expect:
            raise SystemExit("结果不匹配！")

    ns, vs = results["kernel_scalar"], results["kernel_simd"]
    print("[4] 对比: 静态指令 %d → %d (%.0f%%)，动态步数 %d → %d (%.0f%%)"
          % (ns[0], vs[0], 100.0 * vs[0] / ns[0],
             ns[2], vs[2], 100.0 * vs[2] / ns[2]))
    print("[5] demo 完成 ✅  （--trace 看逐条执行；--list 看 MCU 反汇编）")


if __name__ == "__main__":
    main()

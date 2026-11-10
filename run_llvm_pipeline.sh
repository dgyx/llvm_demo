#!/usr/bin/env bash
# run_llvm_pipeline.sh — 演示 LLVM IR 全流程：IR → bitcode → opt → llc(MC) → .o → 可执行
#
# 用法: ./run_llvm_pipeline.sh [dot_scalar|dot_simd]
set -euo pipefail

LLVM=~/package/llvm+mlir-17.0.0-x86_64-linux-gnu-ubuntu-18.04-release
CC=/usr/bin/clang        # 仅用作 C → IR 前端（系统 clang 14 即可）
HERE="$(cd "$(dirname "$0")" && pwd)"
KERNEL="${1:-dot_simd}"

cd "$HERE"
echo "════ 0. C → LLVM IR（中间表示） ════"
$CC -O1 -S -emit-llvm "$KERNEL.c" -o "$KERNEL.ll"
grep -E 'define|<(8 x i32)>|load|mul|add' "$KERNEL.ll" | head -8 || true

echo
echo "════ 1. 文本 IR → bitcode（llvm-as） ════"
$LLVM/bin/llvm-as "$KERNEL.ll" -o "$KERNEL.bc"
ls -l "$KERNEL.bc" | awk '{print $NF, $5" bytes"}'

echo
echo "════ 2. IR 优化（opt, NewPassManager） ════"
$LLVM/bin/opt -passes='default<O2>' "$KERNEL.bc" -o "$KERNEL.opt.bc"
$LLVM/bin/opt -passes='print<loop-lessons>' -disable-output "$KERNEL.bc" 2>/dev/null || true
echo "优化前后指令数: $(llvm-inscount() { :; }; $LLVM/bin/llvm-dis -o - "$KERNEL.bc" | grep -cE '^\s*%.*=')"

echo
echo "════ 3. llc：IR → 汇编（内部: SelectionDAG ISel → MCInst → MC 汇编器） ════"
# -relocation-model=pic：默认 PIE 链接要求位置无关代码
# -mattr=+avx2：启用 AVX2，让 <8 x i32> 映射到 ymm 寄存器
$LLVM/bin/llc "$KERNEL.opt.bc" -o "$KERNEL.s" -filetype=asm \
    -relocation-model=pic -mattr=+avx2
grep -cE '^\s*(v?mov|v?pmul|v?padd|mov|imul|add)' "$KERNEL.s" | xargs echo "ALU/load 指令行数:"
grep -E 'vmovdqu|vpmulld|vpaddd' "$KERNEL.s" | head -4 || echo "（无 AVX 指令：看看 -mattr=+avx2）"

echo
echo "════ 3'. llc -filetype=obj：MC 直接生成 ELF（不经 .s 文本） ════"
$LLVM/bin/llc "$KERNEL.opt.bc" -o "$KERNEL.o" -filetype=obj \
    -relocation-model=pic -mattr=+avx2
$LLVM/bin/llvm-nm "$KERNEL.o" | head -4

echo
echo "════ 4. 独立调用 MC 汇编器：.s → .o（llvm-mc） ════"
$LLVM/bin/llvm-mc -triple=x86_64-linux-gnu -filetype=obj \
    -x86-asm-syntax=att "$KERNEL.s" -o "$KERNEL.mc.o"

echo
echo "════ 5. 链接 → 可执行文件（gcc 提供 crt0/ld） ════"
gcc "$KERNEL.mc.o" -o "$KERNEL.exe"
gcc "$KERNEL.o" -o "$KERNEL.exe2"

echo
echo "════ 6. 运行 ════"
"./$KERNEL.exe"
"./$KERNEL.exe2"

echo
echo "════ 7. 反汇编验证（llvm-objdump） ════"
$LLVM/bin/llvm-objdump -d "$KERNEL.mc.o" | grep -E 'vmovdqu|vpmulld|vpaddd|imul' | head -6 || \
$LLVM/bin/llvm-objdump -d "$KERNEL.mc.o" | sed -n '5,12p'

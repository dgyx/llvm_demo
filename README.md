# LLVM 工具链 Demo — 中间 IR 生成、汇编器对接、可执行文件产出

两部分：

1. **`run_llvm_pipeline.sh`**（真实 LLVM 全流程）：C → **LLVM IR** → bitcode →
   opt 优化 → **llc 对接 MC 汇编器** → 目标文件 → 链接成可执行程序并运行。
   LLVM 工具链使用 `~/package/llvm+mlir-17.0.0-x86_64-linux-gnu-ubuntu-18.04-release`。
2. **mcu_demo**（自制指令集教学版）：自己设计 MCU 指令集（scalar + vector
   SIMD），Python 模拟"后端"翻译 + 自制模拟器执行。

## 快速开始

```bash
cd llvm_test/mcu_demo

# ── 真实 LLVM IR 全流程（dot_simd / dot_scalar）──
./run_llvm_pipeline.sh dot_simd    # <8 x i32> 向量 IR → AVX2 指令
./run_llvm_pipeline.sh dot_scalar  # 标量对照

# ── 自制 MCU 指令集教学 demo ──
python3 run_demo.py
```

期望输出（SIMD 版）：

```
dot = 120        （两条链路 mc.o / .o 链接的程序都输出）
vpmulld -0x20(%rdi,%rcx,4), %ymm1, %ymm1   ← <8 x i32> mul 映射的 AVX2
vpaddd  %ymm0, %ymm1, %ymm0
```

## LLVM 全流程各阶段（run_llvm_pipeline.sh 详解）

| 步骤 | 命令 | 说明 |
|------|------|------|
| 0 | `clang -S -emit-llvm` | C → **文本 LLVM IR** (`.ll`)。SIMD 版能看到 `<8 x i32>` 向量类型、`phi`/`extractelement` |
| 1 | `llvm-as` | 文本 IR → **bitcode** (`.bc`，二进制紧凑格式) |
| 2 | `opt -passes='default<O2>'` | 在 IR 层跑优化（内联、LICM、循环不变量外提…） |
| 3 | `llc -filetype=asm` | IR → 汇编。内部链路：**SelectionDAG/GlobalISel 指令选择 → MCInst → MCAsmPrinter（汇编器打印）**。`-mattr=+avx2` 决定向量指令能否映射到 ymm |
| 3' | `llc -filetype=obj` | MC 层直接 emit **ELF 目标文件**（MCCodeEmitter 编码 + MCObjectStreamer），不经过 `.s` 文本 |
| 4 | `llvm-mc -filetype=obj` | **独立调用汇编器**：`.s` 文本 → `.o`（AsmParser → MCInst → 编码器），验证"汇编器"这一层可以脱离编译器单独使用 |
| 5 | `gcc x.o -o x.exe` | 链接（crt0 + libc + ld）。也可用 `ld.lld` |
| 6 | 运行 | `dot = 120`，SIMD 与标量结果一致 |
| 7 | `llvm-objdump -d` | 反汇编验证编码（如 `c4 e2 75 40 4c 8f e0` = VEX 前缀的 vpmulld） |

关键认知：
- **MC 层是"汇编码器"的抽象**：MCInst（指令的机器无关表示）→ MCCodeEmitter
  （按 TableGen 生成的编码表打二进制）→ MCObjectStreamer（写 ELF section）。
  `llc -filetype=obj` 与 `llvm-mc -filetype=obj` 走的是同一套底层。
- **向量 IR 的 lowering**：`<8 x i32>` 的 `mul` 一条 IR 在 AVX2 下生成一条
  `vpmulld`；不开 avx2 时会拆成 2 条 128-bit `pmulld`（llc 默认只开到 SSE2）。
- **PIC/PIE**：默认 gcc 走 PIE 链接，llc 需 `-relocation-model=pic`，否则
  `R_X86_64_32` 重定位报错。

## 自制 MCU 指令集教学 demo

```
kernel.c ──clang -O1──▶ kernel.s ──mcu_cc.py──▶ prog.mcu ──mcu_sim──▶ 结果
           (LLVM 前端)              (指令选择+寄存器分配)    (指令集模拟器)
```

运行 `python3 run_demo.py`：

```
[3] kernel_scalar  静态指令=23  动态步数=839   结果=-7863    OK
[3] kernel_simd    静态指令=39  动态步数=135   结果=-7863    OK
[4] 对比: 静态指令 23 → 39 (170%)，动态步数 839 → 135 (16%)
```

SIMD 版动态步数只有标量版的 **16%** —— 8-lane 向量化 + vdot 归约的收益。

## 文件说明

| 文件 | 角色 | 对应真实 LLVM 后端中的东西 |
|------|------|--------------------------|
| `mcu_sim.h` | 指令集定义（编码/ABI） | `*.td` (TableGen) |
| `mcu_sim.c` | 指令集模拟器（加载/执行/dump） | QEMU / ISS |
| `mcu_cc.py` | 编译器驱动：clang → 解析 → 指令选择 → 寄存器分配 → 编码 | `llvm-backend` 的 SelectionDAG + RegAlloc |
| `kernel_scalar.c` | 标量内核（点积 baseline） | 测试用例 |
| `kernel_simd.c` | SIMD 内核（`mcu.vld/vdot` 内联汇编） | intrinsic/内联汇编 kernel |
| `run_demo.py` | 一键构建 + 运行 + 比对 | lit 测试 |

## 指令集 (MCU-ISA v1.0)

- 32-bit 定长指令：`[opcode:8][rd:4][rs1:4][rs2:4][imm12]`
- 16 个 32-bit 标量寄存器 r0-r15（r0 返回值、r12 恒零、r13 SP）
- 8 个向量寄存器 v0-v7，每个 8 lanes x 32-bit
- 64KB 平坦内存

**标量指令**：`movi/movihi/mov, add/sub/mul/div/mod, and/or/xor,
shl/shr/sra, ld/st, jmp, beq/bne/blt/bge/bgt/ble, halt`

**Vector SIMD 指令**（lane-wise）：`vld/vst, vadd/vsub/vmul/vdiv,
vmax/vmin, vbcst（标量广播）, vdot（向量点积归约）, vrsum（求和归约）`

## 用法示例

```bash
# 编译内核（data.bin 加载到 0x8000，4 个参数 → r1-r4）
python3 mcu_cc.py kernel_simd.c -o prog.mcu --data data.bin \
    --arg 0x8000 --arg 0x8100 --arg 64 --arg 0xF000 --list

# 执行（--trace 逐条反汇编，--dump BASE:COUNT 读回内存）
./mcu_sim prog.mcu --trace
./mcu_sim prog.mcu --dump f000:1
```

内核中用内联汇编发 SIMD 指令：

```c
int dot;
__asm__("mcu.vld   v0, [%1]\n"    // v0 ← 8 个 int32 从 a
        "mcu.vld   v1, [%2]\n"    // v1 ← 8 个 int32 从 b
        "mcu.vdot  %0, v0, v1\n"  // %0 ← sum(v0[i]*v1[i])
        : "=r"(dot)
        : "r"(a), "r"(b));
```

## 实现说明（教学要点）

1. **为什么经由 x86 汇编**：真实后端挂在 LLVM 里需要编译 LLVM（数小时）。
   本 demo 让 clang 输出受约束的 x86-64 汇编（禁向量化/展开/栈保护），
   Python 端做语义级模式匹配（`mov/add/cmp+jcc/寻址` → MCU 指令），
   教学结构与 SelectionDAG ISel 同构且几分钟即可读完。
2. **寄存器分配**：线性扫描。每个 x86 寄存器映射唯一 vreg（循环回边
   天然正确）；参数寄存器（%rdi..%r9 → r1-r6）预着色且区间扩为
   [0, last] 防止跨回边被覆盖。
3. **cmp+jcc 融合**：MCU 无标志位，`cmp` 不生成指令，紧随的 `jcc`
   直接合成 `b<cc> rs1, rs2`（比较操作数回看取得）。
4. **大常量**：imm12 装不下时 `movihi`（高 12 位 << 12）+ `add` 复合，
   并处理符号扩展进位（lo ≥ 0x800 时 hi+1）。

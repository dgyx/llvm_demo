#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcu_cc.py — MCU-ISA 教学编译器驱动（"用 LLVM 生成执行程序" 的核心）

流水线（与真实 LLVM 后端同构，只是把 ISel 换成了 Python 模式匹配器）：

    kernel.c
      │ ① clang -O1 -fno-integrated-as
      │     （禁 vectorize/unroll/stack-protector → 受约束的整数代码；
      │       -fno-integrated-as 让 mcu.* 内联汇编原样保留在 .s 中）
      ▼
    kernel.s        x86-64 AT&T 汇编（含 #APP 块里的 mcu.* 行）
      │ ② 解析 .s → 线性指令流
      │ ③ 指令选择：x86 语义模式 → MCU 指令
      │     · mov/add/sub/imul/cmp+jcc/ld/st   → 标量指令
      │     · mcu.vld/vmul/vdot/...            → vector SIMD 指令
      │ ④ 线性扫描寄存器分配（r1-r11/r14/r15；参数对应 r1-r6 预着色）
      ▼
    prog.mcu        mcu_sim 可执行镜像

寄存器模型：每个 x86 寄存器在整个函数内映射到唯一一个 vreg
（等价于物理寄存器式翻译），循环回边上的携带值天然正确；
只有 load_imm / 地址计算的临时值使用独立短生命周期 vreg。

用法:
    ./mcu_cc.py kernel.c -o prog.mcu [--data data.bin[@0x8000]] [--arg N]...
                 [--list[=FILE]] [--save-asm kernel.s] [-O1]
    ./mcu_cc.py kernel.s -o prog.mcu ...      # 跳过 clang
"""

import argparse
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------
# 与 mcu_sim.h 一致的机器常量
# ---------------------------------------------------------------
PROG_MAGIC = 0x3155434D
ALLOCA_BASE = 0x4000
ALLOCA_SIZE = 0x2000
DATA_DEFAULT_BASE = 0x8000
IMM12_MIN, IMM12_MAX = -2048, 2047
R_RET, R_ARG0, R_ZERO, R_SP = 0, 1, 12, 13

SCALAR_PHYS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15]
SVBASE = 1000          # 标量虚拟寄存器编号基址（< SVBASE 视为物理寄存器）

X86_ARG64 = ["%rdi", "%rsi", "%rdx", "%rcx", "%r8", "%r9"]
# pin 查找必须按 32 位名归一化（%edx → %rdx），在 REG64TO32 之后初始化

REG64TO32 = {
    "%rax": "%eax", "%rbx": "%ebx", "%rcx": "%ecx", "%rdx": "%edx",
    "%rsi": "%esi", "%rdi": "%edi", "%rbp": "%ebp", "%rsp": "%esp",
    "%r8": "%r8d", "%r9": "%r9d", "%r10": "%r10d", "%r11": "%r11d",
    "%r12": "%r12d", "%r13": "%r13d", "%r14": "%r14d", "%r15": "%r15d",
}
REG8TO32 = {"%al": "%eax", "%bl": "%ebx", "%cl": "%ecx", "%dl": "%edx",
            "%sil": "%esi", "%dil": "%edi", "%r8b": "%r8d", "%r9b": "%r9d"}
ARG_PIN_PHYS = {r: i + 1 for i, r in enumerate(X86_ARG64)}  # → r1..r6
ARG_PIN32 = {REG64TO32[r]: p for r, p in ARG_PIN_PHYS.items()}

OPNUM = {
    "nop": 0x00, "movi": 0x01, "mov": 0x02,
    "add": 0x03, "sub": 0x04, "mul": 0x05, "div": 0x06, "mod": 0x07,
    "and": 0x08, "or": 0x09, "xor": 0x0A,
    "shl": 0x0B, "shr": 0x0C, "sra": 0x0D,
    "ld": 0x0E, "st": 0x0F,
    "jmp": 0x10, "beq": 0x11, "bne": 0x12, "blt": 0x13,
    "bge": 0x14, "bgt": 0x15, "ble": 0x16,
    "movihi": 0x1E, "halt": 0x1F,
    "vld": 0x20, "vst": 0x21,
    "vadd": 0x22, "vsub": 0x23, "vmul": 0x24, "vdiv": 0x25,
    "vmax": 0x26, "vmin": 0x27, "vbcst": 0x28, "vdot": 0x29, "vrsum": 0x2A,
}

# 每条 MCU 指令 3 个操作数类型: 's'=标量 'v'=向量 (None=无)
OPKIND = {
    "movi": ("s", None, None), "movihi": ("s", None, None),
    "mov": ("s", "s", None),
    "add": ("s", "s", "s"), "sub": ("s", "s", "s"), "mul": ("s", "s", "s"),
    "div": ("s", "s", "s"), "mod": ("s", "s", "s"),
    "and": ("s", "s", "s"), "or": ("s", "s", "s"), "xor": ("s", "s", "s"),
    "shl": ("s", "s", "s"), "shr": ("s", "s", "s"), "sra": ("s", "s", "s"),
    "ld": ("s", "s", None), "st": (None, "s", "s"),
    "jmp": (None, None, None),
    "beq": (None, "s", "s"), "bne": (None, "s", "s"), "blt": (None, "s", "s"),
    "bge": (None, "s", "s"), "bgt": (None, "s", "s"), "ble": (None, "s", "s"),
    "halt": (None, None, None),
    "vld": ("v", "s", None), "vst": ("v", "s", None),
    "vadd": ("v", "v", "v"), "vsub": ("v", "v", "v"), "vmul": ("v", "v", "v"),
    "vdiv": ("v", "v", "v"), "vmax": ("v", "v", "v"), "vmin": ("v", "v", "v"),
    "vbcst": ("v", "s", None),
    "vdot": ("s", "v", "v"), "vrsum": ("s", "v", None),
}

JCC_MAP = {"je": "beq", "jne": "bne", "jl": "blt", "jge": "bge",
           "jg": "bgt", "jle": "ble"}

# x86 双操作数 ALU（src, dst）→ MCU op（64 位形式按 32 位值处理：
# 地址空间 ≤64KB，指针截断到 32 位无损）
BINOP_MAP = {
    "addl": "add", "addq": "add", "subl": "sub", "subq": "sub",
    "imull": "mul", "andl": "and", "andq": "and",
    "orl": "or", "orq": "or", "xorl": "xor", "xorq": "xor",
    "sall": "shl", "shll": "shl", "shlq": "shl",
    "shrl": "shr", "shrq": "shr", "sarl": "sra", "sarq": "sra",
}

# mov 变体（位宽不同，MCU 一律按 32 位语义；教学 kernel 只用 int/指针）
MOV_MAP = ["movl", "movq", "movw", "movb", "movslq", "movsxd", "movzbl",
           "movzwl", "movsbl", "movswl", "movsbq", "movzwq"]

# 无操作/纯标记指令，直接忽略
NOOP_MNEMS = {"cltq", "cdqe", "cltd", "cwde", "cdq", "cqto", "cqo",
              "nop", "nopl", "nopw", "endbr64", "leave"}

CLANG = os.environ.get(
    "MCU_CLANG",
    os.path.join(os.path.dirname(os.path.dirname(HERE)),
                 "third_party", "llvm_release", "bin", "clang"))


class CompileError(Exception):
    pass


def reg32(name):
    if name in REG8TO32:
        return REG8TO32[name]
    return REG64TO32.get(name, name)


def is_imm(s):
    return s.startswith("$")


def imm_val(s):
    return int(s[1:], 0)


def split_operands(s):
    """按逗号切操作数，但忽略括号内的逗号（base,index,scale 形式）。"""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def parse_mem(s):
    """AT&T 内存操作数 disp(base,index,scale) → (disp, base, index, scale)"""
    if "(" not in s:
        return int(s, 0), None, None, 1
    disp_s, _, inner = s.partition("(")
    disp = int(disp_s, 0) if disp_s.strip() else 0
    comps = [c.strip() for c in inner.rstrip(")").split(",")]
    base = comps[0] or None
    index = comps[1] if len(comps) > 1 and comps[1] else None
    scale = int(comps[2], 0) if len(comps) > 2 and comps[2] else 1
    return disp, base, index, scale


# ---------------------------------------------------------------
# 中间表示：MCU 机器指令（分配前操作数为 vreg；物理寄存器号 < SVBASE）
# ---------------------------------------------------------------
class MOp(object):
    __slots__ = ("op", "rd", "rs1", "rs2", "imm", "label")

    def __init__(self, op, rd=None, rs1=None, rs2=None, imm=0, label=None):
        self.op, self.rd, self.rs1, self.rs2 = op, rd, rs1, rs2
        self.imm, self.label = imm, label

    def scalar_refs(self):
        """所有标量操作数（rd/rs1/rs2 中 kind=='s' 的）列表。"""
        kinds = OPKIND[self.op]
        return [v for kind, v in zip(kinds, (self.rd, self.rs1, self.rs2))
                if kind == "s" and v is not None]

    def text(self):
        kinds = OPKIND[self.op]
        parts = [self.op]
        for kind, val in zip(kinds, (self.rd, self.rs1, self.rs2)):
            if val is None:
                continue
            parts.append("v%d" % val if kind == "v" else "r%d" % val)
        if self.op in ("ld", "vld"):
            parts.append("[r%d%+d]" % (self.rs1, self.imm))
        elif self.op in ("st", "vst"):
            parts.append("[r%d%+d]" % (self.rs1, self.imm))
        elif self.label is not None:
            parts.append(str(self.label))
        elif self.imm:
            parts.append("#%d" % self.imm)
        return " ".join(parts)


# ---------------------------------------------------------------
# ① clang：C → 受约束的 x86-64 汇编（mcu.* 内联原样保留）
# ---------------------------------------------------------------
def run_clang(src, opt_level):
    if src.endswith(".s"):
        with open(src) as f:
            return f.read()
    if not os.path.exists(CLANG):
        raise CompileError("找不到 clang: %s（可用 MCU_CLANG 环境变量覆盖）"
                           % CLANG)
    out = os.path.join(tempfile.mkdtemp(prefix="mcu_cc_"), "kernel.s")
    cmd = [CLANG, src, "-S", "-O%d" % opt_level,
           "-fno-stack-protector", "-fno-unroll-loops",
           "-fno-vectorize", "-fno-slp-vectorize", "-fno-jump-tables",
           "-fno-integrated-as",
           "-o", out]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise CompileError("clang 编译失败")
    with open(out) as f:
        return f.read()


# ---------------------------------------------------------------
# ② 汇编解析 → item 流
#    item: ("label", name, []) | ("inst", mnem, args)
#          | ("mcuasm", "mcu.xxx ...", [])
# ---------------------------------------------------------------
def parse_assembly(s_text):
    items, func_name, in_app = [], None, False
    for raw in s_text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#APP"):
            in_app = True
            continue
        if stripped.startswith("#NO_APP"):
            in_app = False
            continue
        if in_app:
            if stripped.lower().startswith("mcu."):
                items.append(("mcuasm", stripped, []))
            continue
        line = raw.split("#")[0].strip()
        if not line:
            continue
        if line.startswith(".globl"):
            func_name = line.split()[-1]
            continue
        if line.endswith(":"):              # 先判标签（.LBB0_1: 也以 . 开头）
            items.append(("label", line[:-1].strip(), []))
            continue
        if line.startswith("."):
            continue                        # 其余伪操作（.p2align/.cfi/...）
        # 指令：助记符与操作数之间是 tab（clang 输出风格）
        mnem, _, rest = line.partition("\t")
        rest = rest.strip()
        args = []
        if rest:
            # 先按 tab 分隔（clang 风格："mem\tparam"），再按逗号切操作数
            comps = [c.strip() for c in rest.split("\t") if c.strip()]
            joined = " ".join(comps)        # "mem param" 中间空格保留
            args = split_operands(joined)
        items.append(("inst", mnem.strip().lower(), args))
    if func_name is None:
        raise CompileError("汇编中找不到 .globl 函数名")
    return items


# ---------------------------------------------------------------
# ③ 指令选择
# ---------------------------------------------------------------
class Selector(object):
    def __init__(self):
        self.out = []
        self.labels = {}        # asm label → out 索引
        self.vcount = 0
        self.x86_v = {}         # x86 32 位寄存器名 → 唯一 vreg
        self.pin = {}           # vreg → 参数物理寄存器
        self.pending_cmp = None  # (dst_v, src_v)，最近一条 cmp/test
        self.pushed = 0         # pushq 槽位数（编译期分配）

    # ---- vreg 管理：x86 寄存器 ↔ 唯一 vreg；临时值独立编号 ----
    def new_v(self):
        self.vcount += 1
        return SVBASE + self.vcount

    def get(self, x86_reg):
        """引用 x86 寄存器 → 其唯一 vreg（首次引用时创建，参数寄存器预着色）。"""
        r = reg32(x86_reg)
        if r not in self.x86_v:
            v = self.new_v()
            self.x86_v[r] = v
            if r in ARG_PIN32:              # 32 位名归一化后查 pin
                self.pin[v] = ARG_PIN32[r]
        return self.x86_v[r]

    def emit(self, op, rd=None, rs1=None, rs2=None, imm=0, label=None):
        self.out.append(MOp(op, rd, rs1, rs2, imm, label))

    def load_imm(self, vreg, value):
        """32 位常量 → vreg（imm12 放不下时 movihi+add 复合）。"""
        if IMM12_MIN <= value <= IMM12_MAX:
            self.emit("movi", rd=vreg, imm=value)
            return
        if not 0 <= value <= 0xFFFF:
            raise CompileError("常量 0x%x 超出 16 位地址空间" % value)
        hi, lo = value >> 12, value & 0xFFF
        if lo >= 0x800:                     # imm12 符号扩展进位补偿
            hi, lo = hi + 1, lo - 0x1000
        self.emit("movihi", rd=vreg, imm=hi)
        if lo:
            t = self.new_v()
            self.emit("movi", rd=t, imm=lo)
            self.emit("add", rd=vreg, rs1=vreg, rs2=t)

    def imm12_reg(self, value):
        """把常量装入临时 vreg 并返回。"""
        t = self.new_v()
        self.load_imm(t, value)
        return t

    # ---- 地址计算 ----
    def addr_value(self, mem_str):
        """AT&T 内存操作数 → (地址基 vreg, disp)。disp 折进 ld/st imm12。"""
        disp, base, index, scale = parse_mem(mem_str)
        base_v = None
        if base in ("%rsp", "%esp"):
            base_v = R_SP                    # 物理栈指针
        elif base in ("%rip",):
            raise CompileError("RIP 相对寻址不支持（数据请用指针参数传入）")
        elif base is not None:
            base_v = self.get(base)
        if index is not None:
            idx_v = self.get(index)
            if scale not in (1, 2, 4, 8):
                raise CompileError("非法 scale %d" % scale)
            if scale > 1:
                s = self.new_v()
                self.emit("movi", rd=s, imm=scale.bit_length() - 1)
                t = self.new_v()
                self.emit("shl", rd=t, rs1=idx_v, rs2=s)
                idx_v = t
            if base_v is None:
                base_v = idx_v
            else:
                a = self.new_v()
                self.emit("add", rd=a, rs1=base_v, rs2=idx_v)
                base_v = a
        if base_v is None:
            raise CompileError("不支持的无基址寻址: %s" % mem_str)
        return base_v, disp

    def emit_ld(self, base_v, disp, dst_v):
        if IMM12_MIN <= disp <= IMM12_MAX:
            self.emit("ld", rd=dst_v, rs1=base_v, imm=disp)
        else:
            d = self.imm12_reg(disp)
            a = self.new_v()
            self.emit("add", rd=a, rs1=base_v, rs2=d)
            self.emit("ld", rd=dst_v, rs1=a, imm=0)

    def emit_st(self, base_v, disp, val_v):
        if IMM12_MIN <= disp <= IMM12_MAX:
            self.emit("st", rs1=base_v, rs2=val_v, imm=disp)
        else:
            d = self.imm12_reg(disp)
            a = self.new_v()
            self.emit("add", rd=a, rs1=base_v, rs2=d)
            self.emit("st", rs1=a, rs2=val_v, imm=0)

    # ---- 主入口 ----
    def run(self, items):
        for it in items:
            kind = it[0]
            if kind == "label":
                self.labels[it[1]] = len(self.out)
            elif kind == "mcuasm":
                self.sel_mcu(it[1])
            else:
                self.dispatch(it[1], it[2])
        self.fixup_branches()
        return self.out

    def dispatch(self, mnem, args):
        if mnem in BINOP_MAP:
            return self.sel_binop(mnem, args)
        if mnem in MOV_MAP:
            return self.sel_mov(mnem, args)
        if mnem == "jmp":
            return self.sel_jmp(mnem, args)
        if mnem in JCC_MAP:
            return self.sel_jcc(mnem, args)
        if mnem in ("ret", "retq"):
            return self.sel_ret(mnem, args)
        if mnem in NOOP_MNEMS:
            return
        if mnem in ("idivl", "idivq"):
            return self.sel_idivl(mnem, args)
        base = mnem[:-1] if mnem and mnem[-1] in "bwlq" else mnem
        handler = getattr(self, "sel_" + base, None)
        if handler is None:
            raise CompileError("不支持的指令: %s %s" % (mnem, args))
        try:
            handler(mnem, args)
        except CompileError as e:
            raise CompileError("选择失败 [%s %s]: %s" % (mnem, args, e))

    # ---- mov 家族 ----
    def sel_mov(self, mnem, args):
        if len(args) != 2:
            raise CompileError("mov 操作数异常")
        src, dst = args
        if is_imm(dst):
            raise CompileError("mov 目标为立即数")
        if is_imm(src):
            self.load_imm(self.get(dst), imm_val(src))
        elif "(" in src:                     # 内存 → 寄存器
            base_v, disp = self.addr_value(src)
            self.emit_ld(base_v, disp, self.get(dst))
        elif "(" in dst:                     # 寄存器 → 内存
            base_v, disp = self.addr_value(dst)
            self.emit_st(base_v, disp, self.get(src))
        else:
            self.emit("mov", rd=self.get(dst), rs1=self.get(src))

    def sel_lea(self, mnem, args):
        src, dst = args
        base_v, disp = self.addr_value(src)
        v = self.get(dst)
        if disp == 0:
            self.emit("mov", rd=v, rs1=base_v)
            return
        if not IMM12_MIN <= disp <= IMM12_MAX:
            raise CompileError("lea 位移 %d 超出 imm12" % disp)
        t = self.imm12_reg(disp)
        self.emit("add", rd=v, rs1=base_v, rs2=t)

    # ---- ALU ----
    def sel_binop(self, mnem, args):
        mop = BINOP_MAP[mnem]
        # 栈指针调整（subq $16,%rsp 等）：直接改物理 R_SP
        if args and args[-1] in ("%rsp", "%esp"):
            if not is_imm(args[0]):
                raise CompileError("非立即数的 rsp 调整不支持")
            delta = imm_val(args[0]) * (1 if mop == "add" else -1)
            t = self.imm12_reg(delta)
            self.emit(mop, rd=R_SP, rs1=R_SP, rs2=t)
            return
        if mop == "mul" and len(args) == 3:  # imull $imm, src, dst
            imm, src, dst = args
            t = self.imm12_reg(imm_val(imm))
            self.emit(mop, rd=self.get(dst), rs1=self.get(src), rs2=t)
            return
        src, dst = args
        if src == dst and mop == "xor":      # xorl %eax,%eax 清零习语
            self.emit("movi", rd=self.get(dst), imm=0)
            return
        if is_imm(src):
            src_v = self.imm12_reg(imm_val(src))
        elif "(" in src:                     # e.g. imull mem, %r9d
            base_v, disp = self.addr_value(src)
            src_v = self.new_v()
            self.emit_ld(base_v, disp, src_v)
        else:
            src_v = self.get(src)
        if "(" in dst:
            raise CompileError("ALU 目标为内存不支持（应先 load）")
        self.emit(mop, rd=self.get(dst), rs1=self.get(dst), rs2=src_v)

    def sel_inc(self, mnem, args):
        t = self.imm12_reg(1)
        self.emit("add", rd=self.get(args[0]), rs1=self.get(args[0]), rs2=t)

    def sel_dec(self, mnem, args):
        t = self.imm12_reg(-1)
        self.emit("add", rd=self.get(args[0]), rs1=self.get(args[0]), rs2=t)

    def sel_neg(self, mnem, args):
        self.emit("sub", rd=self.get(args[0]), rs1=R_ZERO,
                  rs2=self.get(args[0]))

    def sel_not(self, mnem, args):
        t = self.imm12_reg(-1)
        self.emit("xor", rd=self.get(args[0]), rs1=self.get(args[0]), rs2=t)

    # ---- 比较与分支 ----
    def _reg_or_mem_or_imm(self, operand):
        if is_imm(operand):
            return self.imm12_reg(imm_val(operand))
        if "(" in operand:
            base_v, disp = self.addr_value(operand)
            t = self.new_v()
            self.emit_ld(base_v, disp, t)
            return t
        return self.get(operand)

    def sel_cmp(self, mnem, args):
        src, dst = args                     # AT&T: flags(dst - src)
        # 先求 dst（如果 src 处理中新建 vreg，不能影响 dst 已有映射）
        dst_v = self._reg_or_mem_or_imm(dst)
        src_v = self._reg_or_mem_or_imm(src)
        self.pending_cmp = (dst_v, src_v)

    def sel_test(self, mnem, args):
        a, b = args
        if a == b:                          # testl %eax,%eax → 与 0 比
            self.pending_cmp = (self.get(a), R_ZERO)
        else:
            t = self.new_v()
            self.emit("and", rd=t, rs1=self.get(a), rs2=self.get(b))
            self.pending_cmp = (t, R_ZERO)

    def sel_jcc(self, mnem, args):
        if self.pending_cmp is None:
            raise CompileError("%s 前没有可比的 cmp/test" % mnem)
        rs1, rs2 = self.pending_cmp
        self.pending_cmp = None
        self.emit(JCC_MAP[mnem], rs1=rs1, rs2=rs2, label=args[0])

    def sel_jmp(self, mnem, args):
        self.pending_cmp = None
        self.emit("jmp", label=args[0])

    # ---- 函数体结构 ----
    def sel_ret(self, mnem, args):
        eax = self.x86_v.get("%eax")
        if eax is not None:
            self.emit("mov", rd=R_RET, rs1=eax)
        self.emit("halt")

    def sel_push(self, mnem, args):
        """callee-saved 保存 → 编译期槽位 st（不移 SP，槽位固定）。"""
        self.pushed += 1
        self.emit("st", rs1=R_SP, rs2=self.get(args[0]), imm=-4 * self.pushed)

    def sel_pop(self, mnem, args):
        if self.pushed == 0:
            raise CompileError("pop 没有匹配的 push")
        off = -4 * self.pushed
        self.pushed -= 1
        self.emit("ld", rd=self.get(args[0]), rs1=R_SP, imm=off)

    def sel_idivl(self, mnem, args):
        dividend = self.get("%rax")
        divisor = self._reg_or_mem_or_imm(args[0])
        self.emit("div", rd=self.get("%rax"), rs1=dividend, rs2=divisor)
        self.emit("mod", rd=self.get("%rdx"), rs1=dividend, rs2=divisor)

    # ---- 内联汇编：vector SIMD 入口 ----
    def sel_mcu(self, line):
        """'mcu.vld v0, [%rdx]' / 'mcu.vdot %eax, v0, v1' → SIMD MOp"""
        body = line.strip()[len("mcu."):]
        parts = [p.strip() for p in body.split(",")]
        head = parts[0].split(None, 1)
        op = head[0]
        if op not in OPNUM:
            raise CompileError("未知内联指令 mcu.%s" % op)
        ops = ([head[1]] if len(head) > 1 else []) + parts[1:]
        kinds = OPKIND[op]
        if len(ops) > 3:
            raise CompileError("mcu.%s 操作数过多" % op)
        fields = [None, None, None]
        for i, opr in enumerate(ops):
            kind = kinds[i]
            if opr.startswith("%"):         # x86 寄存器（"r" 约束）
                fields[i] = self.get(opr)
            elif opr.startswith("[") and opr.endswith("]"):
                fields[i] = self.get(opr[1:-1])   # [%rdx] 地址寄存器
            elif opr.startswith("v") and opr[1:].isdigit():
                n = int(opr[1:])
                if not 0 <= n < 8:
                    raise CompileError("向量寄存器越界 %s" % opr)
                fields[i] = n
            else:
                raise CompileError("无法解析内联操作数 %r" % opr)
        self.emit(op, *fields)

    def fixup_branches(self):
        for mop in self.out:
            if mop.label is not None:
                if mop.label not in self.labels:
                    raise CompileError("未定义标签 %s" % mop.label)
                mop.imm = 4 * self.labels[mop.label]
                mop.label = None


# ---------------------------------------------------------------
# ④ 线性扫描寄存器分配（vreg → 物理寄存器，in-place 回填）
#    区间 = 该 vreg 首次出现 → 最后一次出现（统一映射下天然覆盖循环回边）
#    参数 pin 的物理寄存器（r1-r6 的子集）从通用池中保留，避免与
#    长生命周期 vreg 互相踩踏。
# ---------------------------------------------------------------
def allocate(mops, pins):
    intervals = {}
    for i, m in enumerate(mops):
        for v in m.scalar_refs():
            iv = intervals.get(v)
            if iv is None:
                intervals[v] = [i, i]
            else:
                iv[1] = i
    # 参数 pin 的 vreg（x86 参数寄存器的唯一映射）可能穿越循环回边
    # 被后续迭代引用（静态位置只在循环体内出现一次），区间强制扩为
    # [0, last] —— 等价于 "入口活跃"，教学版最保守也最正确的做法。
    last = max((iv[1] for iv in intervals.values()), default=0)
    for v in pins:
        if v in intervals:
            intervals[v][0] = 0
            intervals[v][1] = last

    pinned_phys = set(pins.values())
    free = [p for p in SCALAR_PHYS if p not in pinned_phys]
    assign, active = {}, []
    for v, (start, end) in sorted(intervals.items(),
                                  key=lambda kv: (kv[1][0], kv[0])):
        still = []                          # 过期释放
        for end_, v_, p_ in active:
            if end_ >= start:
                still.append((end_, v_, p_))
            else:
                free.append(p_)
        active, free = sorted(still), sorted(free)

        if v in pins:                       # pin 的唯一性由 x86 寄存器
            assign[v] = pins[v]             # 唯一 vreg 映射保证
        else:
            if not free:
                raise CompileError(
                    "寄存器压力过大（教学版无溢出支持），请减小内核复杂度")
            assign[v] = free.pop(0)
        active.append((end, v, assign[v]))
        active.sort()

    for m in mops:
        kinds = OPKIND[m.op]
        vals = (m.rd, m.rs1, m.rs2)
        new_vals = []
        for kind, val in zip(kinds, vals):
            if kind == "s" and val is not None and val >= SVBASE:
                if val not in assign:
                    raise CompileError("内部错误：vreg %d 未分配" % val)
                new_vals.append(assign[val])
            else:
                new_vals.append(val)
        m.rd, m.rs1, m.rs2 = new_vals


# ---------------------------------------------------------------
# 编码 & 镜像打包
# ---------------------------------------------------------------
def encode(mop):
    op = OPNUM[mop.op]
    rd = mop.rd & 0xF if mop.rd is not None else 0
    rs1 = mop.rs1 & 0xF if mop.rs1 is not None else 0
    rs2 = mop.rs2 & 0xF if mop.rs2 is not None else 0
    return ((op << 24) | (rd << 20) | (rs1 << 16) | (rs2 << 12) |
            (mop.imm & 0xFFF))


def build_image(mops, data, data_base, args_values):
    code = b"".join(struct.pack("<I", encode(m)) for m in mops)
    if len(args_values) > 8:
        raise CompileError("启动参数最多 8 个")
    header = struct.pack("<8I8I", PROG_MAGIC, 0, len(code),
                         ALLOCA_BASE, ALLOCA_SIZE, data_base, len(data),
                         len(args_values),
                         *(list(args_values) + [0] * (8 - len(args_values))))
    return header + code + data


# ---------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------
def compile_kernel(src, opt_level, data_path=None,
                   data_base=DATA_DEFAULT_BASE, args_values=(),
                   listing=False, save_asm=None):
    s_text = run_clang(src, opt_level)
    if save_asm:
        with open(save_asm, "w") as f:
            f.write(s_text)

    items = parse_assembly(s_text)
    sel = Selector()
    mops = sel.run(items)
    allocate(mops, sel.pin)

    if listing:
        text = "\n".join("%04x: %s" % (4 * i, m.text())
                         for i, m in enumerate(mops)) + "\n"
        if listing is True:
            sys.stdout.write(text)
        else:
            with open(listing, "w") as f:
                f.write(text)

    data = b""
    if data_path:
        with open(data_path, "rb") as f:
            data = f.read()
    return build_image(mops, data, data_base, list(args_values))


def main():
    ap = argparse.ArgumentParser(description="MCU-ISA 教学编译器")
    ap.add_argument("input", help="kernel.c 或 kernel.s")
    ap.add_argument("-o", "--output", required=True, help="输出 prog.mcu")
    ap.add_argument("-O", type=int, default=1, help="优化级别（默认 1）")
    ap.add_argument("--data", help="数据文件 data.bin[@0x8000]")
    ap.add_argument("--arg", action="append", default=[],
                    help="启动参数（写入 r1..），可多次")
    ap.add_argument("--list", nargs="?", const=True, default=False,
                    help="打印/保存指令列表")
    ap.add_argument("--save-asm", help="保存 clang 生成的 .s")
    ns = ap.parse_args()

    data_path, data_base = None, DATA_DEFAULT_BASE
    if ns.data:
        if "@" in ns.data:
            data_path, base_s = ns.data.split("@")
            data_base = int(base_s, 0)
        else:
            data_path = ns.data

    img = compile_kernel(ns.input, ns.O, data_path, data_base,
                         [int(a, 0) for a in ns.arg], ns.list, ns.save_asm)
    with open(ns.output, "wb") as f:
        f.write(img)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CompileError as e:
        sys.exit("mcu_cc: 错误: %s" % e)

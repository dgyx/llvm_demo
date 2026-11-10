//===----------------------------------------------------------------------===//
// mcu_sim.h — MCU 教学指令集 (MCU-ISA v1.0) 定义
//
// 一个 8-bit MCU 教学指令集 demo：32-bit 定长指令，含标量 + vector SIMD 两类指令。
// 本文件是模拟器 (mcu_sim.c)、迷你后端 (mcu_cc.py) 共用的 "架构规范"，
// 角色相当于真实 LLVM 后端中的 *.td (TableGen) 文件。
//===----------------------------------------------------------------------===//

#ifndef MCU_SIM_H
#define MCU_SIM_H

#include <stdint.h>

//----------------------------------------
// 机器参数
//----------------------------------------
#define MEM_SIZE 0x10000u /* 64KB 平坦内存（2 的幂，地址自动回绕） */
#define VLEN 8            /* SIMD 向量长度：8 lanes x 32-bit */
#define NUM_REGS 16       /* 标量寄存器 r0-r15 */
#define NUM_VREGS 8       /* 向量寄存器 v0-v7 */

//----------------------------------------
// 调用/启动约定 (ABI)
//   模拟器启动时：r(i+1) = header.args[i]（参数寄存器）
//                r12 恒 0，r13 = SP = alloca_base + alloca_size
//                PC = header.entry
//   程序结束时：  返回值放 r0，执行 HALT
//----------------------------------------
#define R_RET 0
#define R_ARG0 1
#define R_ZERO 12
#define R_SP 13

//----------------------------------------
// 指令编码（定长 32-bit，小端存储）:
//
//   31        24 23    20 19    16 15    12 11         0
//   +-----------+--------+--------+--------+------------+
//   |  opcode   |  rd    |  rs1   |  rs2   |   imm12    |
//   +-----------+--------+--------+--------+------------+
//
//   标量 ALU : add rd, rs1, rs2          （rs1/rs2/rd 为标量寄存器号）
//   访存     : ld  rd, [rs1+imm12]        st [rs1+imm12], rs2
//   立即数   : movi rd, imm12 (符号扩展)
//   分支     : b<cc> rs1, rs2, imm12      （imm12 = 目标字节地址，绝对寻址）
//   向量     : vld v(rd), [rs1+imm12]     vst v(rd), [rs1+imm12]
//              vadd v(rd), v(rs1), v(rs2) （rd/rs1/rs2 字段复用为向量寄存器号）
//              vdot rd, v(rs1), v(rs2)    （向量归约到标量寄存器）
//----------------------------------------
enum {
  OP_NOP = 0x00,
  OP_MOVI, /* rd ← imm12 (符号扩展)                                */
  OP_MOV,  /* rd ← rs1                                             */

  OP_ADD = 0x03, /* rd ← rs1 + rs2                                 */
  OP_SUB,        /* rd ← rs1 - rs2                                 */
  OP_MUL,        /* rd ← rs1 * rs2 (低 32 位)                      */
  OP_DIV,        /* rd ← rs1 / rs2 (向零截断，除 0 得 0)           */
  OP_MOD,        /* rd ← rs1 % rs2 (向零截断，模 0 得 0)           */
  OP_AND,
  OP_OR,
  OP_XOR,

  OP_SHL = 0x0B, /* rd ← rs1 << rs2                                */
  OP_SHR,        /* rd ← rs1 >> rs2 (逻辑右移)                     */
  OP_SRA,        /* rd ← rs1 >> rs2 (算术右移)                     */

  OP_LD = 0x0E,  /* rd ← mem32[rs1 + imm12]                        */
  OP_ST = 0x0F,  /* mem32[rs1 + imm12] ← rs2                       */

  OP_JMP = 0x10, /* PC ← imm12 (字节地址)                          */
  OP_BEQ,        /* if (rs1 == rs2) PC ← imm12                     */
  OP_BNE,
  OP_BLT, /* 有符号比较 rs1 <  rs2                                */
  OP_BGE, /* 有符号比较 rs1 >= rs2                                */
  OP_BGT, /* 有符号比较 rs1 >  rs2                                */
  OP_BLE, /* 有符号比较 rs1 <= rs2                                */
  OP_MOVIHI = 0x1E, /* rd ← imm12 << 12（load-upper，构造大地址常量） */
  OP_HALT = 0x1F,

  /* ---- Vector SIMD 指令 (lane-wise, 32-bit lanes) ---- */
  OP_VLD = 0x20,  /* v(rd)[i]  ← mem32[rs1+imm12 + 4*i]            */
  OP_VST,         /* mem32[rs1+imm12 + 4*i] ← v(rd)[i]             */
  OP_VADD = 0x22, /* v(rd)[i] ← v(rs1)[i] + v(rs2)[i]              */
  OP_VSUB,
  OP_VMUL,
  OP_VDIV, /* lane-wise 除法（除 0 lane 得 0）                    */
  OP_VMAX = 0x26, /* v(rd)[i] ← max(v(rs1)[i], v(rs2)[i])          */
  OP_VMIN,
  OP_VBCST, /* v(rd)[i] ← regs[rs1] 广播到所有 lane               */
  OP_VDOT,  /* rd ← 64-bit 累加 sum(v(rs1)[i]*v(rs2)[i]) 取低 32 位 */
  OP_VRSUM, /* rd ← 64-bit 累加 sum(v(rs1)[i])           取低 32 位 */
};

/* 指令编码/解码（static inline，模拟器与自测试共用） */
static inline uint32_t mcu_encode(uint32_t op, uint32_t rd, uint32_t rs1,
                                  uint32_t rs2, int32_t imm) {
  return (((op & 0xFFu) << 24) | ((rd & 0xFu) << 20) | ((rs1 & 0xFu) << 16) |
          ((rs2 & 0xFu) << 12) | ((uint32_t)imm & 0xFFFu));
}

static inline uint32_t mcu_op(uint32_t w) { return (w >> 24) & 0xFFu; }
static inline uint32_t mcu_rd(uint32_t w) { return (w >> 20) & 0xFu; }
static inline uint32_t mcu_rs1(uint32_t w) { return (w >> 16) & 0xFu; }
static inline uint32_t mcu_rs2(uint32_t w) { return (w >> 12) & 0xFu; }
static inline int32_t mcu_imm(uint32_t w) { /* imm12 符号扩展 */
  int32_t v = (int32_t)(w & 0xFFFu);
  return (v & 0x800u) ? v - 0x1000 : v;
}

//----------------------------------------
// 程序镜像文件 (prog.mcu) 格式（小端）：
//   [ProgHeader][code: code_size 字节, 加载到地址 0x0]
//                [data: data_size 字节, 加载到地址 data_base]
//----------------------------------------
#define PROG_MAGIC 0x3155434Du /* "MCU1" */

typedef struct {
  uint32_t magic;       /* PROG_MAGIC                            */
  uint32_t entry;       /* 入口 PC（字节地址，一般 0）            */
  uint32_t code_size;   /* 代码段字节数（加载到地址 0x0）         */
  uint32_t alloca_base; /* 内核局部变量区起始地址                 */
  uint32_t alloca_size; /* 内核局部变量区大小                     */
  uint32_t data_base;   /* 测试数据段加载地址                     */
  uint32_t data_size;   /* 测试数据段大小                         */
  uint32_t nargs;       /* 启动参数个数（写入 r1..rn）            */
  uint32_t args[8];     /* 参数值（数据地址或标量）               */
} ProgHeader;           /* 17 个 u32 = 68 字节                    */

#endif /* MCU_SIM_H */

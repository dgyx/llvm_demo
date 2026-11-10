//===----------------------------------------------------------------------===//
// mcu_sim.c — 8-bit MCU 教学指令集模拟器
//
// 用法: ./mcu_sim prog.mcu [--dump BASE:COUNT] [--trace] [--max-steps N]
//   加载程序镜像 → 从 entry 开始执行 → HALT 后退出码 = r0 & 0xff
//   --dump BASE:COUNT  HALT 后打印 "base[0] base[1] ..."（mem32 按有符号解释，
//                      供 Python 端解析比对）
//   --trace            逐条打印反汇编（教学用途）
//===----------------------------------------------------------------------===//

#include "mcu_sim.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_STEPS_DEFAULT 20000000u /* 防死循环 */

static uint8_t mem[MEM_SIZE];
static int32_t regs[NUM_REGS];
static int32_t vregs[NUM_VREGS][VLEN];

/* 符号扩展读 nbytes 字节（小端） */
static int32_t ld_sx(uint32_t addr, int nbytes) {
  uint32_t v = 0;
  for (int i = nbytes - 1; i >= 0; i--)
    v = (v << 8) | mem[(addr + (uint32_t)i) & (MEM_SIZE - 1)];
  uint32_t sign = 1u << (8 * nbytes - 1);
  return (int32_t)((v ^ sign) - sign);
}

static void st_bytes(uint32_t addr, int32_t val, int nbytes) {
  uint32_t u = (uint32_t)val;
  for (int i = 0; i < nbytes; i++)
    mem[(addr + (uint32_t)i) & (MEM_SIZE - 1)] = (uint8_t)(u >> (8 * i));
}

/* C 语义除法（向零截断），除 0 保护为 0 —— 与前端语义一致 */
static int32_t sdiv(int32_t a, int32_t b) { return b ? a / b : 0; }
static int32_t smod(int32_t a, int32_t b) { return b ? a % b : 0; }

static const char *op_name(uint32_t op) {
  switch (op) {
  case OP_NOP: return "nop";
  case OP_MOVI: return "movi";
  case OP_MOV: return "mov";
  case OP_ADD: return "add";
  case OP_SUB: return "sub";
  case OP_MUL: return "mul";
  case OP_DIV: return "div";
  case OP_MOD: return "mod";
  case OP_AND: return "and";
  case OP_OR: return "or";
  case OP_XOR: return "xor";
  case OP_SHL: return "shl";
  case OP_SHR: return "shr";
  case OP_SRA: return "sra";
  case OP_LD: return "ld";
  case OP_ST: return "st";
  case OP_JMP: return "jmp";
  case OP_BEQ: return "beq";
  case OP_BNE: return "bne";
  case OP_BLT: return "blt";
  case OP_BGE: return "bge";
  case OP_BGT: return "bgt";
  case OP_BLE: return "ble";
  case OP_MOVIHI: return "movihi";
  case OP_HALT: return "halt";
  case OP_VLD: return "vld";
  case OP_VST: return "vst";
  case OP_VADD: return "vadd";
  case OP_VSUB: return "vsub";
  case OP_VMUL: return "vmul";
  case OP_VDIV: return "vdiv";
  case OP_VMAX: return "vmax";
  case OP_VMIN: return "vmin";
  case OP_VBCST: return "vbcst";
  case OP_VDOT: return "vdot";
  case OP_VRSUM: return "vrsum";
  default: return "?";
  }
}

/* 单步执行；HALT 时返回 0xFFFFFFFF，否则返回下一条 PC */
static uint32_t step(uint32_t pc, int trace) {
  uint32_t w = (uint32_t)mem[pc & (MEM_SIZE - 1)] |
               ((uint32_t)mem[(pc + 1) & (MEM_SIZE - 1)] << 8) |
               ((uint32_t)mem[(pc + 2) & (MEM_SIZE - 1)] << 16) |
               ((uint32_t)mem[(pc + 3) & (MEM_SIZE - 1)] << 24);
  uint32_t op = mcu_op(w), rd = mcu_rd(w), rs1 = mcu_rs1(w), rs2 = mcu_rs2(w);
  int32_t imm = mcu_imm(w);
  uint32_t next = pc + 4;

  if (trace)
    fprintf(stderr, "%04x: %-5s rd=r%d rs1=r%d rs2=r%d imm=%d\n", pc,
            op_name(op), rd, rs1, rs2, imm);

  switch (op) {
  case OP_NOP: break;
  case OP_MOVI: regs[rd] = imm; break;
  case OP_MOV: regs[rd] = regs[rs1]; break;

  case OP_ADD: regs[rd] = regs[rs1] + regs[rs2]; break;
  case OP_SUB: regs[rd] = regs[rs1] - regs[rs2]; break;
  case OP_MUL: regs[rd] = regs[rs1] * regs[rs2]; break;
  case OP_DIV: regs[rd] = sdiv(regs[rs1], regs[rs2]); break;
  case OP_MOD: regs[rd] = smod(regs[rs1], regs[rs2]); break;
  case OP_AND: regs[rd] = regs[rs1] & regs[rs2]; break;
  case OP_OR:  regs[rd] = regs[rs1] | regs[rs2]; break;
  case OP_XOR: regs[rd] = regs[rs1] ^ regs[rs2]; break;

  case OP_SHL: regs[rd] = (int32_t)((uint32_t)regs[rs1] << (regs[rs2] & 31)); break;
  case OP_SHR: regs[rd] = (int32_t)((uint32_t)regs[rs1] >> (regs[rs2] & 31)); break;
  case OP_SRA: regs[rd] = regs[rs1] >> (regs[rs2] & 31); break;

  case OP_LD: regs[rd] = ld_sx((uint32_t)(regs[rs1] + imm), 4); break;
  case OP_ST: st_bytes((uint32_t)(regs[rs1] + imm), regs[rs2], 4); break;

  case OP_JMP: next = (uint32_t)imm; break;
  case OP_BEQ: if (regs[rs1] == regs[rs2]) next = (uint32_t)imm; break;
  case OP_BNE: if (regs[rs1] != regs[rs2]) next = (uint32_t)imm; break;
  case OP_BLT: if (regs[rs1] < regs[rs2]) next = (uint32_t)imm; break;
  case OP_BGE: if (regs[rs1] >= regs[rs2]) next = (uint32_t)imm; break;
  case OP_BGT: if (regs[rs1] > regs[rs2]) next = (uint32_t)imm; break;
  case OP_BLE: if (regs[rs1] <= regs[rs2]) next = (uint32_t)imm; break;
  case OP_MOVIHI: regs[rd] = (int32_t)(imm << 12); break;
  case OP_HALT: return 0xFFFFFFFFu;

  /* ---- vector SIMD ---- */
  case OP_VLD: {
    uint32_t a = (uint32_t)(regs[rs1] + imm);
    for (int i = 0; i < VLEN; i++)
      vregs[rd][i] = ld_sx(a + 4u * (uint32_t)i, 4);
    break;
  }
  case OP_VST: {
    uint32_t a = (uint32_t)(regs[rs1] + imm);
    for (int i = 0; i < VLEN; i++)
      st_bytes(a + 4u * (uint32_t)i, vregs[rd][i], 4);
    break;
  }
  case OP_VADD:
  case OP_VSUB:
  case OP_VMUL:
  case OP_VDIV:
  case OP_VMAX:
  case OP_VMIN:
    for (int i = 0; i < VLEN; i++) {
      int32_t a = vregs[rs1][i], b = vregs[rs2][i], r = 0;
      switch (op) {
      case OP_VADD: r = a + b; break;
      case OP_VSUB: r = a - b; break;
      case OP_VMUL: r = a * b; break;
      case OP_VDIV: r = sdiv(a, b); break;
      case OP_VMAX: r = a > b ? a : b; break;
      case OP_VMIN: r = a < b ? a : b; break;
      }
      vregs[rd][i] = r;
    }
    break;
  case OP_VBCST:
    for (int i = 0; i < VLEN; i++)
      vregs[rd][i] = regs[rs1];
    break;
  case OP_VDOT: {
    int64_t acc = 0;
    for (int i = 0; i < VLEN; i++)
      acc += (int64_t)vregs[rs1][i] * (int64_t)vregs[rs2][i];
    regs[rd] = (int32_t)acc; /* 64-bit 累加取低 32 位 */
    break;
  }
  case OP_VRSUM: {
    int64_t acc = 0;
    for (int i = 0; i < VLEN; i++)
      acc += vregs[rs1][i];
    regs[rd] = (int32_t)acc;
    break;
  }

  default:
    fprintf(stderr, "mcu_sim: 非法 opcode 0x%02x @ %04x\n", op, pc);
    exit(2);
  }
  return next;
}

static void load_program(const char *path, uint32_t *pentry) {
  FILE *f = fopen(path, "rb");
  if (!f) {
    fprintf(stderr, "mcu_sim: 无法打开 %s\n", path);
    exit(2);
  }
  ProgHeader h;
  if (fread(&h, sizeof(h), 1, f) != 1 || h.magic != PROG_MAGIC) {
    fprintf(stderr, "mcu_sim: 非法程序镜像 %s\n", path);
    exit(2);
  }
  if (h.code_size % 4 || h.code_size > MEM_SIZE) {
    fprintf(stderr, "mcu_sim: code_size 非法\n");
    exit(2);
  }
  if (fread(mem, 1, h.code_size, f) != h.code_size) {
    fprintf(stderr, "mcu_sim: 代码段读取失败\n");
    exit(2);
  }
  if (h.data_size) {
    if (h.data_base + h.data_size > MEM_SIZE ||
        fread(mem + h.data_base, 1, h.data_size, f) != h.data_size) {
      fprintf(stderr, "mcu_sim: 数据段读取失败\n");
      exit(2);
    }
  }
  fclose(f);

  memset(regs, 0, sizeof(regs));
  memset(vregs, 0, sizeof(vregs));
  for (uint32_t i = 0; i < h.nargs && i < 8; i++)
    regs[R_ARG0 + (int)i] = (int32_t)h.args[i];
  regs[R_ZERO] = 0;
  regs[R_SP] = (int32_t)(h.alloca_base + h.alloca_size); /* SP 指向栈顶 */
  *pentry = h.entry;
}

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr, "用法: %s prog.mcu [--dump BASE:COUNT] [--trace] "
                    "[--max-steps N]\n", argv[0]);
    return 2;
  }
  int trace = 0;
  uint32_t dump_base = 0, dump_count = 0, max_steps = MAX_STEPS_DEFAULT;
  for (int i = 2; i < argc; i++) {
    if (!strcmp(argv[i], "--trace")) {
      trace = 1;
    } else if (!strcmp(argv[i], "--dump") && i + 1 < argc) {
      if (sscanf(argv[++i], "%x:%x", &dump_base, &dump_count) != 2) {
        fprintf(stderr, "mcu_sim: --dump 格式应为 BASE:COUNT (hex)\n");
        return 2;
      }
    } else if (!strcmp(argv[i], "--max-steps") && i + 1 < argc) {
      max_steps = (uint32_t)strtoul(argv[++i], NULL, 0);
    }
  }

  uint32_t pc;
  load_program(argv[1], &pc);

  for (uint32_t n = 0; n < max_steps; n++) {
    uint32_t next = step(pc, trace);
    if (next == 0xFFFFFFFFu)
      goto halted;
    pc = next & (MEM_SIZE - 1);
  }
  fprintf(stderr, "mcu_sim: 超过最大步数 %u，疑似死循环\n", max_steps);
  return 3;

halted:
  if (dump_count) {
    printf("%u", (uint32_t)dump_base);
    for (uint32_t i = 0; i < dump_count; i++)
      printf(" %d", ld_sx(dump_base + 4u * i, 4));
    printf("\n");
  }
  return (int)((uint32_t)regs[R_RET] & 0xFFu);
}

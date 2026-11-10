//============================================================
// dot_scalar.c — 标量点积（用于观察 LLVM IR 与标量指令选择）
//
// 流水线位置：
//   clang -S -emit-llvm → .ll (中间 LLVM IR)
//   llvm-as            → .bc (bitcode)
//   opt -O2            → 优化后 IR
//   llc                → .s / .o（内部对接 MC 汇编器/编码器）
//   llvm-mc / ld       → 可执行文件
//============================================================
int dot(const int *a, const int *b, int n) {
  int sum = 0;
  for (int i = 0; i < n; i++)
    sum += a[i] * b[i];
  return sum;
}

#include <stdio.h>
int main(void) {
  static const int a[8] = {1, 2, 3, 4, 5, 6, 7, 8};
  static const int b[8] = {8, 7, 6, 5, 4, 3, 2, 1};
  printf("dot = %d\n", dot(a, b, 8)); // 期望 1*8+...+8*1 = 120
  return 0;
}

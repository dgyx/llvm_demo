//============================================================
// dot_simd.c — Vector SIMD 版点积：真实 LLVM IR 向量类型 <8 x i32>
//
// 不用内联汇编 —— 让 clang 直接生成向量 IR，x86 后端自动
// lower 成 AVX2 指令（vmovdqu / vpmulld / vpaddd）。
// 观察点：
//   1. .ll 中的 <8 x i32> 类型、shufflevector/insertelement
//   2. llc -mattr=+avx2 之后的真实 SIMD 汇编
//   3. 与标量版的指令数对比
//============================================================
typedef int v8si __attribute__((vector_size(32))); // 8 x i32 = 256-bit

int dot(const int *a, const int *b, int n) {
  v8si acc = {0, 0, 0, 0, 0, 0, 0, 0};
  for (int i = 0; i + 8 <= n; i += 8) {
    v8si va = *(const v8si *)(a + i); // 向量 load → IR: load <8 x i32>
    v8si vb = *(const v8si *)(b + i);
    acc += va * vb;                   // IR: mul <8 x i32> + add <8 x i32>
  }
  // 水平归约：acc[0]+...+acc[7]
  int sum = acc[0] + acc[1] + acc[2] + acc[3] +
            acc[4] + acc[5] + acc[6] + acc[7];
  // 尾部标量（n 非 8 倍数）
  for (int i = n & ~7; i < n; i++)
    sum += a[i] * b[i];
  return sum;
}

#include <stdio.h>
int main(void) {
  static const int a[8] = {1, 2, 3, 4, 5, 6, 7, 8};
  static const int b[8] = {8, 7, 6, 5, 4, 3, 2, 1};
  printf("dot = %d\n", dot(a, b, 8)); // 期望 120
  return 0;
}

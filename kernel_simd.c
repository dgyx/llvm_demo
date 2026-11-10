//============================================================
// kernel_simd.c — Vector SIMD 内核：8-lane 向量点积
//
// 通过内联汇编 mcu.* 显式发射 MCU 的 vector SIMD 指令：
//   mcu.vld   vN, [%rp]     — 加载 8 x i32 连续数据
//   mcu.vmul  vD, vA, vB    — lane-wise 乘
//   mcu.vdot  %r, vA, vB    — 归约点积到标量寄存器
//
// 对比 kernel_scalar.c：n=8k 时 SIMD 版每轮循环体只需
//   vld + vld + vdot + 标量累加（约 4 条），
// 而标量版每轮迭代需要 ld + ld + mul + add + cmp + branch。
//============================================================
int kernel(const int *a, const int *b, int n, int *out) {
  int sum = 0;
  int i = 0;
  for (; i + 8 <= n; i += 8) {
    int dot;
    __asm__("mcu.vld   v0, [%1]\n"
            "mcu.vld   v1, [%2]\n"
            "mcu.vdot  %0, v0, v1\n"
            : "=r"(dot)
            : "r"(a), "r"(b));
    sum += dot;
    a += 8;
    b += 8;
  }
  for (; i < n; i++) // 尾部标量处理（n 非 8 整数倍时）
    sum += a[i] * b[i];
  *out = sum;
  return sum;
}

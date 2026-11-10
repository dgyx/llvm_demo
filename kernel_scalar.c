//============================================================
// kernel_scalar.c — 纯标量内核：数组点积（教学 demo 的 baseline）
//
// int kernel(const int *a, const int *b, int n, int *out)
//   r1 = a, r2 = b, r3 = n, r4 = out（参数寄存器约定）
// 计算点积并写入 *out（完整 32-bit 结果经内存返回，不受 8-bit 退出码截断）
//============================================================
int kernel(const int *a, const int *b, int n, int *out) {
  int sum = 0;
  for (int i = 0; i < n; i++)
    sum += a[i] * b[i];
  *out = sum;
  return sum;
}

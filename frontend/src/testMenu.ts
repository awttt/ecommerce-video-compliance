export const TEST_ACCOUNT_EMAIL = "350944364@qq.com";

export function showTestMenu(email: string | null | undefined): boolean {
  return (email || "").trim().toLowerCase() === TEST_ACCOUNT_EMAIL;
}

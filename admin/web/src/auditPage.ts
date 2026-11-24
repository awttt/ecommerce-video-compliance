export const AUDIT_PAGE_SIZES = [20, 50, 100] as const;
export type AuditPageSize = (typeof AUDIT_PAGE_SIZES)[number];
export const AUDIT_PAGE_SIZE_DEFAULT: AuditPageSize = 20;

export function auditPageSize(value: number): AuditPageSize {
  return (AUDIT_PAGE_SIZES as readonly number[]).includes(value) ? (value as AuditPageSize) : AUDIT_PAGE_SIZE_DEFAULT;
}

export function auditLogsPath(page: number, pageSize: number): string {
  const size = auditPageSize(pageSize);
  const current = Number.isInteger(page) && page >= 1 ? page : 1;
  return `/v1/audit-logs?page=${current}&page_size=${size}`;
}

export function nextAuditPage(
  currentSize: number,
  nextPage: number,
  nextSize: number,
  total: number,
): { page: number; pageSize: AuditPageSize } {
  const size = auditPageSize(nextSize);
  const requested = size !== auditPageSize(currentSize) ? 1 : nextPage;
  const pages = Math.max(1, Math.ceil(Math.max(0, total) / size));
  const page = Number.isInteger(requested) && requested >= 1 ? requested : 1;
  return { page: Math.min(page, pages), pageSize: size };
}

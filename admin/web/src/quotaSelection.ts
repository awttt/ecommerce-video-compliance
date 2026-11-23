/** 一次划拨请求的人数上限，和后端 allocate_batch 一致。 */
export const BATCH_CHUNK = 20;

const ADMIN_ROLES = new Set(["tenant_admin", "platform_admin"]);

/** 公司里可以划拨的成员：管理员的余额就是公司余额，不参与全选。 */
export function assignableMemberIds(members: { id: number; role: string }[]): number[] {
  const seen = new Set<number>();
  const ids: number[] = [];
  for (const item of members) {
    if (ADMIN_ROLES.has(item.role) || seen.has(item.id)) continue;
    seen.add(item.id);
    ids.push(item.id);
  }
  return ids;
}

export function chunkIds(ids: number[], size = BATCH_CHUNK): number[][] {
  if (size < 1) return ids.length ? [ids] : [];
  const chunks: number[][] = [];
  for (let index = 0; index < ids.length; index += size) {
    chunks.push(ids.slice(index, index + size));
  }
  return chunks;
}

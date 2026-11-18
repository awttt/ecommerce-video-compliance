PERMISSIONS: dict[str, set[str]] = {
    "platform_admin": {
        "tenant.manage",
        "member.manage",
        "quota.allocate",
        "keyword.submit",
        "keyword.review",
        "keyword.review.global",
        "usage.read.all",
        "audit.read.all",
        "settings.write",
    },
    "tenant_admin": {
        "member.manage",
        "quota.allocate",
        "keyword.submit",
        "keyword.review",
        "usage.read.tenant",
        "audit.read.tenant",
    },
    "member": {
        "keyword.submit",
        "usage.read.self",
        "audit.read.self",
    },
}

ROLE_LABEL = {
    "platform_admin": "平台管理员",
    "tenant_admin": "公司管理员",
    "member": "普通用户",
}


def has_perm(role: str, perm: str) -> bool:
    return perm in PERMISSIONS.get(role, set())

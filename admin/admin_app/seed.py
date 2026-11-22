"""演示数据。已经有成员时不会覆盖；正式环境用 EVC_ADMIN_SEED=0 彻底关掉。"""

import os
from datetime import timedelta

from sqlalchemy.orm import Session

from admin_app.config import get_settings
from admin_app.models import (
    AppSetting,
    AuditLog,
    Device,
    Member,
    QuotaAllocation,
    RelayAccount,
    Tenant,
    UsageSnapshot,
)
from admin_app.security import hash_password
from admin_app.timeutil import shanghai_now, utcnow

DEMO_PASSWORD = "Yujing123"
GROUP = "电商审核专属-bot创建"


def seed(db: Session) -> None:
    if os.environ.get("EVC_ADMIN_SEED", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    if db.query(Member).first() is not None:
        return
    settings = get_settings()
    now = utcnow()
    clock = shanghai_now()
    password = hash_password(DEMO_PASSWORD)
    db.add(
        AppSetting(
            id=1,
            relay_base_url=settings.relay_base_url,
            relay_gateway_url=settings.relay_gateway_url,
            group_name=settings.relay_group_name or GROUP,
            group_id=settings.relay_group_id,
            backend_mode_enabled=False,
            registration_enabled=False,
            email_verify_ok=True,
        )
    )
    jie = Tenant(
        name="锦程商贸",
        status="active",
        seat_quota=10,
        relay_group_name=GROUP,
        pool_total_cents=3_000_000,
        pool_balance_cents=800_000,
        allocated_total_cents=2_200_000,
        previous_month_cost_cents=553_200,
        created_at=now - timedelta(days=60),
    )
    you = Tenant(
        name="优品优选",
        status="active",
        seat_quota=5,
        relay_group_name=GROUP,
        pool_total_cents=2_000_000,
        pool_balance_cents=1_000_000,
        allocated_total_cents=1_000_000,
        previous_month_cost_cents=196_489,
        created_at=now - timedelta(days=40),
    )
    test = Tenant(
        name="测试公司",
        status="disabled",
        seat_quota=2,
        relay_group_name="",
        pool_total_cents=0,
        pool_balance_cents=0,
        allocated_total_cents=0,
        created_at=now - timedelta(days=80),
    )
    db.add_all([jie, you, test])
    db.flush()

    def add_member(tenant, email, name, role, relay, balance, cost, requests, tokens_in, tokens_out, status="active"):
        member = Member(
            tenant_id=tenant.id if tenant else None,
            email=email,
            name=name,
            role=role,
            password_hash=password,
            relay_user_id=relay,
            status=status,
            balance_cents=balance,
            month_cost_cents=cost,
            lifetime_cost_cents=cost,
            month_requests=requests,
            month_input_tokens=tokens_in,
            month_output_tokens=tokens_out,
            created_at=now - timedelta(days=30),
        )
        db.add(member)
        if relay:
            db.add(
                RelayAccount(
                    relay_user_id=relay,
                    email=email,
                    status="disabled" if status == "disabled" else "active",
                    groups=[GROUP] if status != "disabled" else [GROUP],
                )
            )
        return member

    platform = add_member(None, "admin@yujing.local", "平台管理员", "platform_admin", "", 0, 0, 0, 0, 0)
    li = add_member(jie, "li@jiecheng.com", "李运营", "tenant_admin", "user_1043", 510_000, 231_050, 4102, 6_500_000, 2_900_000)
    zhang = add_member(jie, "zhang@jiecheng.com", "张剪辑", "member", "user_1042", 420_000, 184_200, 3268, 5_100_000, 2_400_000)
    wang = add_member(jie, "wang@jiecheng.com", "王审核", "member", "user_1044", 368_200, 110_500, 1961, 3_000_000, 1_500_000)
    zhao = add_member(
        jie, "zhao@jiecheng.com", "赵兼职", "member", "user_1045", 280_000, 96_050, 1705, 2_600_000, 1_300_000, "disabled"
    )
    zhou = add_member(you, "zhou@youpin.com", "周主管", "tenant_admin", "user_1102", 280_000, 82_000, 500, 700_000, 400_000)
    chen = add_member(you, "chen@youpin.com", "陈店长", "member", "user_1101", 320_000, 98_050, 600, 900_000, 500_000)
    wu = add_member(you, "wu@youpin.com", "吴设计", "member", "user_1103", 179_150, 40_800, 350, 400_000, 200_000)
    db.flush()

    db.add_all(
        [
            Device(member_id=zhang.id, name="剪辑台", last_seen=now, client_version="0.1.0"),
            Device(member_id=zhang.id, name="家里的电脑", last_seen=now - timedelta(hours=5), client_version="0.1.0"),
            Device(member_id=li.id, name="办公本", last_seen=now, client_version="0.1.0"),
            Device(member_id=wang.id, name="审核机", last_seen=now - timedelta(hours=2), client_version="0.1.0"),
            Device(member_id=chen.id, name="店长电脑", last_seen=now - timedelta(hours=1), client_version="0.1.0"),
        ]
    )
    db.add_all(
        [
            QuotaAllocation(
                tenant_id=jie.id,
                member_id=zhang.id,
                amount_cents=50_000,
                direction="add",
                period=clock.strftime("%Y-%m"),
                remark="11 月额度",
                idempotency_key="qa_seed_zhang_500",
                operator_id=li.id,
                status="success",
                relay_applied=True,
                summary="分配 ¥500.00 给 张剪辑",
                created_at=now - timedelta(minutes=2),
            ),
            QuotaAllocation(
                tenant_id=jie.id,
                member_id=None,
                amount_cents=400_000,
                direction="add",
                period=clock.strftime("%Y-%m"),
                remark="月初",
                idempotency_key="qa_seed_nov",
                operator_id=li.id,
                status="success",
                relay_applied=True,
                summary="分配 ¥4000.00 给全体",
                created_at=now - timedelta(days=16),
            ),
            QuotaAllocation(
                tenant_id=jie.id,
                member_id=None,
                amount_cents=400_000,
                direction="add",
                period=(clock.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
                remark="上月",
                idempotency_key="qa_seed_oct",
                operator_id=li.id,
                status="success",
                relay_applied=True,
                summary="分配 ¥4000.00 给全体",
                created_at=now - timedelta(days=46),
            ),
        ]
    )
    platform_days = [68_000, 93_000, 56_000, 115_000, 84_000, 126_000, 49_600]
    ratio = 621_800 / 842_650
    today = clock.date()
    for index, cents in enumerate(platform_days):
        day = today - timedelta(days=6 - index)
        jie_cost = int(round(cents * ratio))
        you_cost = cents - jie_cost
        db.add(UsageSnapshot(tenant_id=jie.id, member_id=li.id, day=day, requests=100, cost_cents=jie_cost))
        db.add(UsageSnapshot(tenant_id=you.id, member_id=zhou.id, day=day, requests=40, cost_cents=you_cost))
    db.add_all(
        [
            AuditLog(
                tenant_id=jie.id,
                actor_id=li.id,
                role="tenant_admin",
                action="quota.allocate",
                object="张剪辑",
                summary="分配 ¥500 给 张剪辑",
                after={"amount": 500},
                ip="192.168.1.22",
                created_at=now - timedelta(minutes=2),
            ),
            AuditLog(
                tenant_id=jie.id,
                actor_id=li.id,
                role="tenant_admin",
                action="member.create",
                object="李运营",
                summary="开通成员 李运营",
                ip="192.168.1.22",
                created_at=now - timedelta(hours=1),
            ),
            AuditLog(
                tenant_id=you.id,
                actor_id=platform.id,
                role="platform_admin",
                action="tenant.create",
                object="优品优选",
                summary="新建公司 优品优选",
                ip="192.168.1.9",
                created_at=now - timedelta(hours=5),
            ),
            AuditLog(
                tenant_id=jie.id,
                actor_id=li.id,
                role="tenant_admin",
                action="member.disable",
                object="赵兼职",
                summary="停用成员 赵兼职，席位已空出",
                before={"seats": 4},
                after={"seats": 3},
                ip="192.168.1.22",
                created_at=now - timedelta(days=1),
            ),
        ]
    )
    db.commit()

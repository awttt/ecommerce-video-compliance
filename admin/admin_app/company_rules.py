"""公司审核规则。

生效内容在 company_rules，一家公司一行。命中词和忽略词各有稳定 id。
员工提交的是相对这一行的单条改动（新增、修改、删除），通过一条才改一条。
管理员直接保存仍立刻写这张表，并尽量保住没改过的 id。
"""

import re
import secrets

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from admin_app.builtin_rules import DEFAULT_CATEGORIES, default_phrases
from admin_app.errors import DomainError
from admin_app.models import CompanyRule, CompanyRuleSubmission, Member, SharedRule, Tenant
from admin_app.permissions import has_perm
from admin_app.service import audit
from admin_app.timeutil import iso, utcnow

CATEGORIES = ("价格", "规格", "品牌", "夸大宣传")
SOURCES = ("口播", "画面")
EDITOR_GROUPS = (
    ("口播", "价格"),
    ("口播", "规格"),
    ("口播", "品牌"),
    ("画面", "价格"),
    ("画面", "规格"),
    ("画面", "品牌"),
)
OPS = ("add", "update", "delete")
TARGETS = ("phrase", "ignore", "category", "note")
STATUS_LABEL = {"pending": "待审", "approved": "已通过", "rejected": "已驳回"}
_NOTE_BLOCK = ("允许推测", "可以推测", "允许猜测", "可以猜测")
_MAX_CHANGES = 200
_STALE = "当前规则已经变了，请驳回后让员工重新提交"


def _require_submit(actor: Member) -> None:
    if not has_perm(actor.role, "shared_rule.submit"):
        raise DomainError(403, "没有权限")


def _require_review(actor: Member) -> None:
    if not has_perm(actor.role, "shared_rule.review"):
        raise DomainError(403, "没有权限")


def _company(actor: Member, tenant_id: int | None, *, write: bool) -> int:
    if actor.role == "platform_admin":
        if tenant_id is None:
            raise DomainError(400, "请选择公司")
        return tenant_id
    if actor.tenant_id is None:
        raise DomainError(403, "没有公司范围")
    if tenant_id is not None and tenant_id != actor.tenant_id:
        raise DomainError(403, "只能处理本公司的规则" if write else "只能查看本公司的规则")
    return actor.tenant_id


def _new_id(used: set[str]) -> str:
    while True:
        value = secrets.token_hex(6)
        if value not in used:
            used.add(value)
            return value


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def _lcs_pairs(old: list[str], new: list[str]) -> list[tuple[int, int]]:
    """按文本对齐。两边都有的行保住原来的 id，纯调换顺序不算改动。"""
    rows = len(old)
    cols = len(new)
    scores = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        previous = scores[i - 1]
        current = scores[i]
        token = old[i - 1]
        for j in range(1, cols + 1):
            if token == new[j - 1]:
                current[j] = previous[j - 1] + 1
            else:
                current[j] = previous[j] if previous[j] >= current[j - 1] else current[j - 1]
    pairs: list[tuple[int, int]] = []
    i, j = rows, cols
    while i > 0 and j > 0:
        if old[i - 1] == new[j - 1] and scores[i][j] == scores[i - 1][j - 1] + 1:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif scores[i - 1][j] >= scores[i][j - 1]:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def _leftovers(old_items: list[dict], new_texts: list[str]) -> tuple[list[dict], list[str], dict[int, dict]]:
    pairs = _lcs_pairs([str(item.get("text") or "") for item in old_items], new_texts)
    matched_old = {i for i, _ in pairs}
    matched_new = {j for _, j in pairs}
    kept = {j: dict(old_items[i]) for i, j in pairs}
    leftover_old = [old_items[i] for i in range(len(old_items)) if i not in matched_old]
    leftover_new = [new_texts[j] for j in range(len(new_texts)) if j not in matched_new]
    return leftover_old, leftover_new, kept


def align_texts(old_items: list[dict], new_texts: list[str], used: set[str]) -> list[dict]:
    leftover_old, leftover_new, kept = _leftovers(old_items, new_texts)
    assigned: dict[int, dict] = {}
    new_indexes = [index for index in range(len(new_texts)) if index not in kept]
    pair_count = min(len(leftover_old), len(leftover_new))
    for offset in range(pair_count):
        item = dict(leftover_old[offset])
        item["text"] = leftover_new[offset]
        assigned[new_indexes[offset]] = item
    for offset in range(pair_count, len(leftover_new)):
        assigned[new_indexes[offset]] = {"id": _new_id(used), "text": leftover_new[offset]}
    result: list[dict] = []
    for index, text in enumerate(new_texts):
        item = dict(kept[index] if index in kept else assigned[index])
        item["text"] = text
        if not item.get("id"):
            item["id"] = _new_id(used)
        result.append(item)
    return result


def clean_document(
    categories: list[str],
    phrases: list[dict],
    ignore_phrases: list[str],
    note: str,
) -> dict:
    chosen: list[str] = []
    for item in categories or []:
        if item not in CATEGORIES:
            raise DomainError(400, "类别只能是价格、规格、品牌、夸大宣传")
        if item not in chosen:
            chosen.append(item)
    cleaned_phrases: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in phrases or []:
        if not isinstance(raw, dict):
            raise DomainError(400, "命中词格式不正确")
        text = _compact(raw.get("text"))
        category = str(raw.get("category") or "")
        source = str(raw.get("source") or "口播")
        if not text or len(text) > 40:
            raise DomainError(400, "命中词需要在 1 到 40 个字之间")
        if category not in CATEGORIES:
            raise DomainError(400, "类别只能是价格、规格、品牌、夸大宣传")
        if source not in SOURCES:
            raise DomainError(400, "来源只能是口播或画面")
        if category == "夸大宣传" and source != "口播":
            raise DomainError(400, "夸大宣传只看口播")
        key = (source, category, text)
        if key in seen:
            continue
        seen.add(key)
        cleaned_phrases.append({"text": text, "category": category, "source": source})
    if len(cleaned_phrases) > 300:
        raise DomainError(400, "额外命中词最多 300 条")
    cleaned_ignore: list[str] = []
    for item in ignore_phrases or []:
        text = _compact(item)
        if not text:
            continue
        if len(text) > 40:
            raise DomainError(400, "忽略词需要在 1 到 40 个字之间")
        if text not in cleaned_ignore:
            cleaned_ignore.append(text)
    if len(cleaned_ignore) > 30:
        raise DomainError(400, "忽略词最多 30 条")
    body = _clean_note(note)
    if not chosen and not cleaned_phrases:
        raise DomainError(400, "至少启用一个类别，或添加一条额外命中词")
    return {
        "categories": chosen,
        "phrases": cleaned_phrases,
        "ignore_phrases": cleaned_ignore,
        "note": body,
    }


def _clean_note(note: str) -> str:
    body = (note or "").strip()
    if len(body) > 400:
        raise DomainError(400, "补充说明不能超过 400 字")
    if any(token in body for token in _NOTE_BLOCK):
        raise DomainError(400, "补充说明不能放宽禁止推测")
    return body


def _seed_phrases(db: Session) -> list[dict[str, str]]:
    phrases = default_phrases()
    seen = {(item["source"], item["category"], item["text"]) for item in phrases}
    approved = db.query(SharedRule).filter(SharedRule.status == "approved").all()
    for row in approved:
        sources = ("口播",) if row.category == "夸大宣传" else ("口播", "画面")
        for source in sources:
            if len(phrases) >= 300:
                return phrases
            key = (source, row.category, row.word)
            if key in seen or row.category not in CATEGORIES:
                continue
            phrases.append({"text": row.word, "category": row.category, "source": source})
            seen.add(key)
    return phrases


def _phrase_items(raw_items: list, used: set[str]) -> tuple[list[dict], bool]:
    cleaned: list[dict] = []
    changed = False
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            changed = True
            continue
        text = _compact(raw.get("text"))
        category = str(raw.get("category") or "")
        source = str(raw.get("source") or "口播")
        if not text or category not in CATEGORIES or source not in SOURCES:
            changed = True
            continue
        item_id = str(raw.get("id") or "")
        if not item_id or item_id in used:
            item_id = _new_id(used)
            changed = True
        else:
            used.add(item_id)
        item = {"id": item_id, "text": text, "category": category, "source": source}
        if item != raw:
            changed = True
        cleaned.append(item)
    if len(cleaned) != len(raw_items or []):
        changed = True
    return cleaned, changed


def _ignore_items(raw_items: list, used: set[str]) -> tuple[list[dict], bool]:
    cleaned: list[dict] = []
    changed = False
    seen: set[str] = set()
    for raw in raw_items or []:
        if isinstance(raw, str):
            text = _compact(raw)
            item_id = ""
            changed = True
        elif isinstance(raw, dict):
            text = _compact(raw.get("text"))
            item_id = str(raw.get("id") or "")
        else:
            changed = True
            continue
        if not text or text in seen:
            changed = True
            continue
        seen.add(text)
        if not item_id or item_id in used:
            item_id = _new_id(used)
            changed = True
        else:
            used.add(item_id)
        item = {"id": item_id, "text": text}
        if not isinstance(raw, dict) or item != raw:
            changed = True
        cleaned.append(item)
    return cleaned, changed


def _persist_ids(db: Session, row: CompanyRule) -> None:
    used: set[str] = set()
    phrases, phrases_changed = _phrase_items(list(row.phrases or []), used)
    ignores, ignores_changed = _ignore_items(list(row.ignore_phrases or []), used)
    if phrases_changed or ignores_changed:
        row.phrases = phrases
        row.ignore_phrases = ignores
        db.commit()


def ensure_company_rule(db: Session, tenant_id: int) -> CompanyRule:
    row = db.get(CompanyRule, tenant_id)
    if row is not None:
        _persist_ids(db, row)
        return row
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise DomainError(404, "公司不存在")
    used: set[str] = set()
    phrases, _ = _phrase_items(_seed_phrases(db), used)
    row = CompanyRule(
        tenant_id=tenant_id,
        categories=list(DEFAULT_CATEGORIES),
        phrases=phrases,
        ignore_phrases=[],
        note="",
        version=1,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(CompanyRule, tenant_id)
        if existing is None:
            raise
        _persist_ids(db, existing)
        return existing
    db.refresh(row)
    return row


def _merge_phrases(existing: list[dict], incoming: list[dict], used: set[str]) -> list[dict]:
    merged: list[dict] = []
    for source, category in EDITOR_GROUPS:
        old = [item for item in existing if item.get("source") == source and item.get("category") == category]
        texts = [item["text"] for item in incoming if item["source"] == source and item["category"] == category]
        for item in align_texts(old, texts, used):
            item["source"] = source
            item["category"] = category
            merged.append(item)
    for item in existing:
        if (item.get("source"), item.get("category")) not in EDITOR_GROUPS:
            merged.append(dict(item))
    return merged


def _apply_document(row: CompanyRule, document: dict, actor_id: int | None) -> None:
    used = {str(item.get("id") or "") for item in list(row.phrases or []) + list(row.ignore_phrases or [])}
    used.discard("")
    row.categories = document["categories"]
    row.phrases = _merge_phrases(list(row.phrases or []), document["phrases"], used)
    row.ignore_phrases = align_texts(list(row.ignore_phrases or []), document["ignore_phrases"], used)
    row.note = document["note"]
    row.version = int(row.version or 0) + 1
    row.updated_by = actor_id
    row.updated_at = utcnow()


def document_dict(row: CompanyRule) -> dict:
    return {
        "tenant_id": row.tenant_id,
        "categories": list(row.categories or []),
        "phrases": list(row.phrases or []),
        "ignore_phrases": list(row.ignore_phrases or []),
        "note": row.note or "",
        "version": row.version,
        "updated_at": iso(row.updated_at),
    }


def _op_label(op: str, target: str) -> str:
    if target == "category":
        return "启用" if op == "add" else "停用"
    return {"add": "新增", "update": "修改", "delete": "删除"}.get(op, op)


def _clip(text: str) -> str:
    return text if len(text) <= 200 else text[:197] + "…"


def _summary(op: str, target: str, before: dict | None, after: dict | None) -> str:
    if target == "phrase":
        side = after or before or {}
        place = f"{side.get('source', '')}{side.get('category', '')}"
        if op == "add":
            return _clip(f"新增{place}「{side.get('text', '')}」")
        if op == "delete":
            return _clip(f"删除{place}「{(before or {}).get('text', '')}」")
        previous = before or {}
        nxt = after or {}
        origin = f"{previous.get('source', '')}{previous.get('category', '')}「{previous.get('text', '')}」"
        if previous.get("source") == nxt.get("source") and previous.get("category") == nxt.get("category"):
            return _clip(f"修改{origin}为「{nxt.get('text', '')}」")
        return _clip(f"修改{origin}为{nxt.get('source', '')}{nxt.get('category', '')}「{nxt.get('text', '')}」")
    if target == "ignore":
        if op == "add":
            return _clip(f"新增忽略词「{(after or {}).get('text', '')}」")
        if op == "delete":
            return _clip(f"删除忽略词「{(before or {}).get('text', '')}」")
        return _clip(f"修改忽略词「{(before or {}).get('text', '')}」为「{(after or {}).get('text', '')}」")
    if target == "category":
        name = (after or before or {}).get("name", "")
        return _clip(f"启用类别「{name}」" if op == "add" else f"停用类别「{name}」")
    return "修改补充说明"


def submission_dict(db: Session, row: CompanyRuleSubmission) -> dict:
    author = db.get(Member, row.member_id)
    reviewer = db.get(Member, row.reviewer_id) if row.reviewer_id else None
    tenant = db.get(Tenant, row.tenant_id) if row.tenant_id else None
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "tenant_name": tenant.name if tenant else "",
        "member_id": row.member_id,
        "member_name": author.name if author else "",
        "op": row.op,
        "op_label": _op_label(row.op, row.target),
        "target": row.target,
        "item_id": row.item_id or "",
        "before": row.before,
        "after": row.after,
        "summary": row.summary or "",
        "status": row.status,
        "status_label": STATUS_LABEL.get(row.status, row.status),
        "reviewer_id": row.reviewer_id,
        "reviewer_name": reviewer.name if reviewer else "",
        "review_comment": row.review_comment or "",
        "created_at": iso(row.created_at),
        "reviewed_at": iso(row.reviewed_at),
    }


def read_company_rule(db: Session, actor: Member, tenant_id: int | None) -> CompanyRule:
    target = _company(actor, tenant_id, write=False)
    return ensure_company_rule(db, target)


def update_company_rule(
    db: Session,
    actor: Member,
    *,
    tenant_id: int | None,
    categories: list[str],
    phrases: list[dict],
    ignore_phrases: list[str],
    note: str,
    ip: str = "",
) -> CompanyRule:
    if actor.role not in ("platform_admin", "tenant_admin"):
        raise DomainError(403, "没有权限")
    target = _company(actor, tenant_id, write=True)
    document = clean_document(categories, phrases, ignore_phrases, note)
    row = ensure_company_rule(db, target)
    _apply_document(row, document, actor.id)
    audit(
        db,
        actor,
        "company_rule.update",
        str(target),
        "直接更新公司审核规则",
        tenant_id=target,
        after={"version": row.version, "phrases": len(row.phrases or [])},
        ip=ip,
    )
    db.commit()
    db.refresh(row)
    return row


def _side(raw: object, *fields: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise DomainError(400, "改动格式不正确")
    return {field: raw.get(field) for field in fields}  # type: ignore[misc]


def _phrase_side(raw: object) -> dict[str, str]:
    data = _side(raw, "text", "category", "source")
    text = _compact(data.get("text"))
    category = str(data.get("category") or "")
    source = str(data.get("source") or "")
    if not text or len(text) > 40:
        raise DomainError(400, "命中词需要在 1 到 40 个字之间")
    if category not in CATEGORIES:
        raise DomainError(400, "类别只能是价格、规格、品牌、夸大宣传")
    if source not in SOURCES:
        raise DomainError(400, "来源只能是口播或画面")
    if category == "夸大宣传" and source != "口播":
        raise DomainError(400, "夸大宣传只看口播")
    return {"text": text, "category": category, "source": source}


def _ignore_side(raw: object) -> dict[str, str]:
    data = _side(raw, "text")
    text = _compact(data.get("text"))
    if not text or len(text) > 40:
        raise DomainError(400, "忽略词需要在 1 到 40 个字之间")
    return {"text": text}


def _category_side(raw: object) -> dict[str, str]:
    data = _side(raw, "name")
    name = str(data.get("name") or "")
    if name not in CATEGORIES:
        raise DomainError(400, "类别只能是价格、规格、品牌、夸大宣传")
    return {"name": name}


def _note_side(raw: object) -> dict[str, str]:
    data = _side(raw, "text")
    return {"text": _clean_note(str(data.get("text") or ""))}


def _change_key(op: str, target: str, item_id: str, before: dict | None, after: dict | None) -> str:
    if target == "note":
        return "note"
    if target == "category":
        name = (after or before or {}).get("name", "")
        return f"category:{name}"
    if op == "add":
        if target == "phrase":
            side = after or {}
            return f"phrase:add:{side.get('source')}:{side.get('category')}:{side.get('text')}"
        return f"ignore:add:{(after or {}).get('text')}"
    return f"{target}:{item_id}"


def _find(items: list[dict], item_id: str) -> int | None:
    for index, item in enumerate(items):
        if item.get("id") == item_id:
            return index
    return None


def _phrase_same(item: dict, before: dict) -> bool:
    return item.get("text") == before["text"] and item.get("category") == before["category"] and item.get("source") == before["source"]


def _prepare_change(raw: dict, row: CompanyRule) -> dict:
    if not isinstance(raw, dict):
        raise DomainError(400, "改动格式不正确")
    op = str(raw.get("op") or "")
    target = str(raw.get("target") or "")
    item_id = str(raw.get("item_id") or "")
    if op not in OPS or target not in TARGETS:
        raise DomainError(400, "改动格式不正确")
    phrases = list(row.phrases or [])
    ignores = list(row.ignore_phrases or [])
    categories = list(row.categories or [])
    before: dict | None = None
    after: dict | None = None
    if target == "phrase":
        if op == "add":
            after = _phrase_side(raw.get("after"))
            if any(item.get("source") == after["source"] and item.get("category") == after["category"] and item.get("text") == after["text"] for item in phrases):
                raise DomainError(400, f"已经有命中词「{after['text']}」")
            if len(phrases) >= 300:
                raise DomainError(400, "额外命中词最多 300 条")
            item_id = ""
        elif op == "delete":
            before = _phrase_side(raw.get("before"))
            index = _find(phrases, item_id)
            if index is None or not _phrase_same(phrases[index], before):
                raise DomainError(400, f"「{before['text']}」和当前规则不一致，请先刷新")
        else:
            before = _phrase_side(raw.get("before"))
            after = _phrase_side(raw.get("after"))
            index = _find(phrases, item_id)
            if index is None or not _phrase_same(phrases[index], before):
                raise DomainError(400, f"「{before['text']}」和当前规则不一致，请先刷新")
            if before == after:
                raise DomainError(400, "没有改动")
            if any(
                other.get("id") != item_id
                and other.get("source") == after["source"]
                and other.get("category") == after["category"]
                and other.get("text") == after["text"]
                for other in phrases
            ):
                raise DomainError(400, f"已经有命中词「{after['text']}」")
    elif target == "ignore":
        if op == "add":
            after = _ignore_side(raw.get("after"))
            if any(item.get("text") == after["text"] for item in ignores):
                raise DomainError(400, f"已经有忽略词「{after['text']}」")
            if len(ignores) >= 30:
                raise DomainError(400, "忽略词最多 30 条")
            item_id = ""
        elif op == "delete":
            before = _ignore_side(raw.get("before"))
            index = _find(ignores, item_id)
            if index is None or ignores[index].get("text") != before["text"]:
                raise DomainError(400, f"「{before['text']}」和当前规则不一致，请先刷新")
        else:
            before = _ignore_side(raw.get("before"))
            after = _ignore_side(raw.get("after"))
            index = _find(ignores, item_id)
            if index is None or ignores[index].get("text") != before["text"]:
                raise DomainError(400, f"「{before['text']}」和当前规则不一致，请先刷新")
            if before == after:
                raise DomainError(400, "没有改动")
            if any(item.get("id") != item_id and item.get("text") == after["text"] for item in ignores):
                raise DomainError(400, f"已经有忽略词「{after['text']}」")
    elif target == "category":
        if op == "update":
            raise DomainError(400, "类别只能启用或停用")
        if op == "add":
            after = _category_side(raw.get("after"))
            if after["name"] in categories:
                raise DomainError(400, f"类别「{after['name']}」已经开着")
            item_id = ""
        else:
            before = _category_side(raw.get("before"))
            if before["name"] not in categories:
                raise DomainError(400, f"类别「{before['name']}」已经关了")
            item_id = ""
    else:
        if op != "update":
            raise DomainError(400, "补充说明只能修改")
        before = _note_side(raw.get("before"))
        after = _note_side(raw.get("after"))
        if (row.note or "") != before["text"]:
            raise DomainError(400, "补充说明和当前规则不一致，请先刷新")
        if before == after:
            raise DomainError(400, "没有改动")
        item_id = ""
    if op in ("update", "delete") and target in ("phrase", "ignore") and not item_id:
        raise DomainError(400, "改动格式不正确")
    return {
        "op": op,
        "target": target,
        "item_id": item_id,
        "before": before,
        "after": after,
        "summary": _summary(op, target, before, after),
        "key": _change_key(op, target, item_id, before, after),
    }


def _assert_still_valid(row: CompanyRule) -> None:
    phrases = list(row.phrases or [])
    ignores = list(row.ignore_phrases or [])
    categories = list(row.categories or [])
    if len(phrases) > 300:
        raise DomainError(400, "额外命中词最多 300 条")
    if len(ignores) > 30:
        raise DomainError(400, "忽略词最多 30 条")
    if not categories and not phrases:
        raise DomainError(400, "至少启用一个类别，或保留一条命中词")
    seen: set[tuple[str, str, str]] = set()
    for item in phrases:
        key = (str(item.get("source")), str(item.get("category")), str(item.get("text")))
        if key in seen:
            raise DomainError(400, f"已经有命中词「{item.get('text')}」")
        seen.add(key)


def _apply_change(row: CompanyRule, change: dict) -> None:
    phrases = [dict(item) for item in list(row.phrases or [])]
    ignores = [dict(item) for item in list(row.ignore_phrases or [])]
    categories = list(row.categories or [])
    op = change["op"]
    target = change["target"]
    before = change["before"]
    after = change["after"]
    item_id = change["item_id"]
    if target == "phrase":
        if op == "add":
            if any(item.get("text") == after["text"] and item.get("category") == after["category"] and item.get("source") == after["source"] for item in phrases):
                raise DomainError(400, _STALE)
            used = {str(item.get("id") or "") for item in phrases + ignores}
            phrases.append({"id": _new_id(used), **after})
        else:
            index = _find(phrases, item_id)
            if index is None or not _phrase_same(phrases[index], before):
                raise DomainError(400, _STALE)
            if op == "delete":
                phrases.pop(index)
            else:
                if any(
                    other.get("id") != item_id
                    and other.get("text") == after["text"]
                    and other.get("category") == after["category"]
                    and other.get("source") == after["source"]
                    for other in phrases
                ):
                    raise DomainError(400, _STALE)
                phrases[index] = {**phrases[index], **after}
    elif target == "ignore":
        if op == "add":
            if any(item.get("text") == after["text"] for item in ignores):
                raise DomainError(400, _STALE)
            used = {str(item.get("id") or "") for item in phrases + ignores}
            ignores.append({"id": _new_id(used), "text": after["text"]})
        else:
            index = _find(ignores, item_id)
            if index is None or ignores[index].get("text") != (before or {}).get("text"):
                raise DomainError(400, _STALE)
            if op == "delete":
                ignores.pop(index)
            else:
                if any(item.get("id") != item_id and item.get("text") == after["text"] for item in ignores):
                    raise DomainError(400, _STALE)
                ignores[index] = {**ignores[index], "text": after["text"]}
    elif target == "category":
        name = (after or before or {})["name"]
        if op == "add":
            if name in categories:
                raise DomainError(400, _STALE)
            categories.append(name)
        else:
            if name not in categories:
                raise DomainError(400, _STALE)
            categories = [item for item in categories if item != name]
    else:
        if (row.note or "") != (before or {}).get("text"):
            raise DomainError(400, _STALE)
        row.note = (after or {}).get("text") or ""
    row.phrases = phrases
    row.ignore_phrases = ignores
    row.categories = categories
    _assert_still_valid(row)


def submit_company_rule(
    db: Session,
    actor: Member,
    changes: list[dict],
    *,
    ip: str = "",
) -> list[CompanyRuleSubmission]:
    _require_submit(actor)
    if actor.tenant_id is None:
        raise DomainError(403, "没有公司范围")
    if not changes:
        raise DomainError(400, "没有改动")
    if len(changes) > _MAX_CHANGES:
        raise DomainError(400, "一次最多提交 200 条改动")
    current = ensure_company_rule(db, actor.tenant_id)
    prepared = [_prepare_change(item, current) for item in changes]
    pending = (
        db.query(CompanyRuleSubmission)
        .filter(
            CompanyRuleSubmission.tenant_id == actor.tenant_id,
            CompanyRuleSubmission.status == "pending",
        )
        .all()
    )
    pending_keys = {_change_key(row.op, row.target, row.item_id or "", row.before, row.after) for row in pending}
    seen: set[str] = set()
    rows: list[CompanyRuleSubmission] = []
    for item in prepared:
        if item["key"] in pending_keys:
            raise DomainError(400, f"「{item['summary']}」已有待审，请等审批完再提交")
        if item["key"] in seen:
            raise DomainError(400, f"「{item['summary']}」在这次提交里重复了")
        seen.add(item["key"])
        rows.append(
            CompanyRuleSubmission(
                tenant_id=actor.tenant_id,
                member_id=actor.id,
                op=item["op"],
                target=item["target"],
                item_id=item["item_id"],
                before=item["before"],
                after=item["after"],
                summary=item["summary"],
                base_version=int(current.version or 0),
                status="pending",
            )
        )
    db.add_all(rows)
    audit(
        db,
        actor,
        "company_rule.submit",
        str(actor.tenant_id),
        f"提交 {len(rows)} 条规则改动",
        tenant_id=actor.tenant_id,
        after={"count": len(rows)},
        ip=ip,
    )
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def list_own_submissions(db: Session, actor: Member) -> list[CompanyRuleSubmission]:
    return (
        db.query(CompanyRuleSubmission)
        .filter(CompanyRuleSubmission.member_id == actor.id)
        .order_by(CompanyRuleSubmission.id.desc())
        .limit(100)
        .all()
    )


def list_pending_submissions(db: Session, actor: Member) -> list[CompanyRuleSubmission]:
    _require_review(actor)
    if actor.tenant_id is None:
        raise DomainError(403, "没有权限")
    return (
        db.query(CompanyRuleSubmission)
        .filter(
            CompanyRuleSubmission.status == "pending",
            CompanyRuleSubmission.tenant_id == actor.tenant_id,
        )
        .order_by(CompanyRuleSubmission.id)
        .limit(100)
        .all()
    )


def pending_company_rules(db: Session, actor: Member) -> int:
    if actor.role != "tenant_admin" or actor.tenant_id is None:
        return 0
    return (
        db.query(CompanyRuleSubmission)
        .filter(
            CompanyRuleSubmission.status == "pending",
            CompanyRuleSubmission.tenant_id == actor.tenant_id,
        )
        .count()
    )


def _pending(db: Session, actor: Member, submission_id: int) -> CompanyRuleSubmission:
    _require_review(actor)
    row = db.get(CompanyRuleSubmission, submission_id)
    if row is None:
        raise DomainError(404, "提交不存在")
    if row.status != "pending":
        raise DomainError(400, "这条已经审过了")
    if actor.tenant_id is None or row.tenant_id != actor.tenant_id:
        raise DomainError(403, "只能审批本公司提交的规则")
    if row.member_id == actor.id:
        raise DomainError(403, "不能审批自己提交的规则")
    return row


def approve_company_rule(
    db: Session,
    actor: Member,
    submission_id: int,
    *,
    comment: str = "",
    ip: str = "",
) -> CompanyRuleSubmission:
    row = _pending(db, actor, submission_id)
    note = (comment or "").strip()
    if len(note) > 200:
        raise DomainError(400, "说明不能超过 200 字")
    current = ensure_company_rule(db, row.tenant_id)
    _apply_change(
        current,
        {"op": row.op, "target": row.target, "item_id": row.item_id or "", "before": row.before, "after": row.after},
    )
    current.version = int(current.version or 0) + 1
    current.updated_by = actor.id
    current.updated_at = utcnow()
    row.status = "approved"
    row.reviewer_id = actor.id
    row.review_comment = note
    row.reviewed_at = utcnow()
    audit(
        db,
        actor,
        "company_rule.approve",
        str(row.id),
        row.summary or "通过一条规则改动",
        tenant_id=row.tenant_id,
        before={"status": "pending"},
        after={"status": "approved", "version": current.version},
        ip=ip,
    )
    db.commit()
    db.refresh(row)
    return row


def reject_company_rule(
    db: Session,
    actor: Member,
    submission_id: int,
    *,
    comment: str,
    ip: str = "",
) -> CompanyRuleSubmission:
    row = _pending(db, actor, submission_id)
    note = (comment or "").strip()
    if not note:
        raise DomainError(400, "驳回需要填写原因")
    if len(note) > 200:
        raise DomainError(400, "说明不能超过 200 字")
    row.status = "rejected"
    row.reviewer_id = actor.id
    row.review_comment = note
    row.reviewed_at = utcnow()
    audit(
        db,
        actor,
        "company_rule.reject",
        str(row.id),
        row.summary or "驳回一条规则改动",
        tenant_id=row.tenant_id,
        before={"status": "pending"},
        after={"status": "rejected"},
        ip=ip,
    )
    db.commit()
    db.refresh(row)
    return row

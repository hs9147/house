"""스마트워크 세션 — **세션 하나 = 업무 하나.** 맥락(조직·워크플로)·참여자·대화를 묶는다.

권한은 둘로 나뉜다:
- 소유자: 맥락(조직·워크플로·제목)을 바꾸고, 참여자를 넣고 빼고, 세션을 지운다.
- 참여자(공유받은 사람, 다른 부서여도 된다): 대화를 읽고 말한다.
개인 맥락(폴더·메일)은 **각자 자기 것만** 이 세션에 고른다 — 소유자도 남의 선택은 못
바꾸고, 말할 때 붙는 개인 도구는 말한 사람 자신의 저장소뿐이다(smartwork.personal_toolset).
"""
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (
    AnswerRating, ApiKey, Organization, PersonalContext, SmartworkSession,
    SmartworkSessionMember, SmartworkSessionMessage, UserAccount, Workflow, utcnow,
)
from ..security import viewer_org_ids
from . import smartwork

MAX_TITLE = 200


class SessionError(Exception):
    """status는 HTTP 상태 — 없음(404)과 권한 없음(403)을 api가 그대로 옮긴다."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _member(db: Session, session_id: int, email: str) -> SmartworkSessionMember | None:
    return db.execute(select(SmartworkSessionMember).where(
        SmartworkSessionMember.session_id == session_id,
        SmartworkSessionMember.email == email)).scalar_one_or_none()


def get(db: Session, session_id: int, email: str) -> tuple[SmartworkSession, SmartworkSessionMember]:
    """참여자만 연다. 참여자가 아니면 있는지도 알리지 않는다(404)."""
    row = db.get(SmartworkSession, session_id)
    member = _member(db, session_id, email) if row else None
    if row is None or member is None:
        raise SessionError(404, "세션이 없습니다.")
    return row, member


def require_owner(db: Session, session_id: int, email: str) -> SmartworkSession:
    row, _ = get(db, session_id, email)
    if row.owner != email:
        raise SessionError(403, "세션 소유자만 할 수 있습니다.")
    return row


def org_choices(db: Session, key: ApiKey) -> list[dict]:
    """세션에 고를 수 있는 조직 = 내 소속 조직, 워크플로 = 그 조직의 것."""
    ids = viewer_org_ids(db, key)
    if not ids:
        return []
    orgs = db.execute(select(Organization).where(Organization.id.in_(ids))
                      .order_by(Organization.name)).scalars().all()
    out = []
    for org in orgs:
        flows = db.execute(select(Workflow).where(Workflow.organization_id == org.id)
                           .order_by(Workflow.name)).scalars().all()
        out.append({"id": org.id, "name": org.name,
                    "workflows": [{"id": w.id, "name": w.name, "description": w.description}
                                  for w in flows]})
    return out


def _check_context(db: Session, key: ApiKey, org_id: int | None, workflow_id: int | None) -> None:
    if org_id is not None and org_id not in viewer_org_ids(db, key):
        raise SessionError(403, "소속 조직만 고를 수 있습니다.")
    if workflow_id is not None:
        flow = db.get(Workflow, workflow_id)
        if flow is None or org_id is None or flow.organization_id != org_id:
            raise SessionError(422, "고른 조직의 워크플로가 아닙니다.")


def create(db: Session, key: ApiKey, title: str = "", org_id: int | None = None,
           workflow_id: int | None = None) -> SmartworkSession:
    _check_context(db, key, org_id, workflow_id)
    row = SmartworkSession(owner=key.name, title=title.strip()[:MAX_TITLE],
                           organization_id=org_id, workflow_id=workflow_id)
    db.add(row)
    db.flush()
    db.add(SmartworkSessionMember(session_id=row.id, email=key.name, folders=[], mail=False))
    db.commit()
    return row


def update(db: Session, key: ApiKey, session_id: int, changes: dict) -> SmartworkSession:
    """changes에 온 키만 바꾼다(None은 '비움'). 조직을 바꾸면 그 조직 것이 아닌 워크플로는 뺀다."""
    row = require_owner(db, session_id, key.name)
    org_id = changes["organization_id"] if "organization_id" in changes else row.organization_id
    workflow_id = changes["workflow_id"] if "workflow_id" in changes else row.workflow_id
    if "organization_id" in changes and "workflow_id" not in changes and workflow_id is not None:
        flow = db.get(Workflow, workflow_id)
        if flow is None or flow.organization_id != org_id:
            workflow_id = None
    _check_context(db, key, org_id, workflow_id)
    row.organization_id, row.workflow_id = org_id, workflow_id
    if "title" in changes:
        row.title = str(changes["title"] or "").strip()[:MAX_TITLE]
    row.updated_at = utcnow()
    db.commit()
    return row


def remove(db: Session, email: str, session_id: int) -> None:
    row = require_owner(db, session_id, email)
    # SQLite는 외래키 CASCADE를 기본으로 끄고 돈다 — 딸린 행을 직접 지운다.
    db.execute(delete(SmartworkSessionMessage).where(SmartworkSessionMessage.session_id == row.id))
    db.execute(delete(SmartworkSessionMember).where(SmartworkSessionMember.session_id == row.id))
    db.delete(row)
    db.commit()


def list_for(db: Session, email: str) -> list[dict]:
    rows = db.execute(
        select(SmartworkSession).join(SmartworkSessionMember,
                                      SmartworkSessionMember.session_id == SmartworkSession.id)
        .where(SmartworkSessionMember.email == email)
        .order_by(SmartworkSession.updated_at.desc(), SmartworkSession.id.desc())
    ).scalars().all()
    counts = dict(db.execute(
        select(SmartworkSessionMember.session_id, func.count())
        .where(SmartworkSessionMember.session_id.in_([r.id for r in rows]))
        .group_by(SmartworkSessionMember.session_id)).all())
    return [{**_summary(r), "members": counts.get(r.id, 1)} for r in rows]


def _summary(row: SmartworkSession) -> dict:
    return {
        "id": row.id, "title": row.title, "owner": row.owner,
        "organization_id": row.organization_id,
        "organization": row.organization.name if row.organization else None,
        "workflow_id": row.workflow_id,
        "workflow": row.workflow.name if row.workflow else None,
        "updated_at": row.updated_at.isoformat(),
    }


def _message_out(m: SmartworkSessionMessage, my_rating: int = 0) -> dict:
    return {"id": m.id, "role": m.role, "author": m.author, "content": m.content,
            **(m.view or {}), "my_rating": my_rating,
            "created_at": m.created_at.isoformat()}


def detail(db: Session, row: SmartworkSession, member: SmartworkSessionMember,
           after: int = 0) -> dict:
    """after를 주면 그 뒤의 메시지만 — 공유 세션을 주기적으로 다시 읽는 화면용."""
    members = db.execute(select(SmartworkSessionMember)
                         .where(SmartworkSessionMember.session_id == row.id)
                         .order_by(SmartworkSessionMember.id)).scalars().all()
    messages = db.execute(select(SmartworkSessionMessage).where(
        SmartworkSessionMessage.session_id == row.id, SmartworkSessionMessage.id > after)
        .order_by(SmartworkSessionMessage.id)).scalars().all()
    # 내가 매긴 평가 — 누른 상태가 보여야 같은 답을 또 평가하지 않는다. 남이 매긴 것은
    # 내보내지 않는다(평가는 집계로만 읽는 것이고, 누가 아쉽다고 했는지는 대화의 일이 아니다).
    mine = {r.message_id: r.score for r in db.execute(
        select(AnswerRating).where(AnswerRating.rater == member.email,
                                   AnswerRating.message_id.in_([m.id for m in messages]))
    ).scalars()}
    return {
        **_summary(row),
        "is_owner": row.owner == member.email,
        # 남의 개인 맥락 선택은 내보내지 않는다 — 어떤 폴더를 골랐는지도 그 사람의 것이다.
        "members": [{"email": m.email, "is_owner": m.email == row.owner} for m in members],
        "my_context": {"folders": member.folders or [], "mail": bool(member.mail)},
        "messages": [_message_out(m, mine.get(m.id, 0)) for m in messages],
    }


def add_member(db: Session, owner: str, session_id: int, email: str) -> None:
    require_owner(db, session_id, owner)
    email = email.strip()
    user = db.execute(select(UserAccount).where(UserAccount.email == email)).scalar_one_or_none()
    if user is None or not user.is_approved:
        raise SessionError(404, f"승인된 계정이 없습니다: {email}")
    if _member(db, session_id, email) is None:
        db.add(SmartworkSessionMember(session_id=session_id, email=email, folders=[], mail=False))
        db.commit()


def remove_member(db: Session, email: str, session_id: int, target: str) -> None:
    """소유자는 남을 빼고, 참여자는 스스로 나간다. 소유자는 나갈 수 없다(세션을 지운다)."""
    row, _ = get(db, session_id, email)
    if target == row.owner:
        raise SessionError(422, "소유자는 뺄 수 없습니다 — 세션을 지우세요.")
    if email != row.owner and email != target:
        raise SessionError(403, "세션 소유자만 다른 참여자를 뺄 수 있습니다.")
    found = _member(db, session_id, target)
    if found is not None:
        db.delete(found)
        db.commit()


def set_context(db: Session, email: str, session_id: int, folders: list[str], mail: bool) -> dict:
    """내 개인 맥락 중 이 세션에 쓸 것. 내 저장소에 있는 폴더만 받는다."""
    _, member = get(db, session_id, email)
    ctx = db.execute(select(PersonalContext).where(PersonalContext.email == email)).scalar_one_or_none()
    mine = {f.get("name") for f in (ctx.folders or [])} if ctx else set()
    member.folders = sorted({f for f in folders if f in mine})
    member.mail = bool(mail) and ctx is not None and ctx.mail_synced_at is not None
    db.commit()
    return {"folders": member.folders, "mail": member.mail}


def send(db: Session, key: ApiKey, provider, session_id: int, content: str,
         toolsets: list, attachments: list[dict] | None = None) -> dict:
    """한 턴 — 묻고 답한 두 줄을 **답이 나온 뒤에 함께** 남긴다. 모델이 실패하면 아무것도
    남지 않아 같은 말을 다시 보내면 된다(질문만 남은 대화가 다른 참여자에게 보이지 않는다).

    content가 비면 여는 턴(업무 제안) — 대화가 아직 없을 때만 받는다. attachments는 이번
    요청에 붙인 참고 자료다(services/smartwork.read_attachments).
    """
    row, member = get(db, session_id, key.name)
    content = content.strip()[:smartwork.MAX_MESSAGE_CHARS]
    history = [{"role": m.role, "content": m.content, "author": m.author,
                "attachments": (m.view or {}).get("attachments") if m.role == "user" else None}
               for m in db.execute(select(SmartworkSessionMessage)
                                   .where(SmartworkSessionMessage.session_id == row.id)
                                   .order_by(SmartworkSessionMessage.id)).scalars()]
    if not content and (history or attachments):
        raise SessionError(422, "보낼 내용이 없습니다.")
    try:
        kept, images = smartwork.read_attachments(attachments or [])
    except smartwork.AttachmentError as e:
        raise SessionError(422, str(e))
    if content:
        history.append({"role": "user", "content": content, "author": key.name,
                        "attachments": kept})
    participants = [m.email for m in db.execute(
        select(SmartworkSessionMember).where(SmartworkSessionMember.session_id == row.id)
        .order_by(SmartworkSessionMember.id)).scalars()]
    result = smartwork.chat(
        db, key, provider, history, toolsets, org_id=row.organization_id,
        workflow=row.workflow.name if row.workflow else "", participants=participants,
        folders=member.folders or [], mail=bool(member.mail), images=images)
    if content:
        db.add(SmartworkSessionMessage(session_id=row.id, role="user", author=key.name,
                                       content=content,
                                       view={"attachments": kept} if kept else None))
        if not row.title:
            # 제목을 따로 묻지 않는다 — 첫 요청이 곧 이 업무의 이름이다.
            row.title = content.splitlines()[0][:80]
    answer = SmartworkSessionMessage(
        session_id=row.id, role="assistant", author=key.name, content=result["reply"],
        # **어느 모델이 답했는지 함께 남긴다.** 대화마다 모델을 고를 수 있어서, 나중에 보면
        # 세션 설정으로는 알 수 없다 — 평가(answer_ratings)가 이 값으로 묶인다.
        view={**{k: result[k] for k in ("agent", "report", "suggestions", "choices", "tools")},
              "model": provider.model, "provider": provider.name})
    db.add(answer)
    row.updated_at = utcnow()
    db.commit()
    return {**result, "message": _message_out(answer)}


def rate(db: Session, email: str, message_id: int, score: int, reason: str) -> dict:
    """답변 하나에 좋음(+1)·아쉬움(-1)을 매긴다 — 그 세션 참여자만, 한 사람에 하나.

    남의 세션 답변은 평가할 수 없다(get이 참여 여부를 본다). 다시 누르면 덮어쓴다 —
    여러 번 누른 사람이 집계를 끌면 모델 비교가 무의미해진다.
    """
    if score not in (1, -1):
        raise SessionError(422, "평가는 좋음(1) 또는 아쉬움(-1)입니다.")
    msg = db.get(SmartworkSessionMessage, message_id)
    if msg is None or msg.role != "assistant":
        raise SessionError(404, "평가할 답변을 찾을 수 없습니다.")
    get(db, msg.session_id, email)
    found = db.execute(select(AnswerRating).where(
        AnswerRating.message_id == message_id,
        AnswerRating.rater == email)).scalar_one_or_none()
    if found is None:
        found = AnswerRating(message_id=message_id, rater=email)
        db.add(found)
    view = msg.view or {}
    found.score = score
    found.reason = (reason or "").strip()[:500]
    # 답이 만들어진 시점의 모델을 박아 둔다 — 세션 모델은 나중에 바뀐다.
    found.model = str(view.get("model") or "")[:128]
    found.provider = str(view.get("provider") or "")[:64]
    db.commit()
    return {"message_id": message_id, "score": found.score, "reason": found.reason}

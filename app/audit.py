from sqlalchemy.orm import Session

from .models import AuditEvent


def record(db: Session, actor: str, action: str, target: str, detail: dict | None = None) -> None:
    db.add(AuditEvent(actor=actor, action=action, target=target, detail=detail))
    db.commit()

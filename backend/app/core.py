"""Shared building blocks: auth dependencies, permissions, claim rules and response builders."""
import json
from datetime import date
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from . import models, schemas
from .database import get_db
from .security import read_token

STAFF_ROLES = ("admin", "agent", "claims_officer", "viewer")
PERMS = {
    "admin": {"policy:write", "customer:write", "claim:create", "claim:decide", "pii:read"},
    "agent": {"policy:write", "customer:write", "claim:create", "pii:read"},
    "claims_officer": {"claim:create", "claim:decide", "pii:read"},
    "viewer": set(),
}

# Claim workflow. Transitions are explicit; no decision is ever made automatically.
CLAIM_FLOW = {
    "submitted": ["under_review"],
    "under_review": ["info_requested", "approved", "rejected"],
    "info_requested": ["under_review", "rejected"],
    "approved": ["settled"],
    "rejected": [],
    "settled": [],
}
REASON_REQUIRED = {"rejected", "info_requested"}

# Demo document checklist per claim type (illustrative rules, not an insurer's actual requirements).
DOC_TYPES = {
    "registration_certificate": "Registration certificate (RC)",
    "driving_licence": "Driving licence",
    "damage_photos": "Photos of the damage",
    "repair_estimate": "Repair estimate",
    "fir_copy": "FIR copy",
}
REQUIRED_DOCS = {
    "Accident damage": ["registration_certificate", "driving_licence", "damage_photos", "repair_estimate"],
    "Theft": ["registration_certificate", "driving_licence", "fir_copy"],
    "Fire": ["registration_certificate", "damage_photos", "repair_estimate"],
    "Natural calamity": ["registration_certificate", "damage_photos", "repair_estimate"],
    "Third-party liability": ["registration_certificate", "driving_licence", "fir_copy"],
}


# ---------- identity
class Actor:
    def __init__(self, role: str, user: models.User | None = None):
        self.role = role
        self.user = user

    def can(self, perm: str) -> bool:
        return perm in PERMS.get(self.role, set())

    def require(self, perm: str) -> None:
        if not self.can(perm):
            raise HTTPException(403, f"Role '{self.role}' is not permitted to perform this action")


DB = Annotated[Session, Depends(get_db)]


def get_user(db: DB, authorization: Annotated[str | None, Header()] = None) -> models.User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in to continue")
    uid = read_token(authorization[7:].strip())
    user = db.get(models.User, uid) if uid else None
    if not user:
        raise HTTPException(401, "Your session has expired. Please sign in again")
    return user


def get_actor(user: Annotated[models.User, Depends(get_user)]) -> Actor:
    if user.role not in STAFF_ROLES:
        raise HTTPException(403, "Staff access required")
    return Actor(user.role, user)


def get_customer(user: Annotated[models.User, Depends(get_user)]) -> models.User:
    if user.role != "customer" or not user.customer_id:
        raise HTTPException(403, "Customer account required")
    return user


ActorDep = Annotated[Actor, Depends(get_actor)]
CustomerDep = Annotated[models.User, Depends(get_customer)]


# ---------- helpers
def audit(db: Session, entity: str, entity_id: int, action: str, detail: str, actor_role: str,
          from_status: str | None = None, to_status: str | None = None) -> None:
    db.add(models.AuditEvent(entity=entity, entity_id=entity_id, action=action, detail=detail,
                             actor_role=actor_role, from_status=from_status, to_status=to_status))


def mask_email(e: str) -> str:
    local, _, dom = e.partition("@")
    return f"{local[:1]}***@{dom}"


def mask_phone(p: str) -> str:
    return "*" * max(0, len(p) - 3) + p[-3:]


def policy_q():
    return select(models.Policy).options(
        joinedload(models.Policy.customer), joinedload(models.Policy.claims), joinedload(models.Policy.vehicle))


def claim_q():
    return select(models.Claim).options(
        joinedload(models.Claim.policy).joinedload(models.Policy.customer), selectinload(models.Claim.files))


def get_policy(db: Session, pid: int) -> models.Policy:
    p = db.execute(policy_q().where(models.Policy.id == pid)).unique().scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Policy not found")
    return p


def get_claim(db: Session, cid: int) -> models.Claim:
    c = db.execute(claim_q().where(models.Claim.id == cid)).unique().scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Claim not found")
    return c


def policy_out(p: models.Policy) -> schemas.PolicyOut:
    vehicle = f"{p.vehicle.make_model} · {p.vehicle.reg_number}" if p.vehicle else None
    return schemas.PolicyOut(
        id=p.id, number=p.number, customer_id=p.customer_id, customer_name=p.customer.name, product=p.product,
        insurer=p.insurer, vehicle=vehicle, premium=p.premium, sum_insured=p.sum_insured,
        start_date=p.start_date, end_date=p.end_date, status=p.status, risk_score=p.risk_score,
        days_to_expiry=(p.end_date - date.today()).days, claim_count=len(p.claims),
        breakdown=json.loads(p.breakdown) if p.breakdown else None)


def claim_out(c: models.Claim) -> schemas.ClaimOut:
    files = [schemas.DocFile(id=f.id, doc_type=f.doc_type, name=f.original_name, size=f.size,
                             uploaded_at=f.uploaded_at) for f in c.files]
    legacy = [d for d in c.documents.split(",") if d]
    return schemas.ClaimOut(
        id=c.id, number=c.number, policy_id=c.policy_id, policy_number=c.policy.number,
        customer_name=c.policy.customer.name, product=c.policy.product, claim_type=c.claim_type,
        amount=c.amount, approved_amount=c.approved_amount, incident_date=c.incident_date,
        description=c.description, documents=legacy + [f.name for f in files], files=files,
        status=c.status, decision_reason=c.decision_reason, created_at=c.created_at,
        updated_at=c.updated_at, allowed_transitions=CLAIM_FLOW[c.status])


def next_number(db: Session, prefix: str, model, col) -> str:
    n = (db.scalar(select(func.max(model.id))) or 0) + 1
    while db.scalar(select(model.id).where(col == f"{prefix}-{n:04d}")):
        n += 1
    return f"{prefix}-{n:04d}"


def doc_check(c: models.Claim) -> dict:
    required = REQUIRED_DOCS.get(c.claim_type, [])
    provided = {f.doc_type for f in c.files}
    items = [{"type": t, "label": DOC_TYPES[t], "provided": t in provided} for t in required]
    done = sum(1 for i in items if i["provided"])
    return {"items": items, "provided": done, "required": len(required),
            "percent": round(done / len(required) * 100) if required else 100}


def register_claim(db: Session, policy: models.Policy, claim_type: str, amount: float, incident_date: date,
                   description: str, document_names: list[str], actor_role: str) -> models.Claim:
    """Shared claim-registration rules for staff and customer entry points."""
    if policy.status != "active":
        raise HTTPException(422, f"Claims can only be registered against active policies (policy is {policy.status})")
    if not (policy.start_date <= incident_date <= policy.end_date):
        raise HTTPException(422, "Incident date falls outside the policy period")
    if incident_date > date.today():
        raise HTTPException(422, "Incident date cannot be in the future")
    if amount > policy.sum_insured:
        raise HTTPException(422, "Claimed amount exceeds the sum insured")
    num = next_number(db, f"CLM-{date.today().year}", models.Claim, models.Claim.number)
    c = models.Claim(number=num, policy_id=policy.id, claim_type=claim_type.strip(), amount=amount,
                     incident_date=incident_date, description=description.strip(),
                     documents=",".join(document_names), status="submitted")
    db.add(c)
    db.flush()
    audit(db, "claim", c.id, "created", f"Claim {num} registered against {policy.number}", actor_role,
          to_status="submitted")
    return c


# ---------- Ensure Card ledger
def card_balance(db: Session, customer_id: int) -> float:
    return float(db.scalar(select(func.coalesce(func.sum(models.CardTransaction.amount), 0.0)).where(
        models.CardTransaction.customer_id == customer_id)) or 0.0)


def card_last4(customer_id: int) -> str:
    return f"{(customer_id * 7919 + 4821) % 10000:04d}"

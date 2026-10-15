"""Customer-facing API: accounts, quotes, purchase, sandbox payment, claims, documents and the Ensure Card."""
import json
import re
import secrets
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import models, pricing, schemas
from .config import settings
from .core import (CLAIM_FLOW, DB, DOC_TYPES, REQUIRED_DOCS, CustomerDep, audit, card_balance, card_last4,
                   claim_out, claim_q, doc_check, get_claim, get_policy, get_user, next_number, policy_out,
                   policy_q, register_claim)
from .models import utcnow
from .security import hash_password, make_token, throttle, verify_password

router = APIRouter()

BANKS = ["Axis Bank", "HDFC Bank", "ICICI Bank", "State Bank of India", "Punjab National Bank"]
OPEN_STATES = ("submitted", "under_review", "info_requested")
STAGES = ["Submitted", "In review", "Approved", "Funds available"]


def _user_out(u: models.User) -> schemas.UserOut:
    return schemas.UserOut(id=u.id, email=u.email, full_name=u.full_name, role=u.role, customer_id=u.customer_id)


# ---------------------------------------------------------------- auth
@router.post("/api/auth/register", response_model=schemas.AuthOut, status_code=201, tags=["auth"])
def register(body: schemas.RegisterIn, db: DB):
    email = body.email.lower()
    if db.scalar(select(models.User.id).where(models.User.email == email)) or \
            db.scalar(select(models.Customer.id).where(models.Customer.email == email)):
        raise HTTPException(409, "An account with this email already exists. Try signing in instead")
    cust = models.Customer(name=body.full_name, email=email, phone=body.mobile.strip(), city="Not provided")
    db.add(cust)
    db.flush()
    user = models.User(email=email, password_hash=hash_password(body.password), role="customer",
                       full_name=body.full_name, customer_id=cust.id)
    db.add(user)
    audit(db, "customer", cust.id, "created", f"Customer account created for {cust.name}", "customer")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "An account with this email already exists")
    return schemas.AuthOut(token=make_token(user.id), user=_user_out(user))


@router.post("/api/auth/login", response_model=schemas.AuthOut, tags=["auth"])
def login(body: schemas.LoginIn, db: DB):
    key = body.email.lower()
    if throttle.blocked(key):
        raise HTTPException(429, "Too many failed attempts. Please wait a few minutes and try again")
    user = db.scalar(select(models.User).where(models.User.email == key))
    # Always run a hash comparison so response time does not reveal whether the email exists.
    ok = verify_password(body.password, user.password_hash if user else "scrypt$AAAA$AAAA")
    if not user or not ok:
        throttle.fail(key)
        raise HTTPException(401, "Incorrect email or password")
    throttle.reset(key)
    return schemas.AuthOut(token=make_token(user.id), user=_user_out(user))


@router.get("/api/auth/me", response_model=schemas.UserOut, tags=["auth"])
def me(user: Annotated[models.User, Depends(get_user)]):
    return _user_out(user)


# ---------------------------------------------------------------- public: catalogue, quotes, contact
@router.get("/api/catalog/two-wheelers", tags=["quotes"])
def catalog():
    return [{"key": k, "name": v[0], "engine_class": v[2]} for k, v in pricing.MODELS.items()]


def _quote_payload(q: models.Quote) -> dict:
    name, _price, cls = pricing.MODELS[q.model_key]
    return {
        "id": q.id,
        "expires_at": q.expires_at,
        "vehicle": {"reg_number": q.reg_number, "make_model": name, "engine_class": cls, "reg_year": q.reg_year,
                    "age_years": round(pricing.vehicle_age_years(q.reg_year), 1),
                    "claim_free_years": q.claim_free_years},
        "plans": pricing.all_plans(q.model_key, q.reg_year, q.claim_free_years),
        "disclaimer": "Illustrative demo pricing. Partner insurers are fictional and nothing here is a real quote.",
    }


@router.post("/api/quotes", status_code=201, tags=["quotes"])
def create_quote(body: schemas.QuoteIn, db: DB):
    if body.model_key not in pricing.MODELS:
        raise HTTPException(422, "Select a vehicle model from the list")
    if body.reg_year > date.today().year:
        raise HTTPException(422, "Registration year cannot be in the future")
    lic = re.sub(r"[^A-Za-z0-9]", "", body.license_number)
    if lic and len(lic) < 8:
        raise HTTPException(422, "Driving licence number looks too short")
    q = models.Quote(id=str(uuid.uuid4()), reg_number=body.reg_number, model_key=body.model_key,
                     reg_year=body.reg_year, claim_free_years=body.claim_free_years,
                     license_last4=lic[-4:].upper() if lic else "",
                     expires_at=utcnow() + timedelta(hours=24))
    db.add(q)
    db.commit()
    return _quote_payload(q)


def _get_quote(db, qid: str) -> models.Quote:
    q = db.get(models.Quote, qid)
    if not q:
        raise HTTPException(404, "Quote not found")
    if q.expires_at < utcnow():
        raise HTTPException(410, "This quote has expired. Please start a new one")
    return q


@router.get("/api/quotes/{qid}", tags=["quotes"])
def read_quote(qid: str, db: DB):
    return _quote_payload(_get_quote(db, qid))


@router.post("/api/contact", status_code=201, tags=["public"])
def contact(body: schemas.ContactIn, db: DB):
    db.add(models.ContactMessage(name=body.name.strip(), mobile=body.mobile.strip(), email=body.email,
                                 message=body.message.strip()))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- customer: purchase and payment
def _payment_out(p: models.Payment) -> dict:
    return {"id": p.id, "reference": p.reference, "amount": p.amount, "status": p.status, "method": p.method,
            "policy_id": p.policy_id, "confirmed_at": p.confirmed_at}


@router.post("/api/me/purchases", status_code=201, tags=["me"])
def purchase(body: schemas.PurchaseIn, db: DB, user: CustomerDep):
    q = _get_quote(db, body.quote_id)
    insurer = pricing.INSURER_BY_ID.get(body.plan_id)
    if not insurer:
        raise HTTPException(404, "Plan not found")
    today = date.today()
    if not (today <= body.start_date <= today + timedelta(days=30)):
        raise HTTPException(422, "Policy can start today or within the next 30 days")
    plan = pricing.price_plan(q.model_key, q.reg_year, q.claim_free_years, insurer)  # server-side pricing only
    name, _price, _cls = pricing.MODELS[q.model_key]

    vehicle = db.scalar(select(models.Vehicle).where(
        models.Vehicle.customer_id == user.customer_id, models.Vehicle.reg_number == q.reg_number))
    if vehicle:
        clash = db.scalar(select(models.Policy).where(
            models.Policy.vehicle_id == vehicle.id, models.Policy.status.in_(("active", "pending")),
            models.Policy.end_date >= body.start_date))
        if clash:
            raise HTTPException(409, f"{q.reg_number} already has policy {clash.number} valid until {clash.end_date}")
    else:
        vehicle = models.Vehicle(customer_id=user.customer_id, reg_number=q.reg_number, make_model=name,
                                 reg_year=q.reg_year, license_last4=q.license_last4)
        db.add(vehicle)
        db.flush()

    year = today.year
    number = next_number(db, f"ENS-TWO-{year}", models.Policy, models.Policy.number)
    try:
        end = body.start_date.replace(year=body.start_date.year + 1) - timedelta(days=1)
    except ValueError:
        end = body.start_date.replace(year=body.start_date.year + 1, day=28) - timedelta(days=1)
    age = pricing.vehicle_age_years(q.reg_year)
    policy = models.Policy(
        number=number, customer_id=user.customer_id, vehicle_id=vehicle.id, product="Two-Wheeler",
        insurer=insurer.name, premium=plan["total"], sum_insured=plan["idv"], breakdown=json.dumps(plan),
        start_date=body.start_date, end_date=end, status="pending", risk_score=min(95, int(20 + age * 6)))
    db.add(policy)
    db.flush()
    payment = models.Payment(policy_id=policy.id, customer_id=user.customer_id, amount=plan["total"],
                             reference="PAY-" + secrets.token_hex(5).upper())
    db.add(payment)
    audit(db, "policy", policy.id, "created", f"Policy {number} created, awaiting payment", "customer",
          to_status="pending")
    db.commit()
    return {"policy": policy_out(get_policy(db, policy.id)), "payment": _payment_out(payment)}


def _own_payment(db, pid: int, user) -> models.Payment:
    p = db.get(models.Payment, pid)
    if not p or p.customer_id != user.customer_id:
        raise HTTPException(404, "Payment not found")
    return p


@router.get("/api/me/payments/{pid}", tags=["me"])
def read_payment(pid: int, db: DB, user: CustomerDep):
    p = _own_payment(db, pid, user)
    return {"payment": _payment_out(p), "policy": policy_out(get_policy(db, p.policy_id)),
            "card_balance": card_balance(db, user.customer_id), "banks": BANKS, "sandbox": True}


@router.post("/api/me/payments/{pid}/confirm", tags=["me"])
def confirm_payment(pid: int, body: schemas.PayIn, db: DB, user: CustomerDep):
    """SANDBOX settlement. No real money moves and no bank or card credentials are accepted or stored."""
    p = _own_payment(db, pid, user)
    if p.status == "succeeded":
        return {"payment": _payment_out(p), "policy": policy_out(get_policy(db, p.policy_id))}
    policy = get_policy(db, p.policy_id)
    if policy.status != "pending":
        raise HTTPException(409, f"This policy is {policy.status} and can no longer be paid")
    if body.method == "netbanking" and body.bank not in BANKS:
        raise HTTPException(422, "Choose a bank from the list")
    if body.method == "ensure_card":
        if card_balance(db, user.customer_id) < p.amount:
            raise HTTPException(422, "Your Ensure Card balance is not enough for this payment")
        db.add(models.CardTransaction(customer_id=user.customer_id, kind="premium_payment", amount=-p.amount,
                                      description=f"Premium for {policy.number}"))
    p.method, p.status, p.confirmed_at = body.method, "succeeded", utcnow()
    audit(db, "policy", policy.id, "status_changed", f"{policy.number}: pending -> active. Paid via {body.method}",
          "customer", from_status="pending", to_status="active")
    policy.status = "active"
    db.commit()
    return {"payment": _payment_out(p), "policy": policy_out(get_policy(db, policy.id))}


# ---------------------------------------------------------------- customer: dashboard, policies
def _my_policies(db, user):
    return db.execute(policy_q().where(models.Policy.customer_id == user.customer_id).order_by(
        models.Policy.created_at.desc())).unique().scalars().all()


def _my_claims(db, user):
    ids = [p.id for p in _my_policies(db, user)]
    if not ids:
        return []
    return db.execute(claim_q().where(models.Claim.policy_id.in_(ids)).order_by(
        models.Claim.created_at.desc())).unique().scalars().all()


def _stage(c: models.Claim) -> dict:
    idx = {"submitted": 0, "under_review": 1, "info_requested": 1, "approved": 2, "settled": 3, "rejected": 1}[c.status]
    return {"stages": STAGES, "current": idx, "needs_action": c.status == "info_requested",
            "declined": c.status == "rejected", "done": c.status == "settled"}


def _customer_claim(c: models.Claim) -> dict:
    out = claim_out(c).model_dump()
    out["doc_check"] = doc_check(c)
    out["progress"] = _stage(c)
    return out


@router.get("/api/me/summary", tags=["me"])
def summary(db: DB, user: CustomerDep):
    pols = _my_policies(db, user)
    claims = _my_claims(db, user)
    return {
        "name": user.full_name,
        "active_policies": sum(1 for p in pols if p.status == "active"),
        "pending_payment": sum(1 for p in pols if p.status == "pending"),
        "open_claims": sum(1 for c in claims if c.status in OPEN_STATES),
        "settled_claims": sum(1 for c in claims if c.status == "settled"),
        "card_balance": card_balance(db, user.customer_id),
        "next_expiry": min((p.end_date for p in pols if p.status == "active"), default=None),
    }


@router.get("/api/me/policies", tags=["me"])
def my_policies(db: DB, user: CustomerDep):
    pending = {p.policy_id: p.id for p in db.scalars(select(models.Payment).where(
        models.Payment.customer_id == user.customer_id, models.Payment.status == "pending"))}
    out = []
    for p in _my_policies(db, user):
        d = policy_out(p).model_dump()
        d["pending_payment_id"] = pending.get(p.id) if p.status == "pending" else None
        out.append(d)
    return out


@router.get("/api/me/policies/{pid}", tags=["me"])
def my_policy(pid: int, db: DB, user: CustomerDep):
    p = get_policy(db, pid)
    if p.customer_id != user.customer_id:
        raise HTTPException(404, "Policy not found")
    claims = [_customer_claim(c) for c in _my_claims(db, user) if c.policy_id == pid]
    payment = db.scalar(select(models.Payment).where(models.Payment.policy_id == pid).order_by(
        models.Payment.id.desc()))
    return {"policy": policy_out(p), "claims": claims, "payment": _payment_out(payment) if payment else None}


# ---------------------------------------------------------------- customer: claims
@router.get("/api/me/claim-types", tags=["me"])
def claim_types():
    return {"types": [{"name": t, "required": [{"type": d, "label": DOC_TYPES[d]} for d in docs]}
                      for t, docs in REQUIRED_DOCS.items()],
            "doc_types": [{"type": k, "label": v} for k, v in DOC_TYPES.items()]}


@router.get("/api/me/claims", tags=["me"])
def my_claims(db: DB, user: CustomerDep):
    return [_customer_claim(c) for c in _my_claims(db, user)]


@router.post("/api/me/claims", status_code=201, tags=["me"])
def create_my_claim(body: schemas.CustomerClaimIn, db: DB, user: CustomerDep):
    policy = get_policy(db, body.policy_id)
    if policy.customer_id != user.customer_id:
        raise HTTPException(404, "Policy not found")
    if body.claim_type not in REQUIRED_DOCS:
        raise HTTPException(422, "Choose a claim type from the list")
    c = register_claim(db, policy, body.claim_type, body.amount, body.incident_date, body.description, [],
                       "customer")
    db.commit()
    return _customer_claim(get_claim(db, c.id))


def _own_claim(db, cid: int, user) -> models.Claim:
    c = get_claim(db, cid)
    if c.policy.customer_id != user.customer_id:
        raise HTTPException(404, "Claim not found")
    return c


def _friendly(ev: models.AuditEvent, number: str) -> str | None:
    reason = ev.detail.split(". ", 1)[1].strip() if ". " in ev.detail else ""
    if ev.entity == "claim":
        if ev.action == "created":
            return f"Claim {number} was submitted."
        if ev.action == "document_uploaded":
            return f"A document was added to claim {number}."
        return {
            "under_review": f"Claim {number} is now being reviewed.",
            "info_requested": f"More information is needed for claim {number}." + (f" {reason}" if reason else ""),
            "approved": f"Claim {number} was approved.",
            "rejected": f"Claim {number} was declined." + (f" {reason}" if reason else ""),
            "settled": f"Funds for claim {number} are available on your Ensure Card.",
        }.get(ev.to_status or "")
    if ev.entity == "policy":
        if ev.to_status == "active":
            return f"Policy {number} is active."
        if ev.action == "created":
            return f"Policy {number} was created and is awaiting payment."
        if ev.to_status == "cancelled":
            return f"Policy {number} was cancelled."
        if ev.action == "renewed":
            return f"Policy {number} was renewed."
    return None


@router.get("/api/me/claims/{cid}", tags=["me"])
def my_claim(cid: int, db: DB, user: CustomerDep):
    c = _own_claim(db, cid, user)
    events = db.scalars(select(models.AuditEvent).where(
        models.AuditEvent.entity == "claim", models.AuditEvent.entity_id == cid
    ).order_by(models.AuditEvent.created_at.desc(), models.AuditEvent.id.desc())).all()
    timeline = [{"at": e.created_at, "message": m} for e in events if (m := _friendly(e, c.number))]
    return {"claim": _customer_claim(c), "policy": policy_out(c.policy), "timeline": timeline}


ALLOWED_MAGIC = {b"%PDF": ("application/pdf", ".pdf"), b"\x89PNG": ("image/png", ".png"),
                 b"\xff\xd8\xff": ("image/jpeg", ".jpg")}


@router.post("/api/me/claims/{cid}/documents", status_code=201, tags=["me"])
async def upload_document(cid: int, db: DB, user: CustomerDep, doc_type: str = Form(...),
                          file: UploadFile = File(...)):
    c = _own_claim(db, cid, user)
    if c.status not in OPEN_STATES:
        raise HTTPException(409, "Documents can no longer be added to this claim")
    if doc_type not in DOC_TYPES:
        raise HTTPException(422, "Unknown document type")
    if len(c.files) >= 12:
        raise HTTPException(422, "A claim can have at most 12 documents")
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB")
    if not data:
        raise HTTPException(422, "The file is empty")
    kind = next((v for magic, v in ALLOWED_MAGIC.items() if data.startswith(magic)), None)
    if not kind:
        raise HTTPException(415, "Only PDF, PNG or JPG files are accepted")
    content_type, ext = kind
    stored = uuid.uuid4().hex + ext
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    (Path(settings.upload_dir) / stored).write_bytes(data)
    original = re.sub(r"[^\w.\- ]", "_", Path(file.filename or "document" + ext).name)[:120] or "document" + ext
    doc = models.Document(claim_id=c.id, doc_type=doc_type, original_name=original, stored_name=stored,
                          content_type=content_type, size=len(data))
    db.add(doc)
    audit(db, "claim", c.id, "document_uploaded", f"{c.number}: {DOC_TYPES[doc_type]} uploaded", "customer")
    db.commit()
    db.expire_all()  # reload relationships (sessions keep loaded state after commit)
    return _customer_claim(_own_claim(db, cid, user))


@router.get("/api/me/claims/{cid}/documents/{did}", tags=["me"])
def download_document(cid: int, did: int, db: DB, user: CustomerDep):
    c = _own_claim(db, cid, user)
    d = db.get(models.Document, did)
    if not d or d.claim_id != c.id:
        raise HTTPException(404, "Document not found")
    path = Path(settings.upload_dir) / d.stored_name
    if not path.is_file():
        raise HTTPException(404, "File is no longer available")
    return FileResponse(path, media_type=d.content_type, filename=d.original_name,
                        content_disposition_type="attachment")


# ---------------------------------------------------------------- customer: notifications and card
@router.get("/api/me/notifications", tags=["me"])
def notifications(db: DB, user: CustomerDep, limit: int = 20):
    pols = _my_policies(db, user)
    claims = _my_claims(db, user)
    labels = {("policy", p.id): p.number for p in pols} | {("claim", c.id): c.number for c in claims}
    if not labels:
        return []
    events = db.scalars(select(models.AuditEvent).where(
        models.AuditEvent.entity.in_(("policy", "claim"))).order_by(
        models.AuditEvent.created_at.desc(), models.AuditEvent.id.desc()).limit(500)).all()
    out = []
    for e in events:
        label = labels.get((e.entity, e.entity_id))
        msg = _friendly(e, label) if label else None
        if msg:
            out.append({"id": e.id, "at": e.created_at, "message": msg})
        if len(out) >= limit:
            break
    return out


@router.get("/api/me/card", tags=["me"])
def card(db: DB, user: CustomerDep):
    txs = db.scalars(select(models.CardTransaction).where(
        models.CardTransaction.customer_id == user.customer_id).order_by(
        models.CardTransaction.created_at.desc(), models.CardTransaction.id.desc()).limit(50)).all()
    return {
        "holder": user.full_name.upper(),
        "last4": card_last4(user.customer_id),
        "valid_thru": f"12/{(date.today().year + 3) % 100:02d}",
        "balance": card_balance(db, user.customer_id),
        "transactions": [{"id": t.id, "kind": t.kind, "amount": t.amount, "description": t.description,
                          "at": t.created_at} for t in txs],
        "sandbox": True,
    }


@router.post("/api/me/card/pay", tags=["me"])
def card_pay(body: schemas.CardPayIn, db: DB, user: CustomerDep):
    """SANDBOX merchant payment from the card balance (no real merchant network)."""
    if card_balance(db, user.customer_id) < body.amount:
        raise HTTPException(422, "Your Ensure Card balance is not enough for this payment")
    db.add(models.CardTransaction(customer_id=user.customer_id, kind="merchant_payment", amount=-body.amount,
                                  description=f"Payment to {body.merchant.strip()}"))
    db.commit()
    return card(db, user)

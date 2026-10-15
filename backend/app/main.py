import csv
import io
import logging
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from . import models, schemas
from .config import settings
from .core import (CLAIM_FLOW, DB, PERMS, REASON_REQUIRED, ActorDep, audit, claim_out, claim_q, get_claim,
                   get_policy, mask_email, mask_phone, next_number, policy_out, policy_q, register_claim)
from .database import Base, SessionLocal, engine
from .portal import router as portal_router
from .seed import seed

log = logging.getLogger("ensure")
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    if settings.seed_demo_data:
        with SessionLocal() as db:
            seed(db)
    yield


app = FastAPI(title="ENSURE Insurance API", version="0.2.0", lifespan=lifespan,
              description="DEMO environment with synthetic data. Customer and staff endpoints use bearer tokens; "
                          "payments are sandbox-only.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list, allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=False)
app.include_router(portal_router)


@app.exception_handler(HTTPException)
async def http_exc(_: Request, exc: HTTPException):
    return JSONResponse({"error": {"code": exc.status_code, "message": exc.detail}}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_exc(_: Request, exc: RequestValidationError):
    fields = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"].removeprefix("Value error, ")}
              for e in exc.errors()]
    return JSONResponse({"error": {"code": 422, "message": "Validation failed", "fields": fields}}, status_code=422)


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("Unhandled error: %s", type(exc).__name__)
    return JSONResponse({"error": {"code": 500, "message": "Internal server error"}}, status_code=500)


# ---------- system
@app.get("/api/health", tags=["system"])
def health(db: DB):
    db.execute(select(1))
    return {"status": "ok", "environment": settings.environment_label,
            "database": "connected", "time": date.today().isoformat()}


@app.get("/api/roles", tags=["system"])
def roles():
    return {"roles": {r: sorted(p) for r, p in PERMS.items()}}


# ---------- staff: customers
def customer_out(c: models.Customer, actor, db) -> schemas.CustomerOut:
    pol_ids = [p.id for p in c.policies]
    claims = db.scalar(select(func.count(models.Claim.id)).where(models.Claim.policy_id.in_(pol_ids))) if pol_ids else 0
    pii = actor.can("pii:read")
    return schemas.CustomerOut(id=c.id, name=c.name, email=c.email if pii else mask_email(c.email),
                               phone=c.phone if pii else mask_phone(c.phone), city=c.city,
                               created_at=c.created_at, policy_count=len(c.policies), claim_count=claims or 0)


@app.get("/api/customers", response_model=list[schemas.CustomerOut], tags=["customers"])
def list_customers(db: DB, actor: ActorDep, q: str = "", city: str = "", limit: int = Query(100, le=500)):
    stmt = select(models.Customer).options(joinedload(models.Customer.policies)).order_by(models.Customer.name)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(models.Customer.name.ilike(like), models.Customer.email.ilike(like)))
    if city:
        stmt = stmt.where(models.Customer.city == city)
    return [customer_out(c, actor, db) for c in db.execute(stmt.limit(limit)).unique().scalars()]


@app.post("/api/customers", response_model=schemas.CustomerOut, status_code=201, tags=["customers"])
def create_customer(body: schemas.CustomerIn, db: DB, actor: ActorDep):
    actor.require("customer:write")
    c = models.Customer(**body.model_dump())
    db.add(c)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A customer with this email already exists")
    audit(db, "customer", c.id, "created", f"Customer {c.name} created", actor.role)
    db.commit()
    return customer_out(c, actor, db)


@app.get("/api/customers/{cid}", tags=["customers"])
def customer_detail(cid: int, db: DB, actor: ActorDep):
    c = db.execute(select(models.Customer).options(joinedload(models.Customer.policies)).where(
        models.Customer.id == cid)).unique().scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Customer not found")
    pol_ids = [p.id for p in c.policies]
    policies = [policy_out(get_policy(db, i)) for i in pol_ids]
    claims = []
    if pol_ids:
        rows = db.execute(claim_q().where(models.Claim.policy_id.in_(pol_ids)).order_by(
            models.Claim.created_at.desc())).unique().scalars()
        claims = [claim_out(r) for r in rows]
    return {"customer": customer_out(c, actor, db), "policies": policies, "claims": claims}


@app.patch("/api/customers/{cid}", response_model=schemas.CustomerOut, tags=["customers"])
def update_customer(cid: int, body: schemas.CustomerPatch, db: DB, actor: ActorDep):
    actor.require("customer:write")
    c = db.get(models.Customer, cid)
    if not c:
        raise HTTPException(404, "Customer not found")
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(c, k, v.strip() if isinstance(v, str) else v)
    audit(db, "customer", c.id, "updated", f"Fields changed: {', '.join(changes) or 'none'}", actor.role)
    db.commit()
    return customer_out(c, actor, db)


# ---------- staff: policies
@app.get("/api/policies", response_model=list[schemas.PolicyOut], tags=["policies"])
def list_policies(db: DB, actor: ActorDep, q: str = "", status: str = "", product: str = "",
                  customer_id: int | None = None,
                  expiring_within_days: int | None = Query(None, ge=0, le=730), limit: int = Query(200, le=1000)):
    stmt = policy_q().join(models.Customer)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(models.Policy.number.ilike(like), models.Customer.name.ilike(like)))
    if status:
        stmt = stmt.where(models.Policy.status == status)
    if product:
        stmt = stmt.where(models.Policy.product == product)
    if customer_id:
        stmt = stmt.where(models.Policy.customer_id == customer_id)
    if expiring_within_days is not None:
        today = date.today()
        stmt = stmt.where(models.Policy.status == "active", models.Policy.end_date >= today,
                          models.Policy.end_date <= today + timedelta(days=expiring_within_days))
    rows = db.execute(stmt.order_by(models.Policy.end_date).limit(limit)).unique().scalars()
    return [policy_out(p) for p in rows]


@app.post("/api/policies", response_model=schemas.PolicyOut, status_code=201, tags=["policies"])
def create_policy(body: schemas.PolicyIn, db: DB, actor: ActorDep):
    actor.require("policy:write")
    if body.end_date <= body.start_date:
        raise HTTPException(422, "End date must be after start date")
    if not db.get(models.Customer, body.customer_id):
        raise HTTPException(404, "Customer not found")
    n = next_number(db, f"ENS-{body.product[:3].upper()}-{date.today().year}", models.Policy, models.Policy.number)
    p = models.Policy(number=n, **body.model_dump())
    db.add(p)
    db.flush()
    audit(db, "policy", p.id, "created", f"Policy {n} created with status {p.status}", actor.role,
          to_status=p.status)
    db.commit()
    return policy_out(get_policy(db, p.id))


@app.get("/api/policies/{pid}", tags=["policies"])
def policy_detail(pid: int, db: DB, actor: ActorDep):
    p = get_policy(db, pid)
    claims = db.execute(claim_q().where(models.Claim.policy_id == pid).order_by(
        models.Claim.created_at.desc())).unique().scalars()
    history = db.scalars(select(models.AuditEvent).where(
        models.AuditEvent.entity == "policy", models.AuditEvent.entity_id == pid
    ).order_by(models.AuditEvent.created_at.desc()))
    return {"policy": policy_out(p), "claims": [claim_out(c) for c in claims],
            "history": [schemas.AuditOut.model_validate(h) for h in history]}


@app.patch("/api/policies/{pid}", response_model=schemas.PolicyOut, tags=["policies"])
def update_policy(pid: int, body: schemas.PolicyPatch, db: DB, actor: ActorDep):
    actor.require("policy:write")
    p = get_policy(db, pid)
    if p.status in ("cancelled", "expired"):
        raise HTTPException(409, f"A {p.status} policy cannot be edited")
    changes = body.model_dump(exclude_none=True)
    if "end_date" in changes and changes["end_date"] <= p.start_date:
        raise HTTPException(422, "End date must be after start date")
    for k, v in changes.items():
        setattr(p, k, v)
    audit(db, "policy", p.id, "updated", f"Fields changed: {', '.join(changes) or 'none'}", actor.role)
    db.commit()
    return policy_out(get_policy(db, pid))


POLICY_FLOW = {"pending": ["active", "cancelled"], "active": ["cancelled", "expired"],
               "expired": [], "cancelled": []}


@app.post("/api/policies/{pid}/status", response_model=schemas.PolicyOut, tags=["policies"])
def change_policy_status(pid: int, body: schemas.StatusChange, db: DB, actor: ActorDep):
    actor.require("policy:write")
    p = get_policy(db, pid)
    if body.status not in POLICY_FLOW[p.status]:
        raise HTTPException(409, f"Cannot move a policy from '{p.status}' to '{body.status}'")
    if body.status == "cancelled" and not body.reason.strip():
        raise HTTPException(422, "A reason is required to cancel a policy")
    audit(db, "policy", p.id, "status_changed",
          f"{p.number}: {p.status} -> {body.status}. {body.reason}".strip(), actor.role,
          from_status=p.status, to_status=body.status)
    p.status = body.status
    db.commit()
    return policy_out(get_policy(db, pid))


@app.post("/api/policies/{pid}/renew", response_model=schemas.PolicyOut, tags=["policies"])
def renew_policy(pid: int, db: DB, actor: ActorDep):
    """Extends the policy term by 12 months. Premium is NOT recalculated."""
    actor.require("policy:write")
    p = get_policy(db, pid)
    if p.status != "active":
        raise HTTPException(409, "Only active policies can be renewed")
    old = p.end_date
    try:
        p.end_date = old.replace(year=old.year + 1)
    except ValueError:  # 29 Feb
        p.end_date = old.replace(year=old.year + 1, day=28)
    audit(db, "policy", p.id, "renewed", f"Term extended from {old} to {p.end_date}; premium unchanged", actor.role)
    db.commit()
    return policy_out(get_policy(db, pid))


# ---------- staff: claims
@app.get("/api/claims", response_model=list[schemas.ClaimOut], tags=["claims"])
def list_claims(db: DB, actor: ActorDep, q: str = "", status: str = "", product: str = "",
                limit: int = Query(300, le=1000)):
    stmt = claim_q().join(models.Policy).join(models.Customer)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(models.Claim.number.ilike(like), models.Policy.number.ilike(like),
                              models.Customer.name.ilike(like)))
    if status:
        stmt = stmt.where(models.Claim.status == status)
    if product:
        stmt = stmt.where(models.Policy.product == product)
    rows = db.execute(stmt.order_by(models.Claim.created_at.desc()).limit(limit)).unique().scalars()
    return [claim_out(c) for c in rows]


@app.post("/api/claims", response_model=schemas.ClaimOut, status_code=201, tags=["claims"])
def create_claim(body: schemas.ClaimIn, db: DB, actor: ActorDep):
    actor.require("claim:create")
    p = get_policy(db, body.policy_id)
    c = register_claim(db, p, body.claim_type, body.amount, body.incident_date, body.description,
                       body.documents, actor.role)
    db.commit()
    return claim_out(get_claim(db, c.id))


@app.get("/api/claims/{cid}", tags=["claims"])
def claim_detail(cid: int, db: DB, actor: ActorDep):
    c = get_claim(db, cid)
    history = db.scalars(select(models.AuditEvent).where(
        models.AuditEvent.entity == "claim", models.AuditEvent.entity_id == cid
    ).order_by(models.AuditEvent.created_at.desc(), models.AuditEvent.id.desc()))
    return {"claim": claim_out(c), "policy": policy_out(c.policy),
            "history": [schemas.AuditOut.model_validate(h) for h in history]}


@app.get("/api/claims/{cid}/documents/{did}", tags=["claims"])
def staff_download(cid: int, did: int, db: DB, actor: ActorDep):
    d = db.get(models.Document, did)
    if not d or d.claim_id != cid:
        raise HTTPException(404, "Document not found")
    path = Path(settings.upload_dir) / d.stored_name
    if not path.is_file():
        raise HTTPException(404, "File is no longer available")
    return FileResponse(path, media_type=d.content_type, filename=d.original_name,
                        content_disposition_type="attachment")


@app.post("/api/claims/{cid}/transition", response_model=schemas.ClaimOut, tags=["claims"])
def transition_claim(cid: int, body: schemas.ClaimTransition, db: DB, actor: ActorDep):
    actor.require("claim:decide")
    c = get_claim(db, cid)
    if body.to not in CLAIM_FLOW[c.status]:
        allowed = ", ".join(CLAIM_FLOW[c.status]) or "none (terminal state)"
        raise HTTPException(409, f"Cannot move claim from '{c.status}' to '{body.to}'. Allowed: {allowed}")
    if body.to in REASON_REQUIRED and not body.reason.strip():
        raise HTTPException(422, f"A reason is required when setting a claim to '{body.to}'")
    if body.to == "approved":
        approved = body.approved_amount if body.approved_amount is not None else c.amount
        if approved > c.amount:
            raise HTTPException(422, "Approved amount cannot exceed the claimed amount")
        c.approved_amount = approved
    elif body.approved_amount is not None:
        raise HTTPException(422, "Approved amount can only be set when approving a claim")
    if body.to == "settled":
        payout = c.approved_amount or c.amount
        db.add(models.CardTransaction(customer_id=c.policy.customer_id, kind="claim_credit", amount=payout,
                                      description=f"Claim {c.number} payout", claim_id=c.id))
    audit(db, "claim", c.id, "status_changed", f"{c.number}: {c.status} -> {body.to}. {body.reason}".strip(),
          actor.role, from_status=c.status, to_status=body.to)
    c.status = body.to
    if body.reason.strip():
        c.decision_reason = body.reason.strip()
    db.commit()
    return claim_out(get_claim(db, cid))


# ---------- staff: activity / analytics
@app.get("/api/activity", response_model=list[schemas.AuditOut], tags=["activity"])
def activity(db: DB, actor: ActorDep, limit: int = Query(20, le=100)):
    return db.scalars(select(models.AuditEvent).order_by(
        models.AuditEvent.created_at.desc(), models.AuditEvent.id.desc()).limit(limit)).all()


@app.get("/api/messages", tags=["activity"])
def messages(db: DB, actor: ActorDep, limit: int = Query(50, le=200)):
    actor.require("pii:read")
    rows = db.scalars(select(models.ContactMessage).order_by(models.ContactMessage.created_at.desc()).limit(limit))
    return [{"id": m.id, "name": m.name, "mobile": m.mobile, "email": m.email, "message": m.message,
             "created_at": m.created_at} for m in rows]


def _month_keys(end: date, n: int) -> list[str]:
    y, m = end.year, end.month
    keys = []
    for _ in range(n):
        keys.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return keys[::-1]


@app.get("/api/analytics", tags=["analytics"])
def analytics(db: DB, actor: ActorDep, months: int = Query(12, ge=3, le=24)):
    today = date.today()
    policies = db.execute(policy_q()).unique().scalars().all()
    claims = db.execute(claim_q()).unique().scalars().all()
    keys = _month_keys(today, months)
    window_start = date(int(keys[0][:4]), int(keys[0][5:]), 1)

    written = defaultdict(float)
    for p in policies:
        if p.status != "pending":
            written[p.start_date.strftime("%Y-%m")] += p.premium
    filed, claimed = Counter(), defaultdict(float)
    for c in claims:
        k = c.created_at.strftime("%Y-%m")
        filed[k] += 1
        claimed[k] += c.amount
    trend = [{"month": k, "premium": round(written[k]), "claims": filed[k], "claimed": round(claimed[k])}
             for k in keys]

    active = [p for p in policies if p.status == "active"]
    by_status = Counter(p.status for p in policies)
    by_product = defaultdict(lambda: {"policies": 0, "premium": 0.0})
    for p in active:
        by_product[p.product]["policies"] += 1
        by_product[p.product]["premium"] += p.premium
    claim_status = Counter(c.status for c in claims)
    decided = claim_status["approved"] + claim_status["settled"] + claim_status["rejected"]
    approved = claim_status["approved"] + claim_status["settled"]
    risk_bands = Counter("Low" if p.risk_score < 35 else "Medium" if p.risk_score < 65 else "High" for p in active)
    renew = []
    for lo, hi, label in [(0, 30, "0-30 days"), (31, 60, "31-60 days"), (61, 90, "61-90 days")]:
        renew.append({"band": label, "count": sum(1 for p in active if lo <= (p.end_date - today).days <= hi)})
    claims_in_window = [c for c in claims if c.created_at.date() >= window_start]
    open_states = ("submitted", "under_review", "info_requested")

    return {
        "environment": settings.environment_label,
        "kpis": {
            "total_policies": len(policies),
            "active_policies": len(active),
            "active_premium": round(sum(p.premium for p in active)),
            "total_sum_insured": round(sum(p.sum_insured for p in active)),
            "customers": db.scalar(select(func.count(models.Customer.id))),
            "claims_total": len(claims),
            "claims_open": sum(claim_status[s] for s in open_states),
            "claims_in_window": len(claims_in_window),
            "claimed_amount_in_window": round(sum(c.amount for c in claims_in_window)),
            "approval_rate": round(approved / decided * 100, 1) if decided else None,
            "expiring_30": renew[0]["count"],
        },
        "policy_status": [{"status": s, "count": n} for s, n in by_status.items()],
        "claim_status": [{"status": s, "count": n} for s, n in claim_status.items()],
        "by_product": [{"product": k, **v, "premium": round(v["premium"])} for k, v in by_product.items()],
        "risk_bands": [{"band": b, "count": risk_bands[b]} for b in ("Low", "Medium", "High")],
        "renewals": renew,
        "trend": trend,
    }


@app.get("/api/attention", tags=["analytics"])
def attention(db: DB, actor: ActorDep):
    """Items that need a human: stale open claims and imminent renewals."""
    today = date.today()
    stale = db.execute(claim_q().where(
        models.Claim.status.in_(("submitted", "under_review", "info_requested")),
        models.Claim.created_at < today - timedelta(days=14)).order_by(models.Claim.created_at)
        .limit(8)).unique().scalars().all()
    expiring = db.execute(policy_q().where(
        models.Policy.status == "active", models.Policy.end_date >= today,
        models.Policy.end_date <= today + timedelta(days=14)).order_by(models.Policy.end_date)
        .limit(8)).unique().scalars().all()
    return {"stale_claims": [claim_out(c) for c in stale], "expiring_policies": [policy_out(p) for p in expiring]}


def _csv(rows: list[list], header: list[str], name: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        # neutralise spreadsheet formula injection
        w.writerow([("'" + str(v)) if isinstance(v, str) and v[:1] in "=+-@" else v for v in r])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/export/policies.csv", tags=["analytics"])
def export_policies(db: DB, actor: ActorDep):
    rows = db.execute(policy_q().order_by(models.Policy.number)).unique().scalars()
    return _csv([[p.number, p.customer.name, p.product, p.premium, p.sum_insured, p.start_date, p.end_date,
                  p.status, p.risk_score] for p in rows],
                ["policy_number", "customer", "product", "premium", "sum_insured", "start_date", "end_date",
                 "status", "demo_risk_score"], "policies.csv")


@app.get("/api/export/claims.csv", tags=["analytics"])
def export_claims(db: DB, actor: ActorDep):
    rows = db.execute(claim_q().order_by(models.Claim.number)).unique().scalars()
    return _csv([[c.number, c.policy.number, c.policy.customer.name, c.claim_type, c.amount, c.incident_date,
                  c.status, c.created_at.date()] for c in rows],
                ["claim_number", "policy_number", "customer", "type", "amount", "incident_date", "status",
                 "registered"], "claims.csv")

"""Synthetic DEMO data. All names, emails and figures are fictitious (example.com / ensure.demo addresses)."""
import json
import random
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from . import pricing
from .config import settings
from .models import AuditEvent, CardTransaction, Claim, Customer, Document, Payment, Policy, User, Vehicle
from .security import hash_password

FIRST = ["Aarav", "Meera", "Rohan", "Isha", "Kabir", "Anaya", "Vikram", "Diya", "Arjun", "Sana",
         "Neil", "Tara", "Imran", "Leela", "Dev", "Nisha", "Rahul", "Pooja", "Karan", "Riya",
         "Samir", "Jaya", "Ishan", "Zoya", "Varun", "Mira", "Yash", "Kiara", "Omar", "Latha"]
LAST = ["Sharma", "Iyer", "Nair", "Verma", "Khan", "Reddy", "Menon", "Gupta", "Pillai", "Das",
        "Kapoor", "Rao", "Singh", "Joshi", "Bose", "Mehta", "Chopra", "Kulkarni"]
CITIES = ["Chennai", "Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Pune", "Kochi", "Kolkata"]
PRODUCTS = {  # product -> (premium range, sum insured range, claim types)
    "Two-Wheeler": ((900, 4200), (20_000, 200_000), ["Accident damage", "Theft", "Fire", "Natural calamity"]),
    "Motor": ((6_000, 28_000), (300_000, 1_500_000), ["Collision", "Theft", "Third-party liability", "Windscreen"]),
    "Health": ((9_000, 55_000), (300_000, 2_000_000), ["Hospitalisation", "Day-care procedure", "Diagnostics"]),
    "Home": ((3_000, 18_000), (1_000_000, 8_000_000), ["Water damage", "Fire", "Burglary"]),
    "Travel": ((1_200, 9_000), (100_000, 1_000_000), ["Trip cancellation", "Baggage loss", "Medical abroad"]),
    "Life": ((12_000, 90_000), (1_000_000, 10_000_000), ["Maturity benefit", "Rider benefit"]),
}

STAFF = [
    ("admin@ensure.demo", "Asha Administrator", "admin"),
    ("agent@ensure.demo", "Arun Agent", "agent"),
    ("claims@ensure.demo", "Chitra Claims Officer", "claims_officer"),
    ("viewer@ensure.demo", "Vikram Viewer", "viewer"),
]
DEMO_CUSTOMER_EMAIL = "kamesh@gmail.com"


def _seed_demo_customer(db: Session, today: date) -> None:
    """A ready-made customer account: active two-wheeler policy, a paid-out claim and one in progress."""
    c = Customer(name="Demo Customer", email=DEMO_CUSTOMER_EMAIL, phone="+91 98765 43210", city="Chennai",
                 created_at=datetime.combine(today - timedelta(days=210), datetime.min.time()))
    db.add(c)
    db.flush()
    db.add(User(email=DEMO_CUSTOMER_EMAIL, password_hash=hash_password(settings.demo_password), role="customer",
                full_name=c.name, customer_id=c.id))
    v = Vehicle(customer_id=c.id, reg_number="TN22D4596", make_model="Suzuki Swish 125", reg_year=2017,
                license_last4="4821")
    db.add(v)
    db.flush()
    start = today - timedelta(days=120)
    plan = pricing.price_plan("swish-125", 2017, 2, pricing.INSURER_BY_ID["northstar"], today)
    pol = Policy(number=f"ENS-TWO-{today.year}-9001", customer_id=c.id, vehicle_id=v.id, product="Two-Wheeler",
                 insurer=plan["insurer"], premium=plan["total"], sum_insured=plan["idv"], breakdown=json.dumps(plan),
                 start_date=start, end_date=start + timedelta(days=364), status="active", risk_score=45,
                 created_at=datetime.combine(start, datetime.min.time()))
    db.add(pol)
    db.flush()
    db.add(Payment(policy_id=pol.id, customer_id=c.id, amount=plan["total"], method="upi_qr",
                   status="succeeded", reference="PAY-DEMO0001",
                   created_at=pol.created_at, confirmed_at=pol.created_at))
    db.add(AuditEvent(entity="policy", entity_id=pol.id, action="status_changed",
                      detail=f"{pol.number}: pending -> active. Paid via upi_qr", from_status="pending",
                      to_status="active", actor_role="customer", created_at=pol.created_at))

    def claim(num: str, ctype: str, amount: float, days_ago: int, status: str, docs: list[str],
              approved: float | None = None, reason: str = "") -> Claim:
        created = datetime.combine(today - timedelta(days=days_ago), datetime.min.time()) + timedelta(hours=10)
        cl = Claim(number=num, policy_id=pol.id, claim_type=ctype, amount=amount, approved_amount=approved,
                   incident_date=today - timedelta(days=days_ago + 2), description=f"Demo {ctype.lower()} claim.",
                   status=status, decision_reason=reason, created_at=created,
                   updated_at=created + timedelta(days=2))
        db.add(cl)
        db.flush()
        for t in docs:
            db.add(Document(claim_id=cl.id, doc_type=t, original_name=f"{t}.pdf", stored_name=f"demo-{num}-{t}",
                            content_type="application/pdf", size=1024, uploaded_at=created))
        db.add(AuditEvent(entity="claim", entity_id=cl.id, action="created",
                          detail=f"Claim {num} registered against {pol.number}", to_status="submitted",
                          actor_role="customer", created_at=created))
        chain = {"submitted": [], "under_review": ["under_review"], "approved": ["under_review", "approved"],
                 "settled": ["under_review", "approved", "settled"],
                 "info_requested": ["under_review", "info_requested"]}[status]
        prev = "submitted"
        for i, s in enumerate(chain, start=1):
            why = f" {reason}" if s == "info_requested" else ""
            db.add(AuditEvent(entity="claim", entity_id=cl.id, action="status_changed",
                              detail=f"{num}: {prev} -> {s}.{why}", from_status=prev, to_status=s,
                              actor_role="claims_officer", created_at=created + timedelta(hours=10 * i)))
            prev = s
        return cl

    done = claim("CLM-DEMO-0001", "Accident damage", 12500, 60, "settled",
                 ["registration_certificate", "driving_licence", "damage_photos", "repair_estimate"], approved=12500)
    db.add(CardTransaction(customer_id=c.id, kind="claim_credit", amount=12500, description=f"Claim {done.number} payout",
                           claim_id=done.id, created_at=done.updated_at))
    claim("CLM-DEMO-0002", "Accident damage", 8200, 6, "info_requested", ["registration_certificate", "damage_photos"],
          reason="Please upload the repair estimate from the workshop.")


def seed(db: Session, today: date | None = None) -> None:
    if db.query(Customer).first():
        return
    rnd = random.Random(42)
    today = today or date.today()

    for email, name, role in STAFF:
        db.add(User(email=email, password_hash=hash_password(settings.demo_password), role=role, full_name=name))

    customers: list[Customer] = []
    for i in range(48):
        f, l = rnd.choice(FIRST), rnd.choice(LAST)
        customers.append(Customer(
            name=f"{f} {l}", email=f"{f.lower()}.{l.lower()}{i}@example.com",
            phone=f"+91 9{rnd.randint(100,999)} {rnd.randint(100000,999999)}",
            city=rnd.choice(CITIES),
            created_at=datetime.combine(today - timedelta(days=rnd.randint(30, 700)), datetime.min.time()),
        ))
    db.add_all(customers)
    db.flush()

    policies: list[Policy] = []
    n = 1
    for c in customers:
        for _ in range(rnd.choice([1, 1, 2, 3])):
            product = rnd.choices(list(PRODUCTS), [5, 2, 2, 1, 1, 1])[0]
            (p_lo, p_hi), (s_lo, s_hi), _t = PRODUCTS[product]
            start = today - timedelta(days=rnd.randint(-30, 540))
            end = start + timedelta(days=365)
            if start > today:
                status = "pending"
            elif end < today:
                status = rnd.choices(["expired", "cancelled"], [8, 1])[0]
            else:
                status = rnd.choices(["active", "cancelled"], [30, 1])[0]
            policies.append(Policy(
                number=f"ENS-{product[:3].upper()}-{today.year}-{n:04d}", customer_id=c.id, product=product,
                premium=round(rnd.uniform(p_lo, p_hi), -1), sum_insured=round(rnd.uniform(s_lo, s_hi), -3),
                start_date=start, end_date=end, status=status,
                risk_score=min(100, max(5, int(rnd.gauss(48, 20)))),
                created_at=datetime.combine(start, datetime.min.time()),
            ))
            n += 1
    db.add_all(policies)
    db.flush()

    statuses = ["submitted", "under_review", "info_requested", "approved", "rejected", "settled"]
    weights = [14, 18, 8, 14, 10, 36]
    m = 1
    for p in policies:
        if p.status in ("pending",) or rnd.random() > 0.42:
            continue
        latest = min(today, p.end_date)
        span = (latest - p.start_date).days
        if span < 5:
            continue
        inc = p.start_date + timedelta(days=rnd.randint(1, span))
        created = max(inc + timedelta(days=rnd.randint(0, 6)), p.start_date)
        created = min(created, today)
        claim_type = rnd.choice(PRODUCTS[p.product][2])
        status = rnd.choices(statuses, weights)[0]
        age = (today - created).days
        if age < 10 and status in ("settled", "rejected"):
            status = "under_review"
        amount = round(min(p.sum_insured, rnd.uniform(0.02, 0.35) * p.sum_insured), -2) or 1000
        reason = ""
        if status == "rejected":
            reason = "Demo data: documentation did not support the claimed loss."
        if status == "info_requested":
            reason = "Demo data: please supply the missing invoice."
        c = Claim(
            number=f"CLM-{today.year}-{m:04d}", policy_id=p.id, claim_type=claim_type, amount=amount,
            approved_amount=amount if status in ("approved", "settled") else None,
            incident_date=inc, description=f"Synthetic {claim_type.lower()} claim for demonstration.",
            documents="claim_form.pdf,supporting_evidence.pdf", status=status, decision_reason=reason,
            created_at=datetime.combine(created, datetime.min.time()) + timedelta(hours=rnd.randint(8, 18)),
        )
        c.updated_at = c.created_at + timedelta(days=min(age, rnd.randint(0, 12)))
        db.add(c)
        db.flush()
        db.add(AuditEvent(entity="claim", entity_id=c.id, action="created",
                          detail=f"Claim {c.number} registered (demo seed)", to_status="submitted",
                          actor_role="system", created_at=c.created_at))
        if status != "submitted":
            db.add(AuditEvent(entity="claim", entity_id=c.id, action="status_changed",
                              detail=f"{c.number}: submitted -> {status}", from_status="submitted", to_status=status,
                              actor_role="system", created_at=c.updated_at))
        if status == "settled":
            db.add(CardTransaction(customer_id=p.customer_id, kind="claim_credit", amount=amount,
                                   description=f"Claim {c.number} payout", claim_id=c.id, created_at=c.updated_at))
        m += 1

    _seed_demo_customer(db, today)
    db.commit()

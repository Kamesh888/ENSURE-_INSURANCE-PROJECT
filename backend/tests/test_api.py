import os
import tempfile
import uuid
from datetime import date, timedelta

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["UPLOAD_DIR"] = f"{_tmp}/uploads"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_DEMO_DATA"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PW = settings.demo_password
PDF = b"%PDF-1.4\n%demo\n"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(client, email, password=PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin@ensure.demo")


@pytest.fixture(scope="module")
def agent(client):
    return login(client, "agent@ensure.demo")


@pytest.fixture(scope="module")
def officer(client):
    return login(client, "claims@ensure.demo")


@pytest.fixture(scope="module")
def viewer(client):
    return login(client, "viewer@ensure.demo")


@pytest.fixture(scope="module")
def demo(client):
    return login(client, "kamesh@gmail.com")


def new_customer(client):
    email = f"user{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/register", json={"full_name": "Test Customer", "email": email,
                                                 "mobile": "9876543210", "password": "Secret123"})
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def make_quote(client, reg="TN22AB1234", model="activa-6g", year=2021, cfy=2):
    r = client.post("/api/quotes", json={"reg_number": reg, "model_key": model, "reg_year": year,
                                          "claim_free_years": cfy, "license_number": "TN0120200012345"})
    assert r.status_code == 201, r.text
    return r.json()


def buy(client, headers, quote, plan_id=None, start=None):
    plan_id = plan_id or quote["plans"][0]["plan_id"]
    return client.post("/api/me/purchases", headers=headers, json={
        "quote_id": quote["id"], "plan_id": plan_id, "start_date": (start or date.today()).isoformat()})


# ------------------------------------------------------------------ basics and auth
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["environment"] == "DEMO"


def test_staff_endpoints_require_staff_token(client, demo):
    assert client.get("/api/policies").status_code == 401
    assert client.get("/api/policies", headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert client.get("/api/policies", headers=demo).status_code == 403  # customers are not staff


def test_login_wrong_password_and_throttle(client):
    email = f"nobody{uuid.uuid4().hex[:6]}@example.com"
    for _ in range(6):
        assert client.post("/api/auth/login", json={"email": email, "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": email, "password": "wrong"}).status_code == 429


def test_register_validation_and_duplicate(client):
    bad = client.post("/api/auth/register", json={"full_name": "A", "email": "x", "mobile": "1", "password": "short"})
    assert bad.status_code == 422 and bad.json()["error"]["fields"]
    weak = client.post("/api/auth/register", json={"full_name": "Weak Pass", "email": "weak@example.com",
                                                    "mobile": "9876543210", "password": "onlyletters"})
    assert weak.status_code == 422
    body = {"full_name": "Dup User", "email": f"dup{uuid.uuid4().hex[:6]}@example.com",
            "mobile": "9876543210", "password": "Secret123"}
    assert client.post("/api/auth/register", json=body).status_code == 201
    assert client.post("/api/auth/register", json=body).status_code == 409
    me = client.get("/api/auth/me", headers=login(client, body["email"], "Secret123"))
    assert me.json()["role"] == "customer"


# ------------------------------------------------------------------ staff console
def test_seed_and_analytics_consistent(client, admin):
    a = client.get("/api/analytics", headers=admin).json()
    policies = client.get("/api/policies", params={"limit": 1000}, headers=admin).json()
    claims = client.get("/api/claims", params={"limit": 1000}, headers=admin).json()
    assert a["kpis"]["total_policies"] == len(policies) > 0
    assert a["kpis"]["claims_total"] == len(claims) > 0
    assert a["kpis"]["active_premium"] == round(sum(p["premium"] for p in policies if p["status"] == "active"))


def test_pii_masked_for_viewer(client, admin, viewer):
    full = client.get("/api/customers", headers=admin).json()[0]
    masked = client.get("/api/customers", headers=viewer).json()[0]
    assert "***" in masked["email"] and masked["email"] != full["email"]


def test_viewer_and_claims_officer_cannot_write_policies(client, viewer, officer):
    cust = {"name": "Test Person", "email": "t@example.com", "phone": "+91 99999 99999", "city": "Pune"}
    assert client.post("/api/customers", headers=viewer, json=cust).status_code == 403
    assert client.post("/api/customers", headers=officer, json=cust).status_code == 403


def test_policy_lifecycle_for_staff(client, agent, admin):
    cust = client.get("/api/customers", headers=admin).json()[0]
    today = date.today()
    body = {"customer_id": cust["id"], "product": "Travel", "premium": 2500, "sum_insured": 500000,
            "start_date": today.isoformat(), "end_date": (today + timedelta(days=365)).isoformat()}
    r = client.post("/api/policies", headers=agent, json=body)
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.post("/api/policies", headers=agent, json={**body, "end_date": today.isoformat()}).status_code == 422
    assert client.post(f"/api/policies/{pid}/renew", headers=agent).status_code == 409
    assert client.post(f"/api/policies/{pid}/status", headers=agent, json={"status": "active"}).status_code == 200
    renewed = client.post(f"/api/policies/{pid}/renew", headers=agent).json()
    old_end = date.fromisoformat(body["end_date"])
    assert renewed["end_date"] == old_end.replace(year=old_end.year + 1).isoformat()
    assert renewed["premium"] == 2500
    no_reason = client.post(f"/api/policies/{pid}/status", headers=agent, json={"status": "cancelled"})
    assert no_reason.status_code == 422
    ok = client.post(f"/api/policies/{pid}/status", headers=agent, json={"status": "cancelled", "reason": "Asked"})
    assert ok.status_code == 200
    assert client.patch(f"/api/policies/{pid}", headers=agent, json={"premium": 1}).status_code == 409


def test_staff_search_filter_and_sql_input(client, admin):
    rows = client.get("/api/policies", params={"product": "Motor", "status": "active"}, headers=admin).json()
    assert rows and all(r["product"] == "Motor" and r["status"] == "active" for r in rows)
    needle = rows[0]["number"]
    assert [r["number"] for r in client.get("/api/policies", params={"q": needle}, headers=admin).json()] == [needle]
    assert client.get("/api/policies", params={"q": "' OR 1=1 --"}, headers=admin).json() == []
    assert client.get("/api/export/claims.csv", headers=admin).text.startswith("claim_number,policy_number")
    assert client.get("/api/export/claims.csv").status_code == 401


# ------------------------------------------------------------------ quotes
def test_quote_validation_and_math(client):
    bad = client.post("/api/quotes", json={"reg_number": "BAD", "model_key": "activa-6g", "reg_year": 2020})
    assert bad.status_code == 422
    unknown = client.post("/api/quotes", json={"reg_number": "TN22AB1234", "model_key": "nope", "reg_year": 2020})
    assert unknown.status_code == 422
    future = client.post("/api/quotes", json={"reg_number": "TN22AB1234", "model_key": "activa-6g",
                                               "reg_year": date.today().year + 1})
    assert future.status_code == 422
    q = make_quote(client, reg="tn 22-ab 1234")
    assert q["vehicle"]["reg_number"] == "TN22AB1234" and len(q["plans"]) == 6
    totals = [p["total"] for p in q["plans"]]
    assert totals == sorted(totals)
    for p in q["plans"]:
        assert abs(p["net_premium"] + p["pa_cover"] + p["gst"] - p["total"]) <= 1
        assert p["own_damage"] - p["ncb_discount"] + p["third_party"] == p["net_premium"] or \
            abs(p["own_damage"] - p["ncb_discount"] + p["third_party"] - p["net_premium"]) <= 1
    # more claim-free years can only lower the price
    cheap = make_quote(client, cfy=5)
    assert cheap["plans"][0]["total"] < make_quote(client, cfy=0)["plans"][0]["total"]
    assert client.get(f"/api/quotes/{q['id']}").status_code == 200
    assert client.get(f"/api/quotes/{uuid.uuid4()}").status_code == 404


# ------------------------------------------------------------------ purchase and payment
def test_purchase_requires_login_and_server_prices(client):
    q = make_quote(client)
    assert buy(client, {}, q).status_code == 401
    h = new_customer(client)
    r = buy(client, h, q, plan_id="harbor")
    assert r.status_code == 201
    pol = r.json()["policy"]
    expected = next(p for p in q["plans"] if p["plan_id"] == "harbor")
    assert pol["premium"] == expected["total"] and pol["sum_insured"] == expected["idv"]
    assert pol["status"] == "pending" and r.json()["payment"]["amount"] == expected["total"]
    assert buy(client, h, q, plan_id="nonexistent").status_code == 404
    far = buy(client, h, make_quote(client, reg="KA01AB9999"), start=date.today() + timedelta(days=90))
    assert far.status_code == 422


def test_payment_flow_and_isolation(client):
    h, other = new_customer(client), new_customer(client)
    q = make_quote(client, reg="MH12XY4321")
    purchase = buy(client, h, q).json()
    pay_id = purchase["payment"]["id"]
    assert client.get(f"/api/me/payments/{pay_id}", headers=other).status_code == 404
    assert client.post(f"/api/me/payments/{pay_id}/confirm", headers=other, json={"method": "upi_qr"}).status_code == 404
    assert client.post(f"/api/me/payments/{pay_id}/confirm", headers=h,
                       json={"method": "netbanking", "bank": "Nope Bank"}).status_code == 422
    assert client.post(f"/api/me/payments/{pay_id}/confirm", headers=h,
                       json={"method": "ensure_card"}).status_code == 422  # no balance yet
    done = client.post(f"/api/me/payments/{pay_id}/confirm", headers=h, json={"method": "upi_qr"})
    assert done.status_code == 200 and done.json()["policy"]["status"] == "active"
    # idempotent
    assert client.post(f"/api/me/payments/{pay_id}/confirm", headers=h, json={"method": "upi_qr"}).status_code == 200
    # same bike cannot be insured twice while cover is valid
    assert buy(client, h, q).status_code == 409
    assert client.get(f"/api/me/policies/{purchase['policy']['id']}", headers=other).status_code == 404
    assert any(n["message"].endswith("is active.") for n in client.get("/api/me/notifications", headers=h).json())


def test_ensure_card_pays_premium(client, demo):
    before = client.get("/api/me/card", headers=demo).json()["balance"]
    assert before >= 12500
    q = make_quote(client, reg="TN09ZZ0001", model="splendor-plus", year=2023, cfy=0)
    purchase = buy(client, demo, q).json()
    amount = purchase["payment"]["amount"]
    r = client.post(f"/api/me/payments/{purchase['payment']['id']}/confirm", headers=demo,
                    json={"method": "ensure_card"})
    assert r.status_code == 200
    assert client.get("/api/me/card", headers=demo).json()["balance"] == pytest.approx(before - amount)


# ------------------------------------------------------------------ claims, documents, settlement
def test_customer_claim_end_to_end(client, demo, officer):
    policies = client.get("/api/me/policies", headers=demo).json()
    pol = next(p for p in policies if p["number"].endswith("-9001"))
    base = {"policy_id": pol["id"], "claim_type": "Theft", "amount": 5000,
            "incident_date": date.today().isoformat(), "description": "Bike stolen from parking"}
    assert client.post("/api/me/claims", headers=demo, json={**base, "claim_type": "Alien abduction"}).status_code == 422
    assert client.post("/api/me/claims", headers=demo, json={**base, "amount": pol["sum_insured"] + 1}).status_code == 422
    stranger = new_customer(client)
    assert client.post("/api/me/claims", headers=stranger, json=base).status_code == 404

    created = client.post("/api/me/claims", headers=demo, json=base)
    assert created.status_code == 201
    claim = created.json()
    cid = claim["id"]
    assert claim["status"] == "submitted" and claim["doc_check"]["percent"] == 0

    up = f"/api/me/claims/{cid}/documents"
    assert client.post(up, headers=demo, data={"doc_type": "bogus"}, files={"file": ("a.pdf", PDF)}).status_code == 422
    assert client.post(up, headers=demo, data={"doc_type": "fir_copy"},
                       files={"file": ("evil.exe", b"MZ\x90\x00")}).status_code == 415
    big = PDF + b"0" * (settings.max_upload_mb * 1024 * 1024 + 10)
    assert client.post(up, headers=demo, data={"doc_type": "fir_copy"}, files={"file": ("big.pdf", big)}).status_code == 413
    ok = client.post(up, headers=demo, data={"doc_type": "fir_copy"}, files={"file": ("../../fir copy.pdf", PDF)})
    assert ok.status_code == 201
    assert ok.json()["doc_check"]["provided"] == 1
    file_id = ok.json()["files"][0]["id"]
    assert ".." not in ok.json()["files"][0]["name"] and "/" not in ok.json()["files"][0]["name"]
    assert client.get(f"{up}/{file_id}", headers=demo).content == PDF
    assert client.get(f"{up}/{file_id}", headers=stranger).status_code == 404
    assert client.get(f"/api/claims/{cid}/documents/{file_id}", headers=officer).status_code == 200

    # staff decision path; customers cannot decide
    t = f"/api/claims/{cid}/transition"
    assert client.post(t, headers=demo, json={"to": "under_review"}).status_code == 403
    assert client.post(t, headers=officer, json={"to": "under_review"}).status_code == 200
    assert client.post(t, headers=officer, json={"to": "approved", "approved_amount": 99999}).status_code == 422
    balance_before = client.get("/api/me/card", headers=demo).json()["balance"]
    assert client.post(t, headers=officer, json={"to": "approved", "approved_amount": 4000}).status_code == 200
    mid = client.get(f"/api/me/claims/{cid}", headers=demo).json()
    assert mid["claim"]["progress"]["current"] == 2
    assert client.get("/api/me/card", headers=demo).json()["balance"] == balance_before  # not paid yet
    assert client.post(t, headers=officer, json={"to": "settled"}).status_code == 200
    assert client.get("/api/me/card", headers=demo).json()["balance"] == pytest.approx(balance_before + 4000)
    assert client.post(t, headers=officer, json={"to": "settled"}).status_code == 409
    final = client.get(f"/api/me/claims/{cid}", headers=demo).json()
    assert final["claim"]["progress"]["done"] is True
    assert any("Ensure Card" in e["message"] for e in final["timeline"])
    # no more uploads once the claim is closed
    assert client.post(up, headers=demo, data={"doc_type": "fir_copy"}, files={"file": ("a.pdf", PDF)}).status_code == 409


def test_info_requested_shows_action_needed(client, demo, officer):
    claims = client.get("/api/me/claims", headers=demo).json()
    c = next(c for c in claims if c["number"] == "CLM-DEMO-0002")
    assert c["progress"]["needs_action"] is True and c["decision_reason"]
    assert c["doc_check"]["percent"] < 100


def test_card_spending_limits(client, demo):
    bal = client.get("/api/me/card", headers=demo).json()["balance"]
    assert client.post("/api/me/card/pay", headers=demo, json={"merchant": "Demo Garage", "amount": bal + 1}).status_code == 422
    r = client.post("/api/me/card/pay", headers=demo, json={"merchant": "Demo Garage", "amount": 100})
    assert r.status_code == 200 and r.json()["balance"] == pytest.approx(bal - 100)
    assert client.post("/api/me/card/pay", headers=demo, json={"merchant": "X", "amount": -5}).status_code == 422


def test_contact_form_and_staff_inbox(client, admin, viewer):
    r = client.post("/api/contact", json={"name": "Visitor", "mobile": "9876543210",
                                           "email": "visitor@example.com", "message": "Hello, a question please"})
    assert r.status_code == 201
    inbox = client.get("/api/messages", headers=admin).json()
    assert inbox and inbox[0]["email"] == "visitor@example.com"
    assert client.get("/api/messages", headers=viewer).status_code == 403

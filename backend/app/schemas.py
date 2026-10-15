import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

PolicyStatus = Literal["pending", "active", "expired", "cancelled"]
ClaimStatus = Literal["submitted", "under_review", "info_requested", "approved", "rejected", "settled"]
Product = Literal["Two-Wheeler", "Motor", "Health", "Home", "Travel", "Life"]
PHONE_RE = r"^[0-9+()\-\s]+$"


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- Customers
class CustomerIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str = Field(min_length=6, max_length=32, pattern=PHONE_RE)
    city: str = Field(min_length=2, max_length=80)

    @field_validator("name", "city")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()


class CustomerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, min_length=6, max_length=32, pattern=PHONE_RE)
    city: str | None = Field(default=None, min_length=2, max_length=80)


class CustomerOut(ORM):
    id: int
    name: str
    email: str
    phone: str
    city: str
    created_at: datetime
    policy_count: int = 0
    claim_count: int = 0


# ---- Policies
class PolicyIn(BaseModel):
    customer_id: int
    product: Product
    premium: float = Field(gt=0, le=10_000_000)
    sum_insured: float = Field(gt=0, le=1_000_000_000)
    start_date: date
    end_date: date
    status: PolicyStatus = "pending"
    risk_score: int = Field(default=50, ge=0, le=100)


class PolicyPatch(BaseModel):
    premium: float | None = Field(default=None, gt=0, le=10_000_000)
    sum_insured: float | None = Field(default=None, gt=0, le=1_000_000_000)
    end_date: date | None = None
    risk_score: int | None = Field(default=None, ge=0, le=100)


class StatusChange(BaseModel):
    status: PolicyStatus
    reason: str = Field(default="", max_length=500)


class PolicyOut(ORM):
    id: int
    number: str
    customer_id: int
    customer_name: str
    product: str
    insurer: str = ""
    vehicle: str | None = None
    premium: float
    sum_insured: float
    start_date: date
    end_date: date
    status: str
    risk_score: int
    days_to_expiry: int
    claim_count: int = 0
    breakdown: dict[str, Any] | None = None


# ---- Claims
class ClaimIn(BaseModel):
    policy_id: int
    claim_type: str = Field(min_length=2, max_length=60)
    amount: float = Field(gt=0, le=1_000_000_000)
    incident_date: date
    description: str = Field(default="", max_length=2000)
    documents: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("documents")
    @classmethod
    def clean_docs(cls, v: list[str]) -> list[str]:
        out = []
        for name in v:
            name = name.strip()
            if not name or len(name) > 120 or any(c in name for c in ",/\\"):
                raise ValueError("document names must be 1-120 chars without commas or path separators")
            out.append(name)
        return out


class ClaimTransition(BaseModel):
    to: ClaimStatus
    reason: str = Field(default="", max_length=1000)
    approved_amount: float | None = Field(default=None, gt=0, le=1_000_000_000)


class DocFile(BaseModel):
    id: int
    doc_type: str
    name: str
    size: int
    uploaded_at: datetime


class ClaimOut(ORM):
    id: int
    number: str
    policy_id: int
    policy_number: str
    customer_name: str
    product: str
    claim_type: str
    amount: float
    approved_amount: float | None = None
    incident_date: date
    description: str
    documents: list[str]
    files: list[DocFile] = []
    status: str
    decision_reason: str
    created_at: datetime
    updated_at: datetime
    allowed_transitions: list[str]


class AuditOut(ORM):
    id: int
    entity: str
    entity_id: int
    action: str
    detail: str
    actor_role: str
    created_at: datetime


# ---- Auth
PASSWORD_HINT = "At least 8 characters, with a letter and a number"


class RegisterIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    mobile: str = Field(min_length=10, max_length=15, pattern=PHONE_RE)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def strong(cls, v: str) -> str:
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError(PASSWORD_HINT)
        return v

    @field_validator("full_name")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    role: str
    customer_id: int | None = None


class AuthOut(BaseModel):
    token: str
    user: UserOut


# ---- Quotes / purchase
REG_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{4}$")


class QuoteIn(BaseModel):
    reg_number: str = Field(min_length=6, max_length=16)
    model_key: str = Field(min_length=2, max_length=40)
    reg_year: int = Field(ge=2005, le=2100)
    claim_free_years: int = Field(default=0, ge=0, le=5)
    license_number: str = Field(default="", max_length=20)

    @field_validator("reg_number")
    @classmethod
    def norm_reg(cls, v: str) -> str:
        s = re.sub(r"[\s\-]", "", v).upper()
        if not REG_RE.match(s):
            raise ValueError("Enter a valid registration number, e.g. TN22D4596")
        return s


class PurchaseIn(BaseModel):
    quote_id: str = Field(min_length=36, max_length=36)
    plan_id: str = Field(min_length=2, max_length=40)
    start_date: date


class PayIn(BaseModel):
    method: Literal["upi_qr", "netbanking", "ensure_card"]
    bank: str = Field(default="", max_length=40)


class CardPayIn(BaseModel):
    merchant: str = Field(min_length=2, max_length=80)
    amount: float = Field(gt=0, le=1_000_000)


class CustomerClaimIn(BaseModel):
    policy_id: int
    claim_type: str = Field(min_length=2, max_length=60)
    amount: float = Field(gt=0, le=1_000_000_000)
    incident_date: date
    description: str = Field(default="", max_length=2000)


class ContactIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    mobile: str = Field(min_length=6, max_length=32, pattern=PHONE_RE)
    email: EmailStr
    message: str = Field(min_length=5, max_length=2000)

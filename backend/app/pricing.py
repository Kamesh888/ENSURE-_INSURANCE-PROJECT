"""Illustrative two-wheeler pricing for the DEMO.

This is NOT an actuarial tariff and not a quote from any real insurer. The structure mirrors how Indian
motor premiums are presented (IDV, own-damage, third-party, NCB, GST, owner-driver PA cover) so the
journey is realistic, but every figure below is a demo assumption and the partner insurers are fictional.
"""
from dataclasses import dataclass
from datetime import date

GST_RATE = 0.18

# key -> (display name, illustrative ex-showroom price in INR, engine class)
MODELS: dict[str, tuple[str, int, str]] = {
    "splendor-plus": ("Hero Splendor Plus", 76000, "75-150"),
    "activa-6g": ("Honda Activa 6G", 82000, "75-150"),
    "jupiter": ("TVS Jupiter", 78000, "75-150"),
    "access-125": ("Suzuki Access 125", 84000, "75-150"),
    "swish-125": ("Suzuki Swish 125", 72000, "75-150"),
    "pulsar-150": ("Bajaj Pulsar 150", 112000, "75-150"),
    "fz-s": ("Yamaha FZ-S", 120000, "75-150"),
    "apache-160": ("TVS Apache RTR 160", 121000, "150-350"),
    "classic-350": ("Royal Enfield Classic 350", 195000, "150-350"),
    "duke-390": ("KTM 390 Duke", 310000, "350+"),
}

# Illustrative own-damage rate (share of IDV) and third-party premium (INR) by engine class.
OD_RATE = {"75-150": 0.01708, "150-350": 0.01793, "350+": 0.01879}
TP_PREMIUM = {"75-150": 1366, "150-350": 2804, "350+": 8001}

# Claim-free years -> no-claim-bonus discount on own-damage premium.
NCB = {0: 0.0, 1: 0.20, 2: 0.25, 3: 0.35, 4: 0.45, 5: 0.50}


@dataclass(frozen=True)
class Insurer:
    id: str
    name: str
    idv_factor: float  # insurer-declared IDV relative to the reference IDV
    rate_factor: float  # relative own-damage pricing
    pa_cover: int  # owner-driver personal accident cover price (INR, before GST)
    claim_promise: str


INSURERS = [
    Insurer("northstar", "Northstar General", 1.00, 0.96, 330, "Cashless repair network in 400+ cities"),
    Insurer("meridian", "Meridian Assurance", 1.03, 1.00, 345, "Pickup and drop for repairs"),
    Insurer("harbor", "Harbor Mutual", 0.97, 0.93, 351, "Competitive own-damage rate"),
    Insurer("keystone", "Keystone Insurance", 1.06, 1.04, 330, "Highest declared IDV"),
    Insurer("crestline", "Crestline General", 0.94, 0.90, 391, "Zero paperwork for small claims"),
    Insurer("aurora", "Aurora Insurance", 1.01, 0.98, 353, "24x7 roadside assistance"),
]
INSURER_BY_ID = {i.id: i for i in INSURERS}


def vehicle_age_years(reg_year: int, today: date | None = None) -> float:
    today = today or date.today()
    return max(0.0, (today - date(reg_year, 7, 1)).days / 365.25)


def depreciation(age: float) -> float:
    """Depreciation applied to ex-showroom price to derive the reference IDV."""
    if age <= 0.5:
        return 0.05
    if age <= 1:
        return 0.15
    if age <= 2:
        return 0.20
    if age <= 3:
        return 0.30
    if age <= 4:
        return 0.40
    if age <= 5:
        return 0.50
    return min(0.70, 0.50 + 0.05 * (age - 5))


def reference_idv(model_key: str, reg_year: int, today: date | None = None) -> int:
    _name, price, _cls = MODELS[model_key]
    return int(round(price * (1 - depreciation(vehicle_age_years(reg_year, today))), -1))


def price_plan(model_key: str, reg_year: int, claim_free_years: int, insurer: Insurer,
               today: date | None = None) -> dict:
    _name, _price, cls = MODELS[model_key]
    idv = int(round(reference_idv(model_key, reg_year, today) * insurer.idv_factor, -1))
    od = idv * OD_RATE[cls] * insurer.rate_factor
    ncb_pct = NCB[claim_free_years]
    ncb = od * ncb_pct
    tp = TP_PREMIUM[cls]
    net = od - ncb + tp
    gst = (net + insurer.pa_cover) * GST_RATE
    total = net + insurer.pa_cover + gst
    r = lambda v: int(round(v))  # noqa: E731
    return {
        "plan_id": insurer.id,
        "insurer": insurer.name,
        "claim_promise": insurer.claim_promise,
        "idv": idv,
        "own_damage": r(od),
        "ncb_percent": int(ncb_pct * 100),
        "ncb_discount": r(ncb),
        "third_party": tp,
        "net_premium": r(net),
        "pa_cover": insurer.pa_cover,
        "gst": r(gst),
        "total": r(total),
    }


def all_plans(model_key: str, reg_year: int, claim_free_years: int, today: date | None = None) -> list[dict]:
    plans = [price_plan(model_key, reg_year, claim_free_years, i, today) for i in INSURERS]
    return sorted(plans, key=lambda p: p["total"])

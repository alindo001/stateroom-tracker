import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import glob
import json
import os
import threading
import time
from curl_cffi import requests

DEFAULT_NCL_EMB_PORTS = "MIA,PCV,JAX,TPA,GAL,NYC,SJU,MSY,PHL,BOS,LAX,SAN,SEA,HNL"

NCL_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.ncl.com/vacations",
}

PORT_CODE_NORMALIZATION = {
    "PCV": "PCN",  # Normalize Orlando (Port Canaveral) to PCN to match Royal Caribbean & Florida presets
}

CLEAN_PORT_NAMES = {
    "MIA": "Miami",
    "PCN": "Port Canaveral (Orlando)",
    "PCV": "Port Canaveral (Orlando)",
    "JAX": "Jacksonville",
    "TPA": "Tampa",
    "GAL": "Galveston",
    "NYC": "New York",
    "SJU": "San Juan",
    "MSY": "New Orleans",
    "PHL": "Philadelphia",
    "BOS": "Boston",
    "LAX": "Los Angeles",
    "SAN": "San Diego",
    "SEA": "Seattle",
    "HNL": "Honolulu (Oahu)",
    "FLL": "Fort Lauderdale",
    "BCN": "Barcelona",
    "ROM": "Rome (Civitavecchia)",
}

NCL_SHIP_CLASSES = {
    "AQUA": "Prima Plus",
    "LUNA": "Prima Plus",
    "AURA": "Prima Plus",
    "BELLA": "Prima Plus",
    "PRIMA": "Prima",
    "VIVA": "Prima",
    "ENCORE": "Breakaway Plus",
    "BLISS": "Breakaway Plus",
    "JOY": "Breakaway Plus",
    "ESCAPE": "Breakaway Plus",
    "BREAKAWAY": "Breakaway",
    "GETAWAY": "Breakaway",
    "EPIC": "Epic",
    "JEWEL": "Jewel",
    "PEARL": "Jewel",
    "GEM": "Jewel",
    "JADE": "Jewel",
    "DAWN": "Dawn",
    "STAR": "Dawn",
    "SUN": "Sun",
    "SKY": "Sun",
    "SPIRIT": "Spirit",
    "PRIDE_AMER": "Pride",
}

NCL_SUITE_META = {
    "HAVEN": {
        "tier": "Haven",
        "label": "The Haven by Norwegian",
        "room_name": "The Haven Suite",
        "other_rooms": "The Haven Courtyard Penthouse • The Haven 2-Bedroom Family Villa • The Haven Owner's Suite",
        "amenities": "24-Hr Certified Butler & Concierge • Private Haven Courtyard, Pool & Sundeck • Private Haven Restaurant & Lounge • Priority Boarding & Tenders",
        "royal_type": "HAVEN",
        "guar_code": "HX",
        "guar_name": "Sail Away Haven Suite (HX)",
    },
    "SUITE": {
        "tier": "Suite",
        "label": "NCL Suite",
        "room_name": "Penthouse / Family Suite",
        "other_rooms": "Aft-Facing Penthouse • Forward-Facing Penthouse • 2-Bedroom Family Suite",
        "amenities": "Dedicated Butler & Concierge Service • VIP Breakfast & Lunch Venue • Priority Boarding & Dining Reservations • Sparkling Wine & Treats",
        "royal_type": "SUITE",
        "guar_code": "SX",
        "guar_name": "Sail Away Suite (SX)",
    },
    "MINISUITE": {
        "tier": "ClubBalcony",
        "label": "Club Balcony Suite",
        "room_name": "Club Balcony Suite",
        "other_rooms": "Family Club Balcony Suite (M4) • Club Balcony Suite with Large Balcony (M6)",
        "amenities": "Expanded Bath & Oversized Shower • Earlier Dining & Entertainment Booking • Complimentary Laundry/Pressing Bag • Treats Delivered to Cabin",
        "royal_type": "MINISUITE",
        "guar_code": "MX",
        "guar_name": "Sail Away Club Balcony Suite (MX)",
    },
}

NCL_STATEROOM_META = {
    "INSIDE": {
        "tier": "Interior",
        "label": "Inside & Studio Stateroom",
        "room_name": "Inside Stateroom",
        "other_rooms": "Family Inside (I4) • Mid-Ship Inside (IA/IB) • Solo Inside (IT) & Solo Studio (T1)",
        "amenities": "More at Sea / Free at Sea Eligible • Two lower beds converting to Queen • 24-hr room service",
        "royal_type": "INSIDE",
        "guar_code": "IX",
        "guar_name": "Sail Away Inside (IX)",
    },
    "OCEANVIEW": {
        "tier": "OceanView",
        "label": "Oceanview Stateroom",
        "room_name": "Oceanview Picture Window",
        "other_rooms": "Family Oceanview (O4/O5, Sleeps up to 5) • Large Picture Window Oceanview (OA/OB)",
        "amenities": "More at Sea / Free at Sea Eligible • Ocean-facing picture window or porthole • Sitting area",
        "royal_type": "OCEANVIEW",
        "guar_code": "OX",
        "guar_name": "Sail Away Oceanview (OX)",
    },
    "BALCONY": {
        "tier": "Balcony",
        "label": "Balcony Stateroom",
        "room_name": "Balcony Stateroom",
        "other_rooms": "Family Balcony (B4) • Aft-Facing Balcony (B1) • Large Balcony (B6)",
        "amenities": "More at Sea / Free at Sea Eligible • Private step-out balcony with seating • Floor-to-ceiling glass doors",
        "royal_type": "BALCONY",
        "guar_code": "BX",
        "guar_name": "Sail Away Balcony (BX)",
    },
}

SUITE_GUARANTEE_AMENITIES = (
    "Sail Away Rate (Unassigned Guarantee) • NCL assigns cabin prior to sailing (excludes Free at Sea / More at Sea perks)"
)
ROOM_GUARANTEE_AMENITIES = (
    "Sail Away Rate (Unassigned Guarantee) • NCL assigns cabin prior to sailing (excludes Free at Sea / More at Sea perks)"
)

_thread_local = threading.local()
_rate_lock = threading.Lock()
_next_allowed_ts = 0.0
_min_request_interval = 0.55  # Default ~1.8 req/sec per IP so Akamai burst limit is never triggered


def configure_rate_limit(rps: float):
    global _min_request_interval
    _min_request_interval = 1.0 / max(0.2, float(rps))


def throttle_request():
    global _next_allowed_ts
    with _rate_lock:
        now = time.monotonic()
        if now < _next_allowed_ts:
            wait = _next_allowed_ts - now
            _next_allowed_ts = _next_allowed_ts + _min_request_interval
        else:
            wait = 0.0
            _next_allowed_ts = now + _min_request_interval
    if wait > 0:
        time.sleep(wait)


def get_thread_session(force_new: bool = False) -> requests.Session:
    sess = getattr(_thread_local, "session", None)
    req_cnt = getattr(_thread_local, "req_cnt", 0)
    if sess is None or force_new or req_cnt >= 45:
        sess = requests.Session(impersonate="chrome")
        try:
            throttle_request()
            sess.get("https://www.ncl.com/vacations", headers={"Accept-Language": "en-US,en;q=0.9"}, timeout=15)
        except Exception:
            pass
        _thread_local.session = sess
        _thread_local.req_cnt = 0
    _thread_local.req_cnt = getattr(_thread_local, "req_cnt", 0) + 1
    return sess


def request_json(method: str, url: str, json_body: dict | None = None, timeout: int = 25, retries: int = 3):
    for attempt in range(retries):
        throttle_request()
        sess = get_thread_session(force_new=(attempt > 0))
        try:
            if method == "POST":
                resp = sess.post(url, headers=NCL_HEADERS, json=json_body, timeout=timeout)
            else:
                resp = sess.get(url, headers=NCL_HEADERS, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (403, 429, 500, 502, 503, 504):
                time.sleep(2.5 * (attempt + 1))
                continue
            return None
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return None


def clean_port(raw_code: str, raw_title: str) -> tuple[str, str]:
    raw_up = (raw_code or "").strip().upper()
    norm_code = PORT_CODE_NORMALIZATION.get(raw_up, raw_up)
    if norm_code in CLEAN_PORT_NAMES:
        return norm_code, CLEAN_PORT_NAMES[norm_code]
    title = (raw_title or norm_code or "Unknown Port").strip()
    if "," in title:
        title = title.split(",")[0].strip()
    return norm_code, title


def resolve_ship_class(ship_code: str, dynamic_cache: dict[str, str]) -> str:
    sc = (ship_code or "").strip().upper()
    if sc in NCL_SHIP_CLASSES:
        return NCL_SHIP_CLASSES[sc]
    if sc in dynamic_cache:
        return dynamic_cache[sc]
    data = request_json("GET", f"https://www.ncl.com/api/vacation-builder/ships/{sc}", timeout=12, retries=2)
    cls_raw = (data or {}).get("shipClass") or ""
    cls_clean = cls_raw.replace(" Class", "").strip() if cls_raw else "Other"
    dynamic_cache[sc] = cls_clean or "Other"
    return dynamic_cache[sc]


def fetch_all_ncl_itineraries(emb_ports: str) -> list[dict]:
    all_itins = []
    seen_codes = set()
    offset = 0
    limit = 50
    while True:
        url = (
            f"https://www.ncl.com/api/v2/vacations/search"
            f"?bundles=cruise&embPorts={emb_ports}&limit={limit}&offset={offset}"
        )
        data = request_json("GET", url, timeout=25, retries=3)
        if not data:
            break
        batch = data.get("itineraries") or []
        if not batch:
            break
        for it in batch:
            code = it.get("code")
            if code and code not in seen_codes:
                seen_codes.add(code)
                all_itins.append(it)
        total = int(data.get("total") or 0)
        offset += limit
        if offset >= total:
            break
    return all_itins


def has_ksf_offer(row: dict) -> bool:
    for grp in row.get("offerGroups") or []:
        for off in grp.get("offers") or []:
            if off.get("code") == "Kids-Sail-Free-Offer":
                return True
    return False


def is_peak_holiday_window(sail_date: str) -> bool:
    if not sail_date or len(sail_date) < 10:
        return False
    md = sail_date[5:10]
    if md >= "12-18" or md <= "01-03":
        return True
    if "11-20" <= md <= "11-28":
        return True
    if "03-14" <= md <= "04-12":
        return True
    if "06-28" <= md <= "07-10":
        return True
    return False


def sample_tax_pp_via_price_summary(package_id: str, stateroom_type: str) -> float | None:
    cat_candidates = {
        "BALCONY": ["BA", "BB", "BF"],
        "INSIDE": ["IA", "IB", "IF"],
        "OCEANVIEW": ["OA", "OB", "OF"],
        "MINISUITE": ["MA", "MB", "MX"],
        "SUITE": ["SA", "SC", "SF"],
        "HAVEN": ["HF", "HG", "HJ"],
    }.get(stateroom_type, ["BA", "IA"])

    for cat_code in cat_candidates[:2]:
        body = {
            "packageId": str(package_id),
            "userFareCodes": [],
            "stateroomFilters": [
                {
                    "id": "0",
                    "mainCabin": True,
                    "vouchers": [],
                    "numberOfGuests": 2,
                    "stateroomTypeCode": stateroom_type,
                    "pricedCategoryCode": cat_code,
                    "fareCodes": ["EASYFARE"],
                    "guests": [
                        {"id": "0", "reservationOwner": True},
                        {"id": "1", "reservationOwner": False},
                    ],
                }
            ],
        }
        res = request_json(
            "POST",
            "https://www.ncl.com/api/vacation-builder/price-summary",
            json_body=body,
            timeout=15,
            retries=2,
        )
        if not res:
            continue
        for grp in res.get("priceGroups") or []:
            if grp.get("code") == "TAXES-AND-FEES" and grp.get("price") is not None:
                tax_total_2 = float(grp["price"])
                if 60.0 <= tax_total_2 <= 1400.0:
                    return round(tax_total_2 / 2.0, 2)
    return None


def compute_per_sailing_ksf_tax_pp(m2: dict[str, dict], m4: dict[str, dict]) -> float | None:
    """
    On any NCL sailing with Kids-Sail-Free-Offer, 3rd & 4th guests in Inside/Oceanview/Balcony/ClubBalcony
    pay $0 cruise fare and ONLY port taxes & fees, so (4 * fas4 - 2 * fas2) / 2 gives the exact
    per-person port taxes & fees for that specific sailing date!
    """
    candidates = []
    for st in ("INSIDE", "OCEANVIEW", "BALCONY", "MINISUITE"):
        r2 = m2.get(st)
        r4 = m4.get(st)
        if not (r2 and r4):
            continue
        if not (has_ksf_offer(r2) or has_ksf_offer(r4)):
            continue
        p2_fas = r2.get("fasPrice")
        p4_fas = r4.get("fasPrice")
        if p2_fas and p4_fas:
            cand = round((4.0 * float(p4_fas) - 2.0 * float(p2_fas)) / 2.0, 2)
            if 50.0 <= cand <= 650.0:
                candidates.append(cand)
    if candidates:
        return min(candidates)
    return None


def fetch_itinerary_sailings_pair(itin: dict) -> dict | None:
    code = itin.get("code")
    if not code:
        return None
    d2 = request_json("GET", f"https://www.ncl.com/api/vacations/sailings/{code}?guests=2", timeout=25, retries=3)
    d4 = request_json("GET", f"https://www.ncl.com/api/vacations/sailings/{code}?guests=4", timeout=25, retries=3)
    if not d2 and not d4:
        return None

    rows2 = (d2 or {}).get("pricingStateRooms") or []
    rows4 = (d4 or {}).get("pricingStateRooms") or []
    vb_code = (d2 or d4 or {}).get("itineraryCode") or code.split("-NIC-")[0]

    by_pkg_2: dict[str, dict[str, dict]] = {}
    by_pkg_4: dict[str, dict[str, dict]] = {}
    pkg_dates: dict[str, str] = {}
    pkg_sail_ids: dict[str, str] = {}

    for r in rows2:
        pkg = str(r.get("packageId") or "")
        st = (r.get("stateroomType") or "").upper()
        if not pkg or not st:
            continue
        dt = (r.get("sailStartDate") or r.get("vacationStartDate") or "")[:10]
        if dt:
            pkg_dates[pkg] = dt
        if r.get("sailId"):
            pkg_sail_ids[pkg] = str(r["sailId"])
        if r.get("status") == "AVAILABLE":
            by_pkg_2.setdefault(pkg, {})[st] = r

    for r in rows4:
        pkg = str(r.get("packageId") or "")
        st = (r.get("stateroomType") or "").upper()
        if not pkg or not st:
            continue
        dt = (r.get("sailStartDate") or r.get("vacationStartDate") or "")[:10]
        if dt and pkg not in pkg_dates:
            pkg_dates[pkg] = dt
        if r.get("sailId") and pkg not in pkg_sail_ids:
            pkg_sail_ids[pkg] = str(r["sailId"])
        if r.get("status") == "AVAILABLE":
            by_pkg_4.setdefault(pkg, {})[st] = r

    per_pkg_tax: dict[str, float] = {}
    ksf_tax_samples = []
    for pkg in pkg_dates:
        s_tax = compute_per_sailing_ksf_tax_pp(by_pkg_2.get(pkg) or {}, by_pkg_4.get(pkg) or {})
        if s_tax is not None:
            per_pkg_tax[pkg] = s_tax
            ksf_tax_samples.append(s_tax)

    itin_tax_pp = None
    if ksf_tax_samples:
        ksf_tax_samples.sort()
        itin_tax_pp = ksf_tax_samples[len(ksf_tax_samples) // 2]
    else:
        for pkg in sorted(pkg_dates.keys()):
            m2 = by_pkg_2.get(pkg) or {}
            for st in ("BALCONY", "INSIDE", "OCEANVIEW", "MINISUITE", "SUITE", "HAVEN"):
                if st in m2 and m2[st].get("fasPrice"):
                    itin_tax_pp = sample_tax_pp_via_price_summary(pkg, st)
                    break
            if itin_tax_pp is not None:
                break

    nights = int(((itin.get("duration") or {}).get("days")) or 4)
    if itin_tax_pp is None:
        itin_tax_pp = round(min(320.0, max(95.0, nights * 34.0)), 2)

    return {
        "itin": itin,
        "code": code,
        "vb_code": vb_code,
        "tax_pp": itin_tax_pp,
        "per_pkg_tax": per_pkg_tax,
        "pkg_dates": pkg_dates,
        "pkg_sail_ids": pkg_sail_ids,
        "by_pkg_2": by_pkg_2,
        "by_pkg_4": by_pkg_4,
    }


def make_price_entry(price_pp: float | int | None, base_strike_pp: float | int | None, tax_pp: float, guests: int) -> dict | None:
    if price_pp is None:
        return None
    total_pp = round(float(price_pp), 2)
    if total_pp <= 0:
        return None
    cabin_total = round(total_pp * guests, 2)
    eff_tax_pp = min(total_pp * 0.85, float(tax_pp))
    cabin_taxes = round(eff_tax_pp * guests, 2)
    cabin_base = round(max(0.0, cabin_total - cabin_taxes), 2)
    base_pp = round(cabin_base / guests, 2)
    strike_pp = float(base_strike_pp) if base_strike_pp is not None else total_pp
    discount_pp = max(0.0, round(strike_pp - total_pp, 2))
    cabin_discount = round(discount_pp * guests, 2)
    return {
        "guests": guests,
        "base_pp": base_pp,
        "total_pp": total_pp,
        "cabin_base": cabin_base,
        "cabin_taxes": cabin_taxes,
        "cabin_total": cabin_total,
        "cabin_discount": cabin_discount,
    }


def align_ksf_quad_base(p2: dict | None, p4: dict | None, is_ksf: bool) -> dict | None:
    if not is_ksf or not p2 or not p4:
        return p4
    if abs(p4["cabin_base"] - p2["cabin_base"]) <= 16.0 and p4["cabin_total"] >= p2["cabin_base"]:
        aligned_base = p2["cabin_base"]
        aligned_taxes = round(p4["cabin_total"] - aligned_base, 2)
        if aligned_taxes >= 0:
            p4 = dict(p4)
            p4["cabin_base"] = aligned_base
            p4["cabin_taxes"] = aligned_taxes
            p4["base_pp"] = round(aligned_base / 4.0, 2)
    return p4


def finalize_sailing_record(
    sail_date: str,
    itin_code: str,
    vb_code: str,
    pkg_id: str,
    sail_id: str,
    meta: dict,
    tiers_dict: dict,
    available_tiers: list[str],
    has_ksf_flag: bool,
    default_ncl_type: str,
) -> dict | None:
    if not available_tiers:
        return None

    non_guar = [t for t in available_tiers if t != "Guarantee"]
    pool_for_lead = non_guar if non_guar else available_tiers

    def tier_sort_key(t_key: str) -> float:
        t_obj = tiers_dict[t_key]
        ref = t_obj.get("pricing_quad") or t_obj.get("pricing_2a")
        return ref["cabin_total"] if ref else 9999999.0

    lead_tier = sorted(pool_for_lead, key=tier_sort_key)[0]
    lead_obj = tiers_dict[lead_tier]
    ref_2a = lead_obj.get("pricing_2a")
    ref_quad = lead_obj.get("pricing_quad")
    ref_main = ref_quad or ref_2a
    if not ref_main:
        return None

    nights = meta["nights"]
    cabin_base = ref_main["cabin_base"]
    cabin_taxes = ref_main["cabin_taxes"]
    cabin_total = ref_main["cabin_total"]
    cabin_discount = ref_main["cabin_discount"]
    base_pp = ref_main["base_pp"]
    total_pp = ref_main["total_pp"]
    price_per_night = round(cabin_total / nights, 2) if nights > 0 else cabin_total

    is_ksf_promo = False
    is_holiday_rate = False
    extra_guest_fare_pp = 0.0

    # Check across assigned tiers if ANY tier has $0 3rd/4th guest fare (KSF active on sailing)
    any_tier_zero_extra = False
    lead_extra_pp = None
    for t_k in pool_for_lead:
        tobj = tiers_dict[t_k]
        tp2 = tobj.get("pricing_2a")
        tp4 = tobj.get("pricing_quad")
        if tp2 and tp4:
            diff = max(0.0, round(tp4["cabin_base"] - tp2["cabin_base"], 2))
            epp = round(diff / 2.0, 2)
            if t_k == lead_tier:
                lead_extra_pp = epp
            if epp <= 10.0:
                any_tier_zero_extra = True

    if lead_extra_pp is not None:
        extra_guest_fare_pp = 0.0 if lead_extra_pp <= 10.0 else lead_extra_pp
        if any_tier_zero_extra or (has_ksf_flag and lead_extra_pp <= 15.0):
            is_ksf_promo = True
            if lead_extra_pp <= 15.0:
                extra_guest_fare_pp = 0.0
        else:
            is_holiday_rate = True
    else:
        if any_tier_zero_extra or has_ksf_flag:
            is_ksf_promo = True
        elif is_peak_holiday_window(sail_date):
            is_holiday_rate = True

    if is_ksf_promo and extra_guest_fare_pp == 0.0:
        rate_tag = f"PROMO: Kids/3rd&4th Free (-${cabin_discount:.0f} off)"
    elif is_ksf_promo and extra_guest_fare_pp > 0:
        rate_tag = f"PROMO: Kids/3rd&4th Free on Select Tiers (-${cabin_discount:.0f} off)"
    elif is_holiday_rate:
        if extra_guest_fare_pp > 0:
            rate_tag = f"HOLIDAY/BLACKOUT: 3rd/4th +${extra_guest_fare_pp:.2f}/ea (-${cabin_discount:.0f} off)"
        else:
            rate_tag = f"HOLIDAY/BLACKOUT (-${cabin_discount:.0f} off)"
    else:
        rate_tag = "STANDARD"

    ncl_type = lead_obj.get("royal_type") or default_ncl_type
    booking_link = (
        f"https://www.ncl.com/vacation-builder"
        f"?itineraryCode={vb_code}&packageId={pkg_id}&guests={ref_main['guests']}&stateroomTypeCode={ncl_type}"
    )

    return {
        "cruise_line": "NCL",
        "date": sail_date,
        "itinerary_code": itin_code,
        "vb_itinerary_code": vb_code,
        "package_code": pkg_id,
        "sail_id": sail_id,
        "tax_pp": round(cabin_taxes / max(1, ref_main["guests"]), 2),
        "booking_link": booking_link,
        "itinerary_name": meta["itinerary_name"],
        "ship": meta["ship"],
        "ship_code": meta["ship_code"],
        "ship_class": meta["ship_class"],
        "nights": nights,
        "port": meta["port"],
        "port_code": meta["port_code"],
        "category": lead_obj["label"],
        "suite_tier": lead_tier,
        "available_tiers": available_tiers,
        "tiers": tiers_dict,
        "base_pp": base_pp,
        "total_pp": total_pp,
        "cabin_base": cabin_base,
        "cabin_taxes": cabin_taxes,
        "cabin_total": cabin_total,
        "cabin_discount": cabin_discount,
        "price_per_night": price_per_night,
        "is_ksf_promo": is_ksf_promo,
        "is_holiday_rate": is_holiday_rate,
        "extra_guest_fare_pp": extra_guest_fare_pp,
        "rate_tag": rate_tag,
        "pricing_2a": ref_2a,
        "pricing_quad": ref_quad,
    }


def build_itineraries_summary(deals: list[dict]) -> list[dict]:
    itins_map: dict[str, dict] = {}
    for d in deals:
        code = d["itinerary_code"]
        if code not in itins_map:
            itins_map[code] = {
                "itinerary_code": code,
                "vb_itinerary_code": d.get("vb_itinerary_code", code),
                "itinerary_name": d["itinerary_name"],
                "ship": d["ship"],
                "ship_code": d["ship_code"],
                "ship_class": d["ship_class"],
                "nights": d["nights"],
                "port": d["port"],
                "port_code": d["port_code"],
                "sailing_count": 0,
                "promo_count": 0,
                "lowest_cabin_total": d["cabin_total"],
                "lowest_total_pp": d["total_pp"],
                "lowest_sail_date": d["date"],
                "sail_dates": [],
            }
        entry = itins_map[code]
        entry["sailing_count"] += 1
        if d["is_ksf_promo"]:
            entry["promo_count"] += 1
        entry["sail_dates"].append(d["date"])
        if d["cabin_total"] < entry["lowest_cabin_total"]:
            entry["lowest_cabin_total"] = d["cabin_total"]
            entry["lowest_total_pp"] = d["total_pp"]
            entry["lowest_sail_date"] = d["date"]

    itins_list = list(itins_map.values())
    for it in itins_list:
        it["sail_dates"].sort()
    itins_list.sort(key=lambda x: x["lowest_cabin_total"])
    return itins_list


def save_ncl_catalog_payload(
    output_path: str,
    mode: str,
    norm_ports_str: str,
    scanned_itins_count: int,
    deals: list[dict],
):
    deals.sort(key=lambda x: x["cabin_total"])
    itins = build_itineraries_summary(deals)
    now_iso = datetime.now(timezone.utc).isoformat()
    summary = {
        "scanned_itineraries": max(scanned_itins_count, len(itins)),
        "itineraries_with_suites": len(itins),
        "total_suite_sailings": len(deals),
        "promo_sailings": sum(1 for d in deals if d["is_ksf_promo"]),
        "holiday_sailings": sum(1 for d in deals if d["is_holiday_rate"]),
    }
    if mode == "ncl-suites":
        summary["haven_sailings"] = sum(1 for d in deals if "Haven" in d.get("available_tiers", []))

    payload = {
        "updated_at": now_iso,
        "cruise_line": "NCL",
        "query": {
            "adults": 2,
            "children": 2,
            "total_guests": 4,
            "ports": norm_ports_str,
            "mode": mode,
        },
        "summary": summary,
        "itineraries": itins,
        "sailings": deals,
    }
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    return payload


def build_ncl_baseline_datasets(
    emb_ports: str = DEFAULT_NCL_EMB_PORTS,
    output_suites_json: str = "data/ncl_suites.json",
    output_staterooms_json: str = "data/ncl_staterooms.json",
    workers: int = 2,
    shard_index: int = 0,
    shard_count: int = 1,
    load_itineraries_path: str | None = None,
    save_itineraries_path: str | None = None,
    discover_only: bool = False,
):
    t0 = time.time()
    if load_itineraries_path and os.path.exists(load_itineraries_path):
        itineraries_raw = json.load(open(load_itineraries_path, encoding="utf-8"))
        print(f"Loaded {len(itineraries_raw)} pre-discovered NCL itineraries from {load_itineraries_path}.")
    else:
        print(f"Discovering NCL cruise-only itineraries across embarkation ports: {emb_ports}...")
        itineraries_raw = fetch_all_ncl_itineraries(emb_ports)
        print(f"Discovered {len(itineraries_raw)} unique NCL itineraries in {time.time() - t0:.1f}s.")

    if save_itineraries_path and itineraries_raw:
        os.makedirs(os.path.dirname(save_itineraries_path) or ".", exist_ok=True)
        with open(save_itineraries_path, "w", encoding="utf-8") as f:
            json.dump(itineraries_raw, f, separators=(",", ":"))
        print(f"Saved {len(itineraries_raw)} raw NCL itineraries to {save_itineraries_path}.")

    if discover_only:
        return

    if not itineraries_raw:
        print("Warning: 0 NCL itineraries returned; keeping existing JSON files.")
        return

    dynamic_ship_cache: dict[str, str] = {}
    itin_meta_map: dict[str, dict] = {}
    for it in itineraries_raw:
        code = it.get("code")
        if not code:
            continue
        ship_obj = it.get("ship") or {}
        ship_code = (ship_obj.get("code") or "").upper()
        ship_name = ship_obj.get("title") or ship_code or "Unknown Ship"
        ship_class = resolve_ship_class(ship_code, dynamic_ship_cache)
        emb_obj = it.get("embarkationPort") or {}
        port_code, port_name = clean_port(emb_obj.get("code") or "", emb_obj.get("title") or "")
        nights = int(((it.get("duration") or {}).get("days")) or 4)
        title_obj = it.get("title")
        if isinstance(title_obj, str):
            itin_name = title_obj.strip()
        elif isinstance(title_obj, dict):
            itin_name = (title_obj.get("fullTitle") or title_obj.get("shortTitle") or "").strip()
        else:
            itin_name = ""
        if not itin_name:
            itin_name = f"{nights}-Day Cruise from {port_name}"

        itin_meta_map[code] = {
            "itinerary_code": code,
            "itinerary_name": itin_name,
            "ship": ship_name,
            "ship_code": ship_code,
            "ship_class": ship_class,
            "nights": nights,
            "port": port_name,
            "port_code": port_code,
        }

    shard_itins = [it for idx, it in enumerate(itineraries_raw) if idx % shard_count == shard_index]
    print(
        f"Fetching 2-Guest & 4-Guest sailing pricing for shard {shard_index}/{shard_count} "
        f"({len(shard_itins)} of {len(itineraries_raw)} itineraries, {workers} workers)..."
    )
    fetched_pairs = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(fetch_itinerary_sailings_pair, it) for it in shard_itins]
        done_cnt = 0
        for fut in as_completed(futs):
            done_cnt += 1
            res = fut.result()
            if res:
                fetched_pairs.append(res)
            if done_cnt % 15 == 0 or done_cnt == len(futs):
                print(f"  [{done_cnt}/{len(futs)}] shard itineraries fetched ({len(fetched_pairs)} ok, {time.time() - t0:.1f}s)...")

    suite_deals = []
    room_deals = []

    for item in fetched_pairs:
        itin_code = item["code"]
        vb_code = item["vb_code"]
        itin_tax_pp = item["tax_pp"]
        per_pkg_tax = item.get("per_pkg_tax") or {}
        meta = itin_meta_map.get(itin_code)
        if not meta:
            continue

        for pkg_id, sail_date in sorted(item["pkg_dates"].items(), key=lambda kv: kv[1]):
            if not sail_date:
                continue
            sail_id = item["pkg_sail_ids"].get(pkg_id, "")
            m2 = item["by_pkg_2"].get(pkg_id) or {}
            m4 = item["by_pkg_4"].get(pkg_id) or {}
            if not m2 and not m4:
                continue

            tax_pp = per_pkg_tax.get(pkg_id, itin_tax_pp)
            any_ksf = any(has_ksf_offer(r) for r in m2.values()) or any(has_ksf_offer(r) for r in m4.values())

            # --- 1. Build NCL Haven & Suites record ---
            suite_tiers = {}
            suite_avail = []
            suite_guar_rooms = []

            for ncl_cat in ("HAVEN", "SUITE", "MINISUITE"):
                r2 = m2.get(ncl_cat)
                r4 = m4.get(ncl_cat)
                s_meta = NCL_SUITE_META[ncl_cat]
                t_name = s_meta["tier"]

                fas2 = (r2 or {}).get("fasPrice")
                fas4 = (r4 or {}).get("fasPrice")
                if fas2 is None and r2 and r2.get("xcatPrice") is None:
                    fas2 = r2.get("combinedPrice")
                if fas4 is None and r4 and r4.get("xcatPrice") is None:
                    fas4 = r4.get("combinedPrice")

                p2 = make_price_entry(fas2, (r2 or {}).get("basePrice"), tax_pp, 2)
                p4 = make_price_entry(fas4, (r4 or {}).get("basePrice"), tax_pp, 4)
                cat_ksf = has_ksf_offer(r2 or {}) or has_ksf_offer(r4 or {}) or any_ksf
                p4 = align_ksf_quad_base(p2, p4, cat_ksf)

                if p2 or p4:
                    suite_avail.append(t_name)
                    suite_tiers[t_name] = {
                        "tier": t_name,
                        "label": s_meta["label"],
                        "room_name": s_meta["room_name"],
                        "other_rooms": s_meta["other_rooms"],
                        "amenities": s_meta["amenities"],
                        "royal_type": s_meta["royal_type"],
                        "pricing_2a": p2,
                        "pricing_quad": p4,
                        "is_live_exact": False,
                    }

                x2 = (r2 or {}).get("xcatPrice")
                x4 = (r4 or {}).get("xcatPrice")
                gp2 = make_price_entry(x2, (r2 or {}).get("basePrice"), tax_pp, 2)
                gp4 = make_price_entry(x4, (r4 or {}).get("basePrice"), tax_pp, 4)
                gp4 = align_ksf_quad_base(gp2, gp4, cat_ksf)
                if gp2 or gp4:
                    suite_guar_rooms.append(
                        {
                            "code": s_meta["guar_code"],
                            "name": s_meta["guar_name"],
                            "display_name": s_meta["guar_name"],
                            "royal_type": s_meta["royal_type"],
                            "guarantee": True,
                            "max_guests": 4 if gp4 else 2,
                            "pricing_2a": gp2,
                            "pricing_quad": gp4,
                        }
                    )

            if suite_guar_rooms:
                g2_list = [r for r in suite_guar_rooms if r["pricing_2a"]]
                g4_list = [r for r in suite_guar_rooms if r["pricing_quad"]]
                g2_best = min(g2_list, key=lambda r: r["pricing_2a"]["cabin_total"]) if g2_list else None
                g4_best = min(g4_list, key=lambda r: r["pricing_quad"]["cabin_total"]) if g4_list else None
                lead_g = g4_best or g2_best
                if lead_g:
                    suite_avail.append("Guarantee")
                    suite_tiers["Guarantee"] = {
                        "tier": "Guarantee",
                        "label": "Sail Away Suite (Guarantee)",
                        "room_name": lead_g["display_name"],
                        "room_name_2a": g2_best["display_name"] if g2_best else lead_g["display_name"],
                        "room_name_quad": g4_best["display_name"] if g4_best else lead_g["display_name"],
                        "other_rooms": " • ".join(
                            f"{r['display_name']}: ${round((r['pricing_quad'] or r['pricing_2a'])['cabin_total']):,}"
                            for r in suite_guar_rooms
                            if r is not lead_g
                        ),
                        "amenities": SUITE_GUARANTEE_AMENITIES,
                        "royal_type": lead_g["royal_type"],
                        "pricing_2a": g2_best["pricing_2a"] if g2_best else None,
                        "pricing_quad": g4_best["pricing_quad"] if g4_best else None,
                        "rooms": suite_guar_rooms,
                        "is_live_exact": False,
                    }

            s_rec = finalize_sailing_record(
                sail_date=sail_date,
                itin_code=itin_code,
                vb_code=vb_code,
                pkg_id=pkg_id,
                sail_id=sail_id,
                meta=meta,
                tiers_dict=suite_tiers,
                available_tiers=suite_avail,
                has_ksf_flag=any_ksf,
                default_ncl_type="HAVEN",
            )
            if s_rec:
                suite_deals.append(s_rec)

            # --- 2. Build NCL Regular Staterooms record ---
            room_tiers = {}
            room_avail = []
            room_guar_rooms = []

            for ncl_cat in ("INSIDE", "OCEANVIEW", "BALCONY"):
                r2 = m2.get(ncl_cat)
                r4 = m4.get(ncl_cat)
                rm_meta = NCL_STATEROOM_META[ncl_cat]
                t_name = rm_meta["tier"]

                fas2 = (r2 or {}).get("fasPrice")
                fas4 = (r4 or {}).get("fasPrice")
                if fas2 is None and r2 and r2.get("xcatPrice") is None:
                    fas2 = r2.get("combinedPrice")
                if fas4 is None and r4 and r4.get("xcatPrice") is None:
                    fas4 = r4.get("combinedPrice")

                p2 = make_price_entry(fas2, (r2 or {}).get("basePrice"), tax_pp, 2)
                p4 = make_price_entry(fas4, (r4 or {}).get("basePrice"), tax_pp, 4)
                cat_ksf = has_ksf_offer(r2 or {}) or has_ksf_offer(r4 or {}) or any_ksf
                p4 = align_ksf_quad_base(p2, p4, cat_ksf)

                interior_rooms = []
                if ncl_cat == "INSIDE":
                    if p2 or p4:
                        interior_rooms.append(
                            {
                                "code": "INSIDE",
                                "name": "Inside Stateroom",
                                "display_name": "Inside Stateroom",
                                "royal_type": "INSIDE",
                                "guarantee": False,
                                "max_guests": 4 if p4 else 2,
                                "pricing_2a": p2,
                                "pricing_quad": p4,
                            }
                        )
                    st_r2 = m2.get("STUDIO")
                    if st_r2:
                        st_fas = st_r2.get("fasPrice") or st_r2.get("combinedPrice")
                        st_p2 = make_price_entry(st_fas, st_r2.get("basePrice"), tax_pp, 2)
                        if st_p2:
                            interior_rooms.append(
                                {
                                    "code": "T1",
                                    "name": "Solo Studio Stateroom",
                                    "display_name": "Solo Studio Stateroom (T1 • 1 Guest)",
                                    "royal_type": "STUDIO",
                                    "guarantee": False,
                                    "max_guests": 1,
                                    "pricing_2a": st_p2,
                                    "pricing_quad": None,
                                }
                            )
                            if not p2:
                                p2 = st_p2

                if p2 or p4:
                    room_avail.append(t_name)
                    t_entry = {
                        "tier": t_name,
                        "label": rm_meta["label"],
                        "room_name": rm_meta["room_name"],
                        "other_rooms": rm_meta["other_rooms"],
                        "amenities": rm_meta["amenities"],
                        "royal_type": rm_meta["royal_type"],
                        "pricing_2a": p2,
                        "pricing_quad": p4,
                        "is_live_exact": False,
                    }
                    if interior_rooms:
                        t_entry["rooms"] = interior_rooms
                    room_tiers[t_name] = t_entry

                x2 = (r2 or {}).get("xcatPrice")
                x4 = (r4 or {}).get("xcatPrice")
                gp2 = make_price_entry(x2, (r2 or {}).get("basePrice"), tax_pp, 2)
                gp4 = make_price_entry(x4, (r4 or {}).get("basePrice"), tax_pp, 4)
                gp4 = align_ksf_quad_base(gp2, gp4, cat_ksf)
                if gp2 or gp4:
                    room_guar_rooms.append(
                        {
                            "code": rm_meta["guar_code"],
                            "name": rm_meta["guar_name"],
                            "display_name": rm_meta["guar_name"],
                            "royal_type": rm_meta["royal_type"],
                            "guarantee": True,
                            "max_guests": 4 if gp4 else 2,
                            "pricing_2a": gp2,
                            "pricing_quad": gp4,
                        }
                    )

            if room_guar_rooms:
                g2_list = [r for r in room_guar_rooms if r["pricing_2a"]]
                g4_list = [r for r in room_guar_rooms if r["pricing_quad"]]
                g2_best = min(g2_list, key=lambda r: r["pricing_2a"]["cabin_total"]) if g2_list else None
                g4_best = min(g4_list, key=lambda r: r["pricing_quad"]["cabin_total"]) if g4_list else None
                lead_g = g4_best or g2_best
                if lead_g:
                    room_avail.append("Guarantee")
                    room_tiers["Guarantee"] = {
                        "tier": "Guarantee",
                        "label": "Sail Away Room (Guarantee)",
                        "room_name": lead_g["display_name"],
                        "room_name_2a": g2_best["display_name"] if g2_best else lead_g["display_name"],
                        "room_name_quad": g4_best["display_name"] if g4_best else lead_g["display_name"],
                        "other_rooms": " • ".join(
                            f"{r['display_name']}: ${round((r['pricing_quad'] or r['pricing_2a'])['cabin_total']):,}"
                            for r in room_guar_rooms
                            if r is not lead_g
                        ),
                        "amenities": ROOM_GUARANTEE_AMENITIES,
                        "royal_type": lead_g["royal_type"],
                        "pricing_2a": g2_best["pricing_2a"] if g2_best else None,
                        "pricing_quad": g4_best["pricing_quad"] if g4_best else None,
                        "rooms": room_guar_rooms,
                        "is_live_exact": False,
                    }

            r_rec = finalize_sailing_record(
                sail_date=sail_date,
                itin_code=itin_code,
                vb_code=vb_code,
                pkg_id=pkg_id,
                sail_id=sail_id,
                meta=meta,
                tiers_dict=room_tiers,
                available_tiers=room_avail,
                has_ksf_flag=any_ksf,
                default_ncl_type="BALCONY",
            )
            if r_rec:
                room_deals.append(r_rec)

    norm_ports_str = ",".join(PORT_CODE_NORMALIZATION.get(p, p) for p in emb_ports.split(","))

    if suite_deals and output_suites_json:
        sp = save_ncl_catalog_payload(output_suites_json, "ncl-suites", norm_ports_str, len(itin_meta_map), suite_deals)
        print(
            f"Saved {len(suite_deals)} NCL Haven & Suite sailings "
            f"({sp['summary']['haven_sailings']} with The Haven, {sp['summary']['promo_sailings']} KSF promos) "
            f"to {output_suites_json}!"
        )

    if room_deals and output_staterooms_json:
        rp = save_ncl_catalog_payload(output_staterooms_json, "ncl-staterooms", norm_ports_str, len(itin_meta_map), room_deals)
        print(
            f"Saved {len(room_deals)} NCL Stateroom sailings "
            f"({rp['summary']['promo_sailings']} KSF promos) to {output_staterooms_json}!"
        )


def merge_baseline_catalog_shards(target_json: str, shard_glob: str, mode: str):
    files = sorted(glob.glob(shard_glob))
    if not files:
        print(f"No baseline shard files matched {shard_glob}; keeping {target_json}.")
        return

    deals_by_key: dict[tuple[str, str], dict] = {}
    scanned_count = 0
    ports_str = ",".join(PORT_CODE_NORMALIZATION.get(p, p) for p in DEFAULT_NCL_EMB_PORTS.split(","))

    # Seed from existing target_json if present so partial shard runs never lose coverage
    if os.path.exists(target_json):
        try:
            existing = json.load(open(target_json, encoding="utf-8"))
            ports_str = (existing.get("query") or {}).get("ports") or ports_str
            scanned_count = int((existing.get("summary") or {}).get("scanned_itineraries") or 0)
            for s in existing.get("sailings") or []:
                k = (str(s.get("itinerary_code") or ""), str(s.get("package_code") or s.get("date") or ""))
                deals_by_key[k] = s
        except Exception:
            pass

    shard_sailings_loaded = 0
    for fp in files:
        try:
            part = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        sailings = part.get("sailings") if isinstance(part, dict) else part
        if isinstance(part, dict):
            scanned_count = max(scanned_count, int((part.get("summary") or {}).get("scanned_itineraries") or 0))
            ports_str = (part.get("query") or {}).get("ports") or ports_str
        for s in sailings or []:
            k = (str(s.get("itinerary_code") or ""), str(s.get("package_code") or s.get("date") or ""))
            deals_by_key[k] = s
            shard_sailings_loaded += 1

    if shard_sailings_loaded == 0:
        print(f"0 sailings found across {len(files)} shard files; keeping existing {target_json}.")
        return

    merged_deals = list(deals_by_key.values())
    payload = save_ncl_catalog_payload(target_json, mode, ports_str, scanned_count, merged_deals)
    print(
        f"Merged {len(files)} baseline shards ({shard_sailings_loaded} fresh sailings) -> "
        f"{len(merged_deals)} total sailings across {len(payload['itineraries'])} itineraries in {target_json}!"
    )


def recompute_local_per_sailing_taxes(suites_json: str, staterooms_json: str):
    """
    Reconciles per-sailing exact port taxes & KSF flags across already-fetched local ncl_suites.json
    and ncl_staterooms.json without making network requests.
    """
    if not (os.path.exists(suites_json) and os.path.exists(staterooms_json)):
        return
    ds = json.load(open(suites_json, encoding="utf-8"))
    dr = json.load(open(staterooms_json, encoding="utf-8"))

    # Find exact per-package tax_pp from KSF staterooms/club-balcony where 4G_total - 2G_total == 2 * tax_pp
    pkg_exact_tax: dict[str, float] = {}
    for rec in (dr.get("sailings") or []) + (ds.get("sailings") or []):
        pkg = str(rec.get("package_code") or "")
        if not pkg:
            continue
        cands = []
        for t_name, tobj in (rec.get("tiers") or {}).items():
            if t_name in ("Guarantee", "Haven", "Suite"):
                continue
            p2 = tobj.get("pricing_2a")
            p4 = tobj.get("pricing_quad")
            if p2 and p4:
                delta_pp = round((p4["cabin_total"] - p2["cabin_total"]) / 2.0, 2)
                if 65.0 <= delta_pp <= 420.0:
                    cands.append(delta_pp)
        if cands:
            best_t = min(cands)
            if pkg not in pkg_exact_tax or best_t < pkg_exact_tax[pkg]:
                pkg_exact_tax[pkg] = best_t

    def reprice_pe(pe: dict | None, tax_pp: float) -> dict | None:
        if not pe:
            return None
        g = int(pe.get("guests") or 2)
        tot = float(pe["cabin_total"])
        tot_pp = round(tot / g, 2)
        eff_tax = min(tot_pp * 0.85, tax_pp)
        c_tax = round(eff_tax * g, 2)
        c_base = round(max(0.0, tot - c_tax), 2)
        return {
            "guests": g,
            "base_pp": round(c_base / g, 2),
            "total_pp": tot_pp,
            "cabin_base": c_base,
            "cabin_taxes": c_tax,
            "cabin_total": tot,
            "cabin_discount": pe.get("cabin_discount", 0.0),
        }

    for target_path, data, mode, def_type in [
        (suites_json, ds, "ncl-suites", "HAVEN"),
        (staterooms_json, dr, "ncl-staterooms", "BALCONY"),
    ]:
        new_deals = []
        for s in data.get("sailings") or []:
            pkg = str(s.get("package_code") or "")
            old_tax = float(s.get("tax_pp") or 140.0)
            # Only adopt pkg_exact_tax if it's close to or lower than old_tax (indicating KSF taxes-only 3rd/4th)
            new_tax = old_tax
            if pkg in pkg_exact_tax and pkg_exact_tax[pkg] <= old_tax + 45.0:
                new_tax = pkg_exact_tax[pkg]

            is_ksf_pkg = pkg in pkg_exact_tax and pkg_exact_tax[pkg] <= old_tax + 45.0
            tiers = s.get("tiers") or {}
            for tobj in tiers.values():
                tobj["pricing_2a"] = reprice_pe(tobj.get("pricing_2a"), new_tax)
                tobj["pricing_quad"] = align_ksf_quad_base(
                    tobj["pricing_2a"],
                    reprice_pe(tobj.get("pricing_quad"), new_tax),
                    is_ksf_pkg,
                )
                for r in tobj.get("rooms") or []:
                    r["pricing_2a"] = reprice_pe(r.get("pricing_2a"), new_tax)
                    r["pricing_quad"] = align_ksf_quad_base(
                        r["pricing_2a"],
                        reprice_pe(r.get("pricing_quad"), new_tax),
                        is_ksf_pkg,
                    )

            meta = {
                "itinerary_name": s["itinerary_name"],
                "ship": s["ship"],
                "ship_code": s["ship_code"],
                "ship_class": s["ship_class"],
                "nights": s["nights"],
                "port": s["port"],
                "port_code": s["port_code"],
            }
            rebuilt = finalize_sailing_record(
                sail_date=s["date"],
                itin_code=s["itinerary_code"],
                vb_code=s.get("vb_itinerary_code") or s["itinerary_code"],
                pkg_id=pkg,
                sail_id=s.get("sail_id", ""),
                meta=meta,
                tiers_dict=tiers,
                available_tiers=s.get("available_tiers") or list(tiers.keys()),
                has_ksf_flag=bool(s.get("is_ksf_promo")) or is_ksf_pkg,
                default_ncl_type=def_type,
            )
            if rebuilt:
                new_deals.append(rebuilt)

        ports_str = (data.get("query") or {}).get("ports") or DEFAULT_NCL_EMB_PORTS
        scanned = int((data.get("summary") or {}).get("scanned_itineraries") or len(new_deals))
        pl = save_ncl_catalog_payload(target_path, mode, ports_str, scanned, new_deals)
        print(
            f"Reconciled {target_path}: {len(new_deals)} sailings, "
            f"{pl['summary']['promo_sailings']} KSF promos, {pl['summary']['holiday_sailings']} holiday/blackout."
        )


def sanitize_title(title: str) -> str:
    return (
        (title or "")
        .replace("\ufffd", "'")
        .replace("\u2019", "'")
        .replace("\u00a0", " ")
        .strip()
    )


def fetch_subtype_batch(pkg_batch: list[str]) -> dict[str, dict[int, list[dict]]]:
    filters = []
    for pkg in pkg_batch:
        filters.append({"filterId": f"{pkg}_2", "packageId": str(pkg), "numberOfGuests": 2})
        filters.append({"filterId": f"{pkg}_4", "packageId": str(pkg), "numberOfGuests": 4})

    data = request_json(
        "POST",
        "https://www.ncl.com/api/vacation-builder/v2/stateroom-types-availability",
        json_body={"sailingFilters": filters},
        timeout=35,
        retries=3,
    )
    out: dict[str, dict[int, list[dict]]] = {}
    if not data:
        return out
    for item in data.get("results") or []:
        sid = str(item.get("stateroomId") or "")
        if "_" not in sid:
            continue
        pkg, g_str = sid.rsplit("_", 1)
        guests = int(g_str)
        stps = ((item.get("result") or {}).get("stateroomTypesPricing")) or []
        out.setdefault(pkg, {})[guests] = stps
    return out


def apply_subtypes_to_sailing(sailing: dict, stps_2: list[dict], stps_4: list[dict], is_suites_mode: bool) -> bool:
    if not stps_2 and not stps_4:
        return False

    tax_pp = float(sailing.get("tax_pp") or 140.0)
    is_ksf = bool(sailing.get("is_ksf_promo"))
    cat_to_tier = (
        {"HAVEN": "Haven", "SUITE": "Suite", "MINISUITE": "ClubBalcony"}
        if is_suites_mode
        else {"INSIDE": "Interior", "STUDIO": "Interior", "OCEANVIEW": "OceanView", "BALCONY": "Balcony"}
    )

    subtype_map: dict[tuple[str, str], dict] = {}

    def ingest(stps: list[dict], guests: int):
        for stp in stps:
            ncl_type = (stp.get("code") or "").upper()
            base_tier = cat_to_tier.get(ncl_type)
            if not base_tier:
                continue
            for sp in stp.get("stateroomsPricing") or []:
                if sp.get("isSoldOut") or sp.get("price") is None:
                    continue
                sr = sp.get("stateroom") or {}
                title = sanitize_title(sr.get("title") or ncl_type.title())
                cat_ids = sr.get("categoryIds") or []
                code = str(cat_ids[0]).upper() if cat_ids else ncl_type[:2]
                is_guar = bool(sr.get("isSailAway")) or code in ("IX", "OX", "BX", "MX", "SX", "HX")
                cap = int(sr.get("guestCapacity") or (1 if ncl_type == "STUDIO" or code in ("T1", "IT", "OT", "BT") else 4))
                target_tier = "Guarantee" if is_guar else base_tier
                disp = f"{title} ({code})" if f"({code})" not in title else title

                key = (target_tier, code)
                rec = subtype_map.get(key)
                if rec is None:
                    rec = {
                        "code": code,
                        "name": title,
                        "display_name": disp,
                        "royal_type": ncl_type,
                        "guarantee": is_guar,
                        "max_guests": cap,
                        "pricing_2a": None,
                        "pricing_quad": None,
                    }
                    subtype_map[key] = rec
                else:
                    rec["max_guests"] = max(rec["max_guests"], cap)

                pe = make_price_entry(sp.get("price"), None, tax_pp, guests)
                if guests == 2 and pe:
                    if rec["pricing_2a"] is None or pe["cabin_total"] < rec["pricing_2a"]["cabin_total"]:
                        rec["pricing_2a"] = pe
                elif guests == 4 and pe:
                    if rec["pricing_quad"] is None or pe["cabin_total"] < rec["pricing_quad"]["cabin_total"]:
                        rec["pricing_quad"] = pe

    ingest(stps_2, 2)
    ingest(stps_4, 4)

    if not subtype_map:
        return False

    by_tier: dict[str, list[dict]] = {}
    for (t_name, _), rec in subtype_map.items():
        rec["pricing_quad"] = align_ksf_quad_base(rec["pricing_2a"], rec["pricing_quad"], is_ksf)
        orig_t = (sailing.get("tiers") or {}).get(t_name) or {}
        if rec["pricing_2a"] and orig_t.get("pricing_2a"):
            rec["pricing_2a"]["cabin_discount"] = orig_t["pricing_2a"].get("cabin_discount", 0.0)
        if rec["pricing_quad"] and orig_t.get("pricing_quad"):
            rec["pricing_quad"]["cabin_discount"] = orig_t["pricing_quad"].get("cabin_discount", 0.0)
        by_tier.setdefault(t_name, []).append(rec)

    tiers_dict = sailing.get("tiers") or {}
    updated_any = False

    for t_name, rooms_list in by_tier.items():
        rooms_list.sort(key=lambda r: (r["pricing_quad"] or r["pricing_2a"])["cabin_total"])
        r2_candidates = [r for r in rooms_list if r["pricing_2a"] and r["max_guests"] >= 2]
        if not r2_candidates:
            r2_candidates = [r for r in rooms_list if r["pricing_2a"]]
        r4_candidates = [r for r in rooms_list if r["pricing_quad"] and r["max_guests"] >= 4]

        best_2a = min(r2_candidates, key=lambda r: r["pricing_2a"]["cabin_total"]) if r2_candidates else None
        best_4 = min(r4_candidates, key=lambda r: r["pricing_quad"]["cabin_total"]) if r4_candidates else None
        lead_r = best_4 or best_2a
        if not lead_r:
            continue

        t_obj = tiers_dict.get(t_name)
        if not t_obj:
            continue

        t_obj["rooms"] = rooms_list
        t_obj["is_live_exact"] = True
        t_obj["room_name"] = lead_r["display_name"]
        if best_2a:
            t_obj["room_name_2a"] = best_2a["display_name"]
            t_obj["pricing_2a"] = best_2a["pricing_2a"]
            others_2a = [r for r in r2_candidates if r["code"] != best_2a["code"]][:10]
            t_obj["other_rooms_2a"] = " • ".join(
                f"{r['display_name']}: ${round(r['pricing_2a']['cabin_total']):,}" for r in others_2a
            )
        if best_4:
            t_obj["room_name_quad"] = best_4["display_name"]
            t_obj["pricing_quad"] = best_4["pricing_quad"]
            others_4 = [r for r in r4_candidates if r["code"] != best_4["code"]][:10]
            t_obj["other_rooms_quad"] = " • ".join(
                f"{r['display_name']}: ${round(r['pricing_quad']['cabin_total']):,}" for r in others_4
            )
        else:
            t_obj["pricing_quad"] = None

        t_obj["other_rooms"] = t_obj.get("other_rooms_quad") or t_obj.get("other_rooms_2a") or t_obj.get("other_rooms", "")
        updated_any = True

    return updated_any


def enrich_ncl_subtypes(
    suites_json: str = "data/ncl_suites.json",
    staterooms_json: str = "data/ncl_staterooms.json",
    shard_index: int = 0,
    shard_count: int = 1,
    workers: int = 2,
    batch_size: int = 3,
    output_suites_shard: str | None = None,
    output_staterooms_shard: str | None = None,
):
    suites_data = json.load(open(suites_json, encoding="utf-8")) if os.path.exists(suites_json) else None
    rooms_data = json.load(open(staterooms_json, encoding="utf-8")) if os.path.exists(staterooms_json) else None

    all_pkgs_set = set()
    if suites_data:
        for s in suites_data.get("sailings") or []:
            if s.get("package_code"):
                all_pkgs_set.add(str(s["package_code"]))
    if rooms_data:
        for s in rooms_data.get("sailings") or []:
            if s.get("package_code"):
                all_pkgs_set.add(str(s["package_code"]))

    all_pkgs = sorted(all_pkgs_set)
    shard_pkgs = [p for idx, p in enumerate(all_pkgs) if idx % shard_count == shard_index]
    batches = [shard_pkgs[i : i + batch_size] for i in range(0, len(shard_pkgs), batch_size)]

    t0 = time.time()
    print(
        f"Enriching NCL subtypes for shard {shard_index}/{shard_count}: "
        f"{len(shard_pkgs)} unique packageIds in {len(batches)} batches ({workers} workers)..."
    )

    pkg_results: dict[str, dict[int, list[dict]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(fetch_subtype_batch, b) for b in batches]
        done = 0
        for fut in as_completed(futs):
            done += 1
            res = fut.result()
            if res:
                pkg_results.update(res)
            if done % 20 == 0 or done == len(futs):
                print(f"  [{done}/{len(futs)}] batches complete ({len(pkg_results)} packages enriched, {time.time() - t0:.1f}s)")

    suites_shard_map = {}
    rooms_shard_map = {}

    if suites_data:
        cnt_s = 0
        for s in suites_data.get("sailings") or []:
            pkg = str(s.get("package_code") or "")
            if pkg in pkg_results:
                p_entry = pkg_results[pkg]
                if apply_subtypes_to_sailing(s, p_entry.get(2, []), p_entry.get(4, []), is_suites_mode=True):
                    cnt_s += 1
                    suites_shard_map[pkg] = s["tiers"]
        print(f"Applied exact NCL Suite/Haven subtypes to {cnt_s} sailings.")
        if output_suites_shard:
            os.makedirs(os.path.dirname(output_suites_shard) or ".", exist_ok=True)
            with open(output_suites_shard, "w", encoding="utf-8") as f:
                json.dump(suites_shard_map, f, separators=(",", ":"))
        elif shard_count == 1:
            suites_data["updated_at"] = datetime.now(timezone.utc).isoformat()
            with open(suites_json, "w", encoding="utf-8") as f:
                json.dump(suites_data, f, separators=(",", ":"))

    if rooms_data:
        cnt_r = 0
        for s in rooms_data.get("sailings") or []:
            pkg = str(s.get("package_code") or "")
            if pkg in pkg_results:
                p_entry = pkg_results[pkg]
                if apply_subtypes_to_sailing(s, p_entry.get(2, []), p_entry.get(4, []), is_suites_mode=False):
                    cnt_r += 1
                    rooms_shard_map[pkg] = s["tiers"]
        print(f"Applied exact NCL Stateroom subtypes to {cnt_r} sailings.")
        if output_staterooms_shard:
            os.makedirs(os.path.dirname(output_staterooms_shard) or ".", exist_ok=True)
            with open(output_staterooms_shard, "w", encoding="utf-8") as f:
                json.dump(rooms_shard_map, f, separators=(",", ":"))
        elif shard_count == 1:
            rooms_data["updated_at"] = datetime.now(timezone.utc).isoformat()
            with open(staterooms_json, "w", encoding="utf-8") as f:
                json.dump(rooms_data, f, separators=(",", ":"))


def merge_ncl_shards(target_json: str, shard_glob: str):
    if not os.path.exists(target_json):
        print(f"Target {target_json} does not exist; skipping merge.")
        return
    data = json.load(open(target_json, encoding="utf-8"))
    merged_tiers: dict[str, dict] = {}
    files = sorted(glob.glob(shard_glob))
    for fp in files:
        part = json.load(open(fp, encoding="utf-8"))
        if isinstance(part, dict):
            merged_tiers.update(part)

    updated = 0
    for s in data.get("sailings") or []:
        pkg = str(s.get("package_code") or "")
        if pkg in merged_tiers:
            s["tiers"] = merged_tiers[pkg]
            updated += 1

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(target_json, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"Merged {len(files)} shard files into {target_json} ({updated} sailings updated).")


def main():
    parser = argparse.ArgumentParser(description="Fetch & enrich Norwegian Cruise Line (NCL) Suites & Staterooms")
    parser.add_argument("--ports", default=DEFAULT_NCL_EMB_PORTS, help="Comma-separated NCL embarkation port codes")
    parser.add_argument("--suites-json", default="data/ncl_suites.json")
    parser.add_argument("--staterooms-json", default="data/ncl_staterooms.json")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--rps", type=float, default=1.8, help="Max requests/sec per runner IP")
    parser.add_argument("--discover-only", action="store_true", help="Only discover itinerary list and save to --save-itineraries")
    parser.add_argument("--save-itineraries", default=None)
    parser.add_argument("--load-itineraries", default=None)
    parser.add_argument("--skip-baseline", action="store_true", help="Skip baseline fetch and only run subtype enrichment/merge")
    parser.add_argument("--enrich-subtypes", action="store_true", help="Enrich exact Haven, Suite & Stateroom subtypes")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--output-suites-shard", default=None)
    parser.add_argument("--output-staterooms-shard", default=None)
    parser.add_argument("--merge-baseline-shards", default=None, help="Glob pattern of baseline catalog shard JSONs to merge")
    parser.add_argument("--merge-mode", default="ncl-suites", choices=["ncl-suites", "ncl-staterooms"])
    parser.add_argument("--merge-shards", default=None, help="Glob pattern of subtype shard JSONs to merge into --target-json")
    parser.add_argument("--target-json", default=None, help="Target JSON file when using --merge-shards or --merge-baseline-shards")
    parser.add_argument("--recompute-local", action="store_true", help="Reconcile per-sailing exact taxes & KSF promos locally")
    args = parser.parse_args()

    configure_rate_limit(args.rps)

    if args.recompute_local:
        recompute_local_per_sailing_taxes(args.suites_json, args.staterooms_json)
        return

    if args.merge_baseline_shards and args.target_json:
        merge_baseline_catalog_shards(args.target_json, args.merge_baseline_shards, args.merge_mode)
        return

    if args.merge_shards and args.target_json:
        merge_ncl_shards(args.target_json, args.merge_shards)
        return

    if not args.skip_baseline:
        build_ncl_baseline_datasets(
            emb_ports=args.ports,
            output_suites_json=args.suites_json,
            output_staterooms_json=args.staterooms_json,
            workers=args.workers,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            load_itineraries_path=args.load_itineraries,
            save_itineraries_path=args.save_itineraries,
            discover_only=args.discover_only,
        )

    if args.enrich_subtypes:
        enrich_ncl_subtypes(
            suites_json=args.suites_json,
            staterooms_json=args.staterooms_json,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            workers=args.workers,
            batch_size=args.batch_size,
            output_suites_shard=args.output_suites_shard,
            output_staterooms_shard=args.output_staterooms_shard,
        )


if __name__ == "__main__":
    main()

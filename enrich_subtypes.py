import argparse
import glob
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    from curl_cffi import requests
except ImportError:
    requests = None

_thread_local = threading.local()


def get_thread_session():
    if requests is None:
        raise RuntimeError("curl_cffi is required for HTTP enrichment")
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session(impersonate="chrome")
    return _thread_local.session


ROYAL_SUITE_CLASSES = {"Icon", "Oasis", "Quantum"}

STAR_CODES = {"UL", "RL", "IL", "SL", "TL", "A1", "A2", "UP", "U1"}
SKY_CODES = {"JY", "VP", "SS", "IG", "GS", "SN", "OW", "OS", "CL", "A3", "A4", "GT", "1S", "2S"}
SEA_CODES = {"SF", "JS", "JT", "J1", "J3", "J4"}

SUITE_TIER_AMENITIES = {
    "Sea": "Coastal Kitchen (Dinner), Luxury Pillowtop Mattress, Lavazza Espresso Machine, Priority Boarding",
    "Sky": "All-Day Coastal Kitchen, Suite Lounge & Sun Deck, Complimentary VOOM Wi-Fi, Concierge Service, Priority Boarding",
    "Star": "Royal Genie Service, Complimentary Specialty Dining, Complimentary Deluxe Beverage Package, Gratuities Included, VOOM Wi-Fi",
    "Standard": "Priority Boarding, Dedicated Stateroom Attendant, Luxury Robes & Toiletries, Double Crown & Anchor Points",
    "Guarantee": "We Choose Your Suite (Assigned close to sailing • Minimum Sea Class / Junior Suite • Does NOT include Sky/Star Class perks)",
}

STATEROOM_TIER_AMENITIES = {
    "Interior": "Two twin beds (convert to Royal King), private bathroom, 24-hr room service, nightly turndown",
    "OceanView": "Ocean-facing window or porthole, sitting area, Royal King bed, complimentary room service",
    "Balcony": "Private step-out veranda or Infinite Balcony, floor-to-ceiling view, sitting area",
    "Guarantee": "We Choose Your Room (Unassigned Guarantee rate • Royal assigns exact cabin prior to sailing)",
}

STATEROOM_ROYAL_TYPE = {
    "Interior": "INTERIOR",
    "OceanView": "OUTSIDE",
    "Balcony": "BALCONY",
    "Guarantee": "BALCONY",
}


def find_stateroom_types(obj):
    if isinstance(obj, dict):
        if "stateroomTypes" in obj and isinstance(obj["stateroomTypes"], list):
            return obj["stateroomTypes"]
        for v in obj.values():
            res = find_stateroom_types(v)
            if res is not None:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = find_stateroom_types(item)
            if res is not None:
                return res
    return None


def classify_subtype_tier(sub: dict, ship_cls: str) -> str:
    code = str(sub.get("code") or "").strip().upper()
    name = str(sub.get("name") or "").strip()
    is_guar = bool(sub.get("guarantee")) or code in ("WS", "W", "WD")
    if is_guar:
        return "Guarantee"

    if ship_cls not in ROYAL_SUITE_CLASSES:
        return "Standard"

    text_blobs = list(sub.get("description") or [])
    for feat in sub.get("features") or []:
        if isinstance(feat, dict):
            text_blobs.append(str(feat.get("title") or ""))
            text_blobs.extend(feat.get("lines") or [])
    combined = " ".join(str(x) for x in text_blobs).lower()

    if "star class" in combined or code in STAR_CODES:
        return "Star"
    if "sky class" in combined or "sky junior" in name.lower():
        return "Sky"
    if "sea class" in combined:
        return "Sea"
    if code in SKY_CODES:
        return "Sky"
    if code in SEA_CODES:
        return "Sea"
    return "Sea"


def classify_stateroom_subtype(sub: dict, parent_code: str) -> tuple[str, bool, str]:
    """Returns (base_tier, is_guarantee, royal_type) for INTERIOR/OUTSIDE/BALCONY subtypes."""
    code = str(sub.get("code") or "").strip().upper()
    is_guar = bool(sub.get("guarantee")) or code in ("ZI", "ZQ", "YO", "YQ", "XB", "XQ", "XN")
    if parent_code == "INTERIOR":
        return "Interior", is_guar, "INTERIOR"
    if parent_code == "OUTSIDE":
        return "OceanView", is_guar, "OUTSIDE"
    return "Balcony", is_guar, "BALCONY"


def extract_max_occupancy(sub: dict, code: str, ship_cls: str) -> tuple[int, bool]:
    """Returns (max_guests, pullman_warning) from Royal's room subtype features."""
    feat_lines = []
    for feat in sub.get("features") or []:
        if isinstance(feat, dict):
            feat_lines.extend(str(x) for x in (feat.get("lines") or []))
    joined = " ".join(feat_lines)
    m = re.search(r"up to (\d+)\s*guests", joined, re.I)
    if m:
        max_g = int(m.group(1))
        has_pullman_note = "in staterooms with up to 5 guests" in joined.lower() or code in ("JS", "JY", "J1", "CJ")
        return max_g, (max_g == 5 and has_pullman_note)

    # Fallback by suite/stateroom code and ship class
    if code == "VS":
        return 14, False
    if code in ("UL", "TH", "US", "A1", "A2", "GT", "OT"):
        return 8, False
    if code in ("RL", "VY", "K", "1K", "R", "1R", "1A"):
        return 6, False
    if code in ("JS", "JY", "J1", "CJ") and (ship_cls or "").lower() != "icon":
        return 5, True
    return 4, False


def parse_rsc_all_subtypes(rsc_text: str, ship_cls: str, guests: int) -> tuple[list[dict], list[dict]]:
    """Parses both DELUXE (suite_rooms) and INTERIOR/OUTSIDE/BALCONY (stateroom_rooms) from a single RSC response."""
    suite_rooms = []
    stateroom_rooms = []
    for line in rsc_text.splitlines():
        if '"stateroomTypes":[' not in line:
            continue
        try:
            _, payload = line.split(":", 1)
            data = json.loads(payload)
        except Exception:
            continue

        st_types = find_stateroom_types(data)
        if not st_types:
            continue

        for st in st_types:
            if not isinstance(st, dict):
                continue
            parent_code = str(st.get("code") or "").upper()
            if parent_code not in ("DELUXE", "INTERIOR", "OUTSIDE", "BALCONY"):
                continue

            for sub in st.get("stateroomSubtypes") or []:
                if not isinstance(sub, dict):
                    continue
                pricing = sub.get("pricing") or {}
                inv = pricing.get("invoice") or {}
                total = inv.get("total")
                if total is None:
                    continue
                total_f = round(float(total), 2)
                subtotal_f = round(float(inv.get("subtotal") or 0.0), 2)
                taxes_f = round(float(inv.get("taxesAndFees") or 0.0), 2)
                disc_obj = inv.get("discount") or {}
                disc_f = round(abs(float(disc_obj.get("amount") or 0.0)), 2)
                pp_f = round(float(pricing.get("perPersonAmount") or (total_f / max(1, guests))), 2)
                base_pp_f = round(subtotal_f / max(1, guests), 2)

                code = str(sub.get("code") or "ROOM").strip().upper()
                raw_name = str(sub.get("name") or "Room").strip()
                max_guests, pullman_warning = extract_max_occupancy(sub, code, ship_cls)

                pricing_dict = {
                    "guests": guests,
                    "base_pp": base_pp_f,
                    "total_pp": pp_f,
                    "cabin_base": subtotal_f,
                    "cabin_taxes": taxes_f,
                    "cabin_total": total_f,
                    "cabin_discount": disc_f,
                }

                if parent_code == "DELUXE":
                    tier = classify_subtype_tier(sub, ship_cls)
                    is_guar = tier == "Guarantee"
                    display_name = (
                        f"We Choose Your Suite - Guarantee ({code})"
                        if is_guar
                        else f"{raw_name} ({code})"
                    )
                    suite_rooms.append({
                        "code": code,
                        "name": raw_name,
                        "display_name": display_name,
                        "guarantee": is_guar,
                        "tier": tier,
                        "royal_type": "DELUXE",
                        "max_guests": max_guests,
                        "pullman_warning": pullman_warning,
                        "pricing": pricing_dict,
                    })
                else:
                    base_tier, is_guar, royal_type = classify_stateroom_subtype(sub, parent_code)
                    display_name = (
                        f"We Choose Your Room - {raw_name} Guarantee ({code})"
                        if is_guar
                        else f"{raw_name} ({code})"
                    )
                    stateroom_rooms.append({
                        "code": code,
                        "name": raw_name,
                        "display_name": display_name,
                        "guarantee": is_guar,
                        "tier": base_tier,
                        "royal_type": royal_type,
                        "max_guests": max_guests,
                        "pullman_warning": pullman_warning,
                        "pricing": pricing_dict,
                    })
        break
    return suite_rooms, stateroom_rooms


def parse_rsc_deluxe_subtypes(rsc_text: str, ship_cls: str, guests: int) -> list[dict]:
    suite_rooms, _ = parse_rsc_all_subtypes(rsc_text, ship_cls, guests)
    return suite_rooms


def fetch_sailing_subtypes(task_item):
    key, s = task_item
    session = get_thread_session()
    q = s["booking_link"].replace("/booking/landing?", "")
    ship_cls = s.get("ship_class", "Other")
    has_quad = s.get("pricing_quad") is not None

    suites_4, staterooms_4 = [], []
    suites_2, staterooms_2 = [], []
    got_403 = False

    if has_quad:
        url_4 = f"https://www.royalcaribbean.com/room-selection/type-and-subtype?{q}&r0a=2&r0c=2&r0d=DELUXE"
        try:
            r4 = session.get(url_4, headers={"RSC": "1"}, timeout=18)
            if r4.status_code == 200:
                suites_4, staterooms_4 = parse_rsc_all_subtypes(r4.text, ship_cls, 4)
            elif r4.status_code == 403:
                got_403 = True
        except Exception:
            pass

    if not got_403:
        url_2 = f"https://www.royalcaribbean.com/room-selection/type-and-subtype?{q}&r0a=2&r0c=0&r0d=DELUXE"
        try:
            r2 = session.get(url_2, headers={"RSC": "1"}, timeout=18)
            if r2.status_code == 200:
                suites_2, staterooms_2 = parse_rsc_all_subtypes(r2.text, ship_cls, 2)
            elif r2.status_code == 403:
                got_403 = True
        except Exception:
            pass

    return key, suites_4, suites_2, staterooms_4, staterooms_2, got_403


CLASS_PRIORITY = {
    "icon": 0,
    "oasis": 1,
    "quantum": 2,
    "freedom": 3,
    "voyager": 4,
    "radiance": 5,
    "vision": 6,
}


def apply_rooms_to_sailing(s: dict, rooms_4: list[dict], rooms_2: list[dict], now_iso: str, is_staterooms: bool = False) -> bool:
    if not rooms_4 and not rooms_2:
        return False

    map_2 = {r["code"]: r["pricing"] for r in rooms_2}
    map_4 = {r["code"]: r["pricing"] for r in rooms_4}

    all_rooms_by_code = {}
    for r in rooms_4:
        all_rooms_by_code[r["code"]] = r
    for r in rooms_2:
        if r["code"] not in all_rooms_by_code:
            all_rooms_by_code[r["code"]] = r

    tier_order = ["Interior", "OceanView", "Balcony", "Guarantee"] if is_staterooms else ["Sea", "Sky", "Star", "Standard", "Guarantee"]
    amenities_map = STATEROOM_TIER_AMENITIES if is_staterooms else SUITE_TIER_AMENITIES
    grouped = {t: [] for t in tier_order}

    for code, r_info in all_rooms_by_code.items():
        t = r_info["tier"]
        is_guar = bool(r_info.get("guarantee", False))
        p4 = map_4.get(code)
        p2 = map_2.get(code)

        if p4 and not p2:
            tax_2 = round(p4["cabin_taxes"] / 2.0, 2)
            extra_pp = float(s.get("extra_guest_fare_pp") or 0.0)
            base_2 = round(max(0.0, p4["cabin_base"] - 2.0 * extra_pp), 2)
            tot_2 = round(base_2 + tax_2, 2)
            p2 = {
                "guests": 2,
                "base_pp": round(base_2 / 2.0, 2),
                "total_pp": round(tot_2 / 2.0, 2),
                "cabin_base": base_2,
                "cabin_taxes": tax_2,
                "cabin_total": tot_2,
                "cabin_discount": p4["cabin_discount"],
            }

        entry = {
            "code": code,
            "display_name": r_info["display_name"],
            "guarantee": is_guar,
            "royal_type": r_info.get("royal_type", "BALCONY" if is_staterooms else "DELUXE"),
            "max_guests": r_info.get("max_guests", 4),
            "pullman_warning": bool(r_info.get("pullman_warning", False)),
            "pricing_2a": p2,
            "pricing_quad": p4,
            "sort_price": (p4 or p2)["cabin_total"],
        }

        if is_staterooms:
            # Put in its parent category (Interior / OceanView / Balcony) AND in Guarantee if it's a Guarantee room
            grouped.setdefault(t, []).append(entry)
            if is_guar:
                grouped.setdefault("Guarantee", []).append(entry)
        else:
            grouped.setdefault(t, []).append(entry)

    tiers_dict = {}
    for t in tier_order:
        t_rooms = grouped.get(t) or []
        if not t_rooms:
            continue

        # For Interior/OceanView/Balcony, prefer Assigned (non-Guarantee) rooms as the default lead room
        if is_staterooms and t != "Guarantee":
            assigned_2a = sorted(
                [r for r in t_rooms if r["pricing_2a"] is not None and not r["guarantee"]],
                key=lambda x: x["pricing_2a"]["cabin_total"],
            )
            assigned_quad = sorted(
                [r for r in t_rooms if r["pricing_quad"] is not None and not r["guarantee"]],
                key=lambda x: x["pricing_quad"]["cabin_total"],
            )
            all_2a = sorted(
                [r for r in t_rooms if r["pricing_2a"] is not None],
                key=lambda x: x["pricing_2a"]["cabin_total"],
            )
            all_quad = sorted(
                [r for r in t_rooms if r["pricing_quad"] is not None],
                key=lambda x: x["pricing_quad"]["cabin_total"],
            )
            rooms_2a = assigned_2a if assigned_2a else all_2a
            rooms_quad = assigned_quad if assigned_quad else all_quad
        else:
            rooms_2a = sorted(
                [r for r in t_rooms if r["pricing_2a"] is not None],
                key=lambda x: x["pricing_2a"]["cabin_total"],
            )
            rooms_quad = sorted(
                [r for r in t_rooms if r["pricing_quad"] is not None],
                key=lambda x: x["pricing_quad"]["cabin_total"],
            )

        best_2a = rooms_2a[0] if rooms_2a else None
        best_quad = rooms_quad[0] if rooms_quad else None
        lead = best_quad or best_2a
        if not lead:
            continue

        others_quad = [
            f"{o['display_name']}: ${int(round(o['pricing_quad']['cabin_total'])):,}"
            for o in rooms_quad[1:6]
        ]
        others_2a = [
            f"{o['display_name']}: ${int(round(o['pricing_2a']['cabin_total'])):,}"
            for o in rooms_2a[1:6]
        ]
        other_str_quad = " • ".join(others_quad)
        other_str_2a = " • ".join(others_2a)

        if is_staterooms:
            label = "We Choose Your Room (Guarantee)" if t == "Guarantee" else f"{t} Stateroom"
        else:
            label = "We Choose Your Suite (Guarantee)" if t == "Guarantee" else f"{t} Class"

        tiers_dict[t] = {
            "tier": t,
            "label": label,
            "royal_type": lead.get("royal_type", STATEROOM_ROYAL_TYPE.get(t, "DELUXE")),
            "room_name": (best_quad or best_2a)["display_name"],
            "room_name_2a": best_2a["display_name"] if best_2a else (best_quad["display_name"] if best_quad else ""),
            "room_name_quad": best_quad["display_name"] if best_quad else (best_2a["display_name"] if best_2a else ""),
            "other_rooms": other_str_quad or other_str_2a,
            "other_rooms_2a": other_str_2a,
            "other_rooms_quad": other_str_quad,
            "amenities": amenities_map.get(t, ""),
            "pricing_2a": best_2a["pricing_2a"] if best_2a else None,
            "pricing_quad": best_quad["pricing_quad"] if best_quad else None,
            "rooms": [
                {
                    "code": r["code"],
                    "display_name": r["display_name"],
                    "guarantee": r["guarantee"],
                    "royal_type": r["royal_type"],
                    "max_guests": r["max_guests"],
                    "pullman_warning": r["pullman_warning"],
                    "pricing_2a": r["pricing_2a"],
                    "pricing_quad": r["pricing_quad"],
                }
                for r in sorted(t_rooms, key=lambda x: x["sort_price"])
            ],
            "is_live_exact": True,
        }

    if not tiers_dict:
        return False

    avail_tiers = [t for t in tier_order if t in tiers_dict]
    non_guar = [t for t in avail_tiers if t != "Guarantee"]
    lead_tier = non_guar[0] if non_guar else avail_tiers[0]
    lead_obj = tiers_dict[lead_tier]

    s["suite_tier"] = lead_tier
    s["available_tiers"] = avail_tiers
    s["tiers"] = tiers_dict
    s["pricing_2a"] = lead_obj["pricing_2a"]
    s["pricing_quad"] = lead_obj["pricing_quad"]
    s["enriched_at"] = now_iso

    ref_main = lead_obj["pricing_quad"] or lead_obj["pricing_2a"]
    s["base_pp"] = ref_main["base_pp"]
    s["total_pp"] = ref_main["total_pp"]
    s["cabin_base"] = ref_main["cabin_base"]
    s["cabin_taxes"] = ref_main["cabin_taxes"]
    s["cabin_total"] = ref_main["cabin_total"]
    s["cabin_discount"] = ref_main["cabin_discount"]
    s["price_per_night"] = round(ref_main["cabin_total"] / max(1, s.get("nights", 1)), 2)

    if lead_obj["pricing_2a"] and lead_obj["pricing_quad"]:
        diff = lead_obj["pricing_quad"]["cabin_base"] - lead_obj["pricing_2a"]["cabin_base"]
        extra_pp = round(max(0.0, diff / 2.0), 2)
        s["extra_guest_fare_pp"] = extra_pp
        s["is_ksf_promo"] = extra_pp <= 1.0
        s["is_holiday_rate"] = extra_pp > 1.0
        disc = int(round(ref_main["cabin_discount"]))
        if s["is_ksf_promo"]:
            s["rate_tag"] = f"PROMO: Kids/3rd&4th Free (-${disc} off)"
        else:
            s["rate_tag"] = f"HOLIDAY/BLACKOUT: 3rd/4th +${extra_pp:.2f}/ea (-${disc} off)"

    return True


def rebuild_itineraries_and_save(data: dict, json_path: str):
    sailings = data["sailings"]
    itin_map = {}
    for d in sailings:
        code = d["itinerary_code"]
        if code not in itin_map:
            itin_map[code] = {
                "itinerary_code": code,
                "itinerary_name": d["itinerary_name"],
                "ship": d["ship"],
                "ship_code": d.get("ship_code", ""),
                "ship_class": d["ship_class"],
                "nights": d["nights"],
                "port": d["port"],
                "port_code": d.get("port_code", ""),
                "suite_tier": d.get("suite_tier", "Standard"),
                "available_tiers": d.get("available_tiers", []),
                "sailing_count": 0,
                "promo_count": 0,
                "lowest_cabin_total": d["cabin_total"],
                "lowest_total_pp": d["total_pp"],
                "lowest_sail_date": d["date"],
                "sail_dates": [],
            }
        it = itin_map[code]
        it["sailing_count"] += 1
        if d.get("is_ksf_promo"):
            it["promo_count"] += 1
        it["sail_dates"].append(d["date"])
        if d["cabin_total"] < it["lowest_cabin_total"]:
            it["lowest_cabin_total"] = d["cabin_total"]
            it["lowest_total_pp"] = d["total_pp"]
            it["lowest_sail_date"] = d["date"]
            it["suite_tier"] = d.get("suite_tier", "Standard")
            it["available_tiers"] = d.get("available_tiers", [])

    for it in itin_map.values():
        it["sail_dates"].sort()

    data["itineraries"] = sorted(itin_map.values(), key=lambda x: (x["lowest_cabin_total"], x["ship"]))
    if "summary" in data and isinstance(data["summary"], dict):
        data["summary"]["itineraries_with_suites"] = len(data["itineraries"])
        data["summary"]["total_suite_sailings"] = len(sailings)
        data["summary"]["promo_sailings"] = sum(1 for d in sailings if d.get("is_ksf_promo"))
        data["summary"]["holiday_sailings"] = sum(1 for d in sailings if d.get("is_holiday_rate"))

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def merge_shard_files(json_path: str, shard_glob: str):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    sailings = data["sailings"]
    shard_files = sorted(glob.glob(shard_glob))
    merged_count = 0
    for sf in shard_files:
        with open(sf, "r", encoding="utf-8") as f:
            shard_map = json.load(f)
        for idx_str, updated_sailing in shard_map.items():
            idx = int(idx_str)
            if 0 <= idx < len(sailings):
                sailings[idx] = updated_sailing
                merged_count += 1

    rebuild_itineraries_and_save(data, json_path)
    total_live = sum(
        1 for s in sailings if any(v.get("is_live_exact") for v in (s.get("tiers") or {}).values())
    )
    print(f"Merged {merged_count} enriched sailings from {len(shard_files)} shards into {json_path} (Total live: {total_live}/{len(sailings)}).")


def enrich_suites_json(
    json_path: str = "data/suites.json",
    staterooms_json_path: str | None = None,
    ship_classes: list[str] | None = None,
    workers: int = 6,
    force: bool = False,
    max_sailings: int = 550,
    shard_index: int | None = None,
    shard_count: int | None = None,
    output_shard: str | None = None,
    output_stateroom_shard: str | None = None,
):
    with open(json_path, "r", encoding="utf-8") as f:
        suites_data = json.load(f)
    suite_sailings = suites_data["sailings"]

    staterooms_data = None
    stateroom_sailings = []
    if staterooms_json_path and os.path.exists(staterooms_json_path):
        with open(staterooms_json_path, "r", encoding="utf-8") as f:
            staterooms_data = json.load(f)
        stateroom_sailings = staterooms_data["sailings"]

    suite_idx_by_key = {(s["itinerary_code"], s["date"]): i for i, s in enumerate(suite_sailings)}
    stateroom_idx_by_key = {(s["itinerary_code"], s["date"]): i for i, s in enumerate(stateroom_sailings)}

    # Build unified sailing keys so a single request pair enriches both suites.json and staterooms.json
    all_keys_map = {}
    for s in suite_sailings:
        all_keys_map[(s["itinerary_code"], s["date"])] = s
    for s in stateroom_sailings:
        k = (s["itinerary_code"], s["date"])
        if k not in all_keys_map:
            all_keys_map[k] = s

    sorted_keys = sorted(all_keys_map.keys())
    cls_lower = {c.lower() for c in ship_classes} if ship_classes else None

    if shard_index is not None and shard_count is not None and shard_count > 0:
        targets = [
            (k, all_keys_map[k])
            for i, k in enumerate(sorted_keys)
            if (i % shard_count == shard_index)
            and (not cls_lower or (all_keys_map[k].get("ship_class") or "").lower() in cls_lower)
        ]
    else:
        candidates = []
        for k in sorted_keys:
            s = all_keys_map[k]
            s_cls = (s.get("ship_class") or "").lower()
            if cls_lower and s_cls not in cls_lower:
                continue
            has_live = any(v.get("is_live_exact") for v in (s.get("tiers") or {}).values())
            if not force and has_live:
                continue
            enriched_at = s.get("enriched_at") or ""
            cls_prio = CLASS_PRIORITY.get(s_cls, 9)
            candidates.append((0 if not has_live else 1, cls_prio, enriched_at, k, s))

        candidates.sort(key=lambda x: (x[0], x[1], x[2]))
        targets = [(item[3], item[4]) for item in candidates[:max_sailings]]

    print(
        f"Enriching {len(targets)} unique sailings "
        f"(shard={shard_index}/{shard_count}, workers={workers})..."
    )
    t0 = time.time()
    updated_suites = 0
    updated_staterooms = 0
    consecutive_403 = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    suite_shard_results = {}
    stateroom_shard_results = {}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(fetch_sailing_subtypes, item) for item in targets]
        for count, fut in enumerate(as_completed(futures), 1):
            key, suites_4, suites_2, staterooms_4, staterooms_2, got_403 = fut.result()
            if got_403 and not suites_4 and not suites_2 and not staterooms_4 and not staterooms_2:
                consecutive_403 += 1
                if consecutive_403 >= 15:
                    print("  Notice: Akamai rate limit reached (403); stopping batch early and saving progress.", flush=True)
                    for pending in futures:
                        pending.cancel()
                    break
            else:
                consecutive_403 = 0

            if key in suite_idx_by_key and (suites_4 or suites_2):
                s_idx = suite_idx_by_key[key]
                s_obj = suite_sailings[s_idx]
                if apply_rooms_to_sailing(s_obj, suites_4, suites_2, now_iso, is_staterooms=False):
                    suite_shard_results[str(s_idx)] = s_obj
                    updated_suites += 1

            if key in stateroom_idx_by_key and (staterooms_4 or staterooms_2):
                st_idx = stateroom_idx_by_key[key]
                st_obj = stateroom_sailings[st_idx]
                if apply_rooms_to_sailing(st_obj, staterooms_4, staterooms_2, now_iso, is_staterooms=True):
                    stateroom_shard_results[str(st_idx)] = st_obj
                    updated_staterooms += 1

            if count % 25 == 0 or count == len(targets):
                print(
                    f"  Progress: {count}/{len(targets)} "
                    f"(suites={updated_suites}, staterooms={updated_staterooms}) in {time.time()-t0:.1f}s",
                    flush=True,
                )

    if output_shard:
        out_dir = os.path.dirname(output_shard)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_shard, "w", encoding="utf-8") as f:
            json.dump(suite_shard_results, f)
        print(f"Saved suite shard {shard_index}/{shard_count} ({updated_suites} sailings) to {output_shard}.")

    if output_stateroom_shard:
        out_dir = os.path.dirname(output_stateroom_shard)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_stateroom_shard, "w", encoding="utf-8") as f:
            json.dump(stateroom_shard_results, f)
        print(f"Saved stateroom shard {shard_index}/{shard_count} ({updated_staterooms} sailings) to {output_stateroom_shard}.")

    if output_shard or output_stateroom_shard:
        return

    rebuild_itineraries_and_save(suites_data, json_path)
    if staterooms_data and staterooms_json_path:
        rebuild_itineraries_and_save(staterooms_data, staterooms_json_path)
    print(f"Done! Enriched {updated_suites} suite sailings and {updated_staterooms} stateroom sailings in {time.time()-t0:.1f}s.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enrich suites.json and staterooms.json with live per-subtype room-selection prices from Royal Caribbean")
    parser.add_argument("--json", default="data/suites.json", help="Path to suites.json (or target JSON for --merge-shards)")
    parser.add_argument("--staterooms-json", default="data/staterooms.json", help="Path to staterooms.json")
    parser.add_argument("--ship-class", default="all", help="Comma-separated ship classes to enrich (e.g. Icon,Oasis) or 'all' (default: all)")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent workers (default: 6)")
    parser.add_argument("--max-sailings", type=int, default=550, help="Max sailings to enrich per single-node run (default: 550)")
    parser.add_argument("--shard-index", type=int, default=None, help="0-based shard index for parallel CI jobs")
    parser.add_argument("--shard-count", type=int, default=None, help="Total number of parallel shards")
    parser.add_argument("--output-shard", type=str, default=None, help="Output file path for suite shard JSON")
    parser.add_argument("--output-stateroom-shard", type=str, default=None, help="Output file path for stateroom shard JSON")
    parser.add_argument("--merge-shards", type=str, default=None, help="Glob pattern of shard JSON files to merge into --json")
    parser.add_argument("--force", action="store_true", help="Re-enrich sailings even if already marked is_live_exact")
    args = parser.parse_args()
    if args.merge_shards:
        merge_shard_files(args.json, args.merge_shards)
    else:
        classes = None if args.ship_class.lower() == "all" else [c.strip() for c in args.ship_class.split(",") if c.strip()]
        enrich_suites_json(
            args.json,
            staterooms_json_path=args.staterooms_json,
            ship_classes=classes,
            workers=args.workers,
            force=args.force,
            max_sailings=args.max_sailings,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            output_shard=args.output_shard,
            output_stateroom_shard=args.output_stateroom_shard,
        )

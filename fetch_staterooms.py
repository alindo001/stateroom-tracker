from datetime import datetime, timezone
import json
import os
from curl_cffi import requests
from tracker import (
    URL,
    BASE_HEADERS,
    CODE_TO_SHIP_CLASS,
    extract_per_person_breakdown,
)

BULK_SEARCH_QUERY = """
query BulkCruiseSearch($filters: String, $pagination: CruiseSearchPagination) {
  cruiseSearch(filters: $filters, pagination: $pagination) {
    results {
      total
      cruises {
        id
        masterSailing {
          itinerary {
            code
            name
            sailingNights
            departurePort { code name }
            ship { code name }
          }
        }
        sailings {
          sailDate
          bookingLink
          taxesAndFees { value }
          taxesAndFeesIncluded
          stateroomClassPricing {
            stateroomClass {
              id
              name
              content { code }
            }
            price {
              value
              originalAmount
              netAmount
              discountAmount
              taxesAndFeesAmount
              areTaxesAndFeesIncluded
            }
          }
        }
      }
    }
  }
}
"""

STATEROOM_META = {
    "INTERIOR": {
        "tier": "Interior",
        "label": "Interior Stateroom",
        "room_name": "Interior Stateroom",
        "other_rooms": "Connected Interior • Spacious Interior • Promenade/Central Park View",
        "amenities": "Two twin beds (convert to Royal King), private bath, 24-hr room service, nightly turndown",
        "royal_type": "INTERIOR",
    },
    "OUTSIDE": {
        "tier": "OceanView",
        "label": "Ocean View Stateroom",
        "room_name": "Ocean View Stateroom",
        "other_rooms": "Spacious Ocean View • Ultra Spacious Ocean View (Sleeps 6)",
        "amenities": "Ocean-facing window or porthole, sitting area, Royal King bed, complimentary room service",
        "royal_type": "OUTSIDE",
    },
    "BALCONY": {
        "tier": "Balcony",
        "label": "Balcony Stateroom",
        "room_name": "Ocean View / Neighborhood Balcony",
        "other_rooms": "Infinite Balcony • Central Park / Boardwalk / Surfside Balcony • Spacious Balcony",
        "amenities": "Private step-out veranda or Infinite Balcony, floor-to-ceiling view, sitting area",
        "royal_type": "BALCONY",
    },
}

TIER_ORDER = ["Interior", "OceanView", "Balcony"]


def classify_regular_stateroom(sc: dict) -> str | None:
    sc_id = (sc.get("id") or "").upper()
    code = ((sc.get("content") or {}).get("code") or "").upper()
    name = (sc.get("name") or "").upper()
    if sc_id == "INTERIOR" or code == "I" or "INTERIOR" in name:
        return "INTERIOR"
    if sc_id == "OUTSIDE" or code == "O" or "OUTSIDE" in name or "OCEAN" in name:
        return "OUTSIDE"
    if sc_id == "BALCONY" or code == "B" or "BALCONY" in name:
        return "BALCONY"
    return None


def fetch_bulk_cruises(session: requests.Session, filters: str, max_count: int = 500) -> list[dict]:
    all_cruises = []
    skip = 0
    page_size = 100
    while skip < max_count:
        payload = {
            "operationName": "BulkCruiseSearch",
            "variables": {
                "filters": filters,
                "pagination": {"count": page_size, "skip": skip},
            },
            "query": BULK_SEARCH_QUERY,
        }
        r = session.post(URL, headers=BASE_HEADERS, json=payload, timeout=25)
        if r.status_code != 200:
            break
        res = ((r.json().get("data") or {}).get("cruiseSearch") or {}).get("results") or {}
        batch = res.get("cruises") or []
        if not batch:
            break
        all_cruises.extend(batch)
        total = int(res.get("total") or 0)
        skip += page_size
        if skip >= total:
            break
    return all_cruises


def build_staterooms_dataset(ports: str = "FLL,MIA,PCN,TPA,GAL,BYE,SJU,MSY,BWI,LAX", output_json: str = "data/staterooms.json"):
    session = requests.Session(impersonate="chrome")
    print(f"Fetching 2-Adult and 4-Guest (2A+2C) stateroom pricing across ports: {ports}...")

    cruises_4 = fetch_bulk_cruises(session, f"departurePort:{ports}|adults:2|children:2", max_count=500)
    cruises_2 = fetch_bulk_cruises(session, f"departurePort:{ports}|adults:2|children:0", max_count=500)
    print(f"Fetched {len(cruises_4)} quad itineraries and {len(cruises_2)} double itineraries.")

    # Index 2-adult and 4-guest stateroom prices by (itin_code, sail_date, cat_key)
    lookup_2 = {}
    lookup_4 = {}
    sailing_meta = {}
    itin_meta = {}

    def process_cruise_list(cruise_list: list[dict], guests: int, target_lookup: dict):
        for c in cruise_list:
            itin = ((c.get("masterSailing") or {}).get("itinerary")) or {}
            itin_code = itin.get("code") or c.get("id")
            if not itin_code:
                continue
            ship_obj = itin.get("ship") or {}
            ship_code = (ship_obj.get("code") or "").upper()
            ship_name = ship_obj.get("name") or "Unknown Ship"
            ship_cls = CODE_TO_SHIP_CLASS.get(ship_code, "Other")
            port_obj = itin.get("departurePort") or {}
            port_code = (port_obj.get("code") or "").upper()
            port_name = port_obj.get("name") or port_code or "Unknown Port"
            nights = int(itin.get("sailingNights") or 0)
            itin_name = itin.get("name") or "Cruise"

            itin_meta[itin_code] = {
                "itinerary_code": itin_code,
                "itinerary_name": itin_name,
                "ship": ship_name,
                "ship_code": ship_code,
                "ship_class": ship_cls,
                "nights": nights,
                "port": port_name,
                "port_code": port_code,
            }

            for s in c.get("sailings") or []:
                if not isinstance(s, dict):
                    continue
                sail_date = s.get("sailDate")
                if not sail_date:
                    continue
                b_link = s.get("bookingLink") or ""
                if (itin_code, sail_date) not in sailing_meta or b_link:
                    sailing_meta[(itin_code, sail_date)] = b_link

                for p in s.get("stateroomClassPricing") or []:
                    if not isinstance(p, dict):
                        continue
                    sc = p.get("stateroomClass") or {}
                    cat_key = classify_regular_stateroom(sc)
                    if not cat_key:
                        continue
                    price_obj = p.get("price")
                    if not isinstance(price_obj, dict) or price_obj.get("value") is None:
                        continue
                    base_pp, taxes_pp, total_pp, discount_pp = extract_per_person_breakdown(price_obj, s)
                    target_lookup[(itin_code, sail_date, cat_key)] = {
                        "guests": guests,
                        "base_pp": round(base_pp, 2),
                        "total_pp": round(total_pp, 2),
                        "cabin_base": round(base_pp * guests, 2),
                        "cabin_taxes": round(taxes_pp * guests, 2),
                        "cabin_total": round(total_pp * guests, 2),
                        "cabin_discount": round(discount_pp * guests, 2),
                    }

    process_cruise_list(cruises_4, 4, lookup_4)
    process_cruise_list(cruises_2, 2, lookup_2)

    all_sailing_keys = sorted(sailing_meta.keys())
    deals = []
    itineraries_map = {}

    for itin_code, sail_date in all_sailing_keys:
        meta = itin_meta.get(itin_code)
        if not meta:
            continue
        nights = meta["nights"]
        b_link = sailing_meta.get((itin_code, sail_date)) or ""
        pkg_code = itin_code
        if "packageCode=" in b_link:
            pkg_code = b_link.split("packageCode=")[1].split("&")[0]

        tiers_dict = {}
        available_tiers = []

        for cat_key in ["INTERIOR", "OUTSIDE", "BALCONY"]:
            p2 = lookup_2.get((itin_code, sail_date, cat_key))
            p4 = lookup_4.get((itin_code, sail_date, cat_key))
            if not p2 and not p4:
                continue
            s_meta = STATEROOM_META[cat_key]
            t_name = s_meta["tier"]
            available_tiers.append(t_name)
            tiers_dict[t_name] = {
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

        if not available_tiers:
            continue

        # Pick lowest priced category as lead_tier
        def tier_sort_val(t_key):
            t_obj = tiers_dict[t_key]
            ref = t_obj["pricing_quad"] or t_obj["pricing_2a"]
            return ref["cabin_total"]

        available_tiers_sorted = sorted(available_tiers, key=tier_sort_val)
        lead_tier = available_tiers_sorted[0]
        lead_obj = tiers_dict[lead_tier]
        ref_2a = lead_obj["pricing_2a"]
        ref_quad = lead_obj["pricing_quad"]
        ref_main = ref_quad or ref_2a

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

        if ref_2a and ref_quad:
            extra_base_diff = max(0.0, round(ref_quad["cabin_base"] - ref_2a["cabin_base"], 2))
            extra_guest_fare_pp = round(extra_base_diff / 2.0, 2)
            if extra_base_diff <= 1.0:
                is_ksf_promo = True
            else:
                is_holiday_rate = True
        elif cabin_discount > 0:
            is_ksf_promo = True

        if is_ksf_promo:
            rate_tag = f"PROMO: Kids/3rd&4th Free (-${cabin_discount:.0f} off)"
        elif is_holiday_rate:
            rate_tag = f"HOLIDAY/BLACKOUT: 3rd/4th +${extra_guest_fare_pp:.2f}/ea (-${cabin_discount:.0f} off)"
        else:
            rate_tag = "STANDARD"

        deal_record = {
            "date": sail_date,
            "itinerary_code": itin_code,
            "package_code": pkg_code,
            "booking_link": b_link,
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
        deals.append(deal_record)

        if itin_code not in itineraries_map:
            itineraries_map[itin_code] = {
                "itinerary_code": itin_code,
                "itinerary_name": meta["itinerary_name"],
                "ship": meta["ship"],
                "ship_code": meta["ship_code"],
                "ship_class": meta["ship_class"],
                "nights": nights,
                "port": meta["port"],
                "port_code": meta["port_code"],
                "sailing_count": 0,
                "promo_count": 0,
                "lowest_cabin_total": cabin_total,
                "lowest_total_pp": total_pp,
                "lowest_sail_date": sail_date,
                "sail_dates": [],
            }
        itin_entry = itineraries_map[itin_code]
        itin_entry["sailing_count"] += 1
        if is_ksf_promo:
            itin_entry["promo_count"] += 1
        itin_entry["sail_dates"].append(sail_date)
        if cabin_total < itin_entry["lowest_cabin_total"]:
            itin_entry["lowest_cabin_total"] = cabin_total
            itin_entry["lowest_total_pp"] = total_pp
            itin_entry["lowest_sail_date"] = sail_date

    deals.sort(key=lambda x: x["cabin_total"])
    itineraries_list = list(itineraries_map.values())
    for it in itineraries_list:
        it["sail_dates"].sort()
    itineraries_list.sort(key=lambda x: x["lowest_cabin_total"])

    os.makedirs(os.path.dirname(output_json), exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "query": {
            "adults": 2,
            "children": 2,
            "total_guests": 4,
            "ports": ports,
            "mode": "staterooms",
        },
        "summary": {
            "scanned_itineraries": len(itin_meta),
            "itineraries_with_suites": len(itineraries_list),
            "total_suite_sailings": len(deals),
            "promo_sailings": sum(1 for d in deals if d["is_ksf_promo"]),
            "holiday_sailings": sum(1 for d in deals if d["is_holiday_rate"]),
        },
        "itineraries": itineraries_list,
        "sailings": deals,
    }
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Saved {len(deals)} stateroom sailings across {len(itineraries_list)} itineraries to {output_json}!")


if __name__ == "__main__":
    build_staterooms_dataset()


import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
import sys
from curl_cffi import requests

URL = "https://www.royalcaribbean.com/cruises/graph"

BASE_HEADERS = {
    "accept": "application/json",
    "apollographql-client-name": "rcg-cruise-search-client",
    "brand": "R",
    "content-type": "application/json",
    "country": "USA",
    "currency": "USD",
    "language": "en",
    "office": "MIA",
    "origin": "https://www.royalcaribbean.com",
    "referer": "https://www.royalcaribbean.com/cruises?hp_search_widget=home&search=departurePort%3AFLL%2CMIA%2CPCN",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

SHIP_CLASSES = {
    "icon": ["IC", "ST", "LE", "HE"],                  # Icon, Star, Legend, Hero of the Seas
    "oasis": ["OA", "AL", "HM", "SY", "WN", "UT"],     # Oasis, Allure, Harmony, Symphony, Wonder, Utopia
    "quantum": ["QN", "AN", "OV", "SP", "OD"],         # Quantum, Anthem, Ovation, Spectrum, Odyssey
    "freedom": ["FR", "LB", "ID"],                     # Freedom, Liberty, Independence
    "voyager": ["VY", "EX", "AD", "NV", "MA"],         # Voyager, Explorer, Adventure, Navigator, Mariner
    "radiance": ["RD", "BR", "SR", "JW"],              # Radiance, Brilliance, Serenade, Jewel
    "vision": ["GR", "RH", "EN", "VI"],                # Grandeur, Rhapsody, Enchantment, Vision
}

CODE_TO_SHIP_CLASS = {
    code: cls_name.capitalize()
    for cls_name, codes in SHIP_CLASSES.items()
    for code in codes
}

SHIP_NAME_TO_CODE = {
    "icon": "IC",
    "star": "ST",
    "legend": "LE",
    "hero": "HE",
    "oasis": "OA",
    "allure": "AL",
    "harmony": "HM",
    "symphony": "SY",
    "wonder": "WN",
    "utopia": "UT",
    "odyssey": "OD",
    "freedom": "FR",
    "liberty": "LB",
    "independence": "ID",
    "explorer": "EX",
    "adventure": "AD",
    "navigator": "NV",
    "mariner": "MA",
    "brilliance": "BR",
    "jewel": "JW",
    "radiance": "RD",
    "serenade": "SR",
    "rhapsody": "RH",
    "grandeur": "GR",
    "enchantment": "EN",
    "vision": "VI",
}

SEARCH_QUERY = """
query CruisesSearchResults($filters: String, $pagination: CruiseSearchPagination) {
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
            ship { name code }
          }
        }
        sailings {
          sailDate
          bookingLink
        }
      }
    }
  }
}
"""

ITINERARY_PRICING_QUERY = """
query ItineraryPricing($id: String, $partyFilters: String, $ref2AFilters: String, $refQuadFilters: String) {
  partyCruise: cruise(id: $id, filters: $partyFilters) {
    id
    masterSailing {
      itinerary {
        name
        code
        sailingNights
        departurePort { code name }
        ship { name code }
      }
    }
    sailings {
      id
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
          isPromotionApplied
        }
      }
    }
  }
  ref2ACruise: cruise(id: $id, filters: $ref2AFilters) {
    sailings {
      sailDate
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
          isPromotionApplied
        }
      }
    }
  }
  refQuadCruise: cruise(id: $id, filters: $refQuadFilters) {
    sailings {
      sailDate
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
          isPromotionApplied
        }
      }
    }
  }
}
"""


def is_suite_category(sc: dict) -> bool:
    """Filter strictly for suite categories ('S', 'SUITE', 'DELUXE', or categories containing 'Suite')."""
    if not isinstance(sc, dict):
        return False
    sc_id = str(sc.get("id") or "").strip().upper()
    sc_name = str(sc.get("name") or "").strip().upper()
    content = sc.get("content") or {}
    sc_code = str(content.get("code") or "").strip().upper()

    target_codes = {"S", "SUITE", "DELUXE"}
    if sc_id in target_codes or sc_code in target_codes or sc_name in target_codes:
        return True
    if "SUITE" in sc_name or "SUITE" in sc_id:
        return True
    return False


def extract_per_person_breakdown(price_obj: dict, sailing_obj: dict) -> tuple[float, float, float, float]:
    """
    Returns (per_person_base_fare, per_person_taxes, per_person_total_with_taxes, per_person_discount).
    Respects `areTaxesAndFeesIncluded` / `taxesAndFeesIncluded` so taxes are never double-counted.
    """
    val = float(price_obj.get("value") or 0.0)
    sailing_taxes = float((sailing_obj.get("taxesAndFees") or {}).get("value") or 0.0)
    price_taxes = price_obj.get("taxesAndFeesAmount")
    taxes = float(price_taxes if price_taxes is not None else sailing_taxes)

    included = price_obj.get("areTaxesAndFeesIncluded")
    if included is None:
        included = bool(sailing_obj.get("taxesAndFeesIncluded"))

    if included:
        total_pp = val
        base_pp = max(0.0, val - taxes)
    else:
        base_pp = val
        total_pp = val + taxes

    discount_pp = float(price_obj.get("discountAmount") or 0.0)
    return base_pp, taxes, total_pp, discount_pp


def index_reference_suite_base(cruise_node: dict, guest_count: int) -> dict[tuple[str, str], dict]:
    """Build a lookup of {(sailDate, class_id): pricing_dict} for a reference occupancy."""
    lookup = {}
    if not isinstance(cruise_node, dict):
        return lookup
    for s in cruise_node.get("sailings") or []:
        if not isinstance(s, dict):
            continue
        sail_date = s.get("sailDate")
        if not sail_date:
            continue
        for p in s.get("stateroomClassPricing") or []:
            if not isinstance(p, dict):
                continue
            sc = p.get("stateroomClass") or {}
            if not is_suite_category(sc):
                continue
            price_obj = p.get("price")
            if not isinstance(price_obj, dict) or price_obj.get("value") is None:
                continue
            base_pp, taxes_pp, total_pp, discount_pp = extract_per_person_breakdown(price_obj, s)
            sc_key = str(sc.get("id") or sc.get("name") or "SUITE")
            lookup[(sail_date, sc_key)] = {
                "guests": guest_count,
                "base_pp": round(base_pp, 2),
                "total_pp": round(total_pp, 2),
                "cabin_base": round(base_pp * guest_count, 2),
                "cabin_taxes": round(taxes_pp * guest_count, 2),
                "cabin_total": round(total_pp * guest_count, 2),
                "cabin_discount": round(discount_pp * guest_count, 2),
            }
    return lookup


ROYAL_SUITE_CLASSES = {"Icon", "Oasis", "Quantum"}

SUITE_TIER_AMENITIES = {
    "Sea": "Coastal Kitchen (Dinner), Luxury Pillowtop Mattress, Lavazza Espresso Machine, Priority Boarding",
    "Sky": "All-Day Coastal Kitchen, Suite Lounge & Sun Deck, Complimentary VOOM Wi-Fi, Concierge Service, Priority Boarding",
    "Star": "Royal Genie Service, Complimentary Specialty Dining, Complimentary Deluxe Beverage Package, Gratuities Included, VOOM Wi-Fi",
    "Standard": "Priority Boarding, Dedicated Stateroom Attendant, Luxury Robes & Toiletries, Double Crown & Anchor Points",
}


def scale_ref_pricing(ref: dict | None, factor: float) -> dict | None:
    if not isinstance(ref, dict):
        return None
    guests = max(1, int(ref.get("guests") or 2))
    new_base = round(float(ref.get("cabin_base") or 0.0) * factor, 2)
    taxes = round(float(ref.get("cabin_taxes") or 0.0), 2)
    new_total = round(new_base + taxes, 2)
    new_disc = round(float(ref.get("cabin_discount") or 0.0) * factor, 2)
    return {
        "guests": guests,
        "base_pp": round(new_base / guests, 2),
        "total_pp": round(new_total / guests, 2),
        "cabin_base": new_base,
        "cabin_taxes": taxes,
        "cabin_total": new_total,
        "cabin_discount": new_disc,
    }


def build_suite_tiers(
    ship_cls: str,
    nights: int,
    ref_2a: dict | None,
    ref_quad: dict | None,
    ship_code: str = "",
) -> tuple[str, list[str], dict]:
    """
    Determines the Royal Suite Class tier ('Sea', 'Sky', 'Star', or 'Standard')
    and builds tier-level pricing options with single, specific lead room names.
    """
    sample_ref = ref_2a or ref_quad or {}
    base_2a_est = float(sample_ref.get("cabin_base") or 0.0)
    if not ref_2a and ref_quad:
        base_2a_est = base_2a_est * 0.75
    per_night_2a = base_2a_est / max(1, nights)

    if ship_cls not in ROYAL_SUITE_CLASSES:
        std_room = "Grand Suite - 1 Bedroom (GS)" if per_night_2a >= 650 else "Junior Suite (JS)"
        tiers = {
            "Standard": {
                "tier": "Standard",
                "label": "Standard Suite",
                "room_name": std_room,
                "other_rooms": "Other suites: Grand Suite (GS), Owner's Suite (OS), Royal Suite (RS)",
                "amenities": SUITE_TIER_AMENITIES["Standard"],
                "pricing_2a": ref_2a,
                "pricing_quad": ref_quad,
            }
        }
        return "Standard", ["Standard"], tiers

    if ship_cls == "Icon":
        sea_room = "Surfside Family Suite (SF)"
        sea_other = "Also in Sea Class: Junior Suite (JS)"
        sky_room = "Sky Junior Suite (JY)"
        sky_other = "Also in Sky Class: Panoramic Suite (VP), Sunset Junior Suite (JT), Sunset Suite (SS), Infinite Grand Suite (IG), Grand Suite (GS), Owner's Suite (OW)"
        star_room = "Ultimate Family Townhouse (UL)" if per_night_2a >= 4500 else "Icon Loft Suite (IL)"
        star_other = "Also in Star Class: Royal Loft Suite (RL), Ultimate Family Townhouse (UL)"
        sky_mult = 1.32
        star_mult = 2.65
    else:
        sea_room = "Junior Suite (JS)"
        sea_other = "Also in Sea Class: Spacious Sunset Junior Suite (JT)"
        sky_room = "Sky Junior Suite (JY)" if ship_code in ("WN", "UT") else "Grand Suite - 1 Bedroom (GS)"
        sky_other = "Also in Sky Class: Grand Suite (GS), Crown Loft Suite (CL), Owner's Suite (OW), 1BR AquaTheater Suite (A3/A4)"
        star_room = "Royal Loft Suite (RL)" if per_night_2a >= 3500 else "Star Loft Suite (TL)"
        star_other = "Also in Star Class: 2BR AquaTheater Suite (A1/A2), Ultimate Panoramic Suite (UP), Royal Loft Suite (RL)"
        sky_mult = 1.28
        star_mult = 2.15

    tiers = {}
    if per_night_2a >= 2200:
        lead_tier = "Star"
        tiers["Star"] = {
            "tier": "Star",
            "label": "Star Class",
            "room_name": star_room,
            "other_rooms": star_other,
            "amenities": SUITE_TIER_AMENITIES["Star"],
            "pricing_2a": ref_2a,
            "pricing_quad": ref_quad,
        }
    elif per_night_2a >= 1150:
        lead_tier = "Sky"
        tiers["Sky"] = {
            "tier": "Sky",
            "label": "Sky Class",
            "room_name": sky_room,
            "other_rooms": sky_other,
            "amenities": SUITE_TIER_AMENITIES["Sky"],
            "pricing_2a": ref_2a,
            "pricing_quad": ref_quad,
        }
        tiers["Star"] = {
            "tier": "Star",
            "label": "Star Class",
            "room_name": star_room,
            "other_rooms": star_other,
            "amenities": SUITE_TIER_AMENITIES["Star"],
            "pricing_2a": scale_ref_pricing(ref_2a, round(star_mult / sky_mult, 2)),
            "pricing_quad": scale_ref_pricing(ref_quad, round(star_mult / sky_mult, 2)),
        }
    else:
        lead_tier = "Sea"
        tiers["Sea"] = {
            "tier": "Sea",
            "label": "Sea Class",
            "room_name": sea_room,
            "other_rooms": sea_other,
            "amenities": SUITE_TIER_AMENITIES["Sea"],
            "pricing_2a": ref_2a,
            "pricing_quad": ref_quad,
        }
        tiers["Sky"] = {
            "tier": "Sky",
            "label": "Sky Class",
            "room_name": sky_room,
            "other_rooms": sky_other,
            "amenities": SUITE_TIER_AMENITIES["Sky"],
            "pricing_2a": scale_ref_pricing(ref_2a, sky_mult),
            "pricing_quad": scale_ref_pricing(ref_quad, sky_mult),
        }
        tiers["Star"] = {
            "tier": "Star",
            "label": "Star Class",
            "room_name": star_room,
            "other_rooms": star_other,
            "amenities": SUITE_TIER_AMENITIES["Star"],
            "pricing_2a": scale_ref_pricing(ref_2a, star_mult),
            "pricing_quad": scale_ref_pricing(ref_quad, star_mult),
        }

    return lead_tier, list(tiers.keys()), tiers


def resolve_ship_codes(ship_class: str | None, ships: str | None) -> list[str]:
    codes = []
    if ship_class and ship_class.lower() != "all":
        for cls_name in ship_class.split(","):
            cls_key = cls_name.strip().lower()
            if cls_key in SHIP_CLASSES:
                codes.extend(SHIP_CLASSES[cls_key])
    if ships:
        for token in ships.split(","):
            clean = token.strip().lower()
            if not clean:
                continue
            if clean in SHIP_NAME_TO_CODE:
                codes.append(SHIP_NAME_TO_CODE[clean])
            elif clean in SHIP_CLASSES:
                codes.extend(SHIP_CLASSES[clean])
            else:
                codes.append(clean.upper())
    # Deduplicate preserving order
    return list(dict.fromkeys(codes))


def fetch_itinerary_pricing(itin_code: str, master_itin: dict, party_filter_str: str, pricing_headers: dict) -> tuple[dict, dict]:
    session = requests.Session(impersonate="chrome")
    pricing_payload = {
        "query": ITINERARY_PRICING_QUERY,
        "variables": {
            "id": itin_code,
            "partyFilters": party_filter_str,
            "ref2AFilters": "adults:2|children:0",
            "refQuadFilters": "adults:2|children:2"
        }
    }
    p_resp = session.post(URL, headers=pricing_headers, json=pricing_payload, timeout=20)
    p_resp.raise_for_status()
    return master_itin, p_resp.json()


def track_suite_prices(
    adults: int = 2,
    children: int = 0,
    ports: str = "FLL,MIA,PCN",
    count: int = 300,
    pricing_filter: str = "all",
    ship_class: str | None = None,
    ships: str | None = None,
    output_json: str | None = "data/suites.json",
):
    total_guests = adults + children
    if total_guests < 1:
        print("Error: Party size must include at least 1 guest.", file=sys.stderr)
        return

    party_filter_str = f"adults:{adults}|children:{children}"
    filter_parts = [f"departurePort:{ports}", party_filter_str]

    ship_codes = resolve_ship_codes(ship_class, ships)
    if ship_codes:
        filter_parts.append(f"ship:{','.join(ship_codes)}")

    search_filter_str = "|".join(filter_parts)

    session = requests.Session(impersonate="chrome")
    search_headers = {
        **BASE_HEADERS,
        "apollographql-query-name": "GetSearchResults",
        "x-num-adults": str(adults),
        "x-num-children": str(children),
    }

    cruises = []
    skip = 0
    page_size = min(count, 100)

    while len(cruises) < count:
        batch_count = min(page_size, count - len(cruises))
        search_payload = {
            "query": SEARCH_QUERY,
            "variables": {
                "filters": search_filter_str,
                "pagination": {"count": batch_count, "skip": skip}
            }
        }

        try:
            resp = session.post(URL, headers=search_headers, json=search_payload, timeout=20)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            print(f"Error fetching search results: {e}", file=sys.stderr)
            break

        results_node = (raw.get("data") or {}).get("cruiseSearch", {}).get("results") or {}
        batch = results_node.get("cruises") or []
        total_available = results_node.get("total") or 0

        if not batch:
            break

        cruises.extend(batch)
        skip += len(batch)
        if skip >= total_available:
            break

    if not cruises:
        print("No cruises returned from search.")
        return

    pricing_headers = {
        **BASE_HEADERS,
        "apollographql-query-name": "ItineraryPricing",
        "x-num-adults": str(adults),
        "x-num-children": str(children),
    }

    itineraries_to_fetch = []
    processed_itineraries = set()

    for c in cruises:
        if not isinstance(c, dict):
            continue
        master_itin = (c.get("masterSailing") or {}).get("itinerary") or {}
        itin_code = master_itin.get("code") or c.get("id")
        if not itin_code or itin_code in processed_itineraries:
            continue
        processed_itineraries.add(itin_code)
        itineraries_to_fetch.append((itin_code, master_itin))

    deals = []
    seen_deal_keys = set()
    itineraries_map = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(fetch_itinerary_pricing, itin_code, master_itin, party_filter_str, pricing_headers)
            for itin_code, master_itin in itineraries_to_fetch
        ]

        for future in as_completed(futures):
            try:
                master_itin, p_raw = future.result()
            except Exception:
                continue

            data_node = p_raw.get("data") or {}
            cruise_data = data_node.get("partyCruise") or {}
            ref_2a_lookup = index_reference_suite_base(data_node.get("ref2ACruise") or {}, 2)
            ref_quad_lookup = index_reference_suite_base(data_node.get("refQuadCruise") or {}, 4)

            itin = (cruise_data.get("masterSailing") or {}).get("itinerary") or master_itin
            itin_code = itin.get("code") or master_itin.get("code") or cruise_data.get("id") or "UNKNOWN"
            itin_name = itin.get("name") or master_itin.get("name") or "Caribbean Cruise"
            ship_obj = itin.get("ship") or master_itin.get("ship") or {}
            ship = ship_obj.get("name") or "Unknown Ship"
            ship_code = (ship_obj.get("code") or "").upper()
            ship_cls = CODE_TO_SHIP_CLASS.get(ship_code, "Other")
            port_obj = itin.get("departurePort") or master_itin.get("departurePort") or {}
            port = port_obj.get("name") or port_obj.get("code") or "Unknown Port"
            port_code = (port_obj.get("code") or "").upper()
            nights = int(itin.get("sailingNights") or master_itin.get("sailingNights") or 0)

            sailings = cruise_data.get("sailings") or []
            for s in sailings:
                if not isinstance(s, dict):
                    continue

                sail_date = s.get("sailDate") or "Unknown Date"
                pricing_list = s.get("stateroomClassPricing") or []

                for p in pricing_list:
                    if not isinstance(p, dict):
                        continue

                    stateroom_class = p.get("stateroomClass") or {}
                    if not is_suite_category(stateroom_class):
                        continue

                    price_obj = p.get("price")
                    if not isinstance(price_obj, dict) or price_obj.get("value") is None:
                        continue

                    base_pp, taxes_pp, total_pp, discount_pp = extract_per_person_breakdown(price_obj, s)

                    cabin_base = round(base_pp * total_guests, 2)
                    cabin_taxes = round(taxes_pp * total_guests, 2)
                    cabin_total = round(total_pp * total_guests, 2)
                    cabin_discount = round(discount_pp * total_guests, 2)
                    price_per_night = round(cabin_total / nights, 2) if nights > 0 else cabin_total

                    sc_key = str(stateroom_class.get("id") or stateroom_class.get("name") or "SUITE")
                    ref_2a = ref_2a_lookup.get((sail_date, sc_key))
                    ref_quad = ref_quad_lookup.get((sail_date, sc_key))
                    base_2a = ref_2a["cabin_base"] if ref_2a else None
                    base_quad = ref_quad["cabin_base"] if ref_quad else None

                    # Determine Promo (Kids Sail Free) vs Holiday/Blackout pricing
                    is_ksf_promo = False
                    is_holiday_rate = False
                    extra_guest_fare_pp = None

                    if total_guests > 2 and base_2a is not None:
                        extra_guests = total_guests - 2
                        extra_base_diff = max(0.0, round(cabin_base - base_2a, 2))
                        extra_guest_fare_pp = round(extra_base_diff / extra_guests, 2)
                        if extra_base_diff <= 1.0:
                            is_ksf_promo = True
                        else:
                            is_holiday_rate = True
                    elif base_2a is not None and base_quad is not None:
                        extra_base_diff = max(0.0, round(base_quad - base_2a, 2))
                        extra_guest_fare_pp = round(extra_base_diff / 2.0, 2)
                        if extra_base_diff <= 1.0:
                            is_ksf_promo = True
                        else:
                            is_holiday_rate = True
                    else:
                        if cabin_discount > 0:
                            is_ksf_promo = True

                    if pricing_filter == "promo" and not is_ksf_promo:
                        continue
                    if pricing_filter == "holiday" and not is_holiday_rate:
                        continue

                    if is_ksf_promo:
                        rate_tag = f"PROMO: Kids/3rd&4th Free (-${cabin_discount:.0f} off)"
                    elif is_holiday_rate and extra_guest_fare_pp is not None:
                        rate_tag = f"HOLIDAY/BLACKOUT: 3rd/4th +${extra_guest_fare_pp:.2f}/ea (-${cabin_discount:.0f} off)"
                    elif cabin_discount > 0:
                        rate_tag = f"PROMO (-${cabin_discount:.0f} off)"
                    else:
                        rate_tag = "STANDARD"

                    category = (
                        stateroom_class.get("name")
                        or stateroom_class.get("id")
                        or (stateroom_class.get("content") or {}).get("code")
                        or "Suite"
                    )

                    deal_key = (sail_date, ship, category, cabin_total)
                    if deal_key in seen_deal_keys:
                        continue
                    seen_deal_keys.add(deal_key)

                    lead_tier, available_tiers, tiers_dict = build_suite_tiers(
                        ship_cls, nights, ref_2a, ref_quad, ship_code=ship_code
                    )

                    b_link = s.get("bookingLink") or ""
                    pkg_code = itin_code
                    if "packageCode=" in b_link:
                        pkg_code = b_link.split("packageCode=")[1].split("&")[0]

                    deal_record = {
                        "date": sail_date,
                        "itinerary_code": itin_code,
                        "package_code": pkg_code,
                        "booking_link": b_link,
                        "itinerary_name": itin_name,
                        "ship": ship,
                        "ship_code": ship_code,
                        "ship_class": ship_cls,
                        "nights": nights,
                        "port": port,
                        "port_code": port_code,
                        "category": category,
                        "suite_tier": lead_tier,
                        "available_tiers": available_tiers,
                        "tiers": tiers_dict,
                        "base_pp": round(base_pp, 2),
                        "total_pp": round(total_pp, 2),
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
                            "itinerary_name": itin_name,
                            "ship": ship,
                            "ship_code": ship_code,
                            "ship_class": ship_cls,
                            "nights": nights,
                            "port": port,
                            "port_code": port_code,
                            "sailing_count": 0,
                            "promo_count": 0,
                            "lowest_cabin_total": cabin_total,
                            "lowest_total_pp": round(total_pp, 2),
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
                        itin_entry["lowest_total_pp"] = round(total_pp, 2)
                        itin_entry["lowest_sail_date"] = sail_date

    deals.sort(key=lambda x: x["cabin_total"])

    itineraries_list = list(itineraries_map.values())
    for it in itineraries_list:
        it["sail_dates"].sort()
    itineraries_list.sort(key=lambda x: x["lowest_cabin_total"])

    if output_json and deals:
        # Preserve any existing live room-subtype tiers (from enrich_subtypes.py) so a fresh baseline run
        # never wipes out Sea/Sky/Star room-level breakdowns before enrich_subtypes.py runs.
        if os.path.exists(output_json):
            try:
                with open(output_json, "r", encoding="utf-8") as prev_f:
                    prev_data = json.load(prev_f)
                prev_live_map = {
                    (s.get("ship_code"), s.get("date")): s
                    for s in (prev_data.get("sailings") or [])
                    if any(v.get("is_live_exact") for v in (s.get("tiers") or {}).values())
                }
                for d in deals:
                    prev_s = prev_live_map.get((d.get("ship_code"), d.get("date")))
                    if prev_s:
                        for fld in (
                            "suite_tier",
                            "available_tiers",
                            "tiers",
                            "pricing_2a",
                            "pricing_quad",
                            "base_pp",
                            "total_pp",
                            "cabin_base",
                            "cabin_taxes",
                            "cabin_total",
                            "cabin_discount",
                            "price_per_night",
                            "extra_guest_fare_pp",
                            "is_ksf_promo",
                            "is_holiday_rate",
                            "rate_tag",
                            "enriched_at",
                        ):
                            if fld in prev_s:
                                d[fld] = prev_s[fld]
            except Exception as e:
                print(f"Warning: could not merge previous live tiers from {output_json}: {e}")

        out_dir = os.path.dirname(output_json)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        export_payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "query": {
                "adults": adults,
                "children": children,
                "total_guests": total_guests,
                "ports": ports,
                "pricing_filter": pricing_filter,
                "ship_class": ship_class,
                "ships": ships,
            },
            "summary": {
                "scanned_itineraries": len(itineraries_to_fetch),
                "itineraries_with_suites": len(itineraries_list),
                "total_suite_sailings": len(deals),
                "promo_sailings": sum(1 for d in deals if d["is_ksf_promo"]),
                "holiday_sailings": sum(1 for d in deals if d["is_holiday_rate"]),
            },
            "itineraries": itineraries_list,
            "sailings": deals,
        }
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(export_payload, f, indent=2)

    ship_summary = f"Ships: {','.join(ship_codes)}" if ship_codes else f"Scanned {len(itineraries_to_fetch)} Itineraries"
    print(
        f"=== Royal Caribbean Suite Tracker | Party: {adults} Adult(s), {children} Child(ren) "
        f"({total_guests} Guests) | {ship_summary} | Filter: {pricing_filter.upper()} | Deals Found: {len(deals)} ===\n"
    )

    for d in deals:
        print(
            f"[{d['date']}] {d['ship']} ({d['nights']}N) | {d['port']} -> {d['category']}: "
            f"${d['total_pp']:.2f}/pp (Est Total w/ Taxes:${d['cabin_total']:.2f} "
            f"[Fare:${d['cabin_base']:.2f} + Tax:${d['cabin_taxes']:.2f}]) | {d['rate_tag']}"
        )


def parse_args():
    parser = argparse.ArgumentParser(description="Royal Caribbean Suite Price Tracker with Party Size, Ship Class & Promo/Holiday Breakdown")
    parser.add_argument("-a", "--adults", type=int, default=2, help="Number of adults (ages 13+), default: 2")
    parser.add_argument("-c", "--children", type=int, default=0, help="Number of children (ages 12 and under), default: 0")
    parser.add_argument(
        "-p", "--ports",
        type=str,
        default="FLL,MIA,PCN,TPA,GAL,BYE,SJU,MSY,BWI,LAX",
        help="Comma-separated departure port codes (default: FLL,MIA,PCN,TPA,GAL,BYE,SJU,MSY,BWI,LAX)"
    )
    parser.add_argument("-n", "--count", type=int, default=500, help="Max number of cruise itineraries to search (default: 500)")
    parser.add_argument(
        "-t", "--type",
        choices=["all", "promo", "holiday"],
        default="all",
        help="Pricing filter: 'all' (both promo & holiday), 'promo' (Kids Sail Free), or 'holiday' (blackout/paid 3rd & 4th)"
    )
    parser.add_argument(
        "--ship-class",
        type=str,
        default=None,
        help="Filter by ship class: icon, oasis, quantum, freedom, voyager, radiance, vision (or comma-separated)"
    )
    parser.add_argument(
        "-s", "--ships",
        type=str,
        default=None,
        help="Filter by ship names or 2-letter codes (e.g. 'icon,star,legend' or 'IC,ST,LE,HE')"
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default="data/suites.json",
        help="Path to export JSON data for the GitHub Pages frontend (default: data/suites.json)"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    track_suite_prices(
        adults=args.adults,
        children=args.children,
        ports=args.ports,
        count=args.count,
        pricing_filter=args.type,
        ship_class=args.ship_class,
        ships=args.ships,
        output_json=args.output,
    )
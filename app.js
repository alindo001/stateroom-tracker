const APP_MODE = document.body?.dataset?.mode || "suites";
const IS_STATEROOMS = APP_MODE === "staterooms";

const ALL_TIERS = IS_STATEROOMS
  ? ["Interior", "OceanView", "Balcony"]
  : ["Sea", "Sky", "Star", "Standard", "Guarantee"];

const DEFAULT_TIERS = IS_STATEROOMS
  ? ["Interior", "OceanView", "Balcony"]
  : ["Sea", "Sky", "Star", "Standard"];

const DEFAULT_PORTS = ["FLL", "MIA", "PCN"];
const FLORIDA_PORTS = ["FLL", "MIA", "PCN", "TPA"];
const STORAGE_KEY = IS_STATEROOMS
  ? "rc_stateroom_tracker_prefs_v1"
  : "rc_suite_tracker_prefs_v1";
const DATA_URL = IS_STATEROOMS ? "./data/staterooms.json" : "./data/suites.json";
const ITEM_LABEL = IS_STATEROOMS ? "Stateroom" : "Suite";
const ITEMS_LABEL = IS_STATEROOMS ? "Staterooms" : "Suites";
const ITEMS_LOWER = IS_STATEROOMS ? "stateroom" : "suite";

const KNOWN_PORT_NAMES = {
  FLL: "Fort Lauderdale",
  MIA: "Miami",
  PCN: "Port Canaveral (Orlando)",
  TPA: "Tampa",
  GAL: "Galveston",
  BYE: "Cape Liberty (New York)",
  SJU: "San Juan",
  MSY: "New Orleans",
  BWI: "Baltimore",
  LAX: "Los Angeles",
  SEA: "Seattle",
  BOS: "Boston",
  SAN: "San Diego",
  BCN: "Barcelona",
  ROM: "Rome (Civitavecchia)",
};

const state = {
  rawData: null,
  activeView: "itineraries",
  sortBy: "total_asc",
  adults: 2,
  children: 2,
  roomMode: "two_rooms", // "two_rooms" | "single_room_5p" (used when adults + children > 4)
  allAvailablePorts: new Map(), // code -> { name, count }
  customPorts: new Map(),       // code -> name
  filters: {
    shipClass: "all",
    ship: "all",
    ports: new Set(DEFAULT_PORTS),
    nights: "all",
    dateStart: "",
    dateEnd: "",
    rateType: "all",
    search: "",
    suiteTiers: new Set(DEFAULT_TIERS),
  },
  expandedItineraries: new Set(),
};

function saveStateToStorage() {
  try {
    const payload = {
      activeView: state.activeView,
      sortBy: state.sortBy,
      adults: state.adults,
      children: state.children,
      roomMode: state.roomMode,
      customPorts: Array.from(state.customPorts.entries()),
      filters: {
        shipClass: state.filters.shipClass,
        ship: state.filters.ship,
        ports: Array.from(state.filters.ports),
        nights: state.filters.nights,
        dateStart: state.filters.dateStart,
        dateEnd: state.filters.dateEnd,
        rateType: state.filters.rateType,
        search: state.filters.search,
        suiteTiers: Array.from(state.filters.suiteTiers),
      },
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
    const badge = document.getElementById("saved-prefs-badge");
    if (badge) {
      badge.textContent = "Filters saved";
    }
  } catch (err) {
    console.warn("Could not save filters to localStorage:", err);
  }
}

function loadStateFromStorage() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return false;
    const saved = JSON.parse(raw);
    if (!saved || typeof saved !== "object") return false;

    if (saved.activeView === "itineraries" || saved.activeView === "sailings") {
      state.activeView = saved.activeView;
    }
    if (typeof saved.sortBy === "string") {
      state.sortBy = saved.sortBy;
    }
    if (Number.isInteger(saved.adults) && saved.adults >= 1 && saved.adults <= 4) {
      state.adults = saved.adults;
    }
    if (Number.isInteger(saved.children) && saved.children >= 0 && saved.children <= 6) {
      state.children = saved.children;
    }
    if (saved.roomMode === "two_rooms" || saved.roomMode === "single_room_5p") {
      state.roomMode = IS_STATEROOMS ? "two_rooms" : saved.roomMode;
    }
    if (Array.isArray(saved.customPorts)) {
      state.customPorts = new Map(saved.customPorts);
    }
    if (saved.filters && typeof saved.filters === "object") {
      const f = saved.filters;
      if (typeof f.shipClass === "string") state.filters.shipClass = f.shipClass;
      if (typeof f.ship === "string") state.filters.ship = f.ship;
      if (Array.isArray(f.ports)) {
        state.filters.ports = new Set(f.ports.map((p) => String(p).toUpperCase()));
      }
      if (typeof f.nights === "string") state.filters.nights = f.nights;
      if (typeof f.dateStart === "string") state.filters.dateStart = f.dateStart;
      if (typeof f.dateEnd === "string") state.filters.dateEnd = f.dateEnd;
      if (typeof f.rateType === "string") state.filters.rateType = f.rateType;
      if (typeof f.search === "string") state.filters.search = f.search;
      if (Array.isArray(f.suiteTiers)) {
        state.filters.suiteTiers = new Set(f.suiteTiers.filter((t) => ALL_TIERS.includes(t)));
      }
    }
    return true;
  } catch (err) {
    console.warn("Could not load filters from localStorage:", err);
    return false;
  }
}

const currencyFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const wholeCurrencyFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

function formatMoney(val, decimals = true) {
  if (val == null || Number.isNaN(Number(val))) return "--";
  return decimals ? currencyFmt.format(Number(val)) : wholeCurrencyFmt.format(Number(val));
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function getShipClassBadgeClass(shipClass) {
  const cls = (shipClass || "").toLowerCase();
  if (cls === "icon") return "badge-class-icon";
  if (cls === "oasis") return "badge-class-oasis";
  return "badge-class-default";
}

function getTierDisplayLabel(tierKey) {
  const map = {
    Sea: "Sea Class",
    Sky: "Sky Class",
    Star: "Star Class",
    Standard: "Standard Suite",
    Guarantee: "Suite Guarantee",
    Interior: "Interior",
    OceanView: "Ocean View",
    Balcony: "Balcony",
  };
  return map[tierKey] || tierKey || ITEM_LABEL;
}

function getTierBadgeHtml(tierKey, roomName = "") {
  const icons = {
    Sea: "Sea Class",
    Sky: "Sky Class",
    Star: "Star Class",
    Standard: "Standard Suite",
    Guarantee: "Suite Guarantee (WS)",
    Interior: "Interior",
    OceanView: "Ocean View",
    Balcony: "Balcony",
  };
  const clsMap = {
    Sea: "badge-tier-sea",
    Sky: "badge-tier-sky",
    Star: "badge-tier-star",
    Standard: "badge-tier-standard",
    Guarantee: "badge-tier-guarantee",
    Interior: "badge-tier-interior",
    OceanView: "badge-tier-oceanview",
    Balcony: "badge-tier-balcony",
  };
  const label = icons[tierKey] || tierKey || ITEM_LABEL;
  const badgeCls = clsMap[tierKey] || "badge-tier-standard";
  const roomPart = roomName
    ? ` <span class="tier-lead-room">&bull; Starts at: ${escapeHtml(roomName)}</span>`
    : "";
  return `<span class="badge ${badgeCls}">${label}${roomPart}</span>`;
}

function getRoomOccupancyInfo(roomNameOrCode, shipClass) {
  const str = String(roomNameOrCode || "").trim();
  const codeMatch = str.match(/\(([A-Z0-9]{1,3})\)/);
  const code = (codeMatch ? codeMatch[1] : str).toUpperCase();
  const clsLower = String(shipClass || "").toLowerCase();

  if (code === "VS") {
    return { code, maxGuests: 14, pullmanWarning: false };
  }
  if (["UL", "TH", "US", "A1", "A2", "GT", "OT"].includes(code)) {
    return { code, maxGuests: 8, pullmanWarning: false };
  }
  if (["RL", "VY"].includes(code)) {
    return { code, maxGuests: 6, pullmanWarning: false };
  }
  if (["JS", "JY", "J1", "CJ"].includes(code) && clsLower !== "icon") {
    return { code, maxGuests: 5, pullmanWarning: true };
  }
  return { code, maxGuests: 4, pullmanWarning: false };
}

function splitPartyAcrossTwoRooms(adults, children) {
  const totalGuests = adults + children;
  const room1Total = Math.min(4, Math.ceil(totalGuests / 2));
  const room2Total = Math.min(4, Math.max(1, totalGuests - room1Total));

  // Royal online booking requires >=1 adult per room; if user selected 1 adult total, pass 1A in each room URL
  const r0a = Math.min(room1Total, Math.max(1, Math.ceil(adults / 2)));
  const r0c = Math.max(0, room1Total - r0a);
  const remainingAdults = Math.max(0, adults - r0a);
  const r1a = Math.min(room2Total, Math.max(1, remainingAdults));
  const r1c = Math.max(0, room2Total - r1a);

  return { room1Total, room2Total, r0a, r0c, r1a, r1c };
}

function getSuiteTierCellHtml(sailing) {
  const tierKey = sailing.active_tier || (IS_STATEROOMS ? "Interior" : "Standard");
  const roomName = sailing.room_name || sailing.category || ITEM_LABEL;
  const otherRooms = sailing.other_rooms || "";
  const amenities = sailing.amenities || "";
  const isLive = Boolean(sailing.is_live_exact);
  const twoRoomNote = sailing.two_room_split_note || "";
  const occWarn = sailing.occupancy_warning || "";

  const otherPrefix = otherRooms.toLowerCase().startsWith("also")
    ? escapeHtml(otherRooms)
    : `Also in ${escapeHtml(getTierDisplayLabel(tierKey))}: ${escapeHtml(otherRooms)}`;

  return `
    <div class="suite-cell-header">
      ${getTierBadgeHtml(tierKey)}
      <span class="suite-lead-room" title="Entry-level room for this category price">${escapeHtml(roomName)}</span>
      ${isLive ? `<span class="live-price-tag" title="Exact room price verified from Royal Caribbean">${IS_STATEROOMS ? "Live Rate" : "Live Room Price"}</span>` : ""}
    </div>
    ${twoRoomNote ? `<span class="two-room-split-tag">${escapeHtml(twoRoomNote)}</span>` : ""}
    ${occWarn ? `<span class="occupancy-warn-note">${escapeHtml(occWarn)}</span>` : ""}
    ${otherRooms ? `<span class="suite-other-rooms">${otherPrefix}</span>` : ""}
    <span class="amenities-sub">${escapeHtml(amenities)}</span>
  `;
}

function buildRoyalSuiteUrl(raw, adults, children, royalType = "DELUXE", isTwoRooms = false) {
  const totalGuests = (Number(adults) || 2) + (Number(children) || 0);
  const r0d = royalType || (IS_STATEROOMS ? "BALCONY" : "DELUXE");

  let params;
  if (raw.booking_link) {
    const queryPart = raw.booking_link.replace(/^\/booking\/landing\?/, "");
    params = new URLSearchParams(queryPart);
    for (const key of Array.from(params.keys())) {
      if (/^r\d+[a-zA-Z]$/.test(key)) {
        params.delete(key);
      }
    }
  } else {
    const groupId = raw.itinerary_code || `${raw.ship_code || ""}${String(raw.nights || 7).padStart(2, "0")}${raw.port_code || "MIA"}`;
    const pkgCode = raw.package_code || groupId;
    params = new URLSearchParams({
      groupId,
      sailDate: raw.date || "",
      shipCode: raw.ship_code || "",
      packageCode: pkgCode,
      destinationCode: "CARIB",
      selectedCurrencyCode: "USD",
      country: "USA",
    });
  }

  if (totalGuests > 4 && isTwoRooms) {
    const split = splitPartyAcrossTwoRooms(Number(adults) || 2, Number(children) || 0);
    params.set("r0a", String(split.r0a));
    params.set("r0c", String(split.r0c));
    params.set("r0b", "n");
    params.set("r0d", r0d);
    params.set("r0q", "n");
    params.set("r0r", "n");
    params.set("r0s", "n");
    params.set("r0t", "n");

    params.set("r1a", String(split.r1a));
    params.set("r1c", String(split.r1c));
    params.set("r1b", "n");
    params.set("r1d", r0d);
    params.set("r1q", "n");
    params.set("r1r", "n");
    params.set("r1s", "n");
    params.set("r1t", "n");
  } else {
    // Royal Caribbean caps online single-room URLs at 4 guests total
    let r0a = Math.max(1, Math.min(4, Number(adults) || 2));
    let r0c = Math.max(0, Number(children) || 0);
    if (r0a + r0c > 4) {
      r0c = Math.max(0, 4 - r0a);
    }
    params.set("r0a", String(r0a));
    params.set("r0c", String(r0c));
    params.set("r0b", "n");
    params.set("r0d", r0d);
    params.set("r0q", "n");
    params.set("r0r", "n");
    params.set("r0s", "n");
    params.set("r0t", "n");
  }

  return `https://www.royalcaribbean.com/room-selection/type-and-subtype?${params.toString()}`;
}

function getRateBadgeHtml(sailing) {
  if (sailing.is_ksf_promo) {
    return `<span class="rate-pill rate-promo">${escapeHtml(sailing.rate_tag)}</span>`;
  }
  if (sailing.is_holiday_rate) {
    return `<span class="rate-pill rate-holiday">${escapeHtml(sailing.rate_tag)}</span>`;
  }
  return `<span class="rate-pill rate-standard">${escapeHtml(sailing.rate_tag || "STANDARD")}</span>`;
}

function findCapable5PlusRoomForTier(raw, tierObj, totalGuests) {
  if (!tierObj) return null;
  const shipClass = raw.ship_class || "";

  if (Array.isArray(tierObj.rooms) && tierObj.rooms.length > 0) {
    const valid = tierObj.rooms.filter((r) => {
      if (!r.pricing_quad) return false;
      const occ = getRoomOccupancyInfo(r.display_name || r.code, shipClass);
      const maxG = r.max_guests || occ.maxGuests;
      return maxG >= totalGuests;
    });
    if (valid.length === 0) return null;
    valid.sort((a, b) => a.pricing_quad.cabin_total - b.pricing_quad.cabin_total);
    const chosen = valid[0];
    const occ = getRoomOccupancyInfo(chosen.display_name || chosen.code, shipClass);
    return {
      roomName: chosen.display_name,
      p2: chosen.pricing_2a || tierObj.pricing_2a,
      p4: chosen.pricing_quad,
      maxGuests: chosen.max_guests || occ.maxGuests,
      pullmanWarning: chosen.pullman_warning != null ? Boolean(chosen.pullman_warning) : occ.pullmanWarning,
      otherRooms: valid
        .slice(1, 6)
        .map((o) => `${o.display_name}: $${Math.round(o.pricing_quad.cabin_total).toLocaleString("en-US")}`)
        .join(" • "),
    };
  }

  // Fallback for existing suites.json before tierObj.rooms array is populated
  const leadQuadName = tierObj.room_name_quad || tierObj.room_name || "";
  const leadOcc = getRoomOccupancyInfo(leadQuadName, shipClass);
  const candidates = [];

  if (leadOcc.maxGuests >= totalGuests && tierObj.pricing_quad) {
    candidates.push({
      roomName: leadQuadName,
      p2: tierObj.pricing_2a,
      p4: tierObj.pricing_quad,
      maxGuests: leadOcc.maxGuests,
      pullmanWarning: leadOcc.pullmanWarning,
    });
  }

  const otherStr = tierObj.other_rooms_quad || tierObj.other_rooms || "";
  if (otherStr && tierObj.pricing_quad) {
    const parts = otherStr.split("•").map((s) => s.trim()).filter(Boolean);
    for (const part of parts) {
      const m = part.match(/^(.+?):\s*\$([\d,]+)$/);
      if (!m) continue;
      const rName = m[1].trim();
      const quadTotal = Number(m[2].replace(/,/g, ""));
      if (!quadTotal || Number.isNaN(quadTotal)) continue;
      const occ = getRoomOccupancyInfo(rName, shipClass);
      if (occ.maxGuests >= totalGuests) {
        const taxes4 = tierObj.pricing_quad.cabin_taxes;
        const base4 = Math.max(0, Number((quadTotal - taxes4).toFixed(2)));
        candidates.push({
          roomName: rName,
          p2: null,
          p4: {
            guests: 4,
            cabin_base: base4,
            cabin_taxes: taxes4,
            cabin_total: quadTotal,
            cabin_discount: tierObj.pricing_quad.cabin_discount || 0,
          },
          maxGuests: occ.maxGuests,
          pullmanWarning: occ.pullmanWarning,
        });
      }
    }
  }

  if (candidates.length === 0) return null;
  candidates.sort((a, b) => a.p4.cabin_total - b.p4.cabin_total);
  const best = candidates[0];
  return {
    ...best,
    otherRooms: candidates
      .slice(1, 6)
      .map((o) => `${o.roomName}: $${Math.round(o.p4.cabin_total).toLocaleString("en-US")}`)
      .join(" • "),
  };
}

function computeTierCandidate(raw, tierKey, adults, children) {
  const totalGuests = adults + children;
  const tierObj = (raw.tiers && raw.tiers[tierKey]) || null;
  const effectiveRoomMode = IS_STATEROOMS ? "two_rooms" : state.roomMode;

  let p2 = tierObj ? tierObj.pricing_2a : raw.pricing_2a;
  let p4 = tierObj ? tierObj.pricing_quad : raw.pricing_quad;
  let customRoomName = null;
  let customOtherRooms = null;
  let occupancyWarning = "";
  let twoRoomSplitNote = "";
  let isTwoRooms = false;

  if (totalGuests > 4) {
    if (effectiveRoomMode === "single_room_5p") {
      const capRoom = findCapable5PlusRoomForTier(raw, tierObj, totalGuests);
      if (!capRoom) {
        return null;
      }
      p2 = capRoom.p2;
      p4 = capRoom.p4;
      customRoomName = capRoom.roomName;
      customOtherRooms = capRoom.otherRooms;
      if (capRoom.pullmanWarning) {
        occupancyWarning =
          "Phone Estimate Only (Up to 5 Guests): Select cabins only have a 5th Pullman bed — this room may or may not accommodate 5 guests on this sailing. Calling Royal Caribbean is required.";
      } else {
        occupancyWarning = `Phone Estimate Only (Sleeps up to ${capRoom.maxGuests}): Price is an estimate for ${totalGuests} guests in 1 suite. Calling Royal Caribbean is required to book 5+ guests in 1 room.`;
      }
    } else {
      isTwoRooms = true;
    }
  }

  if (totalGuests > 2 && !isTwoRooms && !p4) {
    return null;
  }
  if (totalGuests <= 2 && tierObj?.is_live_exact && !p2) {
    return null;
  }

  let taxesPp = 0;
  let base2Guests = 0;
  let discount2Guests = 0;

  if (p2) {
    taxesPp = p2.cabin_taxes / 2.0;
    base2Guests = p2.cabin_base;
    discount2Guests = p2.cabin_discount || 0;
  } else if (p4) {
    taxesPp = p4.cabin_taxes / 4.0;
    const extraPp = Number(raw.extra_guest_fare_pp || 0);
    base2Guests = Math.max(0, p4.cabin_base - 2.0 * extraPp);
    discount2Guests = (p4.cabin_discount || 0) * 0.65;
  } else {
    taxesPp = raw.cabin_taxes / Math.max(1, raw.guests || 2);
    base2Guests = raw.cabin_base;
    discount2Guests = raw.cabin_discount || 0;
  }

  let extraGuestFarePp = Number(raw.extra_guest_fare_pp || 0);
  if (p2 && p4) {
    extraGuestFarePp = Math.max(0, Number(((p4.cabin_base - p2.cabin_base) / 2.0).toFixed(2)));
  }

  const isKsfPromo = Boolean(raw.is_ksf_promo);
  const isHolidayRate = Boolean(raw.is_holiday_rate);

  let cabinBase = 0;
  let cabinTaxes = 0;
  let cabinDiscount = discount2Guests;

  if (isTwoRooms) {
    const split = splitPartyAcrossTwoRooms(adults, children);
    if (split.room1Total > 2 && !p4) {
      return null;
    }
    const r1Extra = Math.max(0, split.room1Total - 2);
    const r2Extra = Math.max(0, split.room2Total - 2);
    const room1Base = base2Guests + r1Extra * extraGuestFarePp;
    const room2Base = base2Guests + r2Extra * extraGuestFarePp;
    cabinBase = Number((room1Base + room2Base).toFixed(2));
    cabinTaxes = Number((taxesPp * totalGuests).toFixed(2));
    cabinDiscount = Number((discount2Guests * 2).toFixed(2));
    const r0Label = `${split.r0a}A${split.r0c > 0 ? `+${split.r0c}C` : ""}`;
    const r1Label = `${split.r1a}A${split.r1c > 0 ? `+${split.r1c}C` : ""}`;
    twoRoomSplitNote = `2 Rooms Online (Room 1: ${r0Label} • Room 2: ${r1Label})`;
  } else if (totalGuests > 4 && effectiveRoomMode === "single_room_5p" && p4) {
    // Use the 5+ guest suite's exact 4-guest price as baseline and add the 5th+ guest fare
    const extraBeyond4 = Math.max(0, totalGuests - 4);
    cabinBase = Number((p4.cabin_base + extraBeyond4 * extraGuestFarePp).toFixed(2));
    cabinTaxes = Number((taxesPp * totalGuests).toFixed(2));
    cabinDiscount = p4.cabin_discount || discount2Guests;
  } else {
    const extraGuests = Math.max(0, totalGuests - 2);
    cabinBase = Number((base2Guests + extraGuests * extraGuestFarePp).toFixed(2));
    cabinTaxes = Number((taxesPp * totalGuests).toFixed(2));
    if (totalGuests >= 4 && p4 && p4.cabin_discount != null) {
      cabinDiscount = p4.cabin_discount;
    } else if (totalGuests === 3 && p4 && p2) {
      cabinDiscount = Number(((p2.cabin_discount + p4.cabin_discount) / 2.0).toFixed(2));
    }
  }

  const cabinTotal = Number((cabinBase + cabinTaxes).toFixed(2));
  const totalPp = Number((cabinTotal / totalGuests).toFixed(2));
  const basePp = Number((cabinBase / totalGuests).toFixed(2));
  const nights = raw.nights || 1;
  const pricePerNight = Number((cabinTotal / nights).toFixed(2));

  let rateTag = raw.rate_tag || "STANDARD";
  if (isKsfPromo) {
    rateTag = `PROMO: Kids/3rd&4th Free (-$${Math.round(cabinDiscount)} off)`;
  } else if (isHolidayRate) {
    rateTag = `HOLIDAY/BLACKOUT: 3rd/4th +$${extraGuestFarePp.toFixed(2)}/ea (-$${Math.round(cabinDiscount)} off)`;
  }

  const roomName =
    customRoomName ||
    (totalGuests <= 2 ? tierObj?.room_name_2a : tierObj?.room_name_quad) ||
    tierObj?.room_name ||
    raw.category ||
    ITEM_LABEL;
  const otherRooms =
    customOtherRooms !== null
      ? customOtherRooms
      : (totalGuests <= 2 ? tierObj?.other_rooms_2a : tierObj?.other_rooms_quad) ??
        tierObj?.other_rooms ??
        "";
  const amenities = tierObj?.amenities || "";
  const isLiveExact = Boolean(tierObj?.is_live_exact);
  const royalType = tierObj?.royal_type || (IS_STATEROOMS ? "BALCONY" : "DELUXE");

  return {
    active_tier: tierKey,
    royal_type: royalType,
    room_name: roomName,
    other_rooms: otherRooms,
    amenities: amenities,
    is_live_exact: isLiveExact,
    is_two_rooms: isTwoRooms,
    two_room_split_note: twoRoomSplitNote,
    occupancy_warning: occupancyWarning,
    base_pp: basePp,
    total_pp: totalPp,
    cabin_base: cabinBase,
    cabin_taxes: cabinTaxes,
    cabin_total: cabinTotal,
    cabin_discount: cabinDiscount,
    price_per_night: pricePerNight,
    extra_guest_fare_pp: extraGuestFarePp,
    rate_tag: rateTag,
  };
}

/**
 * Resolves the lowest-priced available Suite/Stateroom tier
 * that matches the user's checked category checkboxes, and dynamically calculates
 * the exact cabin price for the selected party size (adults + children).
 */
function computeSailingForParty(raw, adults, children, selectedTiers) {
  const totalGuests = adults + children;
  if (totalGuests < 1) return null;

  const availTiers = raw.available_tiers || [raw.suite_tier || (IS_STATEROOMS ? "Interior" : "Standard")];
  const matchingTiers = ALL_TIERS.filter(
    (t) => selectedTiers.has(t) && availTiers.includes(t)
  );
  if (matchingTiers.length === 0) {
    return null;
  }

  const candidates = matchingTiers
    .map((t) => computeTierCandidate(raw, t, adults, children))
    .filter(Boolean);

  if (candidates.length === 0) {
    return null;
  }

  candidates.sort((a, b) => a.cabin_total - b.cabin_total);
  const best = candidates[0];
  const royalUrl = buildRoyalSuiteUrl(raw, adults, children, best.royal_type, best.is_two_rooms);

  return {
    ...raw,
    ...best,
    matching_tiers: matchingTiers,
    royal_url: royalUrl,
  };
}

function matchesNightsFilter(nights, filterVal) {
  if (filterVal === "all") return true;
  if (filterVal === "short") return nights >= 1 && nights <= 4;
  if (filterVal === "medium") return nights >= 5 && nights <= 6;
  if (filterVal === "week") return nights === 7;
  if (filterVal === "long") return nights >= 8;
  return true;
}

function getFilteredSailingsAndItineraries() {
  const emptyTierCounts = Object.fromEntries(ALL_TIERS.map((t) => [t, 0]));
  if (!state.rawData || !state.rawData.sailings) {
    return {
      sailings: [],
      itineraries: [],
      tierCounts: emptyTierCounts,
    };
  }

  const { shipClass, ship, ports, nights, dateStart, dateEnd, rateType, search, suiteTiers } = state.filters;
  const searchLower = search.trim().toLowerCase();

  const filteredSailings = [];
  const itinMap = new Map();
  const tierCounts = { ...emptyTierCounts };

  for (const raw of state.rawData.sailings) {
    // First check non-tier filters so we can show accurate counts on each tier checkbox
    if (state.adults + state.children > 2 && !raw.pricing_quad) {
      continue;
    }
    if (shipClass !== "all" && (raw.ship_class || "").toLowerCase() !== shipClass.toLowerCase()) {
      continue;
    }
    if (ship !== "all" && raw.ship !== ship) {
      continue;
    }
    const pCode = (raw.port_code || raw.port || "").toUpperCase();
    if (!ports.has(pCode)) {
      continue;
    }
    if (!matchesNightsFilter(raw.nights, nights)) {
      continue;
    }
    if (dateStart && raw.date < dateStart) {
      continue;
    }
    if (dateEnd && raw.date > dateEnd) {
      continue;
    }
    if (rateType === "promo" && !raw.is_ksf_promo) {
      continue;
    }
    if (rateType === "holiday" && !raw.is_holiday_rate) {
      continue;
    }
    if (searchLower) {
      const haystack = `${raw.ship} ${raw.ship_class} ${raw.itinerary_name} ${raw.itinerary_code} ${raw.port} ${raw.port_code}`.toLowerCase();
      if (!haystack.includes(searchLower)) {
        continue;
      }
    }

    // Increment available tier counts for this sailing (for current party size)
    const avail = raw.available_tiers || [raw.suite_tier || (IS_STATEROOMS ? "Interior" : "Standard")];
    for (const t of avail) {
      if (tierCounts[t] != null && computeTierCandidate(raw, t, state.adults, state.children)) {
        tierCounts[t] += 1;
      }
    }

    const computed = computeSailingForParty(raw, state.adults, state.children, suiteTiers);
    if (!computed) continue;

    filteredSailings.push(computed);

    const code = computed.itinerary_code;
    if (!itinMap.has(code)) {
      itinMap.set(code, {
        itinerary_code: code,
        itinerary_name: computed.itinerary_name,
        ship: computed.ship,
        ship_code: computed.ship_code,
        ship_class: computed.ship_class,
        nights: computed.nights,
        port: computed.port,
        port_code: computed.port_code,
        active_tier: computed.active_tier,
        room_name: computed.room_name,
        other_rooms: computed.other_rooms,
        amenities: computed.amenities,
        is_live_exact: computed.is_live_exact,
        lowest_royal_url: computed.royal_url,
        sailing_count: 0,
        promo_count: 0,
        lowest_cabin_total: computed.cabin_total,
        lowest_total_pp: computed.total_pp,
        lowest_sail_date: computed.date,
        sail_dates: [],
        sailings: [],
      });
    }
    const it = itinMap.get(code);
    it.sailing_count += 1;
    if (computed.is_ksf_promo) {
      it.promo_count += 1;
    }
    it.sail_dates.push(computed.date);
    it.sailings.push(computed);
    if (computed.cabin_total < it.lowest_cabin_total) {
      it.lowest_cabin_total = computed.cabin_total;
      it.lowest_total_pp = computed.total_pp;
      it.lowest_sail_date = computed.date;
      it.active_tier = computed.active_tier;
      it.room_name = computed.room_name;
      it.other_rooms = computed.other_rooms;
      it.amenities = computed.amenities;
      it.is_live_exact = computed.is_live_exact;
      it.lowest_royal_url = computed.royal_url;
    }
  }

  const itineraries = Array.from(itinMap.values());
  for (const it of itineraries) {
    it.sail_dates.sort();
  }

  return {
    sailings: sortSailings(filteredSailings, state.sortBy),
    itineraries: sortItineraries(itineraries, state.sortBy),
    tierCounts,
  };
}

function sortItineraries(itineraries, sortBy) {
  const list = [...itineraries];
  list.sort((a, b) => {
    const aNights = a.nights || 1;
    const bNights = b.nights || 1;
    const aPpn = a.lowest_cabin_total / aNights;
    const bPpn = b.lowest_cabin_total / bNights;

    switch (sortBy) {
      case "total_asc":
        return a.lowest_cabin_total - b.lowest_cabin_total;
      case "total_desc":
        return b.lowest_cabin_total - a.lowest_cabin_total;
      case "pp_asc":
        return a.lowest_total_pp - b.lowest_total_pp;
      case "pp_desc":
        return b.lowest_total_pp - a.lowest_total_pp;
      case "ppn_asc":
        return aPpn - bPpn;
      case "ppn_desc":
        return bPpn - aPpn;
      case "date_asc":
        return (a.sail_dates?.[0] || "").localeCompare(b.sail_dates?.[0] || "");
      case "date_desc":
        return (b.sail_dates?.[0] || "").localeCompare(a.sail_dates?.[0] || "");
      case "nights_desc":
        return b.nights - a.nights || a.lowest_cabin_total - b.lowest_cabin_total;
      case "nights_asc":
        return a.nights - b.nights || a.lowest_cabin_total - b.lowest_cabin_total;
      case "ship_asc":
        return a.ship.localeCompare(b.ship) || a.lowest_cabin_total - b.lowest_cabin_total;
      case "ship_desc":
        return b.ship.localeCompare(a.ship) || a.lowest_cabin_total - b.lowest_cabin_total;
      default:
        return a.lowest_cabin_total - b.lowest_cabin_total;
    }
  });
  return list;
}

function sortSailings(sailings, sortBy) {
  const list = [...sailings];
  list.sort((a, b) => {
    switch (sortBy) {
      case "total_asc":
        return a.cabin_total - b.cabin_total;
      case "total_desc":
        return b.cabin_total - a.cabin_total;
      case "pp_asc":
        return a.total_pp - b.total_pp;
      case "pp_desc":
        return b.total_pp - a.total_pp;
      case "ppn_asc":
        return a.price_per_night - b.price_per_night;
      case "ppn_desc":
        return b.price_per_night - a.price_per_night;
      case "date_asc":
        return a.date.localeCompare(b.date) || a.cabin_total - b.cabin_total;
      case "date_desc":
        return b.date.localeCompare(a.date) || a.cabin_total - b.cabin_total;
      case "nights_desc":
        return b.nights - a.nights || a.cabin_total - b.cabin_total;
      case "nights_asc":
        return a.nights - b.nights || a.cabin_total - b.cabin_total;
      case "ship_asc":
        return a.ship.localeCompare(b.ship) || a.cabin_total - b.cabin_total;
      case "ship_desc":
        return b.ship.localeCompare(a.ship) || a.cabin_total - b.cabin_total;
      default:
        return a.cabin_total - b.cabin_total;
    }
  });
  return list;
}

function populateFilterOptions(rawSailings) {
  const classOrder = ["Icon", "Oasis", "Quantum", "Freedom", "Voyager", "Radiance", "Vision"];
  const classesSet = new Set();
  const portsMap = new Map();
  let minDate = null;
  let maxDate = null;

  for (const s of rawSailings) {
    if (s.ship_class) classesSet.add(s.ship_class);
    if (s.port_code || s.port) {
      const pCode = (s.port_code || s.port).toUpperCase();
      const existing = portsMap.get(pCode);
      if (existing) {
        existing.count += 1;
      } else {
        portsMap.set(pCode, {
          name: s.port || KNOWN_PORT_NAMES[pCode] || pCode,
          count: 1,
        });
      }
    }
    if (s.date) {
      if (!minDate || s.date < minDate) minDate = s.date;
      if (!maxDate || s.date > maxDate) maxDate = s.date;
    }
  }

  for (const [customCode, customName] of state.customPorts.entries()) {
    if (!portsMap.has(customCode)) {
      portsMap.set(customCode, {
        name: customName || KNOWN_PORT_NAMES[customCode] || customCode,
        count: 0,
      });
    }
  }

  state.allAvailablePorts = portsMap;

  const classSelect = document.getElementById("filter-ship-class");
  const sortedClasses = Array.from(classesSet).sort((a, b) => {
    const ia = classOrder.indexOf(a);
    const ib = classOrder.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });

  classSelect.innerHTML =
    `<option value="all">All Ship Classes (${sortedClasses.length})</option>` +
    sortedClasses
      .map((cls) => `<option value="${escapeHtml(cls)}">${escapeHtml(cls)} Class</option>`)
      .join("");
  classSelect.value = state.filters.shipClass;

  const startInput = document.getElementById("filter-date-start");
  const endInput = document.getElementById("filter-date-end");
  if (minDate && maxDate) {
    startInput.min = minDate;
    startInput.max = maxDate;
    endInput.min = minDate;
    endInput.max = maxDate;
  }
  startInput.value = state.filters.dateStart;
  endInput.value = state.filters.dateEnd;

  document.getElementById("filter-nights").value = state.filters.nights;
  document.getElementById("filter-rate").value = state.filters.rateType;
  document.getElementById("filter-search").value = state.filters.search;
  document.getElementById("sort-select").value = state.sortBy;

  document.querySelectorAll(".tab-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === state.activeView);
  });

  document.querySelectorAll(".tier-checkbox").forEach((cb) => {
    cb.checked = state.filters.suiteTiers.has(cb.value);
  });

  renderPortMultiselect();
  updateShipDropdown();
}

function updatePortTriggerLabel() {
  const labelEl = document.getElementById("port-dropdown-label");
  if (!labelEl) return;

  const totalPorts = state.allAvailablePorts.size;
  const selected = Array.from(state.filters.ports);

  if (selected.length === 0) {
    labelEl.textContent = "No Ports Selected (0)";
  } else if (totalPorts > 0 && selected.length >= totalPorts) {
    labelEl.textContent = `All Departure Ports (${totalPorts})`;
  } else if (selected.length <= 4) {
    labelEl.textContent = `${selected.length} Port${selected.length === 1 ? "" : "s"} (${selected.join(", ")})`;
  } else {
    labelEl.textContent = `${selected.length} of ${totalPorts} Ports Selected`;
  }
}

function renderPortMultiselect() {
  const listEl = document.getElementById("port-options-list");
  if (!listEl) return;

  // Sort Florida ports first, then alphabetically by port name
  const flOrder = ["FLL", "MIA", "PCN", "TPA"];
  const entries = Array.from(state.allAvailablePorts.entries()).sort((a, b) => {
    const ia = flOrder.indexOf(a[0]);
    const ib = flOrder.indexOf(b[0]);
    if (ia !== -1 || ib !== -1) {
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    }
    return a[1].name.localeCompare(b[1].name);
  });

  listEl.innerHTML = entries
    .map(([code, info]) => {
      const checked = state.filters.ports.has(code) ? "checked" : "";
      return `
        <label class="ms-port-item">
          <span class="ms-port-left">
            <input type="checkbox" class="port-checkbox" value="${escapeHtml(code)}" ${checked} />
            <span class="ms-port-code">${escapeHtml(code)}</span>
            <span>${escapeHtml(info.name)}</span>
          </span>
          <span class="ms-port-count">${info.count}</span>
        </label>
      `;
    })
    .join("");

  listEl.querySelectorAll(".port-checkbox").forEach((cb) => {
    cb.addEventListener("change", () => {
      const code = cb.value.toUpperCase();
      if (cb.checked) {
        state.filters.ports.add(code);
      } else {
        state.filters.ports.delete(code);
      }
      updatePortTriggerLabel();
      saveStateToStorage();
      render();
    });
  });

  updatePortTriggerLabel();
}

function updateShipDropdown() {
  if (!state.rawData) return;
  const shipSelect = document.getElementById("filter-ship");
  const selectedClass = state.filters.shipClass;

  const shipCounts = new Map();
  const shipClasses = new Map();

  for (const s of state.rawData.sailings) {
    if (selectedClass !== "all" && (s.ship_class || "").toLowerCase() !== selectedClass.toLowerCase()) {
      continue;
    }
    shipCounts.set(s.ship, (shipCounts.get(s.ship) || 0) + 1);
    shipClasses.set(s.ship, s.ship_class);
  }

  const sortedShips = Array.from(shipCounts.keys()).sort((a, b) => a.localeCompare(b));

  if (state.filters.ship !== "all" && !shipCounts.has(state.filters.ship)) {
    state.filters.ship = "all";
  }

  shipSelect.innerHTML =
    `<option value="all">All Ships (${sortedShips.length})</option>` +
    sortedShips
      .map(
        (ship) =>
          `<option value="${escapeHtml(ship)}" ${state.filters.ship === ship ? "selected" : ""}>${escapeHtml(ship)} (${shipClasses.get(ship)} • ${shipCounts.get(ship)})</option>`
      )
      .join("");
}

function updatePartyUI() {
  const totalGuests = state.adults + state.children;
  document.getElementById("adults-val").textContent = state.adults;
  document.getElementById("children-val").textContent = state.children;

  document.getElementById("adults-minus").disabled = state.adults <= 1;
  document.getElementById("adults-plus").disabled = state.adults >= 4 || totalGuests >= 8;
  document.getElementById("children-minus").disabled = state.children <= 0;
  document.getElementById("children-plus").disabled = state.children >= 6 || totalGuests >= 8;

  document.querySelectorAll(".preset-chip").forEach((chip) => {
    const a = Number(chip.dataset.adults);
    const c = Number(chip.dataset.children);
    chip.classList.toggle("active", a === state.adults && c === state.children);
  });

  const roomModeBox = document.getElementById("room-mode-box");
  if (roomModeBox) {
    roomModeBox.classList.toggle("hidden", totalGuests <= 4);
  }
  const effectiveRoomMode = IS_STATEROOMS ? "two_rooms" : state.roomMode;
  document.querySelectorAll(".room-mode-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.roomMode === effectiveRoomMode);
  });

  const partyText = `${state.adults} Adult${state.adults !== 1 ? "s" : ""}${
    state.children > 0 ? `, ${state.children} Kid${state.children !== 1 ? "s" : ""}` : ""
  } (${totalGuests} Guest${totalGuests !== 1 ? "s" : ""})`;
  document.getElementById("party-pill").textContent = `Party: ${partyText}`;

  const noteEl = document.getElementById("party-note");
  if (totalGuests > 4) {
    if (effectiveRoomMode === "single_room_5p") {
      noteEl.textContent =
        `Showing ONLY 5+ occupancy suites for ${partyText} (1-Room Phone Estimate). ` +
        `Prices are an estimate based on 3rd/4th guest rates — calling Royal Caribbean is required to book 5+ guests in 1 room.`;
    } else {
      const split = splitPartyAcrossTwoRooms(state.adults, state.children);
      const r0Label = `${split.r0a}A${split.r0c > 0 ? ` + ${split.r0c}C` : ""}`;
      const r1Label = `${split.r1a}A${split.r1c > 0 ? ` + ${split.r1c}C` : ""}`;
      noteEl.textContent =
        `Showing 2-Room Online Split Pricing for ${partyText} (Room 1: ${r0Label} • Room 2: ${r1Label}). ` +
        `Clicking "View on Royal" opens Royal Caribbean's 2-room booking flow.`;
    }
  } else if (totalGuests > 2) {
    noteEl.textContent = `Showing single-room quad/family ${ITEMS_LOWER}s for ${partyText} (capped at 4 guests/room online). Kids Sail Free dates charge $0 base fare for 3rd/4th guests (taxes only).`;
  } else {
    noteEl.textContent = `Showing double-occupancy ${ITEMS_LOWER} pricing for ${partyText} (incl. all port taxes & fees).`;
  }
}

function updateSummaryAndKpis(itineraries, sailings, tierCounts) {
  const totalItins = state.rawData?.itineraries?.length || 0;
  const totalRawSailings = state.rawData?.sailings?.length || 0;

  // Update tier count badges on checkboxes
  for (const t of ALL_TIERS) {
    const el = document.getElementById(`tier-count-${t}`);
    if (el) {
      el.textContent = `(${tierCounts[t] ?? 0})`;
    }
  }

  const promoCount = sailings.filter((s) => s.is_ksf_promo).length;
  const holidayCount = sailings.filter((s) => s.is_holiday_rate).length;

  document.getElementById("kpi-itineraries").textContent = itineraries.length;
  document.getElementById("kpi-scanned").textContent = `of ${totalItins} ${ITEMS_LOWER} itineraries`;
  document.getElementById("kpi-sailings").textContent = sailings.length;
  document.getElementById("kpi-sailings-sub").textContent = `of ${totalRawSailings} total departure dates`;
  document.getElementById("kpi-promos").textContent = promoCount;
  document.getElementById("kpi-holidays").textContent = `${holidayCount} holiday/blackout dates`;

  if (sailings.length > 0) {
    const cheapest = sailings.reduce((min, s) => (s.cabin_total < min.cabin_total ? s : min), sailings[0]);
    document.getElementById("kpi-lowest").textContent = formatMoney(cheapest.cabin_total);
    document.getElementById("kpi-lowest-sub").textContent = `${cheapest.ship} (${cheapest.nights}N • ${getTierDisplayLabel(cheapest.active_tier)}) on ${cheapest.date}`;
  } else {
    document.getElementById("kpi-lowest").textContent = "--";
    document.getElementById("kpi-lowest-sub").textContent = "No matching sailings";
  }

  document.getElementById("count-itineraries").textContent = itineraries.length;
  document.getElementById("count-sailings").textContent = sailings.length;

  // Build active filter summary text
  const parts = [];
  if (state.filters.suiteTiers.size < ALL_TIERS.length) {
    const checkedLabels = Array.from(state.filters.suiteTiers).map((t) => getTierDisplayLabel(t));
    parts.push(`Categories: ${checkedLabels.join(", ") || "None"}`);
  }
  if (state.filters.shipClass !== "all") parts.push(`${state.filters.shipClass} Class`);
  if (state.filters.ship !== "all") parts.push(state.filters.ship);
  if (state.filters.ports.size < state.allAvailablePorts.size) {
    const selPorts = Array.from(state.filters.ports);
    parts.push(`Ports: ${selPorts.join(", ") || "None"}`);
  }
  if (state.filters.nights !== "all") parts.push(`Length: ${state.filters.nights}`);
  if (state.filters.dateStart) parts.push(`From: ${state.filters.dateStart}`);
  if (state.filters.dateEnd) parts.push(`To: ${state.filters.dateEnd}`);
  if (state.filters.rateType !== "all") parts.push(`Rate: ${state.filters.rateType.toUpperCase()}`);
  if (state.filters.search.trim()) parts.push(`Search: "${state.filters.search.trim()}"`);

  const summaryEl = document.getElementById("active-filters-summary");
  if (parts.length === 0) {
    summaryEl.textContent = `Showing all ${sailings.length} ${ITEMS_LOWER} sailings across ${itineraries.length} itineraries`;
  } else {
    summaryEl.textContent = `Active Filters (${parts.length}): ${parts.join(" • ")} — ${sailings.length} sailings found`;
  }
}

function renderItinerariesView(itineraries) {
  const container = document.getElementById("itineraries-container");
  const bookBtnLabel = IS_STATEROOMS ? "View Rooms on Royal &#8599;" : "View Suites on Royal &#8599;";
  const colHeaderLabel = IS_STATEROOMS ? "Room Category &amp; Highlights" : "Suite Tier &amp; Specific Room";

  const html = itineraries
    .map((itin) => {
      const code = itin.itinerary_code;
      const isExpanded = state.expandedItineraries.has(code);
      const itinSailings = sortSailings(itin.sailings || [], state.sortBy);
      const firstDate = itin.sail_dates?.[0] || "--";
      const lastDate = itin.sail_dates?.[itin.sail_dates.length - 1] || "--";
      const lowestPpn = itin.nights > 0 ? itin.lowest_cabin_total / itin.nights : itin.lowest_cabin_total;

      const rowsHtml = itinSailings
        .map(
          (s) => `
          <tr>
            <th scope="row" class="cell-date-book">
              <span class="date-badge">${escapeHtml(s.date)}</span>
              <div class="date-book-action">
                <a href="${escapeHtml(s.royal_url)}" target="_blank" rel="noopener noreferrer" class="royal-link-btn" title="Open this sailing directly on Royal Caribbean's Room Selection page">
                  ${bookBtnLabel}
                </a>
              </div>
            </th>
            <td class="cell-suite">
              ${getSuiteTierCellHtml(s)}
            </td>
            <td class="num cell-pp"><span class="cell-val">${formatMoney(s.total_pp)}/pp</span></td>
            <td class="num cell-total">
              <div class="cell-val-group">
                <span class="total-cell">${formatMoney(s.cabin_total)}</span>
                <span class="tax-breakdown">Fare: ${formatMoney(s.cabin_base)} + Tax: ${formatMoney(s.cabin_taxes)}</span>
              </div>
            </td>
            <td class="num cell-ppn"><span class="cell-val">${formatMoney(s.price_per_night)}/nt</span></td>
            <td class="cell-status">${getRateBadgeHtml(s)}</td>
          </tr>
        `
        )
        .join("");

      return `
        <article class="itin-card ${isExpanded ? "expanded" : ""}" data-code="${escapeHtml(code)}">
          <div class="itin-summary" onclick="toggleItinerary('${escapeHtml(code)}')">
            <div class="itin-main">
              <div class="itin-badges">
                <span class="badge ${getShipClassBadgeClass(itin.ship_class)}">${escapeHtml(itin.ship_class)} Class</span>
                ${getTierBadgeHtml(itin.active_tier, itin.room_name)}
                <span class="badge badge-nights">${itin.nights} Nights</span>
                <span class="badge badge-port">${escapeHtml(itin.port)} (${escapeHtml(itin.port_code)})</span>
              </div>
              <div class="itin-title">${escapeHtml(itin.ship)} &mdash; ${escapeHtml(itin.itinerary_name)}</div>
              <div class="itin-meta">
                <span><strong>${itin.sailing_count}</strong> matching departure${itin.sailing_count !== 1 ? "s" : ""} (${escapeHtml(firstDate)} to ${escapeHtml(lastDate)})</span>
                <span><strong>${itin.promo_count}</strong> Kids Sail Free promo date${itin.promo_count !== 1 ? "s" : ""}</span>
                <span>${escapeHtml(itin.amenities)}</span>
              </div>
            </div>
            <div class="itin-pricing-box">
              <div class="itin-pricing-top">
                <span class="price-from-label">${escapeHtml(getTierDisplayLabel(itin.active_tier))} From (All-In)</span>
                <div class="price-total">${formatMoney(itin.lowest_cabin_total)}</div>
                <div class="price-pp">${formatMoney(itin.lowest_total_pp)}/pp &bull; ${formatMoney(lowestPpn, false)}/night</div>
              </div>
              <div class="itin-pricing-bottom">
                <div class="itin-card-actions" onclick="event.stopPropagation()">
                  <a href="${escapeHtml(itin.lowest_royal_url)}" target="_blank" rel="noopener noreferrer" class="royal-link-btn" title="Open lowest-priced date (${escapeHtml(itin.lowest_sail_date)}) on Royal Caribbean">
                    Book ${escapeHtml(itin.lowest_sail_date)} on Royal &#8599;
                  </a>
                </div>
                <span class="expand-hint">${isExpanded ? "▲ Hide sailing dates" : `▼ View all ${itin.sailing_count} dates`}</span>
              </div>
            </div>
          </div>
          <div class="itin-details">
            <div class="table-responsive">
              <table class="sailings-table sailings-table-itin">
                <thead>
                  <tr>
                    <th>Sail Date &amp; Booking</th>
                    <th>${colHeaderLabel}</th>
                    <th class="num">Per Guest</th>
                    <th class="num">Total Cabin (w/ Taxes)</th>
                    <th class="num">Per Night</th>
                    <th>Promo / Holiday Status</th>
                  </tr>
                </thead>
                <tbody>
                  ${rowsHtml}
                </tbody>
              </table>
            </div>
          </div>
        </article>
      `;
    })
    .join("");

  container.innerHTML = html;
}

function renderSailingsView(sailings) {
  const tbody = document.getElementById("sailings-tbody");
  const bookBtnLabel = IS_STATEROOMS ? "View Rooms on Royal &#8599;" : "View Suites on Royal &#8599;";

  tbody.innerHTML = sailings
    .map(
      (s) => `
      <tr>
        <th scope="row" class="cell-date">
          <span class="date-badge">${escapeHtml(s.date)}</span>
          <a href="${escapeHtml(s.royal_url)}" target="_blank" rel="noopener noreferrer" class="royal-link-btn mobile-only-book-btn" title="Open Royal Caribbean Room Selection">
            ${bookBtnLabel}
          </a>
        </th>
        <td class="cell-ship">
          <div class="cell-ship-inner">
            <strong>${escapeHtml(s.ship)}</strong>
            <span class="badge ${getShipClassBadgeClass(s.ship_class)}">${escapeHtml(s.ship_class)}</span>
          </div>
        </td>
        <td class="cell-itin">
          <div class="cell-itin-name">${escapeHtml(s.itinerary_name)}</div>
          <div class="itin-code-row">
            <span class="tax-breakdown" title="Royal Caribbean internal itinerary/package ID">ID: ${escapeHtml(s.itinerary_code)}</span>
            <a href="${escapeHtml(s.royal_url)}" target="_blank" rel="noopener noreferrer" class="royal-link-btn desktop-only-book-btn" title="Open Royal Caribbean Room Selection for this date and party size">
              ${bookBtnLabel}
            </a>
          </div>
        </td>
        <td class="cell-port"><span class="cell-val">${escapeHtml(s.port)} (${escapeHtml(s.port_code)})</span></td>
        <td class="cell-nights"><span class="badge badge-nights">${s.nights}N</span></td>
        <td class="cell-suite">
          ${getSuiteTierCellHtml(s)}
        </td>
        <td class="num cell-pp"><span class="cell-val">${formatMoney(s.total_pp)}/pp</span></td>
        <td class="num cell-total">
          <div class="cell-val-group">
            <span class="total-cell">${formatMoney(s.cabin_total)}</span>
            <span class="tax-breakdown">Fare: ${formatMoney(s.cabin_base)} + Tax: ${formatMoney(s.cabin_taxes)}</span>
          </div>
        </td>
        <td class="num cell-ppn"><span class="cell-val">${formatMoney(s.price_per_night)}/nt</span></td>
        <td class="cell-status">${getRateBadgeHtml(s)}</td>
      </tr>
    `
    )
    .join("");

  updateTableSortIndicators();
}

function updateTableSortIndicators() {
  const [field, dir] = state.sortBy.split("_");
  document.querySelectorAll(".sailings-table th.sortable").forEach((th) => {
    const indicator = th.querySelector(".sort-indicator");
    if (!indicator) return;
    if (th.dataset.sort === field) {
      indicator.textContent = dir === "asc" ? "▲" : "▼";
    } else {
      indicator.textContent = "";
    }
  });
}

function render() {
  if (!state.rawData) return;

  updatePartyUI();
  const { sailings, itineraries, tierCounts } = getFilteredSailingsAndItineraries();
  updateSummaryAndKpis(itineraries, sailings, tierCounts);

  const emptyEl = document.getElementById("empty-state");
  const itinView = document.getElementById("view-itineraries");
  const sailingsView = document.getElementById("view-sailings");

  if (sailings.length === 0) {
    emptyEl.classList.remove("hidden");
    itinView.classList.add("hidden");
    sailingsView.classList.add("hidden");
    return;
  }

  emptyEl.classList.add("hidden");

  if (state.activeView === "itineraries") {
    itinView.classList.remove("hidden");
    sailingsView.classList.add("hidden");
    renderItinerariesView(itineraries);
  } else {
    itinView.classList.add("hidden");
    sailingsView.classList.remove("hidden");
    renderSailingsView(sailings);
  }
}

window.toggleItinerary = function toggleItinerary(code) {
  if (state.expandedItineraries.has(code)) {
    state.expandedItineraries.delete(code);
  } else {
    state.expandedItineraries.add(code);
  }
  render();
};

function handleAddCustomPort() {
  const inputEl = document.getElementById("add-port-input");
  const hintEl = document.getElementById("add-port-hint");
  if (!inputEl) return;

  const rawCode = inputEl.value.trim().toUpperCase().replace(/[^A-Z]/g, "");
  if (rawCode.length < 3) {
    if (hintEl) {
      hintEl.textContent = "Please enter a valid 3-letter port code (e.g. TPA, GAL, SEA, BCN).";
    }
    return;
  }

  const portName = KNOWN_PORT_NAMES[rawCode] || `Port ${rawCode}`;
  if (!state.allAvailablePorts.has(rawCode)) {
    state.customPorts.set(rawCode, portName);
    state.allAvailablePorts.set(rawCode, { name: portName, count: 0 });
  }
  state.filters.ports.add(rawCode);
  inputEl.value = "";

  const info = state.allAvailablePorts.get(rawCode);
  if (hintEl) {
    if (info && info.count > 0) {
      hintEl.textContent = `✓ Enabled ${portName} (${rawCode}) — ${info.count} suite sailings loaded.`;
    } else {
      const allCodes = Array.from(state.allAvailablePorts.keys()).join(",");
      hintEl.textContent = `Added ${portName} (${rawCode}). Run: python tracker.py -p ${allCodes}`;
    }
  }

  renderPortMultiselect();
  saveStateToStorage();
  render();
}

function bindEvents() {
  // Mobile filter panel collapse/expand toggle
  const mobileFilterToggle = document.getElementById("mobile-filter-toggle");
  const filterCollapsibleBody = document.getElementById("filter-collapsible-body");
  const mobileFilterChevron = document.getElementById("mobile-filter-chevron");
  if (mobileFilterToggle && filterCollapsibleBody) {
    mobileFilterToggle.addEventListener("click", () => {
      const isCollapsed = filterCollapsibleBody.classList.toggle("collapsed-mobile");
      mobileFilterToggle.setAttribute("aria-expanded", String(!isCollapsed));
      if (mobileFilterChevron) {
        mobileFilterChevron.textContent = isCollapsed ? "▼ Show" : "▲ Hide";
      }
    });
  }

  // View tabs
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.activeView = btn.dataset.view;
      saveStateToStorage();
      render();
    });
  });

  // Sort select
  const sortSelect = document.getElementById("sort-select");
  sortSelect.addEventListener("change", (e) => {
    state.sortBy = e.target.value;
    saveStateToStorage();
    render();
  });

  // Sortable table headers
  document.querySelectorAll(".sailings-table th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      const current = state.sortBy;
      const nextSort = current === `${col}_asc` ? `${col}_desc` : `${col}_asc`;
      state.sortBy = nextSort;
      if ([...sortSelect.options].some((o) => o.value === nextSort)) {
        sortSelect.value = nextSort;
      }
      saveStateToStorage();
      render();
    });
  });

  // Party size steppers
  document.getElementById("adults-minus").addEventListener("click", () => {
    if (state.adults > 1) {
      state.adults -= 1;
      saveStateToStorage();
      render();
    }
  });
  document.getElementById("adults-plus").addEventListener("click", () => {
    if (state.adults < 4 && state.adults + state.children < 8) {
      state.adults += 1;
      saveStateToStorage();
      render();
    }
  });
  document.getElementById("children-minus").addEventListener("click", () => {
    if (state.children > 0) {
      state.children -= 1;
      saveStateToStorage();
      render();
    }
  });
  document.getElementById("children-plus").addEventListener("click", () => {
    if (state.children < 6 && state.adults + state.children < 8) {
      state.children += 1;
      saveStateToStorage();
      render();
    }
  });

  // 5+ Guest Room Mode buttons (2 Rooms Online vs 1 Room 5+ Guest Suite Phone Estimate)
  document.querySelectorAll(".room-mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.roomMode;
      if (mode === "two_rooms" || mode === "single_room_5p") {
        state.roomMode = IS_STATEROOMS ? "two_rooms" : mode;
        saveStateToStorage();
        render();
      }
    });
  });

  // Party presets
  document.querySelectorAll(".preset-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      state.adults = Number(chip.dataset.adults);
      state.children = Number(chip.dataset.children);
      saveStateToStorage();
      render();
    });
  });

  // Suite Tier checkboxes (Sea, Sky, Star, Standard, Guarantee)
  document.querySelectorAll(".tier-checkbox").forEach((cb) => {
    cb.addEventListener("change", () => {
      if (cb.checked) {
        state.filters.suiteTiers.add(cb.value);
      } else {
        state.filters.suiteTiers.delete(cb.value);
      }
      saveStateToStorage();
      render();
    });
  });

  // Filter controls
  document.getElementById("filter-ship-class").addEventListener("change", (e) => {
    state.filters.shipClass = e.target.value;
    updateShipDropdown();
    saveStateToStorage();
    render();
  });

  document.getElementById("filter-ship").addEventListener("change", (e) => {
    state.filters.ship = e.target.value;
    saveStateToStorage();
    render();
  });

  // Multi-select Port Dropdown toggle & outside click
  const portContainer = document.getElementById("port-multiselect");
  const portBtn = document.getElementById("port-dropdown-btn");
  const portMenu = document.getElementById("port-dropdown-menu");

  if (portBtn && portMenu && portContainer) {
    portBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isHidden = portMenu.classList.toggle("hidden");
      portBtn.setAttribute("aria-expanded", String(!isHidden));
    });

    document.addEventListener("click", (e) => {
      if (!portContainer.contains(e.target) && !portMenu.classList.contains("hidden")) {
        portMenu.classList.add("hidden");
        portBtn.setAttribute("aria-expanded", "false");
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !portMenu.classList.contains("hidden")) {
        portMenu.classList.add("hidden");
        portBtn.setAttribute("aria-expanded", "false");
      }
    });
  }

  // Port preset buttons inside dropdown
  document.querySelectorAll(".ms-action-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const preset = btn.dataset.portPreset;
      if (preset === "all") {
        state.filters.ports = new Set(state.allAvailablePorts.keys());
      } else if (preset === "fl") {
        state.filters.ports = new Set(FLORIDA_PORTS);
      } else if (preset === "orig3") {
        state.filters.ports = new Set(DEFAULT_PORTS);
      } else if (preset === "none") {
        state.filters.ports.clear();
      }
      renderPortMultiselect();
      saveStateToStorage();
      render();
    });
  });

  // Custom port adder inside dropdown
  const addPortBtn = document.getElementById("add-port-btn");
  const addPortInput = document.getElementById("add-port-input");
  if (addPortBtn && addPortInput) {
    addPortBtn.addEventListener("click", handleAddCustomPort);
    addPortInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleAddCustomPort();
      }
    });
  }

  document.getElementById("filter-nights").addEventListener("change", (e) => {
    state.filters.nights = e.target.value;
    saveStateToStorage();
    render();
  });

  document.getElementById("filter-date-start").addEventListener("change", (e) => {
    state.filters.dateStart = e.target.value;
    saveStateToStorage();
    render();
  });

  document.getElementById("filter-date-end").addEventListener("change", (e) => {
    state.filters.dateEnd = e.target.value;
    saveStateToStorage();
    render();
  });

  document.getElementById("filter-rate").addEventListener("change", (e) => {
    state.filters.rateType = e.target.value;
    saveStateToStorage();
    render();
  });

  document.getElementById("filter-search").addEventListener("input", (e) => {
    state.filters.search = e.target.value;
    saveStateToStorage();
    render();
  });

  // Reset filters
  document.getElementById("reset-filters-btn").addEventListener("click", () => {
    state.adults = 2;
    state.children = 2;
    state.roomMode = "two_rooms";
    state.sortBy = "total_asc";
    state.customPorts.clear();
    state.filters = {
      shipClass: "all",
      ship: "all",
      ports: new Set(DEFAULT_PORTS),
      nights: "all",
      dateStart: "",
      dateEnd: "",
      rateType: "all",
      search: "",
      suiteTiers: new Set(DEFAULT_TIERS),
    };
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (err) {
      // ignore storage error
    }
    const badge = document.getElementById("saved-prefs-badge");
    if (badge) {
      badge.textContent = "↺ Reset to defaults";
    }
    populateFilterOptions(state.rawData?.sailings || []);
    render();
  });
}

async function init() {
  loadStateFromStorage();
  bindEvents();
  const banner = document.getElementById("status-banner");
  try {
    const resp = await fetch(DATA_URL, { cache: "no-store" });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}: Could not load ${DATA_URL}`);
    }
    state.rawData = await resp.json();

    if (state.rawData.updated_at) {
      const dt = new Date(state.rawData.updated_at);
      document.getElementById("updated-pill").textContent = `Updated: ${dt.toLocaleDateString()} ${dt.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })}`;
    }

    document.getElementById("meta-subtitle").textContent = IS_STATEROOMS
      ? `Interactive explorer for Royal Caribbean Staterooms (Interior, Ocean View & Balcony) across 10 US & Caribbean Ports`
      : `Interactive explorer for Royal Caribbean Suites (Sea, Sky, Star & Standard) across 10 US & Caribbean Ports`;

    populateFilterOptions(state.rawData.sailings || []);
    render();
  } catch (err) {
    banner.classList.remove("hidden");
    banner.textContent = `Failed to load ${ITEMS_LOWER} data: ${err.message}.`;
  }
}

document.addEventListener("DOMContentLoaded", init);

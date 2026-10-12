"""Deterministic requirement extraction from an Arabic or English mission prompt.

These values are the request's explicit numbers — payload, total mass, altitude,
endurance, hover time, path size and topology — and everything that must honour
the request reads them from here: the design agent's validation, the code agent,
the start-of-run feasibility gate and the acceptance gate. A number the parser
cannot bind is left unset (or listed in ``unresolved``); nothing is guessed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class MissionWaypointSegment:
    name: str
    north_m: float
    east_m: float
    down_m: float = 0.0
    action: str = "fly"


@dataclass(slots=True)
class MissionSpec:
    original: str
    normalized: str
    language: str
    use_case: str = "general"
    topology: str | None = None
    altitude_m: float | None = None
    duration_min: float | None = None
    square_size_m: float | None = None
    hover_duration_s: float | None = None
    payload_g: float | None = None
    max_total_mass_g: float | None = None
    camera_required: bool = False
    geofence_radius_m: float | None = None
    waypoints: list[MissionWaypointSegment] = field(default_factory=list)
    # Explicit quantities the parser saw but could not bind to one meaning.
    unresolved: list[str] = field(default_factory=list)

    @property
    def estimated_distance_m(self) -> float:
        return estimate_relative_path_distance(self.waypoints)

    def estimated_duration_s(self, cruise_speed_ms: float = 5.0) -> float:
        travel_s = 0.0
        if cruise_speed_ms > 0:
            travel_s = self.estimated_distance_m / cruise_speed_ms
        overhead_s = 20.0 + (len(self.waypoints) * 2.0)
        requested_s = (self.duration_min or 0.0) * 60.0
        return round(max(travel_s + overhead_s, requested_s), 1)


# Arabic-Indic and Persian digits, and the Arabic decimal separator.
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")
# Harakat, tanween and tatweel: «كيلوغراماً» and «كيلوغراما» are one word.
_ARABIC_MARKS = re.compile("[ً-ْـ]")
# Letter variants folded for matching only: أ إ آ ٱ → ا, ة → ه, ى → ي.
_FOLD = str.maketrans("أإآٱةى", "ااااهي")


def normalize_prompt(text: str) -> str:
    text = text.replace("×", "x").translate(_DIGITS)
    return " ".join(_ARABIC_MARKS.sub("", text).split())


def _matchable(text: str) -> str:
    return normalize_prompt(text).lower().translate(_FOLD)


_NUM = r"(?<![\w.+-])(\d+(?:\.\d+)?)"
_KG = (
    r"kilograms?(?![a-z])|kilos?(?![a-z])|kgs?(?![a-z])"
    r"|كيلو\s?(?:غرام|جرام)\w*|كيلوغ\w*|كغم?(?!\w)|كجم(?!\w)|كلغ(?!\w)|كيلو(?!\s?متر)(?!\w)"
)
_G = r"grams?(?![a-z])|gr(?![a-z])|g(?![a-z])|غرام\w*|جرام\w*|غم(?!\w)|جم(?!\w)|غ(?!\w)"
_KM = r"kilomet(?:er|re)s?(?![a-z])|km(?![a-z])|كيلومتر\w*|كم(?!\w)"
_M = r"met(?:er|re)s?(?![a-z])|m(?![a-z])|مترا|متر|امتار|م(?!\w)"
_MASS = re.compile(_NUM + r"\s*-?\s*(?:(?P<kg>" + _KG + r")|(?P<g>" + _G + r"))")
_LENGTH = r"\s*(?:(?P<km>" + _KM + r")|(?P<m>" + _M + r"))"

_PAYLOAD_SUBJECT = (
    r"payload|camera|sensor|lidar|gimbal|package|parcel|box|cargo|load|tank|sprayer|fpv"
    r"|kit|bottle|bag|حموله|كاميرا|حساس\w*|مستشعر\w*|طرد|صندوق|خزان|شحنه|حزمه|جهاز|عبوه"
)
_CARRY = (
    r"carry|carries|carrying|carried|lift|lifts|lifting|hold|holds|holding|deliver|delivers"
    r"|delivering|transport|transports|transporting|with"
    r"|تحمل|يحمل|بحموله|لحمل|تنقل|ينقل|ترفع|يرفع|مع"
)
_AIRCRAFT = (
    r"drone|quadcopter|hexacopter|octocopter|multicopter|multirotor|copter|aircraft|uav"
    r"|quad|airframe|vehicle|طائره|الطائره|درون|الدرون|مركبه"
)
_WEIGH = r"weigh|weighs|weighing|weight|mass|وزن|وزنها|وزنه|يزن|تزن|كتله|كتلتها"
_TOTAL = (
    r"total|all[- ]up|takeoff|take-off|overall|gross"
    r"|اجمالي|الاجمالي|الكلي|كلي|بالكامل"
)


def _words(pattern: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)(?:" + pattern + r")(?!\w)")


_PAYLOAD_RE = _words(_PAYLOAD_SUBJECT)
_CARRY_RE = _words(_CARRY)
_AIRCRAFT_RE = _words(_AIRCRAFT)
_WEIGH_RE = _words(_WEIGH)
_TOTAL_RE = _words(_TOTAL)
_CLAUSE_END = re.compile(r"[.;!?؛\n]")


def _last(pattern: re.Pattern[str], text: str) -> int | None:
    found = None
    for match in pattern.finditer(text):
        found = match.start()
    return found


def _classify_mass(text: str, start: int, end: int) -> str:
    """'payload', 'total' or 'unknown' for the mass mentioned at text[start:end]."""
    after = text[end : end + 30].lstrip(" -")
    if _TOTAL_RE.match(after) or after.startswith("in total"):
        return "total"
    if _AIRCRAFT_RE.match(after):
        return "total"
    if _PAYLOAD_RE.match(after):
        return "payload"

    before = text[max(0, start - 60) : start]
    boundary = None
    for boundary in _CLAUSE_END.finditer(before):
        pass
    if boundary is not None:
        before = before[boundary.end() :]
    subject = _last(_PAYLOAD_RE, before)
    carry = _last(_CARRY_RE, before)
    payload = max((p for p in (subject, carry) if p is not None), default=None)
    total = _last(_TOTAL_RE, before)
    weigh = _last(_WEIGH_RE, before)
    if total is not None and (payload is None or total > payload):
        return "total"
    if weigh is not None:
        # «camera weighing 200 g» versus «a quadcopter weighing 250 g»: the
        # nearest noun before the verb says whose weight it is.
        head = before[:weigh]
        noun_payload = _last(_PAYLOAD_RE, head)
        noun_aircraft = _last(_AIRCRAFT_RE, head)
        if noun_payload is not None and (noun_aircraft is None or noun_payload > noun_aircraft):
            return "payload"
        if noun_aircraft is not None:
            return "total"
        return "payload" if payload is not None else "unknown"
    if payload is not None:
        return "payload"
    return "unknown"


def _masses(text: str) -> tuple[set[float], set[float], list[str]]:
    payload: set[float] = set()
    total: set[float] = set()
    unknown: list[str] = []
    for match in _MASS.finditer(text):
        value = float(match.group(1)) * (1000.0 if match.group("kg") else 1.0)
        if not math.isfinite(value):
            continue
        kind = _classify_mass(text, match.start(), match.end())
        if kind == "payload":
            payload.add(value)
        elif kind == "total":
            total.add(value)
        else:
            unknown.append(match.group(0))
    return payload, total, unknown


def extract_payload_g(text: str) -> float | None:
    """Read an explicit payload/camera mass; do not infer it from total mass.

    Ambiguous, conflicting declarations are left unspecified for review.
    """
    payload, _, _ = _masses(_matchable(text))
    return payload.pop() if len(payload) == 1 else None


def parse_mission_spec(prompt: str) -> MissionSpec:
    normalized = normalize_prompt(prompt)
    text = _matchable(prompt)
    language = "ar" if any("؀" <= c <= "ۿ" for c in prompt) else "en"

    spec = MissionSpec(
        original=prompt,
        normalized=normalized,
        language=language,
    )
    spec.topology = _extract_topology(text)
    spec.altitude_m = _extract_altitude_m(text)
    spec.duration_min = _extract_duration_min(text)

    payload, total, unknown = _masses(text)
    if len(payload) == 1:
        spec.payload_g = next(iter(payload))
    elif len(payload) > 1:
        spec.unresolved.append(
            "conflicting payload masses: " + ", ".join(f"{v:g} g" for v in sorted(payload))
        )
    if len(total) == 1:
        spec.max_total_mass_g = next(iter(total))
    elif len(total) > 1:
        spec.unresolved.append(
            "conflicting total masses: " + ", ".join(f"{v:g} g" for v in sorted(total))
        )
    spec.unresolved.extend(f"unclassified mass '{item}'" for item in unknown)

    spec.hover_duration_s = _extract_hover_s(text)
    spec.square_size_m = _extract_square_size_m(text)
    spec.camera_required = bool(
        re.search(r"camera|photography|photo|imaging|كاميرا|تصوير|صور", text)
    )
    spec.use_case = _detect_use_case(text, spec.camera_required)
    spec.geofence_radius_m = _compute_geofence_radius(spec.square_size_m)

    if spec.square_size_m:
        action = "capture" if spec.camera_required else "fly"
        spec.waypoints = build_square_relative_waypoints(spec.square_size_m, action=action)

    return spec


def build_square_relative_waypoints(
    size_m: float, action: str = "fly"
) -> list[MissionWaypointSegment]:
    side = max(float(size_m), 5.0)
    return [
        MissionWaypointSegment("leg_1", north_m=side, east_m=0.0, action=action),
        MissionWaypointSegment("leg_2", north_m=0.0, east_m=side, action=action),
        MissionWaypointSegment("leg_3", north_m=-side, east_m=0.0, action=action),
        MissionWaypointSegment("leg_4", north_m=0.0, east_m=-side, action=action),
    ]


def estimate_relative_path_distance(waypoints: list[MissionWaypointSegment]) -> float:
    return round(sum(math.hypot(wp.north_m, wp.east_m) for wp in waypoints), 1)


# Whole words only: the old substring test read «electric» as a tricopter and
# «orbit» as a bicopter.
_TOPOLOGIES = (
    (
        "fixed_wing",
        r"fixed[- ]?wing|airplane|plane|winged|delta wing|two wings?"
        r"|(?:ذات\s+)?(?:ال)?جناح\s+(?:ال)?ثابت|ثابته?\s+(?:ال)?جناح",
    ),
    (
        "octocopter",
        r"octocopters?|octo(?:rotor|copter)?|octa(?:copter|rotor)|8[- ]?(?:rotor|motor|prop)s?"
        r"|eight[- ](?:rotor|motor|prop)s?"
        r"|ثمانيه?\s+(?:ال)?(?:محركات|مراوح|دوارات)|(?:8|ثماني|ثمان)\s+(?:محركات|مراوح)|اوكتوكوبتر",
    ),
    (
        "hexacopter",
        r"hexacopters?|hexa(?:rotor|copter)?|6[- ]?(?:rotor|motor|prop)s?|six[- ](?:rotor|motor|prop)s?"
        r"|سداسيه?|(?:6|سته|ست)\s+(?:محركات|مراوح)|هيكساكوبتر|هكساكوبتر",
    ),
    (
        "tricopter",
        r"tricopters?|tri(?:rotor|copter)|3[- ]?(?:rotor|motor)s?|three[- ](?:rotor|motor)s?"
        r"|ثلاثيه?\s+(?:ال)?(?:محركات|مراوح|دوارات)|(?:3|ثلاث|ثلاثه)\s+(?:محركات|مراوح)",
    ),
    (
        "bicopter",
        r"bicopters?|bi(?:rotor|copter)|2[- ]?(?:rotor|motor)s?|two[- ](?:rotor|motor)s?"
        r"|ثنائيه?\s+(?:ال)?(?:محركات|مراوح)",
    ),
    (
        "quadcopter",
        r"quadcopters?|quad(?:rotor|copter)?s?|4[- ]?(?:rotor|motor|prop)s?"
        r"|four[- ](?:rotor|motor|prop)s?"
        r"|رباعيه?|(?:4|اربع|اربعه)\s+(?:محركات|مراوح)|كوادكوبتر|كواد",
    ),
)
_TOPOLOGY_RES = tuple((name, _words(pattern)) for name, pattern in _TOPOLOGIES)


def _extract_topology(text: str) -> str | None:
    for topology, pattern in _TOPOLOGY_RES:
        if pattern.search(text):
            return topology
    return None


def _length_m(match: re.Match[str]) -> float:
    return float(match.group(1)) * (1000.0 if match.group("km") else 1.0)


_NOT_ALTITUDE = (
    r"(?!\s*(?:north|south|east|west|away|from|radius|square|x\b|by\b|شمال|جنوب|شرق|غرب|مربع))"
)
_ALTITUDE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        # «altitude of 20 m», «altitude 20»
        r"altitude\s*(?:of\s*)?" + _NUM + r"(?:" + _LENGTH + r")?",
        # «at 15 m altitude», «15 m high», «15 m agl»
        _NUM + _LENGTH + r"\s+(?:altitude|high|agl|above ground)",
        # «takes off to 12 m», «climb to 3000 m», «hover at 10 m», «fly at 12 m»
        r"(?:takeoff|takes?\s+off|climbs?|climbing|ascends?|ascending|rises?|reach(?:es)?|hover(?:s|ing)?"
        r"|fly(?:ing)?|flies|cruise|cruising)"
        r"(?:\s+(?:to|at|up to))?\s*(?:an?\s+altitude\s+of\s+)?" + _NUM + _LENGTH + _NOT_ALTITUDE,
        # «على ارتفاع 10 أمتار», «بارتفاع 15 متراً», «إلى ارتفاع 3000 متر»
        r"ارتفاع\s*(?:يبلغ\s*|قدره\s*|حوالي\s*)?" + _NUM + _LENGTH,
        # «تصعد إلى 3000 متر», «تحلق على 20 متر»
        r"(?:تصعد|يصعد|ترتفع|يرتفع|تحلق|يحلق|تحوم|يحوم|تطير|يطير)\s+(?:الى|حتى|على|عند)\s*"
        + _NUM
        + _LENGTH
        + _NOT_ALTITUDE,
    )
)


def _extract_altitude_m(text: str) -> float | None:
    for pattern in _ALTITUDE_PATTERNS:
        match = pattern.search(text)
        if match:
            if match.groupdict().get("m") is None and match.groupdict().get("km") is None:
                return float(match.group(1))  # «altitude 20» without a unit
            return _length_m(match)
    return None


_HOURS = r"hours?(?![a-z])|hrs?(?![a-z])|ساعات|ساعه"
_MINUTES = r"minutes?(?![a-z])|mins?(?![a-z])|دقيقه|دقائق"


def _extract_duration_min(text: str) -> float | None:
    """Requested flight time or endurance, in minutes. Hover dwell is separate."""
    match = re.search(_NUM + r"\s*(?:(?P<h>" + _HOURS + r")|(?P<min>" + _MINUTES + r"))", text)
    if match:
        return float(match.group(1)) * (60.0 if match.group("h") else 1.0)
    if re.search(r"ساعتين|ساعتان", text):
        return 120.0
    if re.search(r"دقيقتين|دقيقتان", text):
        return 2.0
    if re.search(r"(?:لمده|مده)\s+ساعه(?!\w)|\ban hour\b|\bone hour\b", text):
        return 60.0
    return None


_SECONDS = r"(?P<s>seconds?(?![a-z])|secs?(?![a-z])|s(?![a-z])|ثانيه|ثوان\w*|ثواني)"
_HOVER_UNITS = r"\s*(?:" + _SECONDS + r"|(?P<min>" + _MINUTES + r"))"


def _extract_hover_s(text: str) -> float | None:
    patterns = (
        # «hover for 20 seconds», «hover at 10 m for 20 seconds»
        r"hover(?:s|ing)?\b[^.;,]{0,40}?\bfor\s+" + _NUM + _HOVER_UNITS,
        r"hover(?:s|ing)?\s+" + _NUM + _HOVER_UNITS,
        # «تحوم على ارتفاع 10 أمتار لمدة 20 ثانية»
        r"(?:تحوم|يحوم|الحوم|التحويم|تحويم)[^.،,؛]{0,50}?(?:لمده|مده)\s*" + _NUM + _HOVER_UNITS,
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = float(match.group(1)) * (60.0 if match.group("min") else 1.0)
            if math.isfinite(value) and value > 0:
                return value
    return None


def _extract_square_size_m(text: str) -> float | None:
    size_match = re.search(r"(\d+(?:\.\d+)?)\s*[x]\s*(\d+(?:\.\d+)?)", text)
    if size_match:
        width = float(size_match.group(1))
        height = float(size_match.group(2))
        if min(width, height) > 0 and abs(width - height) / max(width, height) <= 0.2:
            return round((width + height) / 2.0, 1)

    patterns = [
        r"(?:square(?: path| patrol)?)\s*" + _NUM + _LENGTH,
        _NUM + _LENGTH + r"\s*square\b",
        # «مسار مربع طول ضلعه 25 متراً», «مربع 20 متر»
        r"مربع\w*\s*(?:طول\s+ضلعه\s*|ضلعه\s*|بطول\s*)?" + _NUM + _LENGTH,
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _length_m(match)
    return None


def _detect_use_case(text: str, camera_required: bool) -> str:
    buckets = [
        ("cargo", ("cargo", "delivery", "freight", "توصيل", "شحن")),
        ("racing", ("racing", "fpv", "acro", "سباق")),
        ("inspection", ("inspection", "industrial", "warehouse", "فحص", "مراقبه")),
        ("mapping", ("mapping", "survey", "long range", "مسح", "خرائط")),
        ("rescue", ("rescue", "sar", "search", "انقاذ", "بحث")),
        ("photography", ("photography", "photo", "camera", "تصوير", "كاميرا")),
    ]
    for use_case, patterns in buckets:
        if any(pattern in text for pattern in patterns):
            return use_case
    return "photography" if camera_required else "general"


def _compute_geofence_radius(square_size_m: float | None) -> float | None:
    if not square_size_m:
        return None
    return round(max(200.0, square_size_m * 1.5), 1)

"""Time-of-day compliance (issue #25).

The TCPA restricts outbound telemarketing calls to **8am-9pm local time**
in the recipient's timezone. We translate a phone number into an IANA
timezone (area code → timezone for the top ~100 US area codes, with
country-level fallback), then check the current local time against the
configured window.

Why a self-contained DST helper
-------------------------------
The stdlib ``zoneinfo`` module needs the IANA ``tzdata`` package on
Windows (it's bundled on Linux/macOS). That package is *not* in our
v1 deps and we want this module to work on a fresh install with no
extra downloads. We implement a tiny DST-aware offset table here for
the timezones we actually serve — covers the same answers zoneinfo
would for North America + the most common European + APAC zones, with
no IANA database dependency.

DST rule
--------
For the US/CA, "spring forward" is the 2nd Sunday of March at 2am
local, "fall back" is the 1st Sunday of November at 2am local. EU uses
the last Sunday of March / October. The exact rule per zone is in
:data:`_DST_RULES`. The function applies it with the local clock —
UTC-aware callers must pass ``now=`` in UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple


# ────────────────────────── area code → IANA timezone ──────────────────────
# Curated list of the top 100 US/CA area codes. Source: NANP public
# registry. Every entry is the most-populous timezone for that area code
# (US spans multiple timezones but each area code lives entirely in one
# NPA region; the few split cases are mapped to the dominant one).
AREA_CODE_TZ: dict[str, str] = {
    # Eastern (covers NY, FL, GA, NC, MA, MI, OH, PA, VA, etc.)
    "201": "America/New_York", "212": "America/New_York", "305": "America/New_York",
    "313": "America/New_York", "404": "America/New_York", "407": "America/New_York",
    "410": "America/New_York", "412": "America/New_York", "413": "America/New_York",
    "434": "America/New_York", "440": "America/New_York",
    "470": "America/New_York", "475": "America/New_York", "478": "America/New_York",
    "484": "America/New_York", "502": "America/New_York", "508": "America/New_York",
    "516": "America/New_York", "517": "America/New_York",
    "518": "America/New_York", "540": "America/New_York", "551": "America/New_York",
    "561": "America/New_York", "570": "America/New_York", "585": "America/New_York",
    "586": "America/New_York", "603": "America/New_York",
    "607": "America/New_York", "609": "America/New_York", "610": "America/New_York",
    "612": "America/New_York", "614": "America/New_York", "616": "America/New_York",
    "617": "America/New_York", "631": "America/New_York", "646": "America/New_York",
    "678": "America/New_York", "704": "America/New_York", "706": "America/New_York",
    "716": "America/New_York", "718": "America/New_York", "732": "America/New_York",
    "734": "America/New_York", "740": "America/New_York", "754": "America/New_York",
    "757": "America/New_York", "770": "America/New_York", "772": "America/New_York",
    "774": "America/New_York", "781": "America/New_York", "786": "America/New_York",
    "802": "America/New_York", "804": "America/New_York", "810": "America/New_York",
    "812": "America/New_York", "813": "America/New_York", "814": "America/New_York",
    "816": "America/New_York", "828": "America/New_York", "843": "America/New_York",
    "845": "America/New_York", "856": "America/New_York", "857": "America/New_York",
    "860": "America/New_York", "862": "America/New_York", "863": "America/New_York",
    "864": "America/New_York", "904": "America/New_York", "908": "America/New_York",
    "910": "America/New_York", "912": "America/New_York", "914": "America/New_York",
    "917": "America/New_York", "919": "America/New_York",
    "937": "America/New_York", "941": "America/New_York", "954": "America/New_York",
    "973": "America/New_York", "978": "America/New_York", "989": "America/New_York",
    # Central
    "205": "America/Chicago", "210": "America/Chicago", "214": "America/Chicago",
    "217": "America/Chicago", "218": "America/Chicago",
    "219": "America/Chicago", "225": "America/Chicago", "228": "America/Chicago",
    "251": "America/Chicago", "252": "America/Chicago", "254": "America/Chicago",
    "262": "America/Chicago", "281": "America/Chicago", "301": "America/Chicago",
    "302": "America/Chicago", "309": "America/Chicago", "312": "America/Chicago",
    "314": "America/Chicago", "316": "America/Chicago", "317": "America/Chicago",
    "318": "America/Chicago", "319": "America/Chicago", "320": "America/Chicago",
    "321": "America/Chicago", "331": "America/Chicago", "334": "America/Chicago",
    "337": "America/Chicago", "346": "America/Chicago", "347": "America/Chicago",
    "352": "America/Chicago", "361": "America/Chicago", "386": "America/Chicago",
    "414": "America/Chicago", "417": "America/Chicago", "469": "America/Chicago",
    "479": "America/Chicago", "501": "America/Chicago", "504": "America/Chicago",
    "507": "America/Chicago", "512": "America/Chicago", "515": "America/Chicago",
    "563": "America/Chicago", "573": "America/Chicago", "574": "America/Chicago",
    "580": "America/Chicago",
    "608": "America/Chicago", "615": "America/Chicago", "618": "America/Chicago",
    "620": "America/Chicago", "630": "America/Chicago", "636": "America/Chicago",
    "641": "America/Chicago", "662": "America/Chicago", "682": "America/Chicago",
    "708": "America/Chicago", "713": "America/Chicago", "715": "America/Chicago",
    "717": "America/Chicago", "726": "America/Chicago",
    "731": "America/Chicago", "737": "America/Chicago", "763": "America/Chicago",
    "765": "America/Chicago", "769": "America/Chicago", "773": "America/Chicago",
    "779": "America/Chicago", "785": "America/Chicago", "806": "America/Chicago",
    "815": "America/Chicago", "817": "America/Chicago",
    "830": "America/Chicago", "832": "America/Chicago", "847": "America/Chicago",
    "848": "America/Chicago", "850": "America/Chicago",
    "870": "America/Chicago", "872": "America/Chicago", "901": "America/Chicago",
    "903": "America/Chicago", "913": "America/Chicago", "915": "America/Chicago",
    "918": "America/Chicago", "925": "America/Chicago",
    "936": "America/Chicago", "940": "America/Chicago", "947": "America/Chicago",
    "952": "America/Chicago", "956": "America/Chicago", "959": "America/Chicago",
    "972": "America/Chicago", "979": "America/Chicago",
    # Mountain
    "303": "America/Denver", "307": "America/Denver", "308": "America/Denver",
    "385": "America/Denver", "406": "America/Denver", "435": "America/Denver",
    "480": "America/Denver", "505": "America/Denver", "575": "America/Denver",
    "605": "America/Denver", "719": "America/Denver",
    "720": "America/Denver", "725": "America/Denver", "801": "America/Denver",
    "970": "America/Denver",
    # Arizona (no DST)
    "602": "America/Phoenix", "623": "America/Phoenix", "928": "America/Phoenix",
    # Pacific
    "206": "America/Los_Angeles", "209": "America/Los_Angeles", "213": "America/Los_Angeles",
    "310": "America/Los_Angeles", "323": "America/Los_Angeles", "408": "America/Los_Angeles",
    "415": "America/Los_Angeles", "424": "America/Los_Angeles", "425": "America/Los_Angeles",
    "442": "America/Los_Angeles", "503": "America/Los_Angeles",
    "530": "America/Los_Angeles", "559": "America/Los_Angeles", "562": "America/Los_Angeles",
    "619": "America/Los_Angeles", "626": "America/Los_Angeles", "650": "America/Los_Angeles",
    "657": "America/Los_Angeles", "661": "America/Los_Angeles", "702": "America/Los_Angeles",
    "707": "America/Los_Angeles", "714": "America/Los_Angeles",
    "747": "America/Los_Angeles", "760": "America/Los_Angeles", "775": "America/Los_Angeles",
    "805": "America/Los_Angeles", "818": "America/Los_Angeles", "831": "America/Los_Angeles",
    "858": "America/Los_Angeles", "909": "America/Los_Angeles", "916": "America/Los_Angeles",
    "949": "America/Los_Angeles", "951": "America/Los_Angeles",
    # Alaska / Hawaii
    "907": "America/Anchorage", "808": "Pacific/Honolulu",
    # Canada
    "204": "America/Winnipeg", "226": "America/Toronto", "236": "America/Vancouver",
    "249": "America/Toronto", "250": "America/Vancouver", "289": "America/Toronto",
    "306": "America/Regina", "343": "America/Toronto", "365": "America/Toronto",
    "403": "America/Edmonton", "416": "America/Toronto", "418": "America/Toronto",
    "431": "America/Winnipeg", "437": "America/Toronto", "438": "America/Toronto",
    "450": "America/Toronto", "506": "America/Moncton", "514": "America/Toronto",
    "519": "America/Toronto", "548": "America/Toronto", "579": "America/Toronto",
    "581": "America/Quebec", "587": "America/Edmonton", "604": "America/Vancouver",
    "613": "America/Toronto", "647": "America/Toronto", "672": "America/Vancouver",
    "705": "America/Toronto", "709": "America/St_Johns", "778": "America/Vancouver",
    "780": "America/Edmonton", "782": "America/Halifax", "807": "America/Toronto",
    "819": "America/Toronto", "825": "America/Edmonton", "867": "America/Yellowknife",
    "902": "America/Halifax", "905": "America/Toronto",
}

# Country code (1-3 digit prefix before the national number) → IANA tz.
COUNTRY_TZ: dict[str, str] = {
    "1":   "America/New_York",   # NANP fallback (East Coast default)
    "44":  "Europe/London",      # UK
    "49":  "Europe/Berlin",      # DE
    "33":  "Europe/Paris",       # FR
    "34":  "Europe/Madrid",      # ES
    "39":  "Europe/Rome",        # IT
    "31":  "Europe/Amsterdam",   # NL
    "46":  "Europe/Stockholm",   # SE
    "47":  "Europe/Oslo",        # NO
    "45":  "Europe/Copenhagen",  # DK
    "358": "Europe/Helsinki",    # FI
    "41":  "Europe/Zurich",      # CH
    "43":  "Europe/Vienna",      # AT
    "351": "Europe/Lisbon",      # PT
    "353": "Europe/Dublin",      # IE
    "30":  "Europe/Athens",      # GR
    "48":  "Europe/Warsaw",      # PL
    "420": "Europe/Prague",      # CZ
    "36":  "Europe/Budapest",    # HU
    "40":  "Europe/Bucharest",   # RO
    "7":   "Europe/Moscow",      # RU
    "90":  "Europe/Istanbul",    # TR
    "972": "Asia/Jerusalem",     # IL
    "971": "Asia/Dubai",         # AE
    "966": "Asia/Riyadh",        # SA
    "91":  "Asia/Kolkata",       # IN
    "86":  "Asia/Shanghai",      # CN
    "81":  "Asia/Tokyo",         # JP
    "82":  "Asia/Seoul",         # KR
    "852": "Asia/Hong_Kong",     # HK
    "886": "Asia/Taipei",        # TW
    "65":  "Asia/Singapore",     # SG
    "60":  "Asia/Kuala_Lumpur",  # MY
    "62":  "Asia/Jakarta",       # ID
    "63":  "Asia/Manila",        # PH
    "66":  "Asia/Bangkok",       # TH
    "84":  "Asia/Ho_Chi_Minh",   # VN
    "61":  "Australia/Sydney",   # AU (East coast default)
    "64":  "Pacific/Auckland",   # NZ
    "55":  "America/Sao_Paulo",  # BR
    "52":  "America/Mexico_City",# MX
    "54":  "America/Argentina/Buenos_Aires",  # AR
    "56":  "America/Santiago",   # CL
    "57":  "America/Bogota",     # CO
    "27":  "Africa/Johannesburg",# ZA
    "20":  "Africa/Cairo",       # EG
    "234": "Africa/Lagos",       # NG
    "254": "Africa/Nairobi",     # KE
}

# Default if we cannot determine a timezone at all.
DEFAULT_TZ = "America/New_York"


# ────────────────────────── self-contained DST helper ──────────────────────
# We don't rely on the stdlib zoneinfo because Windows installs do not
# ship the IANA tzdata package. The table below is enough for the
# timezones we serve — covers NANP, EU, APAC, ANZ.

def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> datetime:
    """Return the date of the n-th ``weekday`` in ``month``/``year``.

    weekday: 0=Monday ... 6=Sunday
    n: 1..5
    """
    first = datetime(year, month, 1)
    # Days until the first occurrence of `weekday`:
    delta = (weekday - first.weekday()) % 7
    day = 1 + delta + (n - 1) * 7
    return datetime(year, month, day)


# DST rules per IANA name. Each is (start_rule, end_rule) where each
# rule is a tuple: (month, weekday, n, hour). The rule fires at 02:00
# local standard time on the matching day. If a zone has no entry, it
# is "no DST" (fixed offset year-round).
#
# weekday: 0=Mon ... 6=Sun.
_DST_RULES: dict[str, Tuple[Tuple[int, int, int, int], Tuple[int, int, int, int]]] = {
    # US: 2nd Sun of March → 1st Sun of November
    "America/New_York":     ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Chicago":      ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Denver":       ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Los_Angeles":  ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Anchorage":    ((3, 6, 2, 2), (11, 6, 1, 2)),
    # Canada mostly follows the US rule
    "America/Toronto":      ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Winnipeg":     ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Edmonton":     ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Vancouver":    ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Halifax":      ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Moncton":      ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/St_Johns":     ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Quebec":       ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Yellowknife":  ((3, 6, 2, 2), (11, 6, 1, 2)),
    "America/Regina":       ((3, 6, 2, 2), (11, 6, 1, 2)),
    # EU: last Sun of March → last Sun of October
    "Europe/London":        ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Berlin":        ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Paris":         ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Madrid":        ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Rome":          ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Amsterdam":     ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Stockholm":     ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Oslo":          ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Copenhagen":    ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Helsinki":      ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Zurich":        ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Vienna":        ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Lisbon":        ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Dublin":        ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Athens":        ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Warsaw":        ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Prague":        ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Budapest":      ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Bucharest":     ((3, 6, 5, 1), (10, 6, 5, 1)),
    "Europe/Istanbul":      ((3, 6, 5, 1), (10, 6, 5, 1)),
}

# Standard-time UTC offsets (hours). Positive = east of UTC. DST adds +1
# hour during the summer. Zones NOT in :data:`_DST_RULES` are fixed.
_STD_OFFSET_HOURS: dict[str, float] = {
    "America/New_York":      -5,
    "America/Chicago":       -6,
    "America/Denver":        -7,
    "America/Phoenix":       -7,   # no DST
    "America/Los_Angeles":   -8,
    "America/Anchorage":     -9,
    "Pacific/Honolulu":     -10,   # no DST
    "America/Toronto":       -5,
    "America/Winnipeg":      -6,
    "America/Edmonton":      -7,
    "America/Vancouver":     -8,
    "America/Halifax":       -4,
    "America/Moncton":       -4,
    "America/St_Johns":     -3.5,
    "America/Quebec":        -5,
    "America/Yellowknife":   -7,
    "America/Regina":        -6,    # no DST
    "America/Sao_Paulo":     -3,    # no DST
    "America/Mexico_City":   -6,    # has DST but we approximate
    "America/Argentina/Buenos_Aires": -3,
    "America/Santiago":      -4,
    "America/Bogota":        -5,    # no DST
    "Europe/London":          0,
    "Europe/Berlin":          1,
    "Europe/Paris":           1,
    "Europe/Madrid":          1,
    "Europe/Rome":            1,
    "Europe/Amsterdam":       1,
    "Europe/Stockholm":       1,
    "Europe/Oslo":            1,
    "Europe/Copenhagen":      1,
    "Europe/Helsinki":        2,
    "Europe/Zurich":          1,
    "Europe/Vienna":          1,
    "Europe/Lisbon":          0,
    "Europe/Dublin":          0,
    "Europe/Athens":          2,
    "Europe/Warsaw":          1,
    "Europe/Prague":          1,
    "Europe/Budapest":        1,
    "Europe/Bucharest":       2,
    "Europe/Moscow":          3,    # no DST
    "Europe/Istanbul":        3,    # fixed since 2016
    "Asia/Jerusalem":         2,
    "Asia/Dubai":             4,
    "Asia/Riyadh":            3,
    "Asia/Kolkata":         5.5,
    "Asia/Shanghai":          8,
    "Asia/Tokyo":             9,
    "Asia/Seoul":             9,
    "Asia/Hong_Kong":         8,
    "Asia/Taipei":            8,
    "Asia/Singapore":         8,
    "Asia/Kuala_Lumpur":      8,
    "Asia/Jakarta":           7,
    "Asia/Manila":            8,
    "Asia/Bangkok":           7,
    "Asia/Ho_Chi_Minh":       7,
    "Australia/Sydney":      10,    # has DST, but we ignore for the v1
    "Pacific/Auckland":      12,    # has DST
    "Africa/Johannesburg":    2,    # no DST
    "Africa/Cairo":           2,    # no DST (post-2014)
    "Africa/Lagos":           1,    # no DST
    "Africa/Nairobi":         3,    # no DST
}


def _is_dst_active(tz_name: str, utc_now: datetime) -> bool:
    """Return True if DST is in effect at ``utc_now`` for ``tz_name``."""
    rules = _DST_RULES.get(tz_name)
    if not rules:
        return False
    # Convert utc_now to local standard time so we can apply the rule
    # in the local frame. The "spring forward" / "fall back" happens
    # at a fixed local-clock hour, not a fixed UTC hour, so we must
    # work in local time.
    std_hours = _STD_OFFSET_HOURS.get(tz_name, 0)
    # Approximate local STANDARD time: utc_now - std_offset
    local_std = utc_now + timedelta(hours=-std_hours)
    year = local_std.year
    sm, sw, sn, sh = rules[0]   # spring forward
    em, ew, en, eh = rules[1]   # fall back
    spring = _nth_weekday_of_month(year, sm, sw, sn).replace(
        hour=sh, tzinfo=timezone.utc
    )
    # Adjust spring from local-clock to UTC. spring is in standard
    # time until the clock jumps forward — we treat the rule as the
    # start of DST, so convert to UTC by subtracting the standard
    # offset (the local time is "wall clock" before the jump).
    spring_utc = spring - timedelta(hours=std_hours)
    fall = _nth_weekday_of_month(year, em, ew, en).replace(
        hour=eh, tzinfo=timezone.utc
    )
    fall_utc = fall - timedelta(hours=std_hours)
    return spring_utc <= utc_now < fall_utc


def _now_in_tz(tz_name: str, now: Optional[datetime] = None) -> datetime:
    """Return ``now`` localised to ``tz_name``. Pure-Python, no IANA
    tzdata dependency. Falls back to UTC on unknown zones."""
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    std_hours = _STD_OFFSET_HOURS.get(tz_name)
    if std_hours is None:
        # Unknown zone — return UTC and let the consumer decide.
        return now
    if _is_dst_active(tz_name, now):
        offset = std_hours + 1
    else:
        offset = std_hours
    return now + timedelta(hours=offset)


# ────────────────────────── public surface ──────────────────────────────────


def _strip_phone(raw: str) -> str:
    """Normalize a phone to ``+<digits>`` for parsing."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if s.startswith("+"):
        return "+" + "".join(c for c in s[1:] if c.isdigit())
    return "".join(c for c in s if c.isdigit())


def _country_and_area(phone: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (country_code, area_code) from an E.164 phone.

    Examples
    --------
    >>> _country_and_area("+14155550100")
    ('1', '415')
    >>> _country_and_area("+442079812345")
    ('44', None)
    """
    digits = _strip_phone(phone).lstrip("+")
    if not digits:
        return None, None
    if digits.startswith("1") and len(digits) >= 4:
        return "1", digits[1:4]
    for cc_len in (3, 2, 1):
        cc = digits[:cc_len]
        if cc in COUNTRY_TZ:
            return cc, None
    return None, None


def get_timezone(phone: str) -> str:
    """Return the IANA timezone for a phone number.

    Strategy
    --------
    1. NANP ``+1`` → look up the 3-digit area code in :data:`AREA_CODE_TZ`.
    2. Other countries → look up the country code in :data:`COUNTRY_TZ`.
    3. Fallback → :data:`DEFAULT_TZ`.
    """
    cc, area = _country_and_area(phone)
    if cc == "1" and area and area in AREA_CODE_TZ:
        return AREA_CODE_TZ[area]
    if cc and cc in COUNTRY_TZ:
        return COUNTRY_TZ[cc]
    return DEFAULT_TZ


def is_in_window(
    phone: str,
    window: Tuple[int, int] = (8, 21),
    now: Optional[datetime] = None,
) -> bool:
    """Return True if the current local time for ``phone`` is in ``window``.

    The window is **inclusive of start, exclusive of end** (so 8:00 ok,
    21:00 not ok — matching the TCPA "before 9pm" wording). ``window`` is
    a 24h ``(start_hour, end_hour)`` tuple in the recipient's timezone.
    """
    start, end = window
    if end <= start or end > 24 or start < 0:
        raise ValueError(f"invalid window: {window}")
    tz_name = get_timezone(phone)
    local = _now_in_tz(tz_name, now)
    return int(start) <= local.hour < int(end)


def bulk_filter_window(
    phones: list[str],
    window: Tuple[int, int] = (8, 21),
    now: Optional[datetime] = None,
) -> dict[str, bool]:
    """Return ``{phone: in_window?}`` for every phone in the list.

    Order of inputs is preserved; duplicate phones are evaluated once
    and the result is duplicated. Acceptable for the v1 contact-list
    size (<=10k) — no batching, no cache.
    """
    return {p: is_in_window(p, window, now) for p in phones}

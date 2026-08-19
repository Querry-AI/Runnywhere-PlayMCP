"""Pace-dependent effort estimates for the course detail page.

Distance and elevation are facts about the course. Time, steps and calories
are not: they depend entirely on how fast the runner actually goes. This
module keeps that dependency explicit and in one place, so the page can
recompute everything the moment the user changes their pace.

Pure functions, no I/O. The same constants are serialised to the browser
(see PACE_MODEL) so the client recomputes with an identical model instead of
a second, silently diverging one.
"""

from __future__ import annotations

# Pace is stored in seconds per km. 7:00/km is the default: a comfortable
# recreational running pace, and the value the page opens with.
DEFAULT_PACE_S = 420
FASTEST_PACE_S = 240   # 4:00/km
SLOWEST_PACE_S = 600   # 10:00/km
PACE_STEP_S = 10

# Named bands as (min_pace_s, name, feel), slowest first. A pace belongs to
# the first band whose floor it meets. 7:00 is deliberately the bottom of
# "조깅" so the default pace lands on the friendliest label.
PACE_TIERS: tuple[tuple[int, str, str], ...] = (
    (540, "산책", "숨이 편한 속도"),
    (420, "조깅", "대화가 되는 속도"),
    (360, "러닝", "말이 짧아지는 속도"),
    (300, "템포", "숨이 차오르는 속도"),
    (0,   "질주", "오래 못 가는 속도"),
)

# Cadence rises with speed: a runner at 5:00/km takes longer, faster strides
# than the same runner at 9:00/km. Linear fit over the recreational range.
CADENCE_BASE_SPM = 145.0
CADENCE_PER_KMH = 6.5
CADENCE_REF_KMH = 6.0
CADENCE_MIN_SPM = 110.0
CADENCE_MAX_SPM = 190.0

# ACSM running equation: VO2 (ml/kg/min) = 0.2 * speed(m/min) + 3.5, so
# MET = VO2 / 3.5 is linear in km/h. An interpolated lookup table was tried
# first and produced calories that FELL as pace got faster, which is wrong.
# Sanity check: 8 km/h -> 8.6 MET, 12 km/h -> 12.4 MET, 15 km/h -> 15.3 MET.
MET_PER_KMH = 0.952
MET_INTERCEPT = 1.0

# No account system, so body mass is an assumption. It is stated on screen
# rather than hidden, because calories are meaningless without it.
DEFAULT_WEIGHT_KG = 65.0


def clamp_pace_s(pace_s: float) -> int:
    """Snap a pace to the supported range and step."""
    snapped = round(float(pace_s) / PACE_STEP_S) * PACE_STEP_S
    return int(max(FASTEST_PACE_S, min(SLOWEST_PACE_S, snapped)))


def format_pace(pace_s: int) -> str:
    """420 -> "7'00\"" """
    minutes, seconds = divmod(int(pace_s), 60)
    return f"{minutes}'{seconds:02d}\""


def pace_tier(pace_s: int) -> tuple[str, str]:
    """(name, one-line feel) for a pace."""
    for floor_s, name, feel in PACE_TIERS:
        if pace_s >= floor_s:
            return name, feel
    return PACE_TIERS[-1][1], PACE_TIERS[-1][2]


def speed_kmh(pace_s: float) -> float:
    return 3600.0 / float(pace_s)


def cadence_spm(pace_s: float) -> float:
    raw = CADENCE_BASE_SPM + CADENCE_PER_KMH * (speed_kmh(pace_s) - CADENCE_REF_KMH)
    return max(CADENCE_MIN_SPM, min(CADENCE_MAX_SPM, raw))


def met(pace_s: float) -> float:
    return MET_PER_KMH * speed_kmh(pace_s) + MET_INTERCEPT


def duration_min(distance_km: float, pace_s: float) -> float:
    return distance_km * pace_s / 60.0


def steps(distance_km: float, pace_s: float) -> int:
    return int(round(cadence_spm(pace_s) * duration_min(distance_km, pace_s)))


def calories(distance_km: float, pace_s: float,
             weight_kg: float = DEFAULT_WEIGHT_KG) -> int:
    minutes = duration_min(distance_km, pace_s)
    return int(round(met(pace_s) * 3.5 * weight_kg / 200.0 * minutes))


def effort(distance_km: float, pace_s: float,
           weight_kg: float = DEFAULT_WEIGHT_KG) -> dict:
    """Everything the pace controls, for one distance."""
    pace_s = clamp_pace_s(pace_s)
    name, feel = pace_tier(pace_s)
    return {
        "pace_s": pace_s,
        "pace_label": format_pace(pace_s),
        "tier": name,
        "tier_feel": feel,
        "duration_min": int(round(duration_min(distance_km, pace_s))),
        "steps": steps(distance_km, pace_s),
        "kcal": calories(distance_km, pace_s, weight_kg),
    }


# Serialised to the browser so the client recomputes with the same numbers.
PACE_MODEL = {
    "default_s": DEFAULT_PACE_S,
    "fastest_s": FASTEST_PACE_S,
    "slowest_s": SLOWEST_PACE_S,
    "step_s": PACE_STEP_S,
    "tiers": [{"min_s": m, "name": n, "feel": f} for m, n, f in PACE_TIERS],
    "cadence": {"base": CADENCE_BASE_SPM, "per_kmh": CADENCE_PER_KMH,
                "ref_kmh": CADENCE_REF_KMH,
                "min": CADENCE_MIN_SPM, "max": CADENCE_MAX_SPM},
    "met": {"per_kmh": MET_PER_KMH, "intercept": MET_INTERCEPT},
    "weight_kg": DEFAULT_WEIGHT_KG,
}

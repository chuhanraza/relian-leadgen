"""Region rotation lists per vertical. Lead Hunter always works one region at a time,
picking whichever (vertical, region) pair in leadgen.regions_covered is least recently
searched (or missing entirely). Add/remove regions here as coverage needs change.

Multi-country groupings that mix languages ("Western Europe", "Southern Europe") were
split into single-language regions so LANGUAGE_BY_REGION below can route precisely instead
of guessing across a mixed-language group.
"""

MOTO_APPAREL_REGIONS = [
    "USA",
    "Canada",
    "UK",
    "Germany/DACH",
    "France/Benelux",
    "Spain",
    "Italy",
    "Nordics",
    "Australia/NZ",
    "Japan",
]

# Mainland China is intentionally absent — combat_sports excludes it per the brief.
COMBAT_SPORTS_REGIONS = [
    "USA",
    "Canada",
    "UK",
    "Ireland",
    "Germany/DACH",
    "France/Benelux",
    "Spain",
    "Italy",
    "Nordics",
    "Eastern Europe",
    "Australia/NZ",
    "Japan",
    "Southeast Asia (ex-China)",
    "Latin America",
    "Middle East",
]

REGIONS_BY_VERTICAL = {
    "moto_apparel": MOTO_APPAREL_REGIONS,
    "combat_sports": COMBAT_SPORTS_REGIONS,
}

LANGUAGE_BY_REGION = {
    "Germany/DACH": "de",
    "France/Benelux": "fr",
    "Spain": "es",
    "Italy": "it",
}
# Every other region defaults to English. This is deliberate — Nordics, Eastern Europe,
# Latin America, and others are NOT covered by a vetted template yet. Do not add languages
# here without a real, ideally native-reviewed, template to back them.


def language_for_region(region: str) -> str:
    return LANGUAGE_BY_REGION.get(region, "en")

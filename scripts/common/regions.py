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
    "Eastern Europe",
    "Southeast Asia (ex-China)",
    "Australia/NZ",
    "Japan",
    "Brazil",
    "Latin America (ex-Brazil)",
    "South Africa",
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
    "Brazil",
    "Latin America (ex-Brazil)",
    "Middle East",
    "South Africa",
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
    "Brazil": "pt",
}
# South Africa, Latin America (ex-Brazil), Eastern Europe, Nordics, Southeast Asia,
# Middle East all stay English by design — no vetted template backs them yet.
# moto_apparel has NO language routing at all (English-only, deliberate, unchanged).


def language_for_region(region: str) -> str:
    return LANGUAGE_BY_REGION.get(region, "en")

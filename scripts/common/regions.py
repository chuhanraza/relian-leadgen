"""Region rotation lists per vertical. Lead Hunter always works one region at a time,
picking whichever (vertical, region) pair in leadgen.regions_covered is least recently
searched (or missing entirely). Add/remove regions here as coverage needs change.
"""

MOTO_APPAREL_REGIONS = [
    "USA",
    "Canada",
    "UK",
    "Germany/DACH",
    "France/Benelux",
    "Nordics",
    "Southern Europe",
    "Australia/NZ",
    "Japan",
]

# Mainland China is intentionally absent — combat_sports excludes it per the brief.
COMBAT_SPORTS_REGIONS = [
    "USA",
    "Canada",
    "UK",
    "Ireland",
    "Western Europe",
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

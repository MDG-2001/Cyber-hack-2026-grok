"""Country names, codes and nationalities.

Territories stay their own country. "International" and "Europe" are not countries.
"""

from __future__ import annotations

import re

from .names import strip_accents

# ISO alpha-2 → the name used in reasons. Long official names are aliases below.
ISO2 = {
    "AD": "Andorra",
    "AE": "United Arab Emirates",
    "AF": "Afghanistan",
    "AG": "Antigua and Barbuda",
    "AL": "Albania",
    "AM": "Armenia",
    "AO": "Angola",
    "AR": "Argentina",
    "AT": "Austria",
    "AU": "Australia",
    "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina",
    "BB": "Barbados",
    "BD": "Bangladesh",
    "BE": "Belgium",
    "BF": "Burkina Faso",
    "BG": "Bulgaria",
    "BH": "Bahrain",
    "BI": "Burundi",
    "BJ": "Benin",
    "BN": "Brunei",
    "BO": "Bolivia",
    "BR": "Brazil",
    "BS": "Bahamas",
    "BT": "Bhutan",
    "BW": "Botswana",
    "BY": "Belarus",
    "BZ": "Belize",
    "CA": "Canada",
    "CD": "Democratic Republic of the Congo",
    "CF": "Central African Republic",
    "CG": "Congo",
    "CH": "Switzerland",
    "CI": "Côte d'Ivoire",
    "CL": "Chile",
    "CM": "Cameroon",
    "CN": "China",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "CU": "Cuba",
    "CV": "Cabo Verde",
    "CY": "Cyprus",
    "CZ": "Czech Republic",
    "DE": "Germany",
    "DJ": "Djibouti",
    "DK": "Denmark",
    "DM": "Dominica",
    "DO": "Dominican Republic",
    "DZ": "Algeria",
    "EC": "Ecuador",
    "EE": "Estonia",
    "EG": "Egypt",
    "ER": "Eritrea",
    "ES": "Spain",
    "ET": "Ethiopia",
    "FI": "Finland",
    "FJ": "Fiji",
    "FM": "Micronesia",
    "FR": "France",
    "GA": "Gabon",
    "GB": "United Kingdom",
    "GD": "Grenada",
    "GE": "Georgia",
    "GH": "Ghana",
    "GM": "Gambia",
    "GN": "Guinea",
    "GQ": "Equatorial Guinea",
    "GR": "Greece",
    "GT": "Guatemala",
    "GW": "Guinea-Bissau",
    "GY": "Guyana",
    "HK": "Hong Kong",
    "HN": "Honduras",
    "HR": "Croatia",
    "HT": "Haiti",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IN": "India",
    "IQ": "Iraq",
    "IR": "Iran",
    "IS": "Iceland",
    "IT": "Italy",
    "JM": "Jamaica",
    "JO": "Jordan",
    "JP": "Japan",
    "KE": "Kenya",
    "KG": "Kyrgyzstan",
    "KH": "Cambodia",
    "KM": "Comoros",
    "KN": "Saint Kitts and Nevis",
    "KP": "North Korea",
    "KR": "South Korea",
    "KW": "Kuwait",
    "KZ": "Kazakhstan",
    "LA": "Laos",
    "LB": "Lebanon",
    "LC": "Saint Lucia",
    "LI": "Liechtenstein",
    "LK": "Sri Lanka",
    "LR": "Liberia",
    "LS": "Lesotho",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "LY": "Libya",
    "MA": "Morocco",
    "MC": "Monaco",
    "MD": "Moldova",
    "ME": "Montenegro",
    "MG": "Madagascar",
    "MK": "North Macedonia",
    "ML": "Mali",
    "MM": "Myanmar",
    "MN": "Mongolia",
    "MO": "Macao",
    "MP": "Northern Mariana Islands",
    "MR": "Mauritania",
    "MT": "Malta",
    "MU": "Mauritius",
    "MV": "Maldives",
    "MW": "Malawi",
    "MX": "Mexico",
    "MY": "Malaysia",
    "MZ": "Mozambique",
    "NA": "Namibia",
    "NE": "Niger",
    "NG": "Nigeria",
    "NI": "Nicaragua",
    "NL": "Netherlands",
    "NO": "Norway",
    "NP": "Nepal",
    "NZ": "New Zealand",
    "OM": "Oman",
    "PA": "Panama",
    "PE": "Peru",
    "PG": "Papua New Guinea",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PR": "Puerto Rico",
    "PS": "Palestine",
    "PT": "Portugal",
    "PY": "Paraguay",
    "QA": "Qatar",
    "RO": "Romania",
    "RS": "Serbia",
    "RU": "Russia",
    "RW": "Rwanda",
    "SA": "Saudi Arabia",
    "SD": "Sudan",
    "SE": "Sweden",
    "SG": "Singapore",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "SL": "Sierra Leone",
    "SN": "Senegal",
    "SO": "Somalia",
    "SR": "Suriname",
    "SS": "South Sudan",
    "SV": "El Salvador",
    "SY": "Syria",
    "SZ": "Eswatini",
    "TD": "Chad",
    "TG": "Togo",
    "TH": "Thailand",
    "TJ": "Tajikistan",
    "TL": "Timor-Leste",
    "TM": "Turkmenistan",
    "TN": "Tunisia",
    "TR": "Turkey",
    "TT": "Trinidad and Tobago",
    "TW": "Taiwan",
    "TZ": "Tanzania",
    "UA": "Ukraine",
    "UG": "Uganda",
    "US": "United States",
    "UY": "Uruguay",
    "UZ": "Uzbekistan",
    "VA": "Vatican City",
    "VE": "Venezuela",
    "VN": "Vietnam",
    "YE": "Yemen",
    "ZA": "South Africa",
    "ZM": "Zambia",
    "ZW": "Zimbabwe",
}

# Nationality words and other spellings that are not the country name itself.
ALIASES = {
    "United States": ["USA", "U.S.A.", "U.S.", "United States of America", "American"],
    "United Kingdom": ["UK", "U.K.", "Great Britain", "Britain", "British"],
    "United Arab Emirates": ["UAE", "U.A.E.", "Emirati"],
    "Côte d'Ivoire": ["Cote d'Ivoire", "Cote D'Ivoire", "Ivory Coast", "Ivorian"],
    "Czech Republic": ["Czechia", "Czech"],
    "Russia": ["Russian Federation", "Russian"],
    "South Korea": ["Republic of Korea", "Korea, Republic of", "South Korean"],
    "North Korea": ["DPRK", "Democratic People's Republic of Korea", "North Korean"],
    "Syria": ["Syrian Arab Republic", "Syrian"],
    "Iran": ["Islamic Republic of Iran", "Iranian"],
    "Vietnam": ["Viet Nam", "Vietnamese"],
    "Moldova": ["Republic of Moldova", "Moldovan"],
    "Tanzania": ["United Republic of Tanzania", "Tanzanian"],
    "Bolivia": ["Bolivia, Plurinational State of", "Bolivian"],
    "Venezuela": ["Venezuela, Bolivarian Republic of", "Venezuelan"],
    "Laos": ["Lao People's Democratic Republic", "Lao", "Laotian"],
    "Brunei": ["Brunei Darussalam"],
    "Macao": ["Macau", "Macao Special Administrative Region of China"],
    "Hong Kong": ["Hong Kong Special Administrative Region of China"],
    "Palestine": ["State of Palestine", "Palestinian"],
    "Myanmar": ["Burma", "Burmese"],
    "Eswatini": ["Swaziland", "Swazi"],
    "North Macedonia": ["Macedonia", "Macedonian"],
    "Cabo Verde": ["Cape Verde", "Cape Verdean"],
    "Netherlands": ["Holland", "Dutch", "The Netherlands"],
    "Nigeria": ["Nigerian"],
    "Spain": ["Spanish"],
    "Germany": ["German"],
    "France": ["French"],
    "Bangladesh": ["Bangladeshi"],
    "Italy": ["Italian"],
    "Ireland": ["Irish"],
    "Poland": ["Polish"],
    "India": ["Indian"],
    "Canada": ["Canadian"],
    "Australia": ["Australian"],
    "Kenya": ["Kenyan"],
    "South Africa": ["South African"],
    "Sweden": ["Swedish"],
    "Singapore": ["Singaporean"],
    "Austria": ["Austrian"],
    "Pakistan": ["Pakistani"],
    "Portugal": ["Portuguese"],
    "Serbia": ["Serbian"],
    "Denmark": ["Danish"],
    "Ghana": ["Ghanaian"],
    "Norway": ["Norwegian"],
    "Brazil": ["Brazilian"],
    "Indonesia": ["Indonesian"],
    "Mexico": ["Mexican"],
    "China": ["Chinese"],
    "Japan": ["Japanese"],
    "Turkey": ["Turkish", "Turkiye", "Türkiye"],
    "Ukraine": ["Ukrainian"],
    "Greece": ["Greek"],
    "Belgium": ["Belgian"],
    "Switzerland": ["Swiss"],
    "Egypt": ["Egyptian"],
    "Saudi Arabia": ["Saudi"],
    "Argentina": ["Argentine", "Argentinian"],
    "Colombia": ["Colombian"],
    "Chile": ["Chilean"],
    "Peru": ["Peruvian"],
    "Philippines": ["Filipino", "Philippine"],
    "Malaysia": ["Malaysian"],
    "Thailand": ["Thai"],
    "Romania": ["Romanian"],
    "Hungary": ["Hungarian"],
    "Finland": ["Finnish"],
    "Croatia": ["Croatian"],
    "Slovenia": ["Slovenian", "Slovene"],
    "Slovakia": ["Slovak"],
    "Bulgaria": ["Bulgarian"],
    "Haiti": ["Haitian"],
    "Jamaica": ["Jamaican"],
    "Panama": ["Panamanian"],
    "Morocco": ["Moroccan"],
    "Algeria": ["Algerian"],
    "Lebanon": ["Lebanese"],
    "Iraq": ["Iraqi"],
    "Israel": ["Israeli"],
    "Jordan": ["Jordanian"],
    "Qatar": ["Qatari"],
    "Kuwait": ["Kuwaiti"],
    "New Zealand": ["New Zealander"],
    "Ecuador": ["Ecuadorian"],
    "Ethiopia": ["Ethiopian"],
    "Nepal": ["Nepali", "Nepalese"],
    "Sri Lanka": ["Sri Lankan"],
    "Afghanistan": ["Afghan"],
    "Albania": ["Albanian"],
    "Armenia": ["Armenian"],
    "Belarus": ["Belarusian"],
    "Cuba": ["Cuban"],
    "Cyprus": ["Cypriot"],
    "Estonia": ["Estonian"],
    "Latvia": ["Latvian"],
    "Lithuania": ["Lithuanian"],
    "Luxembourg": ["Luxembourgish"],
    "Malta": ["Maltese"],
    "Montenegro": ["Montenegrin"],
    "Nicaragua": ["Nicaraguan"],
    "Paraguay": ["Paraguayan"],
    "Uruguay": ["Uruguayan"],
    "Yemen": ["Yemeni"],
    "Zambia": ["Zambian"],
    "Zimbabwe": ["Zimbabwean"],
    "Tunisia": ["Tunisian"],
    "Libya": ["Libyan"],
    "Sudan": ["Sudanese"],
    "Somalia": ["Somali"],
    "Senegal": ["Senegalese"],
    "Cameroon": ["Cameroonian"],
    "Congo": ["Congolese"],
    "Democratic Republic of the Congo": ["Congolese"],
    "Angola": ["Angolan"],
    "Mozambique": ["Mozambican"],
    "Namibia": ["Namibian"],
    "Botswana": ["Motswana"],
    "Rwanda": ["Rwandan"],
    "Uganda": ["Ugandan"],
    "Kazakhstan": ["Kazakh"],
    "Uzbekistan": ["Uzbek"],
    "Azerbaijan": ["Azerbaijani"],
    "Georgia": ["Georgian"],
    "Iceland": ["Icelandic"],
    "Norway": ["Norwegian"],
    "Taiwan": ["Taiwanese"],
    "Cambodia": ["Cambodian"],
    "Mongolia": ["Mongolian"],
    "Bosnia and Herzegovina": ["Bosnian"],
    "Dominican Republic": ["Dominican"],
    "Trinidad and Tobago": ["Trinidadian"],
    "Papua New Guinea": ["Papua New Guinean"],
    "Equatorial Guinea": ["Equatorial Guinean"],
    "Guinea": ["Guinean"],
    "Guinea-Bissau": ["Bissau-Guinean"],
    "Sierra Leone": ["Sierra Leonean"],
    "Liberia": ["Liberian"],
    "Madagascar": ["Malagasy"],
    "Mauritius": ["Mauritian"],
    "Maldives": ["Maldivian"],
    "Bahrain": ["Bahraini"],
    "Oman": ["Omani"],
    "Kosovo": ["Kosovar"],
}

NOT_COUNTRIES = {"international", "europe", "global", "worldwide", "unknown", "none", "n a", "na"}


def _norm(text: str) -> str:
    text = strip_accents(text or "").lower().replace("'", "").replace("’", "").replace(".", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = " ".join(text.split())
    if text.startswith("the "):
        text = text[4:]
    return text


def _build():
    lookup: dict[str, str] = {}
    phrases: list[tuple[str, str]] = []

    def add(alias: str, canonical: str):
        key = _norm(alias)
        if not key or key in NOT_COUNTRIES:
            return
        lookup.setdefault(key, canonical)
        phrases.append((key, canonical))

    for code, name in ISO2.items():
        add(code, name)
        add(name, name)
    for name, aliases in ALIASES.items():
        add(name, name)
        for alias in aliases:
            add(alias, name)
    # Longest phrase first so "equatorial guinea" wins over "guinea".
    phrases.sort(key=lambda item: len(item[0]), reverse=True)
    # Drop duplicate phrases. Two-letter codes stay in LOOKUP for structured
    # fields (residence "US") but must not be searched inside sentences,
    # where "at", "in" and "by" are ordinary words.
    seen = set()
    unique = []
    for phrase, canonical in phrases:
        if phrase in seen or len(phrase) <= 2:
            continue
        seen.add(phrase)
        unique.append((phrase, canonical))
    return lookup, unique


LOOKUP, PHRASES = _build()


def canon(value) -> str | None:
    if value is None:
        return None
    key = _norm(str(value))
    if not key or key in NOT_COUNTRIES:
        return None
    return LOOKUP.get(key)


def from_field(value) -> set[str]:
    """One structured country field: a code, a name, or 'City, Region, Country [tag]'."""
    if value is None:
        return set()
    raw = re.sub(r"\[[^\]]*\]", " ", str(value)).strip()
    if not raw:
        return set()
    exact = canon(raw)
    if exact:
        return {exact}
    if "," in raw:
        exact = canon(raw.split(",")[-1])
        if exact:
            return {exact}
    return set()


def from_fields(values) -> set[str]:
    found: set[str] = set()
    if not values:
        return found
    if isinstance(values, str):
        values = [values]
    for value in values:
        found |= from_field(value)
    return found


def mentioned(text: str) -> set[str]:
    """Country names and nationalities written inside a sentence."""
    if not text:
        return set()
    padded = f" {_norm(text)} "
    found: set[str] = set()
    occupied = [False] * len(padded)
    for phrase, canonical in PHRASES:
        needle = f" {phrase} "
        start = 0
        while True:
            index = padded.find(needle, start)
            if index < 0:
                break
            end = index + len(needle)
            if not any(occupied[index:end]):
                found.add(canonical)
                for cursor in range(index, end):
                    occupied[cursor] = True
            start = index + 1
    return found


def joined(countries: set[str]) -> str:
    return ", ".join(sorted(countries))

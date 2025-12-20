import re
import pandas as pd

# US states and abbreviations
us_states = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY"
}

# Reverse mapping: abbreviation → full name
abbr_to_state = {v: k.title() for k, v in us_states.items()}

state_names = list(us_states.keys())
state_abbrs = list(us_states.values())

zip_regex = re.compile(r"\b\d{5}(?:-\d{4})?\b")

STATES = {
    "alabama","alaska","arizona","arkansas","california","colorado",
    "connecticut","delaware","florida","georgia","hawaii","idaho",
    "illinois","indiana","iowa","kansas","kentucky","louisiana",
    "maine","maryland","massachusetts","michigan","minnesota",
    "mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania",
    "rhode island","south carolina","south dakota","tennessee",
    "texas","utah","vermont","virginia","washington","west virginia",
    "wisconsin","wyoming",
    # abbreviations
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia","ks","ky","la","me",
    "md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj","nm","ny","nc","nd","oh","ok","or","pa",
    "ri","sc","sd","tn","tx","ut","vt","va","wa","wv","wi","wy"
}

COUNTRIES = {"united states", "usa", "united states of america", "america"}
def is_state_token_to_skip(item):
    """
    Return True only if the part should be considered *a state* (and thus skipped).
    We consider it a state only when:
      - the whole part equals a state full name (e.g. "north carolina"), OR
      - the whole part is a single token that is a state abbreviation or full name (e.g. "md" or "maryland").
    """
    if not item:
        return False
    words = item.split()
    # full match (supports multi-word state names)
    if item in STATES:
        return True
    # single token that matches an abbreviation or single-word state
    if len(words) == 1 and words[0] in STATES:
        return True
    return False

def extract_city_region(address):
    if not isinstance(address, str):
        return None

    parts = [p.strip() for p in address.split(",") if p.strip()]
    lowered = [p.lower() for p in parts]

    # --- 1. Find ZIP ---
    zip_pos = None
    for i, p in enumerate(parts):
        if zip_regex.search(p):
            zip_pos = i
            break

    # --- 2. With ZIP: find nearest valid city before it ---
    if zip_pos is not None:
        for i in range(zip_pos - 1, -1, -1):
            item = lowered[i]

            # Extract first token (ex: "md" from "md 20855")
            first_word = item.split()[0]

            if "county" in item:
                continue
            if  item in STATES:
                continue

            if item in COUNTRIES:
                continue
            if is_state_token_to_skip(item):
                continue
            return parts[i]

    # --- 3. Fallback: last meaningful part ---
    for i in range(len(parts) - 1, -1, -1):
        item = lowered[i]
        first_word = item.split()[0]

        if "county" in item:
            continue
        if item in STATES or first_word in STATES:
            continue

        if item in COUNTRIES:
            continue
        return parts[i]

    return None

def extract_state(area):
    if not isinstance(area, str) or area.strip().lower() in ["unknown", "read more", "nan", "n/a"]:
        return None, None

    parts = [p.strip() for p in area.split(",") if p.strip()]

    state = None

    # -------- DETECT STATE --------
    for p in reversed(parts):
        p_low = p.lower()

        # Case 1: Full state name
        if p_low in state_names:
            state = p_low.title()
            break

        # Case 2: Abbreviation at start ("FL 33837")
        match = re.match(r"^([A-Z]{2})\b", p.strip())
        if match:
            abbr = match.group(1)
            if abbr in abbr_to_state:
                state = abbr_to_state[abbr]  # convert to full state name
                break

        # Case 3: ZIP code present
        if re.search(r"\b\d{5}", p):
            # Check previous token for state name/abbr
            idx = parts.index(p)
            if idx > 0:
                prev = parts[idx - 1].strip()
                prev_low = prev.lower()
                if prev_low in state_names:
                    state = prev_low.title()
                    break
                if prev.upper() in abbr_to_state:
                    state = abbr_to_state[prev.upper()]
                    break

    return state

def pre_processing():
    #loading the unprocessed data
    df=pd.read_csv("tripadvisor_attractions_data.csv")

    #filtering
    df = df[df["country"] == "United States"]

    #removing where there is no address (area)
    df=df.dropna(subset=['area'])

    #extracting state from area
    df["state"] = df["area"].apply(extract_state)

    #droping the rows where the state isnt extracted
    df=df.dropna(subset=["state"])

    #extracting city from area
    df["city_region"] = df["area"].apply(extract_city_region)
    
    #extracting required cols
    df=df[['name', 'rating', 'review_count', 'image_url', 'page_link',
       'description', 'duration', 'area',
       'state','city_region']]
    print("data processed")
    #saving the processed df to csv
    return df.to_csv("processed_data.csv",index=False)

pre_processing()

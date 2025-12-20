# app.py
import os
import re
import json
import datetime
from io import BytesIO
from urllib.parse import urlparse

from flask import Flask, render_template, request, send_file, jsonify, abort
import mysql.connector
import pdfkit
import requests
from dotenv import load_dotenv

# Optional: Google GenAI client (if not available, change to your client)
import google.generativeai as genai

load_dotenv()

# ------------- CONFIG -------------
GENAI_KEY = os.getenv("GENAI_KEY", "")  # set in .env
if GENAI_KEY:
    genai.configure(api_key=GENAI_KEY)

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASS = os.getenv("DB_PASS", "Root")
DB_NAME = os.getenv("DB_NAME", "itinerary")

# Path to wkhtmltopdf binary (update if different)
WKHTMLTOPDF_PATH = os.getenv(
    "WKHTMLTOPDF_PATH", r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"
)
PDFKIT_CONFIG = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)

# Header image (single image used at top)
HEADER_IMAGE_LOCAL_PATH = os.getenv("HEADER_IMAGE_LOCAL_PATH", r"D:\projects\iti\static\header.png")
HEADER_IMAGE_FILE_URL = ""
if os.path.exists(HEADER_IMAGE_LOCAL_PATH):
    HEADER_IMAGE_FILE_URL = "file:///" + os.path.abspath(HEADER_IMAGE_LOCAL_PATH).replace("\\", "/")

app = Flask(__name__, template_folder="templates", static_folder="static")

# ------------- DB helpers -------------
def get_db_connection():
    return mysql.connector.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME
    )

def get_states():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT state FROM trips WHERE state IS NOT NULL ORDER BY state")
    states = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return states

def get_regions(states):
    if not states:
        return []
    conn = get_db_connection()
    cur = conn.cursor()
    fmt = ",".join(["%s"] * len(states))
    query = f"SELECT DISTINCT city_region FROM trips WHERE state IN ({fmt}) ORDER BY city_region"
    cur.execute(query, states)
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows

def get_filtered_data(states, regions, limit=200):
    if not states or not regions:
        return []
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    sfmt = ",".join(["%s"] * len(states))
    rfmt = ",".join(["%s"] * len(regions))
    query = f"""
        SELECT place, place_desc, city_region, state, rating, duration, area, image_url
        FROM trips
        WHERE state IN ({sfmt}) AND city_region IN ({rfmt})
        ORDER BY rating DESC
        LIMIT %s
    """
    params = states + regions + [limit]
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

# ------------- Utilities -------------
def extract_json_block(raw_text):
    """
    Extract the first balanced JSON object from raw text (handles fences and extra prose).
    """
    if not raw_text:
        raise ValueError("Empty model output")

    text = raw_text.strip()
    # remove markdown fences
    text = re.sub(r"^```json", "", text, flags=re.I).strip()
    text = re.sub(r"^```", "", text, flags=re.I).strip()
    text = re.sub(r"```$", "", text, flags=re.I).strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON start found")

    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i+1]
    raise ValueError("Could not extract balanced JSON")

def parse_extra_sections(itinerary_text):
    if not itinerary_text:
        return {
            "famous_shopping": "",
            "what_to_pack": "",
            "safety_rules": "",
            "extra_travel_tips": "",
            "estimated_total_budget": "",
            "closing_note": ""
        }
    patterns = {
        "famous_shopping": r"Famous shopping(?: recommendations)?:\s*(.*?)(?=(What to pack|Safety rules|$))",
        "what_to_pack": r"What to pack[:\-]?\s*(.*?)(?=(Safety rules|Extra travel tips|$))",
        "safety_rules": r"Safety rules[:\-]?\s*(.*?)(?=(Extra travel tips|Estimated total budget|$))",
        "extra_travel_tips": r"Extra travel tips[:\-]?\s*(.*?)(?=(Estimated total budget|Closing note|$))",
        "estimated_total_budget": r"Estimated total budget[:\-]?\s*(.*?)(?=(Closing note|$))",
        "closing_note": r"Closing note[:\-]?\s*(.*)$"
    }
    out = {}
    for key, pat in patterns.items():
        m = re.search(pat, itinerary_text, flags=re.I | re.S)
        out[key] = m.group(1).strip() if m else ""
    return out

def ensure_local_images(places):
    """
    Download image_url for each place (if valid) into /static/day_images/
    and attach 'local_image' field with a file:/// path usable by wkhtmltopdf.
    """
    images_dir = os.path.join(app.static_folder, "day_images")
    os.makedirs(images_dir, exist_ok=True)

    for p in places:
        url = p.get("image_url")
        if not url:
            p["local_image"] = None
            continue

        # extract filename
        filename = os.path.basename(urlparse(url).path)
        if not filename:
            filename = f"{p.get('place','img')}.jpg".replace(" ", "_")

        local_path = os.path.join(images_dir, filename)

        # download only if file does not exist
        if not os.path.exists(local_path):
            try:
                resp = requests.get(url, timeout=6)
                if resp.status_code == 200:
                    with open(local_path, "wb") as f:
                        f.write(resp.content)
            except Exception as e:
                print("Image download failed for:", url, "reason:", str(e))
                p["local_image"] = None
                continue

        # convert to file:/// path for wkhtmltopdf
        p["local_image"] = "file:///" + local_path.replace("\\", "/")

    return places


def normalize_daywise_schema(days_list, days_required, places_dataset):
    """
    Accepts various shapes from model and convert into the template's expected:
    each entry must have keys: day, title, morning, afternoon, evening, transport_note_if_long, day_tips
    morning, afternoon, evening are dicts with specific subkeys.
    """
    normalized = []
    for i, d in enumerate(days_list):
        # defensive defaults
        day_num = d.get("day") if isinstance(d, dict) and d.get("day") else (i+1)
        title = d.get("title") or d.get("day_title") or f"Day {day_num}"

        # If model already provided morning/afternoon/evening, keep but ensure subkeys
        if isinstance(d, dict) and "morning" in d and "afternoon" in d and "evening" in d:
            morning = d.get("morning") or {}
            afternoon = d.get("afternoon") or {}
            evening = d.get("evening") or {}
        else:
            # try older schema breakfast/lunch/dinner or simple fields
            breakfast = (d.get("breakfast") if isinstance(d.get("breakfast"), dict) else {"food": d.get("breakfast", "")}) if isinstance(d, dict) else {"food": ""}
            lunch = (d.get("lunch") if isinstance(d.get("lunch"), dict) else {"food": d.get("lunch", "")}) if isinstance(d, dict) else {"food": ""}
            dinner = (d.get("dinner") if isinstance(d.get("dinner"), dict) else {"food": d.get("dinner", "")}) if isinstance(d, dict) else {"food": ""}

            morning = {
                "early_place": d.get("early_place") or breakfast.get("early_place") or "",
                "breakfast": breakfast.get("food") or "",
                "place_to_visit": breakfast.get("place") or breakfast.get("place_to_visit") or d.get("place_to_visit") or "",
                "duration": breakfast.get("duration") or "",
                "transport_to_next": breakfast.get("transport_to_next") or ""
            }
            afternoon = {
                "lunch": lunch.get("food") or "",
                "place_to_visit": lunch.get("place") or lunch.get("place_to_visit") or "",
                "duration": lunch.get("duration") or "",
                "transport_to_next": lunch.get("transport_to_next") or ""
            }
            evening = {
                "dinner": dinner.get("food") or "",
                "place_to_visit": dinner.get("place") or dinner.get("place_to_visit") or "",
                "duration": dinner.get("duration") or "",
                "transport_to_next": dinner.get("transport_to_next") or ""
            }

        # ensure subkeys exist
        morning.setdefault("early_place", "")
        morning.setdefault("breakfast", "")
        morning.setdefault("place_to_visit", "")
        morning.setdefault("duration", "")
        morning.setdefault("transport_to_next", "")
        afternoon.setdefault("lunch", "")
        afternoon.setdefault("place_to_visit", "")
        afternoon.setdefault("duration", "")
        afternoon.setdefault("transport_to_next", "")
        evening.setdefault("dinner", "")
        evening.setdefault("place_to_visit", "")
        evening.setdefault("duration", "")
        evening.setdefault("transport_to_next", "")

        # -------------------------
        #  IMAGE SELECTION LOGIC HERE 
        # -------------------------
        morning_place = morning.get("place_to_visit", "").replace("*", "").strip().lower()
        day_image = None

        for p in places_dataset:
            db_place = (p.get("place") or "").lower().strip()
            if morning_place and morning_place in db_place:
                day_image = p.get("local_image")
                break

        # fallback: pick first available image in dataset
        if not day_image:
            for p in places_dataset:
                if p.get("local_image"):
                    day_image = p.get("local_image")
                    break

        transport_note = d.get("transport_note_if_long") if isinstance(d, dict) else None
        day_tips = d.get("day_tips") or d.get("tips") or ""

        normalized.append({
            "day": day_num,
            "title": title,
            "morning": morning,
            "afternoon": afternoon,
            "evening": evening,
            "transport_note_if_long": transport_note,
            "day_tips": day_tips,
            "day_image": day_image
        })

    # fill missing days up to days_required
    while len(normalized) < days_required:
        idx = len(normalized)
        normalized.append({
            "day": idx+1,
            "title": f"Day {idx+1}",
            "morning": {"early_place": "", "breakfast": "Breakfast suggestion", "place_to_visit": "**Local**", "duration": "1 hour", "transport_to_next": "Short walk, 0.2 mi, walk, $0"},
            "afternoon": {"lunch": "Lunch suggestion", "place_to_visit": "**Local**", "duration": "1 hour", "transport_to_next": "Short walk, 0.5 mi, walk, $0"},
            "evening": {"dinner": "Dinner suggestion", "place_to_visit": "**Local**", "duration": "1-2 hours", "transport_to_next": "N/A"},
            "transport_note_if_long": None,
            "day_tips": "",
            "day_image": None
        })

    # ensure exactly days_required length
    return normalized[:days_required]

# ------------- Prompt Template (escaped braces) -------------
PROMPT_TEMPLATE = """
You are an expert travel planner. Use ONLY the dataset below to create a {days}-day itinerary.
DATASET:
{dataset_text}

User inputs:
States: {States}
Regions: {Regions}
Days: {days}
Season: {season}
Trip type: {trip_type}
Themes: {Themes}
Budget: {budget}
Target places: {target_places}

STRICT RULES:
1. Use ONLY the dataset (and only real verifiable nearby places if dataset is too small). Mark added places as "(added)".
2. Output MUST be valid JSON only, matching the schema below exactly (no extra commentary).
3. Each day must include morning (early_place(optional), breakfast, place_to_visit, duration, transport_to_next),
   afternoon (lunch, place_to_visit, duration, transport_to_next), evening (dinner, place_to_visit, duration, transport_to_next).
4. Transport MUST be specified for each segment. If long travel (>80 miles or interstate) include transport_note_if_long.
5. Food items MUST include approximate price.
6. Keep each text short (10-40 words) and PDF-friendly.

Return JSON object EXACTLY with this structure:

{{
  "summary": {{
    "total_places": 0,
    "places": [],
    "theme_coverage": {{
      "sightseeing": "0%",
      "museums": "0%",
      "nature": "0%"
    }},
    "excitement_note": ""
  }},
  "days": [
    {{
      "day": 1,
      "title": "Day 1 — Caption",
      "morning": {{
        "early_place": "optional short text",
        "breakfast": "Food + price",
        "place_to_visit": "Place Name",
        "duration": "time",
        "transport_to_next": "From X to Y, mode, miles, approx cost (one sentence)"
      }},
      "afternoon": {{
        "lunch": "Food + price",
        "place_to_visit": "Place Name",
        "duration": "time",
        "transport_to_next": "From X to Y, mode, miles, approx cost (one sentence)"
      }},
      "evening": {{
        "dinner": "Food + price",
        "place_to_visit": "Place Name",
        "duration": "time",
        "transport_to_next": "From X to Y, mode, miles, approx cost (one sentence)"
      }},
      "transport_note_if_long": null,
      "day_tips": "short tip"
    }}
  ],
  "extras": {{
    "famous_shopping": "",
    "what_to_pack": "",
    "safety_rules": "",
    "extra_travel_tips": "",
    "estimated_total_budget": "",
    "closing_note": ""
  }}
}}
"""

# ------------- Routes -------------
@app.route("/", methods=["GET"])
def index():
    states = get_states()
    return render_template("form.html", states=states)

@app.route("/get_regions", methods=["POST"])
def regions_route():
    data = request.json or {}
    states = data.get("states", [])
    if not states:
        return jsonify({"regions": []})
    return jsonify({"regions": get_regions(states)})

@app.route("/generate_pdf", methods=["POST"])
def generate_pdf():
    # read form data
    states = request.form.getlist("states")
    regions = request.form.getlist("regions")
    if not states or not regions:
        return abort(400, "Select at least one state and region")

    days = int(request.form.get("days", 3))
    season = request.form.get("season", "summer")
    trip_type = request.form.get("trip_type", "solo")
    themes = request.form.getlist("themes") or ["Any"]
    budget = request.form.get("budget", "mid")
    target_places = int(request.form.get("target_places", 5))

    # fetch DB rows (dataset)
    places = get_filtered_data(states, regions, limit=200)

    if not places:
        return abort(400, "No attractions found for selected filters")

    # build dataset_text for prompt (small sample)
    sample = places[:20]
    dataset_lines = []
    for p in sample:
        name = p.get("place") or ""
        region = p.get("city_region") or ""
        rating = p.get("rating") or ""
        desc = (p.get("place_desc") or "").replace("\n", " ").strip()
        dataset_lines.append(f"{name} | {region} | {rating} | {desc}")
    dataset_text = "\n".join(dataset_lines)

    States = ", ".join(states)
    Regions = ", ".join(regions)
    Themes = ", ".join(themes)

    places = get_filtered_data(states, regions, limit=200)
    places = ensure_local_images(places)
    app.config["PLACES_DATASET"] = places

    prompt = PROMPT_TEMPLATE.format(
        days=days,
        dataset_text=dataset_text,
        States=States,
        Regions=Regions,
        season=season,
        trip_type=trip_type,
        Themes=Themes,
        budget=budget,
        target_places=target_places
    )

    # Call Gemini (if configured). If GENAI_KEY is empty, return a helpful error.
    if not GENAI_KEY:
        return abort(500, "GENAI_KEY not set in environment (.env)")

    model = genai.GenerativeModel("gemini-2.5-flash")
    response = model.generate_content(prompt)

    raw_text = response.text
    # debug print (server log)
    print("RAW GEMINI OUTPUT:", raw_text[:800])

    # parse JSON block
    try:
        clean_json = extract_json_block(raw_text)
        data = json.loads(clean_json)
    except Exception as e:
        # fallback: error message to server log and abort
        print("Failed to parse JSON from model:", str(e))
        return abort(500, "Model did not return valid JSON. Check server logs.")

    # validate and normalize
    summary = data.get("summary", {})
    day_entries = data.get("days", [])
    extras = data.get("extras", {})

    daywise = normalize_daywise_schema(day_entries, days,app.config["PLACES_DATASET"])

    # detect long travel (transport_note_if_long) automatically if user provided distances or if a day has explicit transport)
    # (assumes model filled transport_note_if_long when >80 miles as required by prompt)
    # leave as provided

    # render HTML
    html = render_template(
        "pdf_template.html",
        destination=", ".join(regions),
        duration=f"{days} days",
        date="",
        departure="",
        days=days,
        daywise=daywise,
        summary=summary,
        extras=extras,
        header_image_url=HEADER_IMAGE_FILE_URL or "",
        generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    )

    # convert to PDF
    options = {
        "page-size": "A4",
        "margin-top": "12mm",
        "margin-bottom": "12mm",
        "margin-left": "12mm",
        "margin-right": "12mm",
        "encoding": "UTF-8",
        "enable-local-file-access": None
    }

    pdf_bytes = pdfkit.from_string(html, False, options=options, configuration=PDFKIT_CONFIG)

    return send_file(BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True, download_name="itinerary.pdf")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

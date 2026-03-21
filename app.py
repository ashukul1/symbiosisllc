import os, requests, uuid, json
from flask_cors import CORS
from datetime import datetime
from flask import Flask, request, redirect, url_for, session
from jinja2 import Environment
import pg8000.native
import urllib.parse

# ═══════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_db():
    url = urllib.parse.urlparse(DATABASE_URL)
    conn = pg8000.native.Connection(
        host=url.hostname,
        port=url.port or 5432,
        database=url.path.lstrip("/"),
        user=url.username,
        password=url.password,
        ssl_context=True
    )
    return conn

def init_db():
    try:
        conn = get_db()
        conn.run("""
            CREATE TABLE IF NOT EXISTS patients (
                id                         TEXT PRIMARY KEY,
                created_at                 TEXT NOT NULL,
                name                       TEXT,
                age                        INTEGER,
                gender                     TEXT,
                bmi                        REAL,
                vegetarian                 BOOLEAN,
                family_cvd_before_60       BOOLEAN,
                family_diabetes            BOOLEAN,
                confirmed_fasting          BOOLEAN,
                illness_or_vaccination_14d BOOLEAN,
                biotin_within_72hrs        BOOLEAN
            )
        """)
        conn.close()
    except Exception as e:
        print(f"DB init patients error: {e}")
    try:
        conn = get_db()
        conn.run("""
            CREATE TABLE IF NOT EXISTS reports (
                id            TEXT PRIMARY KEY,
                created_at    TEXT NOT NULL,
                patient_name  TEXT,
                age           INTEGER,
                gender        TEXT,
                bmi           REAL,
                vegetarian    BOOLEAN,
                sa_risk_score INTEGER,
                risk_category TEXT,
                patterns      TEXT,
                lab_values    TEXT,
                report_text   TEXT,
                doctor_name   TEXT,
                approved      BOOLEAN DEFAULT FALSE
            )
        """)
        conn.close()
    except Exception as e:
        print(f"DB init reports error: {e}")

init_db()

app = Flask(__name__)
CORS(app, resources={r"/waitlist": {"origins": "*"}})
app.secret_key = os.environ.get("SECRET_KEY", "symbiosis-2025-sa")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── PER-SESSION STORE ─────────────────────────────────────────────
_store = {}

def get_sid():
    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    return session["sid"]

def sget(key, default=None):
    return _store.get(get_sid(), {}).get(key, default)

def sset(key, value):
    sid = get_sid()
    if sid not in _store:
        _store[sid] = {}
    _store[sid][key] = value

def spop(key):
    sid = get_sid()
    if sid in _store:
        _store[sid].pop(key, None)

def sclear():
    sid = get_sid()
    _store.pop(sid, None)


# ═══════════════════════════════════════════════════════════════════
# MARKERS — all thresholds from Symbiosis SA Biomarker Panel
# Sources: MASALA, INTERHEART, ACC SA Review (JACC Adv 2023),
#          NLA SA Statement, KDIGO, ADA, Endocrine Society, WHO,
#          LP-IR SA Validation (Fosam et al. J Endocrine Soc 2022)
# ═══════════════════════════════════════════════════════════════════
MARKERS = {
    # ── Cardiovascular (10 markers) ───────────────────────────────
    "lpa": {
        "name": "Lipoprotein(a)", "unit": "nmol/L", "domain": "cardiovascular", "weight": 3,
        "direction": "higher_worse",
        "optimal": {"max": 30}, "borderline": {"min": 30, "max": 75}, "high": {"min": 75},
        "citation": "Patel et al. JAHA 2025 + INTERHEART 2019",
        "sa_note": "SA have smaller apo(a) isoforms making even moderate Lp(a) more atherogenic. EAS rule-out threshold of 30 used."
    },
    "apob": {
        "name": "ApoB", "unit": "mg/dL", "domain": "cardiovascular", "weight": 3,
        "direction": "higher_worse",
        "optimal": {"max": 80}, "borderline": {"min": 80, "max": 100}, "high": {"min": 100},
        "citation": "INTERHEART JAMA 2007 + ACC/AHA Cholesterol 2018",
        "sa_note": "SA have more small dense LDL — ApoB counts every atherogenic particle regardless of size."
    },
    "apoa1": {
        "name": "ApoA1", "unit": "mg/dL", "domain": "cardiovascular", "weight": 2,
        "direction": "lower_worse",
        "optimal": {"min": 120}, "borderline": {"min": 100, "max": 120}, "high": {"max": 100},
        "citation": "INTERHEART JAMA 2007 + JACC Advances 2023",
        "sa_note": "ApoB/ApoA-1 ratio is the single strongest MI predictor in the INTERHEART SA cohort."
    },
    "ldl": {
        "name": "LDL (direct)", "unit": "mg/dL", "domain": "cardiovascular", "weight": 2,
        "direction": "higher_worse",
        "optimal": {"max": 100}, "borderline": {"min": 100, "max": 129}, "high": {"min": 130},
        "citation": "ACC/AHA 2018 + JACC Advances 2023",
        "sa_note": "Direct measurement only — Friedewald formula underestimates LDL in SA due to elevated TG."
    },
    "hdl": {
        "name": "HDL", "unit": "mg/dL", "domain": "cardiovascular", "weight": 2,
        "direction": "lower_worse",
        "optimal": {"min": 50}, "borderline": {"min": 40, "max": 50}, "high": {"max": 40},
        "citation": "MASALA Study Am J Cardiol 2023 + ACC/AHA 2018",
        "sa_note": "SA HDL is qualitatively inferior regardless of concentration — reduced HDL2b subfraction. Female threshold applied universally."
    },
    "triglycerides": {
        "name": "Triglycerides", "unit": "mg/dL", "domain": "cardiovascular", "weight": 2,
        "direction": "higher_worse",
        "optimal": {"max": 100}, "borderline": {"min": 100, "max": 150}, "high": {"min": 150},
        "citation": "MASALA Atherosclerosis 2023 + JACC Advances 2023",
        "sa_note": "SA optimal ceiling is 100 mg/dL (not 150) due to genetic predisposition to postprandial hypertriglyceridaemia."
    },
    "totalCholesterol": {
        "name": "Total Cholesterol", "unit": "mg/dL", "domain": "cardiovascular", "weight": 1,
        "direction": "higher_worse",
        "optimal": {"max": 200}, "borderline": {"min": 200, "max": 239}, "high": {"min": 240},
        "citation": "ACC/AHA 2018 + WHO 2020",
        "sa_note": "Standard Western threshold retained. ApoB is the primary lipid signal for SA."
    },
    "nonHdl": {
        "name": "Non-HDL Cholesterol", "unit": "mg/dL", "domain": "cardiovascular", "weight": 2,
        "direction": "higher_worse", "derived": True,
        "optimal": {"max": 130}, "borderline": {"min": 130, "max": 159}, "high": {"min": 160},
        "citation": "ACC/AHA 2018 + JACC Advances 2023",
        "sa_note": "Single best lipid summary for SA — captures full atherogenic particle load including VLDL remnants."
    },
    "hsCrp": {
        "name": "hs-CRP", "unit": "mg/L", "domain": "cardiovascular", "weight": 2,
        "direction": "higher_worse",
        "optimal": {"max": 1.0}, "borderline": {"min": 1.0, "max": 2.0}, "high": {"min": 2.0},
        "citation": "MASALA REF Atherosclerosis 2023 + Ridker Circulation 2016",
        "sa_note": "SA flag threshold lowered to 2.0 mg/L (not 3.0) per MASALA data showing earlier inflammatory CVD risk in SA."
    },
    "homocysteine": {
        "name": "Homocysteine", "unit": "µmol/L", "domain": "cardiovascular", "weight": 2,
        "direction": "higher_worse",
        "optimal": {"max": 8}, "borderline": {"min": 8, "max": 12}, "high": {"min": 12},
        "citation": "NLA SA CVD Statement ACC.org 2021 + Ganguly & Alam Nutr J 2015",
        "sa_note": "Optimal tightened to <8 µmol/L (vs standard 15). B12-homocysteine-CVD pathway is specifically more lethal in SA — and fully reversible."
    },

    # ── Metabolic / Insulin Resistance (8 markers) ────────────────
    "lpir": {
        "name": "LP-IR Score", "unit": "score 0–100", "domain": "metabolic", "weight": 3,
        "direction": "higher_worse",
        "optimal": {"max": 45}, "borderline": {"min": 45, "max": 60}, "high": {"min": 60},
        "citation": "Fosam et al. J Endocrine Soc 2022 (SA-specific NIH validation) + Shalaurova Met Syndr 2014",
        "sa_note": "Most accurate IR test for SA. Validated specifically in SA at NIH (NHLBI). Detects IR in lean individuals — the SA thin-fat phenotype."
    },
    "fastingInsulin": {
        "name": "Fasting Insulin", "unit": "µIU/mL", "domain": "metabolic", "weight": 3,
        "direction": "higher_worse",
        "optimal": {"max": 8}, "borderline": {"min": 8, "max": 12}, "high": {"min": 12},
        "citation": "NLA SA CVD Statement ACC.org 2021 + MASALA Ann Epidemiol 2015",
        "sa_note": "Optimal <8 µIU/mL (not standard 15). By fasting insulin 15 in a SA patient, IR has likely been present 5–8 years."
    },
    "hba1c": {
        "name": "HbA1c", "unit": "%", "domain": "metabolic", "weight": 3,
        "direction": "higher_worse",
        "optimal": {"max": 5.4}, "borderline": {"min": 5.4, "max": 5.7}, "high": {"min": 5.7},
        "citation": "ADA 2015 Asian-specific + MASALA/NLA SA Statement 2021",
        "sa_note": "Action threshold 5.4% per ADA Asian-specific recommendation — 6–12 month lifestyle window before formal pre-diabetes."
    },
    "fastingGlucose": {
        "name": "Fasting Glucose", "unit": "mg/dL", "domain": "metabolic", "weight": 2,
        "direction": "higher_worse",
        "optimal": {"max": 90}, "borderline": {"min": 90, "max": 100}, "high": {"min": 100},
        "citation": "MASALA Ann Epidemiol 2015 + JACC Advances 2023",
        "sa_note": "90–100 mg/dL is borderline for SA (not normal). High visceral fat drives hepatic glucose output even at normal BMI."
    },
    "homaIr": {
        "name": "HOMA-IR", "unit": "ratio", "domain": "metabolic", "weight": 1,
        "direction": "higher_worse", "derived": True,
        "optimal": {"max": 1.5}, "borderline": {"min": 1.5, "max": 2.5}, "high": {"min": 2.5},
        "citation": "Gayoso-Diz Nutr Metab 2013 + Misra Diab Technol Ther 2012",
        "sa_note": "Optimal <1.5 for SA (not standard 2.5). LP-IR takes diagnostic precedence over HOMA-IR for SA patients."
    },
    "uricAcid": {
        "name": "Uric Acid", "unit": "mg/dL", "domain": "metabolic", "weight": 1,
        "direction": "higher_worse",
        "optimal": {"max": 6.0}, "borderline": {"min": 6.0, "max": 7.0}, "high": {"min": 7.0},
        "citation": "Choi et al. Arthritis Rheum 2009 + JACC Advances 2023",
        "sa_note": "Useful proxy for visceral fat and metabolic dysfunction in SA. Optimal ceiling 6.0 mg/dL detects early SA metabolic pattern."
    },
    "cPeptide": {
        "name": "C-Peptide", "unit": "ng/mL", "domain": "metabolic", "weight": 1,
        "direction": "range",
        "optimal": {"min": 0.5, "max": 2.0}, "borderline": {"min": 2.0, "max": 3.0}, "high": {"min": 3.0},
        "citation": "Leighton J Clin Endocrinol Metab 2017 + Jones & Hattersley Diabet Med 2013",
        "sa_note": "Elevated C-peptide confirms SA beta-cell compensation phenotype — beta cells overproducing insulin to compensate for peripheral resistance."
    },
    "tgHdlRatio": {
        "name": "TG/HDL Ratio", "unit": "ratio", "domain": "metabolic", "weight": 2,
        "direction": "higher_worse", "derived": True,
        "optimal": {"max": 2.0}, "borderline": {"min": 2.0, "max": 3.0}, "high": {"min": 3.0},
        "citation": "MASALA JAHA 2014 + Murguía-Romero J Clin Lab Anal 2013",
        "sa_note": "Validated IR surrogate in SA. TG/HDL >3 signals combined dyslipidaemia-IR phenotype even without LP-IR."
    },

    # ── Liver & Kidney (7 markers) ────────────────────────────────
    "alt": {
        "name": "ALT", "unit": "U/L", "domain": "liverKidney", "weight": 2,
        "direction": "higher_worse",
        "optimal": {"max": 25}, "borderline": {"min": 25, "max": 40}, "high": {"min": 40},
        "citation": "MASALA NAFLD Kanaya 2024 + Chalasani Hepatology 2018",
        "sa_note": "SA optimal ceiling 25 U/L (not 40) — lean NAFLD develops at normal BMI in SA."
    },
    "ast": {
        "name": "AST", "unit": "U/L", "domain": "liverKidney", "weight": 2,
        "direction": "higher_worse",
        "optimal": {"max": 25}, "borderline": {"min": 25, "max": 40}, "high": {"min": 40},
        "citation": "Chalasani Hepatology 2018 + MASALA NAFLD Kanaya 2024",
        "sa_note": "AST:ALT ratio >1 in a SA patient with elevated LP-IR suggests progressing hepatic steatosis."
    },
    "ggt": {
        "name": "GGT", "unit": "U/L", "domain": "liverKidney", "weight": 1,
        "direction": "higher_worse",
        "optimal": {"max": 30}, "borderline": {"min": 30, "max": 50}, "high": {"min": 50},
        "citation": "Lee Diabetologia 2007 + Whitfield Crit Rev Clin Lab Sci 2001",
        "sa_note": "GGT is an early metabolic syndrome marker in SA — moves before ALT."
    },
    "creatinine": {
        "name": "Creatinine", "unit": "mg/dL", "domain": "liverKidney", "weight": 2,
        "direction": "range",
        "optimal": {"min": 0.6, "max": 1.0}, "borderline": {"min": 1.0, "max": 1.2}, "high": {"min": 1.2},
        "citation": "MASALA NAFLD Kanaya 2024 + KDIGO 2012",
        "sa_note": "SA have lower muscle mass — creatinine 1.1 represents worse kidney function in SA than in European patients."
    },
    "egfr": {
        "name": "eGFR", "unit": "mL/min/1.73m²", "domain": "liverKidney", "weight": 3,
        "direction": "lower_worse",
        "optimal": {"min": 90}, "borderline": {"min": 60, "max": 90}, "high": {"max": 60},
        "citation": "KDIGO 2012 + de Boer NEJM 2011",
        "sa_note": "eGFR 60–90 is actionable borderline for SA — eGFR trajectory declines faster in SA with T2D."
    },
    "albumin": {
        "name": "Albumin", "unit": "g/dL", "domain": "liverKidney", "weight": 1,
        "direction": "range",
        "optimal": {"min": 4.0, "max": 5.0}, "borderline": {"min": 3.5, "max": 4.0}, "high": {"max": 3.5},
        "citation": "Kaysen J Am Soc Nephrol 2001 + Soeters Clin Nutr 2019",
        "sa_note": "Low albumin in SA vegetarian context indicates protein insufficiency."
    },
    "urineAcr": {
        "name": "Urine ACR", "unit": "mg/g", "domain": "liverKidney", "weight": 3,
        "direction": "higher_worse",
        "optimal": {"max": 10}, "borderline": {"min": 10, "max": 30}, "high": {"min": 30},
        "citation": "ADA 2024 + KDIGO 2012",
        "sa_note": "ACR 10–30 flagged as borderline (vs standard 30) given 25% T2D prevalence in SA."
    },

    # ── Thyroid (3 markers) ───────────────────────────────────────
    "tsh": {
        "name": "TSH", "unit": "mIU/L", "domain": "thyroid", "weight": 2,
        "direction": "range",
        "optimal": {"min": 1.0, "max": 2.5}, "borderline": {"min": 2.5, "max": 4.0}, "high": {"min": 4.0},
        "citation": "Garber Thyroid 2012 + Jonklaas Thyroid 2014",
        "sa_note": "Narrowed to 1.0–2.5 (not standard 0.4–4.5). TSH 3.8 looks normal but causes metabolic drag in SA."
    },
    "freeT3": {
        "name": "Free T3", "unit": "pg/mL", "domain": "thyroid", "weight": 2,
        "direction": "lower_worse",
        "optimal": {"min": 3.0, "max": 4.2}, "borderline": {"min": 2.3, "max": 3.0}, "high": {"max": 2.3},
        "citation": "Wiersinga Eur Thyroid J 2012 + Bianco & Kim J Clin Invest 2006",
        "sa_note": "Optimal lower bound raised from 2.3 to 3.0 for SA metabolic optimisation."
    },
    "freeT4": {
        "name": "Free T4", "unit": "ng/dL", "domain": "thyroid", "weight": 2,
        "direction": "lower_worse",
        "optimal": {"min": 1.0, "max": 1.6}, "borderline": {"min": 0.8, "max": 1.0}, "high": {"max": 0.8},
        "citation": "Wiersinga Eur Thyroid J 2012 + Garber Thyroid 2012",
        "sa_note": "Cortisol elevation from chronic acculturation stress impairs T4-to-T3 conversion."
    },

    # ── Nutritional (7 markers) ───────────────────────────────────
    "vitaminD": {
        "name": "Vitamin D (25-OH)", "unit": "ng/mL", "domain": "nutritional", "weight": 2,
        "direction": "lower_worse",
        "optimal": {"min": 40}, "borderline": {"min": 30, "max": 40}, "high": {"max": 30},
        "citation": "Holick Endocrine Society 2011 + Darling Osteoporos Int 2013",
        "sa_note": "Optimal raised to ≥40 ng/mL (not standard 30). 80%+ of SA diaspora are deficient."
    },
    "vitaminB12": {
        "name": "Vitamin B12", "unit": "pg/mL", "domain": "nutritional", "weight": 2,
        "direction": "lower_worse",
        "optimal": {"min": 400}, "borderline": {"min": 200, "max": 400}, "high": {"max": 200},
        "citation": "Allen Am J Clin Nutr 2009 + Refsum Am J Clin Nutr 2001",
        "sa_note": "Optimal raised to ≥400 pg/mL (not standard 200). At B12 200–400, homocysteine is elevated and arterial damage accumulates."
    },
    "folate": {
        "name": "Folate (serum)", "unit": "ng/mL", "domain": "nutritional", "weight": 2,
        "direction": "lower_worse",
        "optimal": {"min": 4.0}, "borderline": {"min": 2.0, "max": 4.0}, "high": {"max": 2.0},
        "citation": "WHO 2015 + Refsum Am J Clin Nutr 2001",
        "sa_note": "Optimal >4.0 ng/mL ensures adequate substrate for homocysteine remethylation."
    },
    "ferritin": {
        "name": "Ferritin", "unit": "ng/mL", "domain": "nutritional", "weight": 2,
        "direction": "range",
        "optimal": {"min": 50, "max": 150}, "borderline": {"min": 20, "max": 50}, "high": {"max": 20},
        "citation": "WHO 2011 + Thankachan J Nutr 2008",
        "sa_note": "Tight optimal 50–150 ng/mL — flags both iron deficiency anaemia and NAFLD."
    },
    "iron": {
        "name": "Iron (serum)", "unit": "µg/dL", "domain": "nutritional", "weight": 1,
        "direction": "range",
        "optimal": {"min": 60, "max": 170}, "borderline": {"min": 40, "max": 60}, "high": {"max": 40},
        "citation": "WHO 2011 + Camaschella NEJM 2015",
        "sa_note": "Paired with ferritin for complete iron status."
    },
    "magnesium": {
        "name": "Magnesium", "unit": "mg/dL", "domain": "nutritional", "weight": 1,
        "direction": "range",
        "optimal": {"min": 2.0, "max": 2.5}, "borderline": {"min": 1.7, "max": 2.0}, "high": {"max": 1.7},
        "citation": "Barbagallo Arch Biochem Biophys 2007 + Clarys Nutrients 2014",
        "sa_note": "Phytates in dal and roti bind Mg — SA vegetarians at high structural risk."
    },
    "zinc": {
        "name": "Zinc", "unit": "µg/dL", "domain": "nutritional", "weight": 1,
        "direction": "lower_worse",
        "optimal": {"min": 80}, "borderline": {"min": 60, "max": 80}, "high": {"max": 60},
        "citation": "WHO 2009 + Saunders Nutrients 2013",
        "sa_note": "Phytic acid in lentils, chickpeas, and rotis blocks zinc absorption by up to 60%."
    },

    # ── Blood Count & Inflammation (5 markers) ────────────────────
    "hemoglobin": {
        "name": "Hemoglobin", "unit": "g/dL", "domain": "blood", "weight": 2,
        "direction": "lower_worse",
        "optimal": {"min": 13.0}, "borderline": {"min": 11.0, "max": 13.0}, "high": {"max": 11.0},
        "citation": "WHO 2011 + Thankachan J Nutr 2008",
        "sa_note": "13.0 g/dL applied uniformly given high nutritional anaemia burden in SA."
    },
    "hematocrit": {
        "name": "Hematocrit", "unit": "%", "domain": "blood", "weight": 1,
        "direction": "lower_worse",
        "optimal": {"min": 40}, "borderline": {"min": 36, "max": 40}, "high": {"max": 36},
        "citation": "WHO 2011 + Camaschella NEJM 2015",
        "sa_note": "Paired with haemoglobin. Low hematocrit in SA vegetarian context almost always indicates nutritional deficiency."
    },
    "wbc": {
        "name": "WBC", "unit": "×10³/µL", "domain": "blood", "weight": 1,
        "direction": "range",
        "optimal": {"min": 4.0, "max": 10.5}, "borderline": {"min": 3.0, "max": 4.0}, "high": {"max": 3.0},
        "citation": "Buttarello & Plebani Am J Clin Pathol 2008 + Forget BioMed Res Int 2017",
        "sa_note": "WBC differential provides neutrophil-to-lymphocyte ratio — independent predictor of metabolic syndrome in SA."
    },
    "platelets": {
        "name": "Platelets", "unit": "×10³/µL", "domain": "blood", "weight": 1,
        "direction": "range",
        "optimal": {"min": 150, "max": 400}, "borderline": {"min": 100, "max": 150}, "high": {"max": 100},
        "citation": "Buttarello & Plebani 2008 + Kamath Eur Heart J 2001",
        "sa_note": "Elevated platelet aggregability documented in SA — contributes to higher thrombotic CVD risk."
    },
    "esr": {
        "name": "ESR", "unit": "mm/hr", "domain": "blood", "weight": 2,
        "direction": "higher_worse",
        "optimal": {"max": 20}, "borderline": {"min": 20, "max": 40}, "high": {"min": 40},
        "citation": "Sox & Liang Ann Intern Med 1986 + Saadeh Semin Arthritis Rheum 1998",
        "sa_note": "ESR + hs-CRP together provide more complete inflammatory burden assessment."
    },
}

DOMAINS = {
    "cardiovascular": {"label": "Cardiovascular",       "weight": 0.32},
    "metabolic":      {"label": "Metabolic / IR",        "weight": 0.25},
    "liverKidney":    {"label": "Liver & Kidney",        "weight": 0.12},
    "thyroid":        {"label": "Thyroid",               "weight": 0.08},
    "nutritional":    {"label": "Nutritional",           "weight": 0.13},
    "blood":          {"label": "Blood & Inflammation",  "weight": 0.10},
}

CRITICAL_THRESHOLDS = {
    "egfr":          {"halt": True,  "condition": lambda v: v < 30,   "message": "eGFR critically low (<30) — immediate nephrologist escalation required"},
    "fastingGlucose":{"halt": True,  "condition": lambda v: v >= 200, "message": "Fasting glucose critically elevated (≥200) — urgent diabetes review required"},
    "hba1c":         {"halt": True,  "condition": lambda v: v >= 10,  "message": "HbA1c critically elevated (≥10%) — urgent diabetes review required"},
    "platelets":     {"halt": True,  "condition": lambda v: v < 50,   "message": "Platelets critically low (<50) — immediate haematology review required"},
    "wbc":           {"halt": True,  "condition": lambda v: v < 2.0,  "message": "WBC critically low (<2.0) — immediate haematology review required"},
    "hemoglobin":    {"halt": True,  "condition": lambda v: v < 7,    "message": "Haemoglobin critically low (<7) — immediate clinical review required"},
    "lpa":           {"halt": False, "condition": lambda v: v > 200,  "message": "Lp(a) markedly elevated (>200 nmol/L) — urgent cardiology review recommended"},
}


# ═══════════════════════════════════════════════════════════════════
# PATTERN DETECTION
# ═══════════════════════════════════════════════════════════════════
def _at(mr, key, status):
    r = mr.get(key)
    if not r or r.get("excluded"): return False
    rank = {"optimal": 0, "borderline": 1, "high": 2}
    return rank.get(r.get("status"), 0) >= rank.get(status, 0)

def _count(mr, keys, status):
    return sum(1 for k in keys if _at(mr, k, status))

def detect_ir(mr):
    keys = ["lpir", "fastingInsulin", "hba1c", "fastingGlucose", "homaIr", "tgHdlRatio"]
    f, h = _count(mr, keys, "borderline"), _count(mr, keys, "high")
    if f < 3: return {"triggered": False}
    return {"triggered": True,
            "severity": "high" if (h >= 2 or f >= 4) else "moderate",
            "contributing": [k for k in keys if _at(mr, k, "borderline")],
            "evidence": f"{f} of 6 insulin resistance markers outside SA-optimal range."}

def detect_cvd(mr):
    keys = ["lpa", "apob", "hsCrp", "homocysteine", "ldl", "nonHdl", "triglycerides"]
    f, h = _count(mr, keys, "borderline"), _count(mr, keys, "high")
    if f < 2: return {"triggered": False}
    return {"triggered": True,
            "severity": "high" if (h >= 4 or (_at(mr, "lpa", "high") and h >= 2)) else "moderate",
            "contributing": [k for k in keys if _at(mr, k, "borderline")],
            "evidence": f"{f} of 7 cardiovascular markers outside SA-optimal range."}

def detect_b12(mr):
    if _at(mr, "vitaminB12", "borderline") and _at(mr, "folate", "borderline") and _at(mr, "homocysteine", "borderline"):
        return {"triggered": True, "severity": "high",
                "contributing": ["vitaminB12", "folate", "homocysteine"],
                "evidence": "B12, Folate, and Homocysteine all outside SA-optimal range."}
    return {"triggered": False}

def detect_nafld(mr):
    if _at(mr, "alt", "borderline") and _at(mr, "ast", "borderline") and _at(mr, "lpir", "borderline"):
        return {"triggered": True,
                "severity": "high" if (_at(mr, "alt", "high") or _at(mr, "ast", "high")) else "moderate",
                "contributing": ["alt", "ast", "lpir"],
                "evidence": "ALT, AST, and LP-IR all flagged — lean NAFLD pattern (classic SA phenotype)."}
    return {"triggered": False}

def detect_thyroid(mr):
    if _at(mr, "tsh", "borderline") and _at(mr, "freeT3", "borderline"):
        all3 = _at(mr, "freeT4", "borderline")
        return {"triggered": True,
                "severity": "high" if all3 else "moderate",
                "contributing": ["tsh", "freeT3"] + (["freeT4"] if all3 else []),
                "evidence": "TSH and Free T3 outside SA-optimal range — subclinical hypothyroid pattern."}
    return {"triggered": False}

def detect_anaemia(mr):
    if not _at(mr, "hemoglobin", "borderline"): return {"triggered": False}
    causes, cont = [], ["hemoglobin"]
    if _at(mr, "ferritin", "borderline") or _at(mr, "iron", "borderline"):
        causes.append("iron deficiency"); cont += ["ferritin", "iron"]
    if _at(mr, "vitaminB12", "borderline"):
        causes.append("B12 deficiency"); cont.append("vitaminB12")
    if not causes: return {"triggered": False}
    return {"triggered": True, "severity": "moderate", "contributing": cont,
            "evidence": f"Haemoglobin flagged with nutritional deficiency markers ({', '.join(causes)})."}

def detect_kidney(mr):
    if _at(mr, "egfr", "borderline") and _at(mr, "urineAcr", "borderline"):
        return {"triggered": True,
                "severity": "high" if (_at(mr, "egfr", "high") or _at(mr, "urineAcr", "high")) else "early",
                "contributing": ["egfr", "urineAcr"],
                "evidence": "eGFR and Urine ACR both outside SA-optimal range — early kidney stress pattern."}
    return {"triggered": False}

PATTERNS = [
    {"id": "ir",      "name": "Insulin Resistance Cluster",     "detect": detect_ir,      "actions": {"moderate": ["lifestyle"], "high": ["endocrinologist"]}},
    {"id": "cvd",     "name": "Cardiovascular Risk Cluster",    "detect": detect_cvd,     "actions": {"moderate": ["lifestyle"], "high": ["cardiology"]}},
    {"id": "b12",     "name": "B12-Folate-Homocysteine Alert",  "detect": detect_b12,     "actions": {"high": ["b12_supplementation"]}},
    {"id": "nafld",   "name": "SA Lean NAFLD Pattern",          "detect": detect_nafld,   "actions": {"moderate": ["lifestyle"], "high": ["hepatologist"]}},
    {"id": "thyroid", "name": "Thyroid-Metabolic Pattern",      "detect": detect_thyroid, "actions": {"moderate": ["gp_thyroid"], "high": ["gp_thyroid"]}},
    {"id": "anaemia", "name": "Nutritional Anaemia Pattern",    "detect": detect_anaemia, "actions": {"moderate": ["supplementation"]}},
    {"id": "kidney",  "name": "Early Kidney Stress Pattern",    "detect": detect_kidney,  "actions": {"early": ["monitoring"], "high": ["nephrology"]}},
]


# ═══════════════════════════════════════════════════════════════════
# SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════
def _score_marker(value, defn):
    d = defn["direction"]
    opt, bord, high = defn["optimal"], defn["borderline"], defn["high"]
    if d == "higher_worse":
        if "min" in high and value >= high["min"]: return "high"
        if "min" in bord and value >= bord["min"]: return "borderline"
        return "optimal"
    if d == "lower_worse":
        if "max" in high and value <= high["max"]: return "high"
        if "max" in bord and value <= bord["max"]: return "borderline"
        if "max" in opt and value > opt["max"]: return "borderline"
        return "optimal"
    if d == "range":
        if "min" in high and value >= high["min"]: return "high"
        if "max" in high and value <= high["max"]: return "high"
        in_opt = True
        if "min" in opt and value < opt["min"]: in_opt = False
        if "max" in opt and value > opt["max"]: in_opt = False
        if in_opt: return "optimal"
        return "borderline"
    return "optimal"

def run_engine(patient, lab_results):
    labs = dict(lab_results)
    if "hba1c" in labs and labs["hba1c"] < 0.2:
        labs["hba1c"] = round(labs["hba1c"] * 100, 1)

    criticals = [
        {"key": k, "value": labs[k], "message": d["message"], "halt": d["halt"]}
        for k, d in CRITICAL_THRESHOLDS.items()
        if k in labs and d["condition"](labs[k])
    ]
    if any(c["halt"] for c in criticals):
        return {"status": "HALTED", "criticalValues": criticals, "patient": patient}

    tc, hdl = labs.get("totalCholesterol"), labs.get("hdl")
    gi, ins = labs.get("fastingGlucose"), labs.get("fastingInsulin")
    tg = labs.get("triglycerides")
    if tc and hdl:  labs["nonHdl"]    = round(tc - hdl, 1)
    if gi and ins:  labs["homaIr"]    = round((gi * ins) / 405, 2)
    if tg and hdl and hdl > 0: labs["tgHdlRatio"] = round(tg / hdl, 2)

    excl, excl_reasons = set(), {}
    if not patient.get("confirmed_fasting"):
        for k in ["fastingGlucose", "fastingInsulin", "triglycerides", "homaIr", "tgHdlRatio"]:
            excl.add(k); excl_reasons[k] = "Fasting not confirmed"
    if patient.get("biotin_within_72hrs"):
        for k in ["tsh", "freeT3", "freeT4"]:
            excl.add(k); excl_reasons[k] = "Biotin within 72 hours"
    if patient.get("illness_or_vaccination_14d"):
        for k in ["hsCrp", "esr", "wbc", "ferritin"]:
            excl.add(k); excl_reasons[k] = "Recent illness or vaccination"

    mr = {}
    for key, defn in MARKERS.items():
        value = labs.get(key)
        base = {"key": key, "name": defn["name"], "unit": defn["unit"],
                "domain": defn["domain"], "weight": defn["weight"]}
        if value is None:
            mr[key] = {**base, "status": "not_tested", "rawScore": 0, "value": None, "excluded": False}
        elif key in excl:
            mr[key] = {**base, "status": "excluded", "rawScore": 0, "value": value,
                       "excluded": True, "exclusionReason": excl_reasons.get(key, "")}
        else:
            st  = _score_marker(value, defn)
            raw = 2 if st == "high" else 1 if st == "borderline" else 0
            mr[key] = {**base, "status": st, "rawScore": raw,
                       "weightedScore": raw * defn["weight"], "value": value, "excluded": False}

    domain_scores = {}
    for dk, ddef in DOMAINS.items():
        ind = [r for r in mr.values() if r["domain"] == dk and not r["excluded"] and r["status"] != "not_tested"]
        if not ind:
            domain_scores[dk] = {"pct": 0, "tested": 0, "label": ddef["label"]}
            continue
        raw = sum(r["rawScore"] * r["weight"] for r in ind)
        mx  = sum(2 * r["weight"] for r in ind)
        domain_scores[dk] = {"pct": round((raw / mx) * 100) if mx else 0, "tested": len(ind), "label": ddef["label"]}

    age, bmi = patient.get("age", 40), patient.get("bmi", 22)
    composite = sum(domain_scores[dk]["pct"] * ddef["weight"] for dk, ddef in DOMAINS.items())
    composite = min(100, round(
        composite * (1.20 if age < 30 else 1.15 if age < 35 else 1.10 if age < 40 else 1.05 if age < 50 else 1.0)
        + (10 if bmi >= 27.5 else 7 if bmi >= 25 else 4 if bmi >= 23 else 0)
        + (5 if patient.get("family_cvd_before_60") else 0)
        + (3 if patient.get("family_diabetes") else 0)
    ))
    cat = "low" if composite < 25 else "moderate" if composite < 50 else "high" if composite < 75 else "very_high"

    pats = []
    for p in PATTERNS:
        res = p["detect"](mr)
        if res.get("triggered"):
            pats.append({
                "id": p["id"], "name": p["name"], "severity": res["severity"],
                "contributing": res.get("contributing", []),
                "evidence": res.get("evidence", ""),
                "actions": p["actions"].get(res["severity"], [])
            })

    tested     = [r for r in mr.values() if r["status"] not in ("not_tested",) and not r["excluded"]]
    high_risk  = [r for r in tested if r["status"] == "high"]
    borderline = [r for r in tested if r["status"] == "borderline"]

    return {
        "status": "OK",
        "saRiskScore": composite,
        "riskCategory": cat,
        "riskCategoryLabel": {"low": "Low risk", "moderate": "Moderate risk", "high": "High risk", "very_high": "Very high risk"}[cat],
        "domainScores": domain_scores,
        "markerResults": mr,
        "highRiskMarkers": high_risk,
        "borderlineMarkers": borderline,
        "patternsDetected": pats,
        "criticalValues": criticals,
        "exclusionReasons": excl_reasons,
        "summary": {
            "totalTested":      len(tested),
            "totalOptimal":     sum(1 for r in tested if r["status"] == "optimal"),
            "totalBorderline":  len(borderline),
            "totalHighRisk":    len(high_risk),
            "totalExcluded":    sum(1 for r in mr.values() if r["excluded"]),
            "totalNotTested":   sum(1 for r in mr.values() if r["status"] == "not_tested"),
            "patternsCount":    len(pats)
        },
        "patient": {
            "age": age, "gender": patient.get("gender"), "bmi": bmi,
            "vegetarian": patient.get("vegetarian"),
            "bmiCategory": "Obese (SA)" if bmi >= 27.5 else "Overweight (SA obese-equiv)" if bmi >= 25 else "Overweight (SA)" if bmi >= 23 else "Normal"
        },
        "engineVersion": "4.0.0", "rulesVersion": "2.0-SA",
    }


# ═══════════════════════════════════════════════════════════════════
# CLAUDE PROMPT + API
# ═══════════════════════════════════════════════════════════════════
def build_report_prompt(er):
    p      = er["patient"]
    veg    = p.get("vegetarian")
    high   = er["highRiskMarkers"]
    bord   = er["borderlineMarkers"]
    pats   = er["patternsDetected"]
    domains = er["domainScores"]
    s      = er["summary"]
    score  = er["saRiskScore"]
    cat    = er["riskCategoryLabel"]

    high_lines   = "\n".join(f"  • {m['name']}: {m['value']} {m['unit']} [HIGH RISK]"   for m in high)  or "  None"
    bord_lines   = "\n".join(f"  • {m['name']}: {m['value']} {m['unit']} [BORDERLINE]"  for m in bord)  or "  None"
    domain_lines = "\n".join(f"  • {d['label']}: {d['pct']}/100"                         for d in domains.values() if d.get("tested", 0) > 0)
    pat_lines    = "\n".join(f"  • {pt['name']} [{pt['severity'].upper()}]: {pt['evidence']}" for pt in pats) or "  None"

    lpa_high      = any(m["key"] == "lpa"     for m in high)
    ir_pattern    = any(pt["id"] == "ir"      for pt in pats)
    cvd_pattern   = any(pt["id"] == "cvd"     for pt in pats)
    b12_pattern   = any(pt["id"] == "b12"     for pt in pats)
    nafld_pattern = any(pt["id"] == "nafld"   for pt in pats)
    thyroid_pat   = any(pt["id"] == "thyroid" for pt in pats)
    anaemia_pat   = any(pt["id"] == "anaemia" for pt in pats)
    kidney_pat    = any(pt["id"] == "kidney"  for pt in pats)

    return f"""HARD CONSTRAINTS — NEVER VIOLATE:
1. Never state or imply a specific diagnosis
2. Never predict a specific clinical outcome or probability
3. Never recommend any prescription drug by name
4. Always recommend discussing results with a qualified clinician
5. No medication recommendations whatsoever — lifestyle and diet only
6. If any result seems inconsistent or physiologically implausible, note it rather than score it

You are writing a personalised health report for a real patient at Symbiosis Health — a preventive health platform built specifically for South Asians.

Think of how Function Health writes their member reports: warm, direct, science-backed, empowering. Not clinical and cold. Not vague wellness fluff. The patient should finish reading this and feel like they finally understand what is actually going on in their body — and know exactly what to do about it.

This patient may have been told their results are "normal" by their GP. Many of their markers fall within standard NHS or US lab reference ranges. They do NOT fall within South Asian-specific optimal ranges used by Symbiosis Health. Part of your job is to explain this gap clearly and kindly.

A physician will review this report before the patient sees it. Write directly to the patient using "you" throughout.

PATIENT
{p.get('gender','').title()}, {p.get('age')} years old
BMI {p.get('bmi')} — {p.get('bmiCategory')}
Diet: {'Vegetarian' if veg else 'Omnivore'}
Family history of heart disease: {'Yes' if p.get('family_cvd_before_60') else 'No'}
Family history of diabetes: {'Yes' if p.get('family_diabetes') else 'No'}

RESULTS
SA Risk Score: {score}/100 ({cat})
Domain scores:
{domain_lines}

HIGH RISK markers:
{high_lines}

BORDERLINE markers:
{bord_lines}

Patterns detected:
{pat_lines}

CLINICAL CONTEXT
{'Lp(a) IS GENETIC: Cannot be changed by diet or exercise. Strategy is to optimise every other risk factor.' if lpa_high else ''}
{'INSULIN RESISTANCE CLUSTER: Not diabetes — this is the reversible stage before. LP-IR is the most sensitive early marker. SA develop IR at lower BMIs and earlier ages than any other ethnicity.' if ir_pattern else ''}
{'CVD CLUSTER: Multiple cardiovascular markers elevated simultaneously. Risk compounds. SA have the highest rate of premature heart disease globally.' if cvd_pattern else ''}
{'B12-FOLATE-HOMOCYSTEINE: All three flagged. Most common and most reversible CVD risk factor in SA vegetarians. Correctable in 8–12 weeks with supplementation.' if b12_pattern else ''}
{'LEAN NAFLD: Fatty liver at near-normal BMI — classic SA pattern missed by standard doctors.' if nafld_pattern else ''}
{'SUBCLINICAL HYPOTHYROID: Even mild TSH elevation worsens insulin resistance and raises cholesterol.' if thyroid_pat else ''}
{'NUTRITIONAL ANAEMIA: Haemoglobin flagged alongside iron or B12 markers.' if anaemia_pat else ''}
{'EARLY KIDNEY STRESS: Time to intervene now before eGFR falls further.' if kidney_pat else ''}

Write exactly these five sections. No bullet points anywhere — warm conversational paragraphs throughout.

## Here's what we found
2–3 sentences giving the honest headline. Reference their SA Risk Score. Direct but not alarming.

Then one paragraph per flagged domain only. For each: what it does in the body, what the specific numbers mean (use actual values), why it matters for South Asians.
{'Add a paragraph titled "What caught our attention" connecting these patterns as a story: ' + ', '.join(pt['name'] for pt in pats) + '.' if pats else ''}
{'When discussing Lp(a): be clear it is genetic and not their fault — and not changeable through lifestyle.' if lpa_high else ''}

## What this means for your body right now
2–3 paragraphs translating results into lived experience. Connect numbers to symptoms they may recognise — energy, sleep, after-meal feelings, brain fog, weight around the middle.
{'Explain insulin resistance as the body working overtime to manage blood sugar — like an engine running too hard. Highly reversible at this stage.' if ir_pattern else ''}
{'Explain B12 deficiency often shows as fatigue, brain fog, tingling in hands or feet.' if b12_pattern else ''}
End with something genuinely positive from their results or acknowledge the courage it takes to look at this data proactively.

## Your personalised action plan

### Food
{'This patient is vegetarian. Every recommendation must be vegetarian. No meat, poultry, or fish.' if veg else ''}
Be specific to South Asian eating patterns. Name actual foods.
Address: white rice portions, roti (maida vs atta vs sourdough), dal frequency, chai sugar, cooking oils (ghee fine in moderation, avoid seed oils), breakfast skipping, late dinners.
{'Protein sources: moong dal, masoor dal, chana, paneer in moderation, Greek yoghurt, tofu, hemp seeds, edamame.' if veg else ''}
Give 3–5 specific food swaps based on the actual flagged markers.

### Movement
Do not assume gym access. Home-based options only.
Lead with:
1. Post-meal walks: 10 minutes after every meal reduces postprandial glucose by up to 30% in South Asians.
2. Resistance training: muscle is the primary site of glucose disposal. 3x per week bodyweight — squats, lunges, push-ups.
{'Emphasise resistance training urgency — confirmed insulin resistance cluster.' if ir_pattern else ''}
{'Zone 2 cardio 30 mins 4x per week directly reduces ApoB and triglycerides.' if cvd_pattern else ''}

### Supplements
Only what the evidence supports for this patient's specific flagged markers.
Format each as: name — dose — when to take — why for this patient specifically.
{'Specify methylcobalamin (not cyanocobalamin) for B12, algae-based omega-3, vitamin D3 from lichen.' if veg else ''}
{'- Methylcobalamin B12: 1000mcg daily with morning meal — urgent, no dietary source as vegetarian' if (b12_pattern or any(m["key"]=="vitaminB12" for m in high+bord)) else ''}
{'- Methylfolate: 400mcg daily — supports homocysteine clearance' if (b12_pattern or any(m["key"]=="folate" for m in high+bord)) else ''}
{'- Vitamin D3: 2000–4000 IU daily with fattiest meal' if any(m["key"]=="vitaminD" for m in high+bord) else ''}
{'- Magnesium glycinate: 300mg before bed — supports insulin sensitivity and sleep' if any(m["key"]=="magnesium" for m in high+bord) else ''}
End with: "Your Symbiosis doctor will review these and advise on whether any prescription-level interventions are appropriate."

### Who to see
{'- Cardiologist: review Lp(a) and CVD risk profile' if (lpa_high or cvd_pattern) else ''}
{'- Endocrinologist or metabolic physician: insulin resistance cluster review, request glucose tolerance test' if ir_pattern else ''}
{'- Hepatologist: if liver enzymes do not improve within 12 weeks of lifestyle changes' if nafld_pattern else ''}
{'- GP: thyroid panel review' if thyroid_pat else ''}
{'- Nephrologist: repeat eGFR and ACR in 3 months' if kidney_pat else ''}
State what follow-up bloods to repeat and when — metabolic markers at 3 months, everything else at 6 months.

## Your three focuses for the next 30 days
Exactly three, ranked by clinical impact for this specific patient.

Format each as:
Focus [N]: [Short name]
What to do: [One specific concrete action]
Why it matters for you: [One sentence linking to their actual flagged markers]
What to expect: [Measurable change in 4–8 weeks]

## Closing
One short paragraph. Acknowledge that seeing results like these can feel overwhelming. Remind them that looking at this data and acting on it puts them ahead of almost everyone. Be genuine and warm.

STYLE: No bullet points — paragraphs only except the three focuses. Second person throughout. Warm, direct, intelligent. Name actual SA foods. Reference actual numbers. Never diagnose. Never name prescription drugs."""


def call_claude(prompt, max_tokens=3000):
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=115
        )
        data = r.json()
        if "error" in data:
            return f"API Error: {data['error'].get('message', 'Unknown error')}"
        return data.get("content", [{}])[0].get("text", "Report generation failed.")
    except Exception as e:
        return f"Claude API error: {e}"


# ═══════════════════════════════════════════════════════════════════
# LAB FIELDS
# ═══════════════════════════════════════════════════════════════════
LAB_FIELDS = [
    {"section": "Cardiovascular", "fields": [
        {"key": "lpa",             "label": "Lp(a)",             "unit": "nmol/L"},
        {"key": "apob",            "label": "ApoB",              "unit": "mg/dL"},
        {"key": "apoa1",           "label": "ApoA1",             "unit": "mg/dL"},
        {"key": "ldl",             "label": "LDL (direct)",      "unit": "mg/dL"},
        {"key": "hdl",             "label": "HDL",               "unit": "mg/dL"},
        {"key": "triglycerides",   "label": "Triglycerides",     "unit": "mg/dL"},
        {"key": "totalCholesterol","label": "Total Cholesterol", "unit": "mg/dL"},
        {"key": "hsCrp",           "label": "hs-CRP",            "unit": "mg/L"},
        {"key": "homocysteine",    "label": "Homocysteine",      "unit": "µmol/L"},
    ]},
    {"section": "Metabolic", "fields": [
        {"key": "lpir",            "label": "LP-IR Score",       "unit": "0–100"},
        {"key": "fastingInsulin",  "label": "Fasting Insulin",   "unit": "µIU/mL"},
        {"key": "hba1c",           "label": "HbA1c",             "unit": "%"},
        {"key": "fastingGlucose",  "label": "Fasting Glucose",   "unit": "mg/dL"},
        {"key": "uricAcid",        "label": "Uric Acid",         "unit": "mg/dL"},
        {"key": "cPeptide",        "label": "C-Peptide",         "unit": "ng/mL"},
    ]},
    {"section": "Liver & Kidney", "fields": [
        {"key": "alt",       "label": "ALT",         "unit": "U/L"},
        {"key": "ast",       "label": "AST",         "unit": "U/L"},
        {"key": "ggt",       "label": "GGT",         "unit": "U/L"},
        {"key": "creatinine","label": "Creatinine",  "unit": "mg/dL"},
        {"key": "egfr",      "label": "eGFR",        "unit": "mL/min/1.73m²"},
        {"key": "albumin",   "label": "Albumin",     "unit": "g/dL"},
        {"key": "urineAcr",  "label": "Urine ACR",   "unit": "mg/g"},
    ]},
    {"section": "Thyroid", "fields": [
        {"key": "tsh",    "label": "TSH",     "unit": "mIU/L"},
        {"key": "freeT3", "label": "Free T3", "unit": "pg/mL"},
        {"key": "freeT4", "label": "Free T4", "unit": "ng/dL"},
    ]},
    {"section": "Nutritional", "fields": [
        {"key": "vitaminD",   "label": "Vitamin D",   "unit": "ng/mL"},
        {"key": "vitaminB12", "label": "Vitamin B12", "unit": "pg/mL"},
        {"key": "folate",     "label": "Folate",      "unit": "ng/mL"},
        {"key": "ferritin",   "label": "Ferritin",    "unit": "ng/mL"},
        {"key": "iron",       "label": "Iron",        "unit": "µg/dL"},
        {"key": "magnesium",  "label": "Magnesium",   "unit": "mg/dL"},
        {"key": "zinc",       "label": "Zinc",        "unit": "µg/dL"},
    ]},
    {"section": "Blood & Inflammation", "fields": [
        {"key": "hemoglobin", "label": "Hemoglobin", "unit": "g/dL"},
        {"key": "hematocrit", "label": "Hematocrit", "unit": "%"},
        {"key": "wbc",        "label": "WBC",        "unit": "×10³/µL"},
        {"key": "platelets",  "label": "Platelets",  "unit": "×10³/µL"},
        {"key": "esr",        "label": "ESR",        "unit": "mm/hr"},
    ]},
]

DEMO = {
    "lpa": 87, "apob": 118, "apoa1": 108, "ldl": 122, "hdl": 38,
    "triglycerides": 165, "totalCholesterol": 200, "hsCrp": 3.8,
    "homocysteine": 14, "lpir": 62, "fastingInsulin": 18, "hba1c": 5.9,
    "fastingGlucose": 97, "uricAcid": 6.1, "cPeptide": 2.4,
    "alt": 32, "ast": 28, "ggt": 38, "creatinine": 1.1, "egfr": 88,
    "albumin": 4.3, "urineAcr": 8, "tsh": 3.1, "freeT3": 2.7,
    "freeT4": 1.1, "vitaminD": 19, "vitaminB12": 310, "folate": 3.2,
    "ferritin": 45, "iron": 65, "magnesium": 1.9, "zinc": 72,
    "hemoglobin": 13.2, "hematocrit": 41, "wbc": 7.2, "platelets": 220, "esr": 24,
}


# ═══════════════════════════════════════════════════════════════════
# CSS
# ═══════════════════════════════════════════════════════════════════
CSS = """
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=DM+Serif+Display&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{background:#F7F5F2;padding:32px 16px;font-family:'DM Sans',sans-serif;color:#1a1a1a}
.wrap{max-width:780px;margin:0 auto}
.brand{font-family:'DM Serif Display',serif;font-size:22px;letter-spacing:-.3px;color:#1a1a1a;margin-bottom:2px}
.brand span{color:#2D6A4F}
.sub{font-size:13px;color:#6b7280;margin-bottom:28px}
.card{background:#fff;border:1px solid #E8E4DF;border-radius:16px;padding:20px 24px;margin-bottom:14px}
.sl{font-size:10px;font-weight:600;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em;margin-bottom:14px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.field{display:flex;flex-direction:column;gap:4px}
.field label{font-size:11px;font-weight:600;color:#374151}
.u{font-size:10px;color:#9ca3af}
input[type=text],input[type=number],select,textarea{border:1px solid #E0DDD9;border-radius:8px;padding:8px 10px;font-size:13px;width:100%;font-family:inherit;background:#FAFAF9}
input:focus,select:focus,textarea:focus{outline:none;border-color:#2D6A4F;background:#fff}
.ck{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid #F3F0EC;font-size:13px;color:#374151;cursor:pointer}
.ck:last-child{border-bottom:none}
.ck input{width:15px;height:15px;flex-shrink:0;accent-color:#2D6A4F}
.btn{background:#2D6A4F;color:#fff;border:none;border-radius:10px;padding:11px 26px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;transition:background .15s}
.btn:hover{background:#235C43}
.btns{background:#fff;color:#374151;border:1px solid #E0DDD9;border-radius:10px;padding:11px 20px;font-size:13px;cursor:pointer;text-decoration:none;display:inline-block;font-family:inherit}
.row{display:flex;gap:10px;margin-top:10px;align-items:center}
.pill{font-size:10px;padding:2px 9px;border-radius:20px;font-weight:600}
.pr{background:#fef2f2;color:#b91c1c}.pa{background:#fffbeb;color:#92400e}
.stat-g{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.stat{background:#F7F5F2;border-radius:10px;padding:10px 12px}
.sn{font-size:20px;font-weight:600}.sl2{font-size:11px;color:#6b7280}
.drow{display:flex;align-items:center;gap:12px;margin-bottom:10px}
.dn{font-size:12px;color:#6b7280;width:160px;flex-shrink:0}
.bt{flex:1;height:6px;background:#F3F0EC;border-radius:3px;overflow:hidden}
.bf{height:6px;border-radius:3px}
.dp{font-size:12px;font-weight:600;width:32px;text-align:right}
.mrow{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #F3F0EC}
.mrow:last-child{border-bottom:none}
.pc{border-left:3px solid;padding:10px 14px;border-radius:0 10px 10px 0;margin-bottom:8px}
.ph{border-color:#ef4444;background:#fef2f2}.pm{border-color:#f59e0b;background:#fffbeb}
.pt{font-size:12px;font-weight:600}.pb2{font-size:11px;margin-top:3px;line-height:1.5}
.pending{background:#fffbeb;border:1px solid #fcd34d;border-radius:10px;padding:10px 14px;font-size:12px;color:#92400e;margin-bottom:14px}
.approved{background:#f0fdf4;border:1px solid #86efac;border-radius:10px;padding:10px 14px;font-size:12px;color:#166534;margin-bottom:14px}
.disclaimer{background:#F0F7F4;border:1px solid #A7D7C5;border-radius:10px;padding:11px 15px;margin-bottom:14px;font-size:12px;color:#1a5c3f}
.err{background:#fef2f2;border:1px solid #fca5a5;border-radius:10px;padding:12px 15px;font-size:13px;color:#b91c1c;margin-bottom:14px}
"""


# ═══════════════════════════════════════════════════════════════════
# HTML TEMPLATES
# ═══════════════════════════════════════════════════════════════════
LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Symbiosis Health — Predictive Health for South Asians</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;1,300;1,400&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --green:#1C4A3A;
  --green-mid:#2D6A4F;
  --green-light:#E8F2EE;
  --cream:#FAF8F4;
  --cream-dark:#F0EDE6;
  --text:#1a1a18;
  --muted:#6b7260;
  --border:#DDD9D0;
}
html{scroll-behavior:smooth}
body{background:var(--cream);font-family:'DM Sans',sans-serif;color:var(--text);overflow-x:hidden}

/* NAV */
nav{position:fixed;top:0;left:0;right:0;z-index:100;padding:0 48px;height:64px;display:flex;align-items:center;justify-content:space-between;background:rgba(250,248,244,.92);backdrop-filter:blur(12px);border-bottom:1px solid var(--border)}
.nav-logo{display:flex;align-items:center;gap:10px}
.logo-mark{width:32px;height:32px;background:var(--green);border-radius:8px;display:flex;align-items:center;justify-content:center;font-family:'Cormorant Garamond',serif;font-size:14px;font-weight:500;color:#fff;letter-spacing:.5px}
.nav-wordmark{font-family:'Cormorant Garamond',serif;font-size:20px;font-weight:400;color:var(--text);letter-spacing:-.3px}
.nav-wordmark span{color:var(--green-mid)}
.nav-cta{background:var(--green);color:#fff;border:none;border-radius:8px;padding:9px 20px;font-size:13px;font-weight:500;cursor:pointer;font-family:'DM Sans',sans-serif;text-decoration:none;transition:background .2s}
.nav-cta:hover{background:var(--green-mid)}

/* HERO */
.hero{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:120px 24px 80px;position:relative;overflow:hidden}
.hero-eyebrow{font-size:11px;font-weight:500;letter-spacing:.12em;text-transform:uppercase;color:var(--green-mid);margin-bottom:24px;opacity:0;animation:fadeUp .8s .2s forwards}
.hero-headline{font-family:'Cormorant Garamond',serif;font-size:clamp(52px,8vw,96px);font-weight:300;line-height:1.0;letter-spacing:-.02em;color:var(--text);margin-bottom:16px;opacity:0;animation:fadeUp .8s .4s forwards}
.hero-headline em{font-style:italic;color:var(--green)}
.hero-sub{font-size:17px;font-weight:300;color:var(--muted);max-width:520px;line-height:1.7;margin-bottom:40px;opacity:0;animation:fadeUp .8s .6s forwards}
.hero-form{display:flex;gap:10px;max-width:440px;width:100%;opacity:0;animation:fadeUp .8s .8s forwards}
.hero-input{flex:1;border:1px solid var(--border);border-radius:10px;padding:13px 16px;font-size:14px;font-family:'DM Sans',sans-serif;background:#fff;color:var(--text);outline:none;transition:border-color .2s}
.hero-input:focus{border-color:var(--green-mid)}
.hero-input::placeholder{color:#aaa}
.hero-btn{background:var(--green);color:#fff;border:none;border-radius:10px;padding:13px 24px;font-size:14px;font-weight:500;cursor:pointer;font-family:'DM Sans',sans-serif;white-space:nowrap;transition:background .2s}
.hero-btn:hover{background:var(--green-mid)}
.hero-note{font-size:11px;color:#aaa;margin-top:12px;opacity:0;animation:fadeUp .8s 1s forwards}
.hero-bg{position:absolute;top:0;left:0;right:0;bottom:0;pointer-events:none;z-index:-1}
.hero-circle{position:absolute;border-radius:50%;opacity:.06}
.circle-1{width:600px;height:600px;background:var(--green);top:-200px;right:-200px}
.circle-2{width:400px;height:400px;background:var(--green);bottom:-100px;left:-150px}

/* STATS BAR */
.stats-bar{background:var(--green);padding:28px 48px;display:flex;justify-content:center;gap:80px}
.stat-item{text-align:center}
.stat-num{font-family:'Cormorant Garamond',serif;font-size:40px;font-weight:300;color:#fff;line-height:1}
.stat-label{font-size:12px;color:rgba(255,255,255,.65);margin-top:4px;font-weight:300}

/* PROBLEM */
.section{padding:96px 48px;max-width:1100px;margin:0 auto}
.section-tag{font-size:11px;font-weight:500;letter-spacing:.1em;text-transform:uppercase;color:var(--green-mid);margin-bottom:16px}
.section-headline{font-family:'Cormorant Garamond',serif;font-size:clamp(36px,5vw,60px);font-weight:300;line-height:1.1;letter-spacing:-.02em;margin-bottom:24px}
.section-headline em{font-style:italic;color:var(--green)}
.section-body{font-size:16px;font-weight:300;color:var(--muted);line-height:1.8;max-width:600px}
.problem-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:2px;margin-top:56px;background:var(--border);border-radius:16px;overflow:hidden}
.problem-card{background:var(--cream);padding:32px 28px}
.problem-card:first-child{border-radius:14px 0 0 14px}
.problem-card:last-child{border-radius:0 14px 14px 0}
.problem-num{font-family:'Cormorant Garamond',serif;font-size:48px;font-weight:300;color:var(--green);line-height:1;margin-bottom:12px}
.problem-title{font-size:15px;font-weight:500;color:var(--text);margin-bottom:8px}
.problem-desc{font-size:13px;font-weight:300;color:var(--muted);line-height:1.7}

/* HOW IT WORKS */
.how-section{background:var(--cream-dark);padding:96px 48px}
.how-inner{max-width:1100px;margin:0 auto}
.steps{display:grid;grid-template-columns:repeat(5,1fr);gap:0;margin-top:56px;position:relative}
.steps::before{content:'';position:absolute;top:28px;left:10%;right:10%;height:1px;background:var(--border);z-index:0}
.step{text-align:center;position:relative;z-index:1}
.step-dot{width:56px;height:56px;border-radius:50%;background:#fff;border:1px solid var(--border);display:flex;align-items:center;justify-content:center;margin:0 auto 16px;font-family:'Cormorant Garamond',serif;font-size:20px;font-weight:400;color:var(--green)}
.step-title{font-size:13px;font-weight:500;color:var(--text);margin-bottom:6px}
.step-desc{font-size:12px;font-weight:300;color:var(--muted);line-height:1.6;padding:0 8px}

/* SCIENCE */
.science-section{padding:96px 48px;max-width:1100px;margin:0 auto}
.science-grid{display:grid;grid-template-columns:1fr 1fr;gap:48px;margin-top:56px;align-items:start}
.biomarker-table{border:1px solid var(--border);border-radius:14px;overflow:hidden}
.bt-header{display:grid;grid-template-columns:1.5fr 1fr 1fr;background:var(--green);padding:12px 20px;gap:12px}
.bt-header span{font-size:11px;font-weight:500;color:rgba(255,255,255,.75);letter-spacing:.06em;text-transform:uppercase}
.bt-row{display:grid;grid-template-columns:1.5fr 1fr 1fr;padding:12px 20px;gap:12px;border-bottom:1px solid var(--border);background:#fff}
.bt-row:last-child{border-bottom:none}
.bt-row:nth-child(odd){background:var(--cream)}
.bt-name{font-size:12px;font-weight:500;color:var(--text)}
.bt-western{font-size:12px;color:var(--muted)}
.bt-sa{font-size:12px;font-weight:500;color:var(--green-mid)}
.science-points{display:flex;flex-direction:column;gap:20px}
.sci-point{display:flex;gap:16px;align-items:flex-start}
.sci-icon{width:40px;height:40px;background:var(--green-light);border-radius:10px;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:18px}
.sci-title{font-size:14px;font-weight:500;color:var(--text);margin-bottom:4px}
.sci-desc{font-size:13px;font-weight:300;color:var(--muted);line-height:1.6}

/* FOUNDER */
.founder-section{background:var(--green);padding:96px 48px}
.founder-inner{max-width:800px;margin:0 auto;text-align:center}
.founder-quote{font-family:'Cormorant Garamond',serif;font-size:clamp(24px,4vw,42px);font-weight:300;font-style:italic;color:#fff;line-height:1.3;margin-bottom:32px;letter-spacing:-.01em}
.founder-attr{font-size:13px;color:rgba(255,255,255,.6);font-weight:300}
.founder-attr strong{color:rgba(255,255,255,.9);font-weight:500}

/* WAITLIST */
.waitlist-section{padding:96px 48px;text-align:center}
.waitlist-inner{max-width:560px;margin:0 auto}
.waitlist-headline{font-family:'Cormorant Garamond',serif;font-size:clamp(36px,5vw,56px);font-weight:300;line-height:1.1;letter-spacing:-.02em;margin-bottom:16px}
.waitlist-sub{font-size:16px;font-weight:300;color:var(--muted);line-height:1.7;margin-bottom:40px}
.waitlist-form{display:flex;flex-direction:column;gap:12px;align-items:center}
.waitlist-input{width:100%;border:1px solid var(--border);border-radius:10px;padding:14px 18px;font-size:15px;font-family:'DM Sans',sans-serif;background:#fff;color:var(--text);outline:none;transition:border-color .2s;max-width:400px}
.waitlist-input:focus{border-color:var(--green-mid)}
.waitlist-btn{background:var(--green);color:#fff;border:none;border-radius:10px;padding:14px 36px;font-size:15px;font-weight:500;cursor:pointer;font-family:'DM Sans',sans-serif;transition:background .2s;width:100%;max-width:400px}
.waitlist-btn:hover{background:var(--green-mid)}
.waitlist-note{font-size:12px;color:#aaa;margin-top:8px}
.success-msg{display:none;background:var(--green-light);border:1px solid var(--green-mid);border-radius:10px;padding:16px 24px;font-size:14px;color:var(--green);max-width:400px;width:100%}

/* FOOTER */
footer{border-top:1px solid var(--border);padding:24px 48px;display:flex;align-items:center;justify-content:space-between}
.footer-brand{font-family:'Cormorant Garamond',serif;font-size:16px;color:var(--muted)}
.footer-note{font-size:12px;color:#bbb}

/* ANIMATIONS */
@keyframes fadeUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
.reveal{opacity:0;transform:translateY(24px);transition:opacity .7s ease,transform .7s ease}
.reveal.visible{opacity:1;transform:none}

@media(max-width:768px){
  nav{padding:0 20px}
  .stats-bar{flex-wrap:wrap;gap:32px;padding:32px 24px}
  .section,.waitlist-section,.science-section{padding:64px 24px}
  .how-section{padding:64px 24px}
  .problem-grid{grid-template-columns:1fr}
  .steps{grid-template-columns:1fr 1fr;gap:24px}
  .steps::before{display:none}
  .science-grid{grid-template-columns:1fr}
  .founder-section{padding:64px 24px}
  footer{flex-direction:column;gap:8px;text-align:center;padding:24px}
}
</style>
</head>
<body>

<nav>
  <div class="nav-logo">
    <div class="logo-mark">S·H</div>
    <div class="nav-wordmark">Symbiosis <span>Health</span></div>
  </div>
  <a href="#waitlist" class="nav-cta">Join waitlist</a>
</nav>

<!-- HERO -->
<section class="hero">
  <div class="hero-bg">
    <div class="hero-circle circle-1"></div>
    <div class="hero-circle circle-2"></div>
  </div>
  <div class="hero-eyebrow">Now accepting early members — India launch 2025</div>
  <h1 class="hero-headline">Your health.<br><em>Finally measured right.</em></h1>
  <p class="hero-sub">The first predictive health platform built on South Asian biology. 40 biomarkers. SA-specific thresholds. Physician-reviewed insights.</p>
  <form class="hero-form" onsubmit="joinWaitlist(event, 'hero-success')">
    <input class="hero-input" type="email" placeholder="Enter your email" required id="hero-email">
    <button class="hero-btn" type="submit">Join waitlist</button>
  </form>
  <div id="hero-success" class="success-msg" style="display:none;margin-top:12px">✓ You're on the list. We'll be in touch soon.</div>
  <p class="hero-note">Free early access · No credit card required</p>
</section>

<!-- STATS -->
<div class="stats-bar">
  <div class="stat-item"><div class="stat-num">2B+</div><div class="stat-label">South Asians globally</div></div>
  <div class="stat-item"><div class="stat-num">10yrs</div><div class="stat-label">Earlier T2D onset vs Western populations</div></div>
  <div class="stat-item"><div class="stat-num">40</div><div class="stat-label">SA-specific biomarkers analysed</div></div>
  <div class="stat-item"><div class="stat-num">0</div><div class="stat-label">Platforms built for South Asian biology — until now</div></div>
</div>

<!-- PROBLEM -->
<section class="section">
  <div class="reveal">
    <div class="section-tag">The problem</div>
    <h2 class="section-headline">You've been told<br><em>your labs are normal.</em><br>They're not.</h2>
    <p class="section-body">Every lab reference range in the world is calibrated on Western populations. South Asians are measured against the wrong ruler — and pay for it with their lives.</p>
  </div>
  <div class="problem-grid reveal">
    <div class="problem-card">
      <div class="problem-num">10</div>
      <div class="problem-title">Years earlier</div>
      <div class="problem-desc">South Asians develop Type 2 diabetes and cardiovascular disease a decade before Western populations — yet are screened the same way.</div>
    </div>
    <div class="problem-card">
      <div class="problem-num">77%</div>
      <div class="problem-title">Of SA deaths</div>
      <div class="problem-desc">Are caused by cardiovascular disease and diabetes — conditions that are largely preventable when caught early with the right thresholds.</div>
    </div>
    <div class="problem-card">
      <div class="problem-num">0</div>
      <div class="problem-title">SA-specific tools</div>
      <div class="problem-desc">No health platform — not Function Health, not your GP, not any lab in India — uses South Asian-validated reference ranges. Until now.</div>
    </div>
  </div>
</section>

<!-- HOW IT WORKS -->
<div class="how-section">
  <div class="how-inner">
    <div class="reveal">
      <div class="section-tag">How it works</div>
      <h2 class="section-headline">Simple for you.<br><em>Rigorous behind the scenes.</em></h2>
    </div>
    <div class="steps reveal">
      <div class="step">
        <div class="step-dot">1</div>
        <div class="step-title">Sign up</div>
        <div class="step-desc">Enter your baseline info and family history</div>
      </div>
      <div class="step">
        <div class="step-dot">2</div>
        <div class="step-title">Visit a lab</div>
        <div class="step-desc">Go to any partner diagnostic lab in India</div>
      </div>
      <div class="step">
        <div class="step-dot">3</div>
        <div class="step-title">We analyse</div>
        <div class="step-desc">40 markers scored against SA-specific thresholds</div>
      </div>
      <div class="step">
        <div class="step-dot">4</div>
        <div class="step-title">Doctor reviews</div>
        <div class="step-desc">A physician approves your report before you see it</div>
      </div>
      <div class="step">
        <div class="step-dot">5</div>
        <div class="step-title">You act</div>
        <div class="step-desc">Personalised diet, movement and supplement plan</div>
      </div>
    </div>
  </div>
</div>

<!-- SCIENCE -->
<section class="science-section">
  <div class="reveal">
    <div class="section-tag">The science</div>
    <h2 class="section-headline">The thresholds<br><em>are the moat.</em></h2>
    <p class="section-body" style="margin-bottom:0">Every cutoff is peer-reviewed and SA-specific. Competitors use Western ranges. We don't.</p>
  </div>
  <div class="science-grid reveal">
    <div>
      <div class="biomarker-table">
        <div class="bt-header">
          <span>Biomarker</span>
          <span>Western</span>
          <span>Symbiosis SA</span>
        </div>
        <div class="bt-row"><div class="bt-name">Fasting Glucose</div><div class="bt-western">&lt; 100 mg/dL</div><div class="bt-sa">&lt; 90 mg/dL</div></div>
        <div class="bt-row"><div class="bt-name">Fasting Insulin</div><div class="bt-western">&lt; 15 µIU/mL</div><div class="bt-sa">&lt; 8 µIU/mL</div></div>
        <div class="bt-row"><div class="bt-name">Triglycerides</div><div class="bt-western">&lt; 150 mg/dL</div><div class="bt-sa">&lt; 100 mg/dL</div></div>
        <div class="bt-row"><div class="bt-name">hs-CRP</div><div class="bt-western">&lt; 3.0 mg/L</div><div class="bt-sa">&lt; 1.0 mg/L</div></div>
        <div class="bt-row"><div class="bt-name">Vitamin B12</div><div class="bt-western">≥ 200 pg/mL</div><div class="bt-sa">≥ 400 pg/mL</div></div>
        <div class="bt-row"><div class="bt-name">LP-IR Score</div><div class="bt-western">Not tested</div><div class="bt-sa">&lt; 45</div></div>
      </div>
    </div>
    <div class="science-points">
      <div class="sci-point">
        <div class="sci-icon">🔬</div>
        <div><div class="sci-title">80+ peer-reviewed citations</div><div class="sci-desc">Every threshold is backed by SA-specific studies — MASALA, INTERHEART, JACC Advances 2023, ADA Asian guidelines.</div></div>
      </div>
      <div class="sci-point">
        <div class="sci-icon">🧬</div>
        <div><div class="sci-title">The thin-fat paradox</div><div class="sci-desc">South Asians carry dangerous visceral fat at normal BMIs. Our panel detects insulin resistance in lean individuals that standard tools miss entirely.</div></div>
      </div>
      <div class="sci-point">
        <div class="sci-icon">👩‍⚕️</div>
        <div><div class="sci-title">Physician-reviewed, always</div><div class="sci-desc">Every report is reviewed and approved by a qualified doctor before you see a single result. Medical integrity is non-negotiable.</div></div>
      </div>
      <div class="sci-point">
        <div class="sci-icon">📈</div>
        <div><div class="sci-title">Longitudinal data moat</div><div class="sci-desc">Tested twice yearly, we're building the world's largest South Asian health dataset — improving every recommendation over time.</div></div>
      </div>
    </div>
  </div>
</section>

<!-- FOUNDER -->
<div class="founder-section">
  <div class="founder-inner reveal">
    <p class="founder-quote">"My grandfather died of a sudden heart attack. My parents have diabetes. They were both told their labs were normal. They weren't — they were just being measured against the wrong ruler."</p>
    <p class="founder-attr"><strong>The founder of Symbiosis Health</strong> · NYU '2X · Built for the family this problem belongs to</p>
  </div>
</div>

<!-- WAITLIST -->
<section class="waitlist-section" id="waitlist">
  <div class="waitlist-inner reveal">
    <h2 class="waitlist-headline">Be among the first<br><em>to know your real risk.</em></h2>
    <p class="waitlist-sub">Join the waitlist for early access to Symbiosis Health. Launching in India in 2025.</p>
    <form class="waitlist-form" onsubmit="joinWaitlist(event, 'waitlist-success')">
      <input class="waitlist-input" type="email" placeholder="your@email.com" required id="waitlist-email">
      <button class="waitlist-btn" type="submit">Join the waitlist →</button>
      <div id="waitlist-success" class="success-msg">✓ You're on the list. We'll reach out with early access details.</div>
      <p class="waitlist-note">No spam. Early access only. Unsubscribe anytime.</p>
    </form>
  </div>
</section>

<footer>
  <div class="footer-brand">Symbiosis Health · 2025</div>
  <div class="footer-note">Built at NYU · Peer-reviewed science · Physician-reviewed reports</div>
</footer>

<script>
// Scroll reveal
const observer = new IntersectionObserver((entries) => {
  entries.forEach(e => { if(e.isIntersecting) { e.target.classList.add('visible'); } });
}, {threshold: 0.1});
document.querySelectorAll('.reveal').forEach(el => observer.observe(el));

// Waitlist form
function joinWaitlist(e, successId) {
  e.preventDefault();
  const emailId = successId === 'hero-success' ? 'hero-email' : 'waitlist-email';
  const email = document.getElementById(emailId).value;
  const success = document.getElementById(successId);
  // Store email via fetch to /waitlist endpoint
  fetch('/waitlist', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({email: email})
  }).then(() => {
    success.style.display = 'block';
    document.getElementById(emailId).value = '';
    document.getElementById(emailId).style.display = 'none';
    if(successId === 'hero-success') {
      document.querySelector('.hero-btn').style.display = 'none';
    } else {
      document.querySelector('.waitlist-btn').style.display = 'none';
    }
  }).catch(() => {
    success.style.display = 'block';
    success.textContent = '✓ You\'re on the list!';
  });
}
</script>
</body>
</html>"""

INTAKE_HTML = """<!DOCTYPE html><html><head><title>Symbiosis Health</title>
<style>""" + CSS + """</style></head>
<body><div class="wrap">
<div class="brand">Symbiosis <span>Health</span></div>
<p class="sub">Step 1 of 2 — Patient intake</p>
<form method="POST">
<div class="card"><p class="sl">Basic information</p><div class="grid3">
<div class="field"><label>Full name</label><input type="text" name="name" placeholder="Priya Sharma" required></div>
<div class="field"><label>Age</label><input type="number" name="age" placeholder="38" required></div>
<div class="field"><label>Gender</label><select name="gender"><option value="female">Female</option><option value="male">Male</option></select></div>
<div class="field"><label>Height (cm)</label><input type="number" name="height" placeholder="163" required></div>
<div class="field"><label>Weight (kg)</label><input type="number" name="weight" placeholder="68" required></div>
</div></div>
<div class="card"><p class="sl">Lifestyle & family history</p>
<label class="ck"><input type="checkbox" name="vegetarian"> I follow a vegetarian diet</label>
<label class="ck"><input type="checkbox" name="family_cvd_before_60"> First-degree family member had heart disease before age 60</label>
<label class="ck"><input type="checkbox" name="family_diabetes"> First-degree family member has type 2 diabetes</label>
</div>
<div class="card"><p class="sl">Before your blood draw</p>
<label class="ck"><input type="checkbox" name="confirmed_fasting"> I fasted for at least 8 hours before the blood draw</label>
<label class="ck"><input type="checkbox" name="illness_or_vaccination_14d"> I had an illness or vaccination in the past 14 days</label>
<label class="ck"><input type="checkbox" name="biotin_within_72hrs"> I took Biotin (Vitamin B7) in the past 72 hours</label>
</div>
<button type="submit" class="btn">Continue to lab results →</button>
</form></div></body></html>"""

LABS_HTML = """<!DOCTYPE html><html><head><title>Symbiosis — Labs</title>
<style>""" + CSS + """</style></head>
<body><div class="wrap">
<div class="brand">Symbiosis <span>Health</span></div>
<p class="sub">Step 2 of 2 — Enter values from your lab report. Leave blank if not tested.</p>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<div class="row" style="margin-bottom:18px;margin-top:0">
<a href="/labs/{{ patient_id }}?demo=1" class="btn" style="font-size:12px;padding:8px 16px;text-decoration:none">Load demo values</a>
<span style="font-size:12px;color:#9ca3af">or enter manually below</span>
</div>
<form method="POST">
{% for section in fields %}
<div class="card"><p class="sl">{{ section.section }}</p><div class="grid3">
{% for f in section.fields %}
<div class="field"><label>{{ f.label }}</label><span class="u">{{ f.unit }}</span>
<input type="number" step="any" name="{{ f.key }}" placeholder="—" value="{{ labs.get(f.key,'') }}">
</div>{% endfor %}
</div></div>{% endfor %}
<div class="row">
<a href="/start" class="btns">← Back</a>
<input type="hidden" name="patient_id" value="{{ patient_id }}">
<button type="submit" class="btn">Run SA analysis →</button>
</div>
</form></div></body></html>"""

RESULTS_HTML = """<!DOCTYPE html><html><head><title>Symbiosis — Results</title>
<style>""" + CSS + """</style></head>
<body><div class="wrap">
{% set er=result %}{% set p=er.patient %}{% set s=er.summary %}
<div class="brand">Symbiosis <span>Health</span></div>
<p class="sub">{{ p.gender|title }}, {{ p.age }} yrs · BMI {{ p.bmi }} ({{ p.bmiCategory }})</p>

{% if er.status=="HALTED" %}
<div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:16px;padding:24px">
<h2 style="color:#b91c1c;margin-bottom:12px;font-family:'DM Serif Display',serif">Critical values — physician review required</h2>
{% for c in er.criticalValues %}<p style="font-size:13px;color:#991b1b;margin-bottom:6px">• {{ c.message }}</p>{% endfor %}
</div>

{% else %}
{% set cat=er.riskCategory %}
{% set cc="#166534" if cat=="low" else "#92400e" if cat=="moderate" else "#b91c1c" %}
{% set bg="#f0fdf4" if cat=="low" else "#fffbeb" if cat=="moderate" else "#fef2f2" %}

<div class="disclaimer"><strong>About this score:</strong> The Symbiosis SA Risk Index is scored against South Asian-specific reference ranges validated in peer-reviewed literature (MASALA, INTERHEART, JACC Advances 2023, LP-IR SA Validation). This is a screening tool reviewed by a physician before delivery — not a diagnosis.</div>

<div class="card">
<div style="display:flex;gap:24px;align-items:center">
<div>
<div style="font-size:64px;font-weight:600;color:{{ cc }};line-height:1;font-family:'DM Serif Display',serif">{{ er.saRiskScore }}</div>
<div style="font-size:11px;color:#6b7280;margin-top:4px">SA Risk Index / 100</div>
<div style="display:inline-block;margin-top:8px;font-size:12px;font-weight:600;padding:4px 14px;border-radius:20px;background:{{ bg }};color:{{ cc }}">{{ er.riskCategoryLabel }}</div>
</div>
<div style="flex:1"><div class="stat-g">
{% for lbl,val,col in [("Tested",s.totalTested,""),("High risk",s.totalHighRisk,"#b91c1c"),("Borderline",s.totalBorderline,"#92400e"),("Optimal",s.totalOptimal,"#166534"),("Patterns",s.patternsCount,""),("Excluded",s.totalExcluded,"")] %}
<div class="stat"><div class="sn" style="color:{{ col or '#1a1a1a' }}">{{ val }}</div><div class="sl2">{{ lbl }}</div></div>
{% endfor %}
</div></div>
</div></div>

<div class="card"><p class="sl">Domain scores</p>
{% for key,d in er.domainScores.items() %}{% if d.tested>0 %}
{% set pct=d.pct %}{% set c="#166534" if pct<30 else "#92400e" if pct<60 else "#b91c1c" %}
<div class="drow"><span class="dn">{{ d.label }}</span>
<div class="bt"><div class="bf" style="width:{{ pct }}%;background:{{ c }}"></div></div>
<span class="dp" style="color:{{ c }}">{{ pct }}</span></div>
{% endif %}{% endfor %}</div>

{% if er.highRiskMarkers %}
<div class="card"><p class="sl">High risk markers</p>
{% for m in er.highRiskMarkers %}
<div class="mrow"><span style="font-size:13px;font-weight:600">{{ m.name }}</span>
<div style="display:flex;align-items:center;gap:10px">
<span style="font-size:13px;color:#b91c1c;font-weight:600">{{ m.value }} {{ m.unit }}</span>
<span class="pill pr">HIGH</span></div></div>{% endfor %}</div>{% endif %}

{% if er.borderlineMarkers %}
<div class="card"><p class="sl">Borderline markers</p>
{% for m in er.borderlineMarkers %}
<div class="mrow"><span style="font-size:13px">{{ m.name }}</span>
<div style="display:flex;align-items:center;gap:10px">
<span style="font-size:13px;color:#92400e">{{ m.value }} {{ m.unit }}</span>
<span class="pill pa">BORDERLINE</span></div></div>{% endfor %}</div>{% endif %}

{% if er.patternsDetected %}
<div class="card"><p class="sl">Patterns detected</p>
{% for pt in er.patternsDetected %}
<div class="pc {{ 'ph' if pt.severity=='high' else 'pm' }}">
<div class="pt" style="color:{{ '#991b1b' if pt.severity=='high' else '#92400e' }}">{{ pt.name }} — {{ pt.severity }}</div>
<div class="pb2" style="color:{{ '#b91c1c' if pt.severity=='high' else '#a16207' }}">{{ pt.evidence }}</div>
</div>{% endfor %}</div>{% endif %}

<div class="card"><p class="sl">AI-drafted patient report</p>
{% if approved %}
<div class="approved">✓ Approved by {{ approved.doctor }} — ready for patient delivery.</div>
<textarea disabled style="min-height:520px;background:#FAFAF9;line-height:1.9;font-size:13px">{{ approved.report }}</textarea>
{% else %}
<div class="pending">⏳ Pending physician review. Edit below then approve.</div>
<form method="POST" action="/results/{{ report_id }}">
<textarea name="report_text" style="min-height:520px;line-height:1.9;font-size:13px">{{ report_draft }}</textarea>
<div class="row" style="margin-top:14px">
<input type="text" name="doctor_name" placeholder="Reviewing doctor's full name" required style="flex:1">
<button type="submit" class="btn">Approve & mark ready →</button>
</div></form>{% endif %}
</div>{% endif %}

<div class="row" style="margin-top:6px">
<a href="/labs" class="btns">← Edit labs</a>
<a href="/start" class="btns">New patient</a>
</div></div></body></html>"""


# ═══════════════════════════════════════════════════════════════════
# JINJA RENDERER
# ═══════════════════════════════════════════════════════════════════
jinja = Environment()

def render(template, **kwargs):
    return jinja.from_string(template).render(**kwargs)


# ═══════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ═══════════════════════════════════════════════════════════════════
def save_report(er, report_text="", doctor_name="", approved=False):
    patient = er.get("patient", {})
    report_id = str(uuid.uuid4())
    patterns = json.dumps([p["id"] for p in er.get("patternsDetected", [])])
    lab_values = json.dumps({
        m["key"]: m["value"]
        for m in er.get("markerResults", {}).values()
        if m.get("value") is not None
    })
    try:
        conn = get_db()
        conn.run("""
            INSERT INTO reports
            (id, created_at, patient_name, age, gender, bmi, vegetarian,
             sa_risk_score, risk_category, patterns, lab_values,
             report_text, doctor_name, approved)
            VALUES (:id,:created_at,:patient_name,:age,:gender,:bmi,:vegetarian,
                    :sa_risk_score,:risk_category,:patterns,:lab_values,
                    :report_text,:doctor_name,:approved)
        """,
            id=report_id,
            created_at=datetime.utcnow().isoformat(),
            patient_name=patient.get("name", ""),
            age=patient.get("age"),
            gender=patient.get("gender"),
            bmi=patient.get("bmi"),
            vegetarian=patient.get("vegetarian", False),
            sa_risk_score=er.get("saRiskScore"),
            risk_category=er.get("riskCategory"),
            patterns=patterns,
            lab_values=lab_values,
            report_text=report_text,
            doctor_name=doctor_name,
            approved=approved
        )
        conn.close()
    except Exception as e:
        print(f"Save report error: {e}")
    return report_id

def get_recent_reports(limit=20):
    try:
        conn = get_db()
        rows = conn.run("""
            SELECT id, created_at, patient_name, age, gender, bmi,
                   sa_risk_score, risk_category, patterns, approved
            FROM reports
            ORDER BY created_at DESC
            LIMIT :limit
        """, limit=limit)
        cols = [c["name"] for c in conn.columns]
        conn.close()
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        print(f"Get reports error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════
# ROUTES

@app.route("/")
def landing():
    return render(LANDING_HTML)

@app.route("/waitlist", methods=["POST"])
def waitlist():
    try:
        data = request.get_json()
        email = data.get("email", "")
        conn = get_db()
        conn.run("""
            CREATE TABLE IF NOT EXISTS waitlist
            (id TEXT PRIMARY KEY, email TEXT, created_at TEXT)
        """)
        conn.run(
            "INSERT INTO waitlist (id, email, created_at) VALUES (:id, :email, :created_at)",
            id=str(uuid.uuid4()), email=email, created_at=datetime.utcnow().isoformat()
        )
        conn.close()
    except Exception as e:
        print(f"Waitlist error: {e}")
    return {"ok": True}
# ═══════════════════════════════════════════════════════════════════
@app.route("/start", methods=["GET", "POST"])
def intake():
    if request.method == "POST":
        h = float(request.form.get("height", 0)) / 100
        w = float(request.form.get("weight", 0))
        bmi = round(w / (h * h), 1) if h > 0 else 0
        patient = {
            "name":                    request.form.get("name"),
            "age":                     int(request.form.get("age", 0)),
            "gender":                  request.form.get("gender"),
            "bmi":                     bmi,
            "vegetarian":              "vegetarian"              in request.form,
            "family_cvd_before_60":    "family_cvd_before_60"    in request.form,
            "family_diabetes":         "family_diabetes"         in request.form,
            "confirmed_fasting":       "confirmed_fasting"       in request.form,
            "illness_or_vaccination_14d": "illness_or_vaccination_14d" in request.form,
            "biotin_within_72hrs":     "biotin_within_72hrs"     in request.form,
        }
        # Save patient to DB and get a patient_id to carry forward
        patient_id = str(uuid.uuid4())
        try:
            conn = get_db()
            conn.run("""
                INSERT INTO patients
                (id, created_at, name, age, gender, bmi, vegetarian,
                 family_cvd_before_60, family_diabetes,
                 confirmed_fasting, illness_or_vaccination_14d, biotin_within_72hrs)
                VALUES (:id,:created_at,:name,:age,:gender,:bmi,:vegetarian,
                        :family_cvd_before_60,:family_diabetes,
                        :confirmed_fasting,:illness_or_vaccination_14d,:biotin_within_72hrs)
            """,
                id=patient_id,
                created_at=datetime.utcnow().isoformat(),
                name=patient["name"],
                age=patient["age"],
                gender=patient["gender"],
                bmi=patient["bmi"],
                vegetarian=patient["vegetarian"],
                family_cvd_before_60=patient["family_cvd_before_60"],
                family_diabetes=patient["family_diabetes"],
                confirmed_fasting=patient["confirmed_fasting"],
                illness_or_vaccination_14d=patient["illness_or_vaccination_14d"],
                biotin_within_72hrs=patient["biotin_within_72hrs"]
            )
            conn.close()
        except Exception as e:
            print(f"Save patient error: {e}")
        return redirect(url_for("labs", patient_id=patient_id))
    return render(INTAKE_HTML)


@app.route("/labs/<patient_id>", methods=["GET", "POST"])
def labs(patient_id):
    # Load patient from DB
    try:
        conn = get_db()
        rows = conn.run("SELECT * FROM patients WHERE id=:id", id=patient_id)
        cols = [c["name"] for c in conn.columns]
        conn.close()
        if not rows:
            return redirect(url_for("intake"))
        patient = dict(zip(cols, rows[0]))
    except Exception as e:
        print(f"Load patient error: {e}")
        return redirect(url_for("intake"))

    if request.method == "POST":
        lab_results = {}
        for sec in LAB_FIELDS:
            for f in sec["fields"]:
                val = request.form.get(f["key"], "").strip()
                if val:
                    try: lab_results[f["key"]] = float(val)
                    except: pass

        if len(lab_results) < 5:
            return render(LABS_HTML, fields=LAB_FIELDS,
                          error="Please enter at least 5 lab values.",
                          labs={}, patient_id=patient_id)

        er = run_engine(patient, lab_results)

        if er["status"] == "HALTED":
            report_id = save_report(er, report_text="HALTED")
            return redirect(url_for("results", report_id=report_id))

        report_draft = call_claude(build_report_prompt(er))
        report_id = save_report(er, report_text=report_draft)
        return redirect(url_for("results", report_id=report_id))

    demo = request.args.get("demo") == "1"
    return render(LABS_HTML, fields=LAB_FIELDS, error=None,
                  labs=DEMO if demo else {}, patient_id=patient_id)


@app.route("/results/<report_id>", methods=["GET", "POST"])
def results(report_id):
    if request.method == "POST":
        report_text = request.form.get("report_text", "")
        doctor_name = request.form.get("doctor_name", "")
        try:
            conn = get_db()
            conn.run("""
                UPDATE reports
                SET report_text=:report_text, doctor_name=:doctor_name, approved=TRUE
                WHERE id=:id
            """, report_text=report_text, doctor_name=doctor_name, id=report_id)
            conn.close()
        except Exception as e:
            print(f"Approval error: {e}")
        return redirect(url_for("patient_report", report_id=report_id))

    # GET — load report from DB for doctor review
    try:
        conn = get_db()
        rows = conn.run("SELECT * FROM reports WHERE id=:id", id=report_id)
        cols = [c["name"] for c in conn.columns]
        conn.close()
        if not rows:
            return redirect(url_for("intake"))
        row = dict(zip(cols, rows[0]))
    except Exception as e:
        print(f"Load report error: {e}")
        return redirect(url_for("intake"))

    er = {
        "status": "HALTED" if row.get("report_text") == "HALTED" else "OK",
        "saRiskScore": row.get("sa_risk_score", 0),
        "riskCategory": row.get("risk_category", "moderate"),
        "riskCategoryLabel": {"low": "Low risk", "moderate": "Moderate risk", "high": "High risk", "very_high": "Very high risk"}.get(row.get("risk_category", "moderate"), ""),
        "criticalValues": [],
        "patternsDetected": [],
        "highRiskMarkers": [],
        "borderlineMarkers": [],
        "domainScores": {},
        "summary": {"totalTested": 0, "totalOptimal": 0, "totalBorderline": 0, "totalHighRisk": 0, "totalExcluded": 0, "totalNotTested": 0, "patternsCount": 0},
        "patient": {
            "name": row.get("patient_name"),
            "age": row.get("age"),
            "gender": row.get("gender"),
            "bmi": row.get("bmi"),
            "bmiCategory": "Normal",
            "vegetarian": row.get("vegetarian"),
        }
    }

    # Rebuild markers from stored lab values
    lab_values = json.loads(row.get("lab_values", "{}"))
    mr = {}
    for key, value in lab_values.items():
        if key in MARKERS and value is not None:
            defn = MARKERS[key]
            status = _score_marker(value, defn)
            entry = {"key": key, "name": defn["name"], "value": value, "unit": defn["unit"],
                     "domain": defn["domain"], "weight": defn["weight"],
                     "status": status, "excluded": False,
                     "rawScore": 2 if status=="high" else 1 if status=="borderline" else 0}
            mr[key] = entry
            if status == "high":
                er["highRiskMarkers"].append(entry)
            elif status == "borderline":
                er["borderlineMarkers"].append(entry)

    er["summary"]["totalHighRisk"] = len(er["highRiskMarkers"])
    er["summary"]["totalBorderline"] = len(er["borderlineMarkers"])
    er["summary"]["totalOptimal"] = sum(1 for m in mr.values() if m["status"]=="optimal")
    er["summary"]["totalTested"] = len(mr)

    approved = None
    if row.get("approved") and row.get("doctor_name"):
        approved = {"doctor": row.get("doctor_name"), "report": row.get("report_text")}

    return render(RESULTS_HTML,
                  result=er,
                  report_draft=row.get("report_text", ""),
                  approved=approved,
                  report_id=report_id)




PATIENT_REPORT_HTML = """<!DOCTYPE html><html><head><title>Symbiosis Health</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=DM+Serif+Display&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{background:#F5F4F1;font-family:'Inter',sans-serif;color:#1a1a1a;font-size:13px}
.page{max-width:860px;margin:0 auto;padding:24px 16px 48px}
.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}
.brand{font-family:'DM Serif Display',serif;font-size:18px;color:#1a1a1a}.brand span{color:#2D6A4F}
.meta{font-size:12px;color:#9ca3af}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.card{background:#fff;border-radius:14px;border:1px solid #ECEAE6;padding:16px}
.lbl{font-size:10px;font-weight:600;color:#9ca3af;text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px}
.score-hero{display:flex;align-items:center;gap:20px}
.score-ring{width:88px;height:88px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;border:5px solid currentColor;flex-shrink:0}
.score-n{font-family:'DM Serif Display',serif;font-size:32px;line-height:1}
.score-d{font-size:9px;color:#9ca3af;margin-top:1px}
.risk-badge{display:inline-flex;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;margin-top:8px}
.stat-mini{text-align:center}
.stat-n{font-size:22px;font-weight:600;line-height:1;margin-bottom:3px}
.stat-l{font-size:11px;color:#9ca3af}
.drow{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.drow:last-child{margin-bottom:0}
.dlbl{font-size:12px;color:#6b7280;width:140px;flex-shrink:0}
.dtrack{flex:1;height:5px;background:#F0EEE9;border-radius:3px;overflow:hidden}
.dfill{height:5px;border-radius:3px}
.dpct{font-size:11px;font-weight:600;width:28px;text-align:right}
.mrow{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid #F5F4F1}
.mrow:last-child{border-bottom:none}
.mname{font-size:12px;font-weight:500}
.mval{font-size:12px;font-weight:600}
.chip{font-size:9px;font-weight:700;padding:2px 7px;border-radius:10px}
.chip-h{background:#fef2f2;color:#b91c1c}
.chip-b{background:#fffbeb;color:#92400e}
.pdot{width:7px;height:7px;border-radius:50%;flex-shrink:0;margin-top:3px}
.pname{font-size:12px;font-weight:600;margin-bottom:2px}
.pev{font-size:11px;color:#6b7280;line-height:1.5}
.rhead{font-size:11px;font-weight:600;color:#2D6A4F;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;margin-top:14px}
.rhead:first-child{margin-top:0}
.ritem{display:flex;gap:8px;align-items:flex-start;margin-bottom:5px}
.rbullet{width:4px;height:4px;border-radius:50%;background:#2D6A4F;margin-top:5px;flex-shrink:0}
.rtext{font-size:12px;color:#374151;line-height:1.6}
.intro{font-size:13px;color:#374151;line-height:1.8;margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid #F0EEE9}
.next-bar{background:#F0F7F4;border-radius:12px;padding:14px 18px;display:flex;align-items:center;justify-content:space-between;gap:16px;margin-top:12px}
.next-btn{background:#2D6A4F;color:#fff;border:none;border-radius:8px;padding:8px 16px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit}
</style></head>
<body><div class="page">
<div class="topbar">
  <div class="brand">Symbiosis <span>Health</span></div>
  <div class="meta">{{ patient_name }} &nbsp;·&nbsp; {{ doctor_name }}</div>
</div>
{% set cc="#166534" if risk_category=="low" else "#92400e" if risk_category=="moderate" else "#b91c1c" %}
{% set bg="#f0fdf4" if risk_category=="low" else "#fffbeb" if risk_category=="moderate" else "#fef2f2" %}
<div class="grid2">
  <div class="card">
    <div class="lbl">SA Risk Score</div>
    <div class="score-hero">
      <div class="score-ring" style="color:{{ cc }};background:{{ bg }}">
        <div class="score-n" style="color:{{ cc }}">{{ sa_risk_score }}</div>
        <div class="score-d">/ 100</div>
      </div>
      <div>
        <div style="font-size:15px;font-weight:600;color:{{ cc }}">{{ risk_category_label }}</div>
        <div style="font-size:11px;color:#9ca3af;margin-top:4px;line-height:1.5">Scored against SA-specific thresholds from MASALA, INTERHEART & JACC 2023</div>
        <div class="risk-badge" style="color:{{ cc }};background:{{ bg }}">{{ risk_category_label }}</div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="lbl">Marker summary</div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px">
      <div class="stat-mini"><div class="stat-n" style="color:#b91c1c">{{ high_count }}</div><div class="stat-l">High risk</div></div>
      <div class="stat-mini"><div class="stat-n" style="color:#92400e">{{ borderline_count }}</div><div class="stat-l">Borderline</div></div>
      <div class="stat-mini"><div class="stat-n" style="color:#166534">{{ optimal_count }}</div><div class="stat-l">Optimal</div></div>
    </div>
    {% if domain_bars %}
    <div class="lbl">By domain</div>
    {% for d in domain_bars %}
    {% set c="#166534" if d.pct<30 else "#92400e" if d.pct<60 else "#b91c1c" %}
    <div class="drow">
      <div class="dlbl">{{ d.label }}</div>
      <div class="dtrack"><div class="dfill" style="width:{{ d.pct }}%;background:{{ c }}"></div></div>
      <div class="dpct" style="color:{{ c }}">{{ d.pct }}</div>
    </div>
    {% endfor %}
    {% endif %}
  </div>
</div>
<div class="grid2">
  {% if high_markers %}
  <div class="card">
    <div class="lbl">High risk ({{ high_count }})</div>
    {% for m in high_markers %}
    <div class="mrow">
      <div class="mname">{{ m.name }}</div>
      <div style="display:flex;align-items:center;gap:8px">
        <div class="mval" style="color:#b91c1c">{{ m.value }} {{ m.unit }}</div>
        <div class="chip chip-h">HIGH</div>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
  <div>
    {% if borderline_markers %}
    <div class="card" style="margin-bottom:12px">
      <div class="lbl">Borderline ({{ borderline_count }})</div>
      {% for m in borderline_markers %}
      <div class="mrow">
        <div class="mname">{{ m.name }}</div>
        <div style="display:flex;align-items:center;gap:8px">
          <div class="mval" style="color:#92400e">{{ m.value }} {{ m.unit }}</div>
          <div class="chip chip-b">BORDERLINE</div>
        </div>
      </div>
      {% endfor %}
    </div>
    {% endif %}
    {% if patterns %}
    <div class="card">
      <div class="lbl">Patterns detected</div>
      {% for pt in patterns %}
      <div style="display:flex;gap:10px;align-items:flex-start;padding:7px 0;border-bottom:1px solid #F5F4F1">
        <div class="pdot" style="background:{{ '#b91c1c' if pt.severity=='high' else '#f59e0b' }}"></div>
        <div><div class="pname">{{ pt.name }}</div></div>
      </div>
      {% endfor %}
    </div>
    {% endif %}
  </div>
</div>
{% if summary_paras or recommendations %}
<div class="card" style="margin-bottom:12px">
  {% if summary_paras %}
  <div class="intro">{{ summary_paras[0] }}</div>
  {% endif %}
  {% if recommendations %}
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
    {% for section in recommendations %}
    <div>
      <div class="rhead">{{ section.title }}</div>
      {% for item in section.bullets[:4] %}
      <div class="ritem"><div class="rbullet"></div><div class="rtext">{{ item }}</div></div>
      {% endfor %}
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>
{% endif %}
<div class="next-bar">
  <div>
    <div style="font-size:13px;font-weight:600;color:#1a1a1a">Schedule your next panel</div>
    <div style="font-size:11px;color:#6b7280;margin-top:2px">Retest metabolic markers in 3 months &nbsp;·&nbsp; Full panel in 6 months</div>
  </div>
  <button class="next-btn" onclick="alert('Booking coming soon!')">Book now →</button>
</div>
</div></body></html>"""


@app.route("/report/<report_id>")
def patient_report(report_id):
    try:
        conn = get_db()
        rows = conn.run("SELECT * FROM reports WHERE id=:id", id=report_id)
        cols = [c["name"] for c in conn.columns]
        conn.close()
        if not rows:
            return redirect(url_for("intake"))
        row = dict(zip(cols, rows[0]))
    except Exception as e:
        print(f"Patient report error: {e}")
        return redirect(url_for("intake"))

    lab_values = json.loads(row.get("lab_values", "{}"))
    patterns_raw = json.loads(row.get("patterns", "[]"))

    high_markers, borderline_markers = [], []
    for key, value in lab_values.items():
        if key in MARKERS and value is not None:
            defn = MARKERS[key]
            status = _score_marker(value, defn)
            entry = {"name": defn["name"], "value": value, "unit": defn["unit"]}
            if status == "high":
                high_markers.append(entry)
            elif status == "borderline":
                borderline_markers.append(entry)

    # Domain bars
    domain_bars = []
    for dk, ddef in DOMAINS.items():
        dm = [(k,v) for k,v in lab_values.items() if k in MARKERS and MARKERS[k]["domain"]==dk and v is not None]
        if dm:
            scored = [_score_marker(v, MARKERS[k]) for k,v in dm]
            raw = sum(2 if s=="high" else 1 if s=="borderline" else 0 for s in scored)
            mx = len(scored) * 2
            pct = round((raw/mx)*100) if mx else 0
            domain_bars.append({"label": ddef["label"], "pct": pct})

    # Patterns
    pattern_names = {
        "ir": "Insulin Resistance Cluster",
        "cvd": "Cardiovascular Risk Cluster",
        "b12": "B12-Folate-Homocysteine Alert",
        "nafld": "SA Lean NAFLD Pattern",
        "thyroid": "Thyroid-Metabolic Pattern",
        "anaemia": "Nutritional Anaemia Pattern",
        "kidney": "Early Kidney Stress Pattern",
    }
    patterns = [{"name": pattern_names.get(p, p), "severity": "high", "evidence": ""} for p in patterns_raw]

    cat = row.get("risk_category", "moderate")
    score = row.get("sa_risk_score", 0)
    cat_label = {"low": "Low risk", "moderate": "Moderate risk", "high": "High risk", "very_high": "Very high risk"}.get(cat, "")

    report_text = row.get("report_text", "")

    # Parse recommendations
    recommendations = []
    section_map = {"Food": "Food", "Movement": "Movement", "Supplements": "Supplements", "Who to see": "Who to see"}
    current_section, current_items = None, []
    for line in report_text.split("\n"):
        line = line.strip()
        for key, label in section_map.items():
            if key in line and "###" in line:
                if current_section and current_items:
                    recommendations.append({"title": current_section, "bullets": current_items})
                current_section = label
                current_items = []
                break
        else:
            if current_section and line and not line.startswith("#") and len(line) > 20:
                line = line.lstrip("- •*").strip()
                if line:
                    current_items.append(line)
    if current_section and current_items:
        recommendations.append({"title": current_section, "bullets": current_items})

    # Summary paragraphs
    summary_paras = []
    for line in report_text.split("\n"):
        line = line.strip()
        if line.startswith("###"):
            break
        if line.startswith("##"):
            continue
        if line and len(line) > 30:
            summary_paras.append(line)

    optimal_count = sum(
        1 for k,v in lab_values.items()
        if k in MARKERS and v is not None and _score_marker(v, MARKERS[k]) == "optimal"
    )

    return render(PATIENT_REPORT_HTML,
        patient_name=row.get("patient_name", "Patient"),
        doctor_name=row.get("doctor_name", "Symbiosis Health"),
        sa_risk_score=score,
        risk_category=cat,
        risk_category_label=cat_label,
        high_count=len(high_markers),
        borderline_count=len(borderline_markers),
        optimal_count=optimal_count,
        domain_bars=domain_bars,
        high_markers=high_markers,
        borderline_markers=borderline_markers,
        patterns=patterns,
        recommendations=recommendations,
        summary_paras=summary_paras[:4],
        report_id=report_id,
    )


@app.route("/admin")
def admin():
    reports = get_recent_reports(50)
    waitlist_emails = []
    try:
        conn = get_db()
        rows_w = conn.run("SELECT email, created_at FROM waitlist ORDER BY created_at DESC LIMIT 100")
        cols_w = [c["name"] for c in conn.columns]
        conn.close()
        waitlist_emails = [dict(zip(cols_w, r)) for r in rows_w]
    except Exception as e:
        print(f"Waitlist fetch error: {e}")

    report_rows = ""
    for r in reports:
        patterns = ", ".join(json.loads(r["patterns"])) if r.get("patterns") else "none"
        approved = "✓" if r["approved"] else "pending"
        score = r.get("sa_risk_score", 0) or 0
        col = "#b91c1c" if score >= 75 else "#92400e" if score >= 50 else "#166534"
        report_rows += f"""<tr>
            <td>{r['created_at'][:10]}</td>
            <td>{r.get('patient_name') or '—'}</td>
            <td>{r.get('age') or '—'}</td>
            <td>{r.get('gender') or '—'}</td>
            <td>{r.get('bmi') or '—'}</td>
            <td style="font-weight:600;color:{col}">{score}</td>
            <td>{r.get('risk_category') or '—'}</td>
            <td style="font-size:11px">{patterns}</td>
            <td><a href="/report/{r['id']}" style="color:#2D6A4F">{approved}</a></td>
        </tr>"""

    waitlist_rows = ""
    for w in waitlist_emails:
        waitlist_rows += f"<tr><td>{w['created_at'][:10]}</td><td>{w['email']}</td></tr>"

    return f"""<!DOCTYPE html><html><head><title>Symbiosis Admin</title>
    <style>
    body{{font-family:'DM Sans',sans-serif;padding:32px;background:#F7F5F2;color:#1a1a1a}}
    h1{{font-size:20px;margin-bottom:4px}}
    h2{{font-size:15px;margin:28px 0 10px;color:#1a1a1a}}
    table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #E8E4DF;margin-bottom:8px}}
    th{{font-size:11px;font-weight:600;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em;padding:10px 14px;text-align:left;border-bottom:1px solid #E8E4DF;background:#fafaf9}}
    td{{font-size:13px;padding:10px 14px;border-bottom:1px solid #F3F0EC}}
    tr:last-child td{{border-bottom:none}}
    .badge{{background:#F0F7F4;color:#2D6A4F;font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;margin-left:8px}}
    </style></head>
    <body>
    <h1>Symbiosis Health — Admin</h1>
    <h2>Waitlist <span class="badge">{len(waitlist_emails)} signups</span></h2>
    <table><tr><th>Date</th><th>Email</th></tr>
    {waitlist_rows or '<tr><td colspan="2" style="text-align:center;color:#9ca3af;padding:20px">No signups yet</td></tr>'}
    </table>
    <h2>Reports <span class="badge">{len(reports)} total</span></h2>
    <table><tr>
        <th>Date</th><th>Patient</th><th>Age</th><th>Gender</th>
        <th>BMI</th><th>Score</th><th>Category</th><th>Patterns</th><th>Status</th>
    </tr>
    {report_rows or '<tr><td colspan="9" style="text-align:center;color:#9ca3af;padding:20px">No reports yet</td></tr>'}
    </table>
    <br><a href="/start" style="font-size:13px;color:#2D6A4F">← New patient</a>
    </body></html>"""

@app.route("/new")
def new_patient():
    return redirect(url_for("intake"))
# ═══════════════════════════════════════════════════════════════════
# LAUNCH
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

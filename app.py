import os, requests, uuid, json
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
# BIOLOGICAL AGE ENGINE — SA-calibrated (v2)
# ═══════════════════════════════════════════════════════════════════
from dataclasses import dataclass
from typing import Optional

@dataclass
class BioMarker:
    key: str
    name: str
    unit: str
    weight: int
    domain: str

    def is_optimal(self, v: float) -> bool:   return False
    def is_high_risk(self, v: float) -> bool: return False

    def status(self, v: float) -> str:
        if self.is_optimal(v):   return "optimal"
        if self.is_high_risk(v): return "high_risk"
        return "borderline"

    def score(self, v: float) -> int:
        s = self.status(v)
        if s == "optimal":    return 0
        if s == "borderline": return 1 * self.weight
        if s == "high_risk":  return 2 * self.weight
        return 0

    def max_score(self) -> int:
        return 2 * self.weight

def _bm(key, name, unit, weight, domain, optimal_fn, high_risk_fn):
    m = BioMarker(key=key, name=name, unit=unit, weight=weight, domain=domain)
    m.is_optimal   = lambda v, f=optimal_fn:   f(v)
    m.is_high_risk = lambda v, f=high_risk_fn: f(v)
    return m

BIO_MARKERS = [
    # CVD
    _bm("lpa",            "Lipoprotein(a)",      "nmol/L", 3, "cvd",    lambda v: v < 30,            lambda v: v > 75),
    _bm("apob",           "ApoB",                "mg/dL",  3, "cvd",    lambda v: v < 80,            lambda v: v > 100),
    _bm("apoa1",          "ApoA1",               "mg/dL",  3, "cvd",    lambda v: v > 120,           lambda v: v < 100),
    _bm("ldl",            "LDL Cholesterol",     "mg/dL",  2, "cvd",    lambda v: v < 100,           lambda v: v >= 130),
    _bm("hdl",            "HDL Cholesterol",     "mg/dL",  2, "cvd",    lambda v: v > 50,            lambda v: v < 40),
    _bm("triglycerides",  "Triglycerides",       "mg/dL",  2, "cvd",    lambda v: v < 100,           lambda v: v > 150),
    _bm("totalCholesterol","Total Cholesterol",  "mg/dL",  1, "cvd",    lambda v: v < 200,           lambda v: v >= 240),
    _bm("nonHdl",         "Non-HDL Cholesterol", "mg/dL",  2, "cvd",    lambda v: v < 130,           lambda v: v >= 160),
    _bm("hsCrp",          "hs-CRP",              "mg/L",   3, "cvd",    lambda v: v < 1.0,           lambda v: v > 2.0),
    _bm("homocysteine",   "Homocysteine",        "µmol/L", 3, "cvd",    lambda v: v < 8,             lambda v: v > 12),
    # Metabolic
    _bm("lpir",           "LP-IR Score",         "0-100",  3, "metabolic", lambda v: v < 45,         lambda v: v > 60),
    _bm("fastingInsulin", "Fasting Insulin",     "µIU/mL", 3, "metabolic", lambda v: v < 8,          lambda v: v > 12),
    _bm("hba1c",          "HbA1c",               "%",      3, "metabolic", lambda v: v < 5.4,        lambda v: v > 5.7),
    _bm("fastingGlucose", "Fasting Glucose",     "mg/dL",  2, "metabolic", lambda v: v < 90,         lambda v: v >= 100),
    _bm("homaIr",         "HOMA-IR",             "ratio",  2, "metabolic", lambda v: v < 1.5,        lambda v: v >= 2.5),
    _bm("uricAcid",       "Uric Acid",           "mg/dL",  2, "metabolic", lambda v: v < 6.0,        lambda v: v > 7.0),
    _bm("cPeptide",       "C-Peptide",           "ng/mL",  2, "metabolic", lambda v: 0.5<=v<=2.0,    lambda v: v > 3.0 or v < 0.5),
    _bm("tgHdlRatio",     "TG/HDL Ratio",        "ratio",  2, "metabolic", lambda v: v < 2.0,        lambda v: v > 3.0),
    # Liver & Kidney
    _bm("alt",            "ALT",                 "U/L",    3, "liver",  lambda v: v < 25,            lambda v: v > 40),
    _bm("ast",            "AST",                 "U/L",    2, "liver",  lambda v: v < 25,            lambda v: v > 40),
    _bm("ggt",            "GGT",                 "U/L",    2, "liver",  lambda v: v < 30,            lambda v: v > 50),
    _bm("creatinine",     "Creatinine",          "mg/dL",  2, "liver",  lambda v: 0.6<=v<=1.0,       lambda v: v > 1.2),
    _bm("egfr",           "eGFR",                "mL/min", 3, "liver",  lambda v: v > 90,            lambda v: v < 60),
    _bm("albumin",        "Albumin",             "g/dL",   2, "liver",  lambda v: 4.0<=v<=5.0,       lambda v: v < 3.5),
    _bm("urineAcr",       "Urine ACR",           "mg/g",   3, "liver",  lambda v: v < 10,            lambda v: v > 30),
    # Thyroid
    _bm("tsh",            "TSH",                 "mIU/L",  3, "thyroid",lambda v: 1.0<=v<=2.5,       lambda v: v >= 4.0 or v < 1.0),
    _bm("freeT3",         "Free T3",             "pg/mL",  2, "thyroid",lambda v: 3.0<=v<=4.2,       lambda v: v < 2.3),
    _bm("freeT4",         "Free T4",             "ng/dL",  2, "thyroid",lambda v: 1.0<=v<=1.6,       lambda v: v < 0.8),
    # Nutritional
    _bm("vitaminD",       "Vitamin D",           "ng/mL",  3, "nutritional", lambda v: v >= 40,      lambda v: v < 30),
    _bm("vitaminB12",     "Vitamin B12",         "pg/mL",  3, "nutritional", lambda v: v >= 400,     lambda v: v < 200),
    _bm("folate",         "Folate",              "ng/mL",  2, "nutritional", lambda v: v > 4.0,      lambda v: v < 2.0),
    _bm("ferritin",       "Ferritin",            "ng/mL",  2, "nutritional", lambda v: 50<=v<=150,   lambda v: v < 20 or v > 200),
    _bm("iron",           "Iron",                "µg/dL",  1, "nutritional", lambda v: 60<=v<=170,   lambda v: v < 40),
    _bm("magnesium",      "Magnesium",           "mg/dL",  2, "nutritional", lambda v: 2.0<=v<=2.5,  lambda v: v < 1.7),
    _bm("zinc",           "Zinc",                "µg/dL",  1, "nutritional", lambda v: v >= 80,      lambda v: v < 60),
    # Blood
    _bm("hemoglobin",     "Hemoglobin",          "g/dL",   2, "blood",  lambda v: v >= 13.0,         lambda v: v < 11.0),
    _bm("hematocrit",     "Hematocrit",          "%",      1, "blood",  lambda v: v > 40,            lambda v: v < 36),
    _bm("wbc",            "WBC",                 "×10³/µL",1, "blood",  lambda v: 4.0<=v<=10.5,      lambda v: v < 3.0 or v > 11.0),
    _bm("platelets",      "Platelets",           "×10³/µL",1, "blood",  lambda v: 150<=v<=400,       lambda v: v < 100),
    _bm("esr",            "ESR",                 "mm/hr",  2, "blood",  lambda v: v < 20,            lambda v: v > 40),
]

BIO_MARKER_MAP = {m.key: m for m in BIO_MARKERS}

BIO_DOMAINS = {
    "cvd":         {"weight": 0.30, "label": "Heart",        "base_offset": -8,  "max_penalty": 24},
    "metabolic":   {"weight": 0.28, "label": "Metabolic",    "base_offset": -6,  "max_penalty": 20},
    "liver":       {"weight": 0.18, "label": "Liver & Kidney","base_offset": -4, "max_penalty": 14},
    "thyroid":     {"weight": 0.10, "label": "Thyroid",      "base_offset": -3,  "max_penalty": 10},
    "nutritional": {"weight": 0.08, "label": "Nutritional",  "base_offset": -3,  "max_penalty": 10},
    "blood":       {"weight": 0.06, "label": "Blood",        "base_offset": -2,  "max_penalty": 8},
}

def _interpret_delta(delta):
    if delta is None:  return "Insufficient data"
    if delta <= -5:    return "Excellent — well below your chronological age"
    if delta <= 0:     return "Good — at or below your chronological age"
    if delta <= 5:     return "Moderate — some areas need attention"
    if delta <= 10:    return "Elevated — meaningful biological ageing acceleration"
    return "High — significant biological ageing acceleration"

def compute_biological_age(chronological_age, lab_values, gender="female"):
    chron = chronological_age
    marker_details = {}
    for key, value in lab_values.items():
        if key not in BIO_MARKER_MAP or value is None:
            continue
        m = BIO_MARKER_MAP[key]
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        marker_details[key] = {
            "name": m.name, "value": v, "unit": m.unit,
            "status": m.status(v), "score": m.score(v),
            "max_score": m.max_score(), "weight": m.weight, "domain": m.domain,
        }

    domain_scores = {}
    for domain_id, cfg in BIO_DOMAINS.items():
        domain_markers = [m for m in BIO_MARKERS if m.domain == domain_id]
        entered = [m for m in domain_markers if m.key in marker_details]
        if not entered:
            domain_scores[domain_id] = {"domain_age": None, "label": cfg["label"],
                                         "markers_entered": 0, "markers_total": len(domain_markers),
                                         "penalty_ratio": None}
            continue
        total_score = sum(marker_details[m.key]["score"] for m in entered)
        total_max   = sum(marker_details[m.key]["max_score"] for m in entered)
        penalty_ratio = total_score / total_max if total_max > 0 else 0.0
        domain_age = round(chron + cfg["base_offset"] + penalty_ratio * cfg["max_penalty"])
        domain_scores[domain_id] = {
            "domain_age": domain_age, "label": cfg["label"],
            "markers_entered": len(entered), "markers_total": len(domain_markers),
            "penalty_ratio": round(penalty_ratio, 3),
        }

    weighted_sum = weighted_total = 0.0
    for domain_id, cfg in BIO_DOMAINS.items():
        d = domain_scores[domain_id]
        if d["domain_age"] is not None:
            weighted_sum   += d["domain_age"] * cfg["weight"]
            weighted_total += cfg["weight"]

    biological_age = round(weighted_sum / weighted_total) if weighted_total > 0 else chron
    delta = biological_age - chron

    # Top gains — high risk weight-3 markers
    top_gains = []
    gain_labels = {
        "lpa": "Manage Lp(a) CVD risk factors", "apob": "Lower ApoB below 80",
        "hsCrp": "Reduce inflammation (hs-CRP)", "homocysteine": "Lower homocysteine below 8",
        "lpir": "Improve LP-IR score below 45", "fastingInsulin": "Lower fasting insulin below 8",
        "hba1c": "Bring HbA1c below 5.4%", "fastingGlucose": "Bring fasting glucose below 90",
        "homaIr": "Reduce insulin resistance", "vitaminD": "Optimise Vitamin D above 40",
        "vitaminB12": "Optimise B12 above 400", "alt": "Reduce liver stress (ALT)",
        "egfr": "Protect kidney function (eGFR)", "tsh": "Optimise thyroid (TSH 1-2.5)",
        "triglycerides": "Lower triglycerides below 100", "hdl": "Raise HDL above 50",
    }
    for key, detail in sorted(marker_details.items(), key=lambda x: -x[1]["score"]):
        if detail["status"] != "optimal" and key in gain_labels:
            domain = detail["domain"]
            cfg = BIO_DOMAINS.get(domain, {})
            penalty = detail["score"] / detail["max_score"] if detail["max_score"] else 0
            yrs = round(penalty * cfg.get("max_penalty", 10) * cfg.get("weight", 0.1), 1)
            if yrs > 0:
                top_gains.append({"key": key, "label": detail["name"],
                                   "gain_text": gain_labels.get(key, detail["name"]),
                                   "age_add": yrs, "domain": domain})
        if len(top_gains) >= 5:
            break

    return {
        "biological_age": biological_age,
        "chronological_age": chron,
        "delta": delta,
        "interpretation": _interpret_delta(delta),
        "sub_ages": {d: domain_scores[d]["domain_age"] or chron for d in ["cvd", "metabolic", "liver"]},
        "sub_labels": {"cvd": "Heart age", "metabolic": "Metabolic age", "liver": "Organ age"},
        "domain_scores": domain_scores,
        "top_gains": top_gains,
        "marker_details": marker_details,
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

    # Compute biological age for prompt context
    from collections import defaultdict
    lab_vals_for_bio = {m["key"]: m["value"] for m in high + bord if m.get("value") is not None}
    bio = compute_biological_age(p.get("age", 30), lab_vals_for_bio, p.get("gender", "female"))
    bio_age_str = f"Biological age: {bio['biological_age']} (chronological: {bio['chronological_age']}, delta: +{bio['delta']} years)"
    bio_sub_str = f"Metabolic age: {bio['sub_ages']['metabolic']} | Heart age: {bio['sub_ages']['heart']} | Inflammation age: {bio['sub_ages']['inflammation']}"

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
{bio_age_str}
{bio_sub_str}
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

Write exactly these four sections. Keep it concise and action-focused — this feeds into a visual dashboard.

## What's aging you faster
2-3 sentences only. Tell them their biological age is older than their chronological age and what's primarily driving it. Reference the top 1-2 flagged patterns. Warm and direct — not alarming.
{'When discussing Lp(a): be clear it is genetic — not their fault and not changeable through lifestyle.' if lpa_high else ''}

### Food
Frame every recommendation as: "to reduce your biological age, change X."
{'This patient is vegetarian. Every recommendation must be vegetarian — no meat, fish or poultry.' if veg else ''}
Be specific to South Asian eating. Name actual foods. Address white rice, roti, dal, chai sugar, cooking oils, late dinners.
3-5 bullet points maximum. Each one tied directly to a flagged marker.

### Movement
Frame as: "these changes will directly lower your biological age."
Home-based only. No gym assumed.
Lead with post-meal walks (10 min reduces postprandial glucose 30% in SA) and resistance training (muscle is primary glucose disposal site).
{'Emphasise urgency — confirmed insulin resistance cluster.' if ir_pattern else ''}
3-4 bullet points maximum.

### Supplements
Only what evidence supports for their specific flagged markers.
Format: supplement — dose — why it lowers your biological age specifically.
{'Use methylcobalamin not cyanocobalamin. Algae-based omega-3. Lichen-based D3.' if veg else ''}
3-5 bullet points maximum.

### Who to see
One line per specialist. Only include what's relevant to their flagged patterns.
{'- Cardiologist: Lp(a) and CVD risk' if (lpa_high or cvd_pattern) else ''}
{'- Endocrinologist: insulin resistance cluster' if ir_pattern else ''}
{'- Hepatologist: if liver enzymes persist after 12 weeks' if nafld_pattern else ''}
{'- GP: thyroid panel review' if thyroid_pat else ''}
{'- Nephrologist: repeat eGFR in 3 months' if kidney_pat else ''}

STYLE: Second person throughout. Warm, direct, intelligent. Every recommendation tied to biological age reduction. Name actual SA foods. Reference actual numbers. Never diagnose. Never name prescription drugs."""


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

@app.route("/", methods=["GET", "POST"])
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
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=DM+Serif+Display&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#F5F4F1;font-family:'Inter',sans-serif;color:#1a1a1a;font-size:13px}
.page{max-width:900px;margin:0 auto;padding:24px 16px 48px}
.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}
.brand{font-family:'DM Serif Display',serif;font-size:18px;color:#1a1a1a}.brand span{color:#2D6A4F}
.meta{font-size:12px;color:#9ca3af}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px}
.card{background:#fff;border-radius:14px;border:1px solid #ECEAE6;padding:18px}
.lbl{font-size:10px;font-weight:600;color:#9ca3af;text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px}
/* Bio age hero */
.bio-hero{background:linear-gradient(135deg,#1C4A3A 0%,#2D6A4F 100%);border-radius:16px;padding:24px;margin-bottom:12px;color:#fff}
.bio-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
.bio-ages{display:flex;align-items:flex-end;gap:24px}
.bio-age-num{font-family:'DM Serif Display',serif;font-size:72px;line-height:1;color:#fff}
.bio-age-lbl{font-size:11px;color:rgba(255,255,255,.6);margin-bottom:8px}
.bio-vs{font-size:13px;color:rgba(255,255,255,.5);margin-bottom:4px;padding-bottom:8px}
.bio-chrono{font-family:'DM Serif Display',serif;font-size:36px;color:rgba(255,255,255,.5);line-height:1}
.bio-delta{background:rgba(255,255,255,.15);border-radius:10px;padding:8px 14px;text-align:center}
.bio-delta-num{font-family:'DM Serif Display',serif;font-size:28px;color:#fff;line-height:1}
.bio-delta-lbl{font-size:10px;color:rgba(255,255,255,.6);margin-top:2px}
.bio-sub{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.bio-sub-card{background:rgba(255,255,255,.1);border-radius:10px;padding:12px;text-align:center}
.bio-sub-num{font-size:22px;font-weight:600;color:#fff;line-height:1;margin-bottom:4px}
.bio-sub-lbl{font-size:10px;color:rgba(255,255,255,.55)}
/* Gains */
.gain-item{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid #F5F4F1}
.gain-item:last-child{border-bottom:none}
.gain-text{font-size:12px;color:#374151;flex:1}
.gain-badge{background:#F0F7F4;color:#2D6A4F;font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px;white-space:nowrap;margin-left:10px}
/* Score ring */
.score-hero{display:flex;align-items:center;gap:20px}
.score-ring{width:80px;height:80px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;border:5px solid currentColor;flex-shrink:0}
.score-n{font-family:'DM Serif Display',serif;font-size:28px;line-height:1}
.score-d{font-size:9px;color:#9ca3af;margin-top:1px}
.risk-badge{display:inline-flex;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;margin-top:6px}
.stat-mini{text-align:center}
.stat-n{font-size:20px;font-weight:600;line-height:1;margin-bottom:3px}
.stat-l{font-size:11px;color:#9ca3af}
.drow{display:flex;align-items:center;gap:10px;margin-bottom:7px}
.drow:last-child{margin-bottom:0}
.dlbl{font-size:12px;color:#6b7280;width:130px;flex-shrink:0}
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
{% set delta=bio_age.delta %}

<!-- BIOLOGICAL AGE HERO -->
<div class="bio-hero">
  <div class="bio-top">
    <div class="bio-ages">
      <div>
        <div class="bio-age-lbl">Biological age</div>
        <div class="bio-age-num">{{ bio_age.biological_age }}</div>
      </div>
      <div style="padding-bottom:10px">
        <div class="bio-vs">vs chronological</div>
        <div class="bio-chrono">{{ bio_age.chronological_age }}</div>
      </div>
    </div>
    <div class="bio-delta">
      <div class="bio-delta-num">+{{ delta if delta > 0 else delta }}</div>
      <div class="bio-delta-lbl">{{ "years older" if delta > 0 else "years younger" }}</div>
    </div>
  </div>
  <div class="bio-sub">
    <div class="bio-sub-card">
      <div class="bio-sub-num">{{ bio_age.sub_ages.cvd }}</div>
      <div class="bio-sub-lbl">Heart age</div>
    </div>
    <div class="bio-sub-card">
      <div class="bio-sub-num">{{ bio_age.sub_ages.metabolic }}</div>
      <div class="bio-sub-lbl">Metabolic age</div>
    </div>
    <div class="bio-sub-card">
      <div class="bio-sub-num">{{ bio_age.sub_ages.liver }}</div>
      <div class="bio-sub-lbl">Organ age</div>
    </div>
  </div>
</div>

<!-- SA RISK + MARKERS -->
<div class="grid2">
  <div class="card">
    <div class="lbl">SA Risk Score</div>
    <div class="score-hero">
      <div class="score-ring" style="color:{{ cc }};background:{{ bg }}">
        <div class="score-n" style="color:{{ cc }}">{{ sa_risk_score }}</div>
        <div class="score-d">/ 100</div>
      </div>
      <div>
        <div style="font-size:14px;font-weight:600;color:{{ cc }}">{{ risk_category_label }}</div>
        <div style="font-size:11px;color:#9ca3af;margin-top:4px;line-height:1.5">Scored against SA-specific thresholds</div>
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

<!-- YEARS TO GAIN -->
{% if bio_age.top_gains %}
<div class="card" style="margin-bottom:12px">
  <div class="lbl">How to reclaim years</div>
  {% for g in bio_age.top_gains %}
  <div class="gain-item">
    <div class="gain-text">{{ g.gain_text }}</div>
    <div class="gain-badge">+{{ g.age_add }} yrs back</div>
  </div>
  {% endfor %}
</div>
{% endif %}

<!-- MARKERS -->
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

<!-- RECOMMENDATIONS -->
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

    # Biological age
    bio_age = compute_biological_age(
        chronological_age=row.get("age", 30),
        lab_values=lab_values,
        gender=row.get("gender", "female")
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
        bio_age=bio_age,
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

import os
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import google.generativeai as genai

# -----------------------------------
# Configuration / connexion DB + Gemini
# -----------------------------------

load_dotenv(override=True)

st.set_page_config(
    page_title="Détails spot",
    layout="wide",
)


def get_config_value(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


DATABASE_URL = get_config_value("DATABASE_URL")
GEMINI_API_KEY = get_config_value("GEMINI_API_KEY")

if GEMINI_API_KEY:
    GEMINI_API_KEY = GEMINI_API_KEY.strip()

if not DATABASE_URL:
    st.error(
        "DATABASE_URL est manquant.\n\n"
        "Vérifie ton fichier .env en local ou les Secrets Streamlit Cloud."
    )
    st.stop()

engine = create_engine(DATABASE_URL, echo=False, future=True)

gemini_model = None
model_name_used = None
gemini_unavailable_reason = None


def format_gemini_error(error: Exception) -> str:
    error_text = str(error)
    if "CONSUMER_SUSPENDED" in error_text or "has been suspended" in error_text:
        return (
            "La clé API ou le projet Google associé est suspendu. "
            "Crée une nouvelle clé Gemini dans Google AI Studio, vérifie que "
            "l'API Generative Language est active, puis remplace GEMINI_API_KEY "
            "dans le `.env` local ou dans les Secrets Streamlit Cloud."
        )
    if "API_KEY_INVALID" in error_text or "API key not valid" in error_text:
        return (
            "La clé Gemini est invalide. Remplace GEMINI_API_KEY dans le `.env` local "
            "ou dans les Secrets Streamlit Cloud."
        )
    if "PERMISSION_DENIED" in error_text or "403" in error_text:
        return (
            "Google refuse l'accès Gemini pour cette clé ou ce projet. "
            "Vérifie la clé, le projet Google et l'activation de l'API Generative Language."
        )
    if "is not found" in error_text or "404" in error_text:
        return (
            "Le modèle Gemini configuré n'est pas disponible pour cette clé. "
            "Vérifie GEMINI_MODEL dans le `.env` local ou dans les Secrets Streamlit Cloud."
        )
    return "Gemini n'est pas disponible pour le moment. Vérifie la configuration de GEMINI_API_KEY."

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model_name_used = get_config_value("GEMINI_MODEL", "gemini-2.5-flash").strip()
        gemini_model = genai.GenerativeModel(model_name_used)
    except Exception as e:
        gemini_model = None
        gemini_unavailable_reason = format_gemini_error(e)
else:
    gemini_unavailable_reason = (
        "GEMINI_API_KEY manquant : le résumé avec Gemini ne pourra pas être généré.\n"
        "Ajoute ta clé dans le `.env` local ou dans les Secrets Streamlit Cloud."
    )


# -----------------------------------
# Chargement des données
# -----------------------------------

@st.cache_data(ttl=600)
def load_scores_with_forecast():
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    sp.id AS spot_id,
                    sp.name,
                    sp.latitude,
                    sp.longitude,
                    sc.timestamp,
                    sc.score,
                    sc.conditions_label,
                    f.wave_height_m,
                    f.wave_period_s,
                    f.wave_direction_deg,
                    f.wind_speed_ms,
                    f.wind_direction_deg
                FROM surf_scores sc
                JOIN surf_spots sp ON sp.id = sc.spot_id
                JOIN surf_forecasts f
                  ON f.spot_id = sc.spot_id
                 AND f.timestamp = sc.timestamp
                WHERE sc.timestamp >= NOW() - INTERVAL '24 hours'
                  AND sc.timestamp < NOW() + INTERVAL '72 hours'
                ORDER BY sc.timestamp
                """
            )
        ).mappings().all()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Conversion en datetime naïf en heure locale (Europe/Paris)
    df["timestamp"] = (
        pd.to_datetime(df["timestamp"], utc=True)
        .dt.tz_convert("Europe/Paris")
        .dt.tz_localize(None)
    )

    return df


@st.cache_data(ttl=600)
def load_database_status():
    with engine.begin() as conn:
        return conn.execute(
            text(
                """
                SELECT
                    COUNT(*) AS rows_count,
                    MIN(sc.timestamp) AS first_timestamp,
                    MAX(sc.timestamp) AS last_timestamp,
                    MAX(sc.created_at) AS last_refresh
                FROM surf_scores sc
                """
            )
        ).mappings().one()


df = load_scores_with_forecast()

st.title("Détails d’un spot – Surf Monitor")

if df.empty:
    db_status = load_database_status()
    if db_status["rows_count"]:
        first_ts = pd.to_datetime(db_status["first_timestamp"], utc=True).tz_convert("Europe/Paris")
        last_ts = pd.to_datetime(db_status["last_timestamp"], utc=True).tz_convert("Europe/Paris")
        refreshed_at = pd.to_datetime(db_status["last_refresh"], utc=True).tz_convert("Europe/Paris")
        st.info(
            "Aucune donnée dans la fenêtre actuelle (-24h / +72h).\n\n"
            f"La base contient {db_status['rows_count']} scores, de "
            f"{first_ts:%d/%m/%Y %H:%M} à {last_ts:%d/%m/%Y %H:%M} "
            f"(dernier rafraîchissement : {refreshed_at:%d/%m/%Y %H:%M}).\n\n"
            "Relance `python surf_backend.py`, puis clique sur Rerun dans Streamlit "
            "si le cache affiche encore l'ancien résultat."
        )
    else:
        st.info(
            "Aucune donnée en base pour l'instant.\n\n"
            "Lance d'abord le script backend (surf_backend.py) pour alimenter Neon."
        )
    st.info(
        "Astuce : l'application lit DATABASE_URL depuis le `.env` en local "
        "ou depuis les Secrets Streamlit Cloud en production."
    )
    st.stop()


# -----------------------------------
# Sélection du spot + fenêtre temporelle
# -----------------------------------

spots = df["name"].unique().tolist()
spot_selected = st.selectbox("Choisis un spot", spots)

now = datetime.now()
date_min = now - timedelta(hours=3)
date_max = now + timedelta(days=3)

start_time, end_time = st.slider(
    "Fenêtre temporelle",
    min_value=date_min,
    max_value=date_max,
    value=(date_min, date_max),
    format="DD/MM HH:mm",
)

df_spot = df[
    (df["name"] == spot_selected)
    & (df["timestamp"] >= start_time)
    & (df["timestamp"] <= end_time)
].sort_values("timestamp")

if df_spot.empty:
    st.info("Pas de données pour ce spot sur cette période.")
    st.stop()

# -----------------------------------
# Construction du prompt pour Gemini
# -----------------------------------

def build_summary_prompt(spot_name: str, df_for_prompt: pd.DataFrame) -> str:
    """
    Prompt en français, orienté débutant, basé sur les données du spot.
    On réduit les colonnes et le nombre de lignes pour garder le prompt compact.
    """
    df_small = df_for_prompt[
        [
            "timestamp",
            "score",
            "conditions_label",
            "wave_height_m",
            "wave_period_s",
            "wind_speed_ms",
            "wind_direction_deg",
        ]
    ].copy()

    # Limite à 40 lignes max pour le prompt
    df_small = df_small.head(40)

    csv_data = df_small.to_csv(index=False)

    prompt = f"""
Tu es un expert des prévisions de surf.

Rédige un résumé en français, concis (5 à 10 lignes max), pour un surfeur débutant
qui habite en Loire-Atlantique et hésite à aller surfer au spot suivant :

Spot : {spot_name}

Tu dois :
- décrire globalement les conditions sur la période (score, houle, vent),
- indiquer les créneaux horaires les plus intéressants (scores les plus élevés),
- signaler s'il faut être prudent (vent fort, houle longue, etc.),
- rester clair, pédagogique et concret.

Voici les données au format CSV (chaque ligne = un créneau horaire) :

{csv_data}

Donne UNIQUEMENT le texte du résumé, sans autre commentaire, sans puces.
"""
    return prompt.strip()


@st.cache_data(ttl=3600)
def generate_gemini_summary(model_name: str, prompt: str) -> str:
    model = genai.GenerativeModel(model_name)
    response = model.generate_content(prompt)
    return response.text


# -----------------------------------
# Résumé avec Gemini
# -----------------------------------

st.subheader(f"Résumé des conditions – {spot_selected}")

if gemini_model is None:
    st.info(gemini_unavailable_reason)
else:
    if model_name_used:
        st.caption(f"Modèle utilisé : {model_name_used}")

    with st.spinner("Génération du résumé avec Gemini..."):
        prompt = build_summary_prompt(spot_selected, df_spot)

        try:
            summary_text = generate_gemini_summary(model_name_used, prompt)
        except Exception as e:
            summary_text = format_gemini_error(e)

    st.write(summary_text)


# -----------------------------------
# Courbe score / temps
# -----------------------------------

st.subheader("Timeline des scores")

df_plot = df_spot.set_index("timestamp")[["score"]]
st.line_chart(df_plot)


# -----------------------------------
# Détails tabulaires
# -----------------------------------

st.subheader("Détails des créneaux horaires")

st.dataframe(
    df_spot[
        [
            "timestamp",
            "score",
            "conditions_label",
            "wave_height_m",
            "wave_period_s",
            "wave_direction_deg",
            "wind_speed_ms",
            "wind_direction_deg",
        ]
    ],
    use_container_width=True,
)

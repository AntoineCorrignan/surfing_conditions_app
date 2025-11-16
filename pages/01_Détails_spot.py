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

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if not DATABASE_URL:
    st.error(
        "DATABASE_URL est manquant.\n\n"
        "Vérifie ton fichier .env (DATABASE_URL)."
    )
    st.stop()

engine = create_engine(DATABASE_URL, echo=False, future=True)

gemini_model = None
model_name_used = None

if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)

        # Récupérer les modèles et filtrer ceux qui supportent generateContent
        available_models = list(genai.list_models())
        text_models = [
            m for m in available_models
            if hasattr(m, "supported_generation_methods")
            and "generateContent" in m.supported_generation_methods
        ]

        if not text_models:
            st.warning(
                "Aucun modèle Gemini ne supporte generateContent sur ce compte/projet.\n"
                "Vérifie ta configuration Google AI Studio."
            )
        else:
            # On essaie de privilégier un modèle 1.5 (flash ou pro), sinon le premier.
            preferred = None
            for m in text_models:
                # m.name est du type "models/gemini-1.5-flash" ou "models/gemini-1.0-pro"
                if "1.5" in m.name:
                    preferred = m
                    break
            if preferred is None:
                preferred = text_models[0]

            model_name_used = preferred.name  # ex: "models/gemini-1.5-flash"
            gemini_model = genai.GenerativeModel(model_name_used)

    except Exception as e:
        gemini_model = None
        st.warning(f"Erreur de configuration Gemini : {e}")
else:
    st.warning(
        "GOOGLE_API_KEY manquant : le résumé avec Gemini ne pourra pas être généré.\n"
        "Ajoute ta clé dans le fichier .env."
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


df = load_scores_with_forecast()

st.title("Détails d’un spot – Surf Monitor")

if df.empty:
    st.info(
        "Aucune donnée en base pour l'instant.\n\n"
        "Lance d'abord le script backend (surf_backend.py) pour alimenter Neon."
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


# -----------------------------------
# Résumé avec Gemini
# -----------------------------------

st.subheader(f"Résumé des conditions – {spot_selected}")

if gemini_model is None:
    st.info(
        "Aucun modèle Gemini n'est configuré correctement.\n"
        "Vérifie GOOGLE_API_KEY et les modèles disponibles dans Google AI Studio."
    )
else:
    if model_name_used:
        st.caption(f"Modèle utilisé : {model_name_used}")

    with st.spinner("Génération du résumé avec Gemini..."):
        prompt = build_summary_prompt(spot_selected, df_spot)

        try:
            response = gemini_model.generate_content(prompt)
            summary_text = response.text
        except Exception as e:
            summary_text = f"Erreur lors de l'appel à Gemini : {e}"

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

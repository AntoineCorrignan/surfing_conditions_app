import os
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
import pydeck as pdk
from dotenv import load_dotenv

# -----------------------------------
# Configuration / connexion DB
# -----------------------------------

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

st.set_page_config(
    page_title="Surf Monitor Atlantique",
    layout="wide",
)

if not DATABASE_URL:
    st.error(
        "DATABASE_URL est manquant.\n\n"
        "Vérifie ton fichier .env (DATABASE_URL)."
    )
    st.stop()

engine = create_engine(DATABASE_URL, echo=False, future=True)


# -----------------------------------
# Chargement des données
# -----------------------------------

@st.cache_data(ttl=600)
def load_latest_scores():
    """
    Charge les scores + données météo associées (jointure forecast + score).
    Fenêtre : -24h / +72h.
    """
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


df = load_latest_scores()

st.title("Surf Monitor – Loire-Atlantique & Vendée (débutant)")

if df.empty:
    st.info(
        "Aucune donnée en base pour l'instant.\n\n"
        "Lance d'abord le script backend (surf_backend.py) pour alimenter Neon."
    )
    st.stop()


# -----------------------------------
# Filtres sidebar (globaux)
# -----------------------------------

st.sidebar.header("Filtres")

score_min = st.sidebar.slider("Score minimum", 0, 100, 60, step=5)

now = datetime.now()
date_min = now - timedelta(hours=3)
date_max = now + timedelta(days=3)

start_time, end_time = st.sidebar.slider(
    "Fenêtre temporelle",
    min_value=date_min,
    max_value=date_max,
    value=(date_min, date_max),
    format="DD/MM HH:mm",
)

df_filtered = df[
    (df["timestamp"] >= start_time)
    & (df["timestamp"] <= end_time)
    & (df["score"] >= score_min)
]


# -----------------------------------
# Carte (pydeck) – page principale
# -----------------------------------

st.subheader("Carte des spots et scores")

# Dernier score par spot sur la période filtrée
latest_by_spot = (
    df_filtered.sort_values("timestamp")
    .groupby("spot_id")
    .tail(1)
)


def score_to_color(score: int):
    if score >= 80:
        return [0, 200, 0]      # vert
    elif score >= 60:
        return [255, 165, 0]    # orange
    else:
        return [200, 0, 0]      # rouge


if not latest_by_spot.empty:
    latest_by_spot = latest_by_spot.copy()
    latest_by_spot["color"] = latest_by_spot["score"].apply(score_to_color)

    view_state = pdk.ViewState(
        latitude=latest_by_spot["latitude"].mean(),
        longitude=latest_by_spot["longitude"].mean(),
        zoom=7,
        pitch=0,
    )

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=latest_by_spot,
        get_position="[longitude, latitude]",
        get_radius=15000,
        get_fill_color="color",
        pickable=True,
    )

    # Infobulle détaillée : score + houle + vent
    tooltip = {
        "html": (
            "<b>{name}</b><br/>"
            "Score: {score} – {conditions_label}<br/>"
            "Date/heure: {timestamp}<br/>"
            "<br/>"
            "<b>Houle</b><br/>"
            "Hauteur: {wave_height_m} m<br/>"
            "Période: {wave_period_s} s<br/>"
            "Direction: {wave_direction_deg}°<br/>"
            "<br/>"
            "<b>Vent</b><br/>"
            "Vitesse: {wind_speed_ms} m/s<br/>"
            "Direction: {wind_direction_deg}°"
        ),
        "style": {"color": "white"},
    }

    st.pydeck_chart(
        pdk.Deck(
            layers=[layer],
            initial_view_state=view_state,
            tooltip=tooltip,
        )
    )
else:
    st.info("Aucun créneau au-dessus du score minimum sur la période sélectionnée.")

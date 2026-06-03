from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
import pydeck as pdk
from dotenv import load_dotenv
from app_config import get_config_value, missing_config_message

# -----------------------------------
# Configuration / connexion DB
# -----------------------------------

load_dotenv(override=True)

st.set_page_config(
    page_title="Surf Monitor Atlantique",
    layout="wide",
)

DATABASE_URL = get_config_value("DATABASE_URL")

if not DATABASE_URL:
    st.error(missing_config_message("DATABASE_URL"))
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


df = load_latest_scores()

st.title("Surf Monitor – Loire-Atlantique & Vendée (débutant)")

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
# Filtres sidebar (globaux)
# -----------------------------------

st.sidebar.header("Filtres")

score_min = st.sidebar.slider("Score minimum", 0, 100, 0, step=5)

now = datetime.now(ZoneInfo("Europe/Paris")).replace(tzinfo=None)
st.sidebar.caption(f"Heure prise en compte : {now:%d/%m/%Y %H:%M}")


# -----------------------------------
# Carte + classement – page principale
# -----------------------------------

# Créneau disponible le plus proche de l'heure actuelle pour chaque spot.
current_by_spot = df.copy()
current_by_spot["time_distance"] = (current_by_spot["timestamp"] - now).abs()
current_by_spot = (
    current_by_spot.sort_values(["spot_id", "time_distance", "timestamp"])
    .groupby("spot_id")
    .head(1)
)
current_by_spot = current_by_spot[current_by_spot["score"] >= score_min]

st.subheader("Carte des spots et scores")


def score_to_color(score: int):
    if score >= 80:
        return [0, 200, 0]      # vert
    elif score >= 60:
        return [255, 165, 0]    # orange
    else:
        return [200, 0, 0]      # rouge


if not current_by_spot.empty:
    current_by_spot = current_by_spot.copy()
    current_by_spot["color"] = current_by_spot["score"].apply(score_to_color)

    view_state = pdk.ViewState(
        latitude=current_by_spot["latitude"].mean(),
        longitude=current_by_spot["longitude"].mean(),
        zoom=7,
        pitch=0,
    )

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=current_by_spot,
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
    st.info("Aucun spot au-dessus du score minimum pour le créneau actuel.")

st.subheader("Classement des spots")

if not current_by_spot.empty:
    ranking_df = (
        current_by_spot.sort_values(
            ["score", "timestamp"],
            ascending=[False, True],
        )
        .reset_index(drop=True)
        .copy()
    )
    ranking_df.insert(0, "rang", ranking_df.index + 1)
    ranking_df["timestamp"] = ranking_df["timestamp"].dt.strftime("%d/%m %H:%M")

    st.dataframe(
        ranking_df[
            [
                "rang",
                "name",
                "score",
                "conditions_label",
                "timestamp",
            ]
        ],
        column_config={
            "rang": st.column_config.NumberColumn("Rang", width="small"),
            "name": st.column_config.TextColumn("Spot"),
            "score": st.column_config.ProgressColumn(
                "Indice",
                min_value=0,
                max_value=100,
                format="%d",
            ),
            "conditions_label": st.column_config.TextColumn("Conditions"),
            "timestamp": st.column_config.TextColumn("Créneau"),
        },
        hide_index=True,
        use_container_width=True,
    )
else:
    st.info("Aucun spot à classer avec les filtres actuels.")

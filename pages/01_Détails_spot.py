from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import google.generativeai as genai
from app_config import get_config_value, missing_config_message
from db_schema import ensure_tide_forecast_columns

# -----------------------------------
# Configuration / connexion DB + Gemini
# -----------------------------------

load_dotenv(override=True)

st.set_page_config(
    page_title="Détails spot",
    layout="wide",
)

DATABASE_URL = get_config_value("DATABASE_URL")
GEMINI_API_KEY = get_config_value("GEMINI_API_KEY")

if GEMINI_API_KEY:
    GEMINI_API_KEY = GEMINI_API_KEY.strip()

if not DATABASE_URL:
    st.error(missing_config_message("DATABASE_URL"))
    st.stop()

engine = create_engine(DATABASE_URL, echo=False, future=True)
ensure_tide_forecast_columns(engine)

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
                    f.wind_direction_deg,
                    f.tide_height_m,
                    f.tide_state,
                    f.next_tide_type,
                    f.next_tide_time,
                    f.next_tide_height_m,
                    f.tide_coefficient,
                    f.minutes_to_next_tide
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
    if "next_tide_time" in df and df["next_tide_time"].notna().any():
        df["next_tide_time"] = (
            pd.to_datetime(df["next_tide_time"], utc=True)
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
# Sélection du spot + créneaux temporels
# -----------------------------------

spots = df["name"].unique().tolist()
spot_selected = st.selectbox("Choisis un spot", spots)

now = datetime.now(ZoneInfo("Europe/Paris")).replace(tzinfo=None)
today = now.date()

weekdays_fr = (
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "dimanche",
)
time_slots = [
    ("8h-11h", 8, 11),
    ("11h-14h", 11, 14),
    ("14h-17h", 14, 17),
    ("17h-20h", 17, 20),
    ("20h-23h", 20, 23),
]

df_selected_spot = df[df["name"] == spot_selected].copy()
available_days = sorted(
    day
    for day in df_selected_spot["timestamp"].dt.date.unique()
    if day >= today
)

slot_options = []
for day in available_days:
    for label, start_hour, end_hour in time_slots:
        start = datetime.combine(day, datetime.min.time()).replace(hour=start_hour)
        end = datetime.combine(day, datetime.min.time()).replace(hour=end_hour)
        has_data = (
            (df_selected_spot["timestamp"] >= start)
            & (df_selected_spot["timestamp"] < end)
        ).any()
        if not has_data or (day == today and end <= now):
            continue
        slot_options.append(
            {
                "label": f"{weekdays_fr[day.weekday()]} {day:%d/%m} · {label}",
                "start": start,
                "end": end,
            }
        )

if not slot_options:
    st.info("Pas de créneau disponible pour ce spot.")
    st.stop()

default_slot_labels = [slot_options[0]["label"]]
for option in slot_options:
    if option["start"] <= now < option["end"]:
        default_slot_labels = [option["label"]]
        break

selected_slot_labels = st.pills(
    "Jours et créneaux",
    options=[option["label"] for option in slot_options],
    default=default_slot_labels,
    selection_mode="multi",
)
if not selected_slot_labels:
    st.info("Sélectionne au moins un créneau pour afficher les détails.")
    st.stop()

selected_slots = [
    option for option in slot_options if option["label"] in selected_slot_labels
]
slot_mask = pd.Series(False, index=df_selected_spot.index)
for option in selected_slots:
    slot_mask = slot_mask | (
        (df_selected_spot["timestamp"] >= option["start"])
        & (df_selected_spot["timestamp"] < option["end"])
    )

df_spot = df_selected_spot[slot_mask].sort_values("timestamp")

if df_spot.empty:
    st.info("Pas de données pour ce spot sur ces créneaux.")
    st.stop()

slot_summary_rows = []
incomplete_slot_labels = []
for option in selected_slots:
    df_slot = df_selected_spot[
        (df_selected_spot["timestamp"] >= option["start"])
        & (df_selected_spot["timestamp"] < option["end"])
    ].copy()
    expected_hours = int((option["end"] - option["start"]).total_seconds() // 3600)
    observed_hours = df_slot["timestamp"].nunique()
    if observed_hours < expected_hours:
        incomplete_slot_labels.append(
            f"{option['label']} ({observed_hours}/{expected_hours} heures)"
        )
    if df_slot.empty:
        continue
    slot_summary_rows.append(
        {
            "créneau": option["label"],
            "début": option["start"],
            "fin": option["end"],
            "heures_disponibles": observed_hours,
            "heures_attendues": expected_hours,
            "score_moyen": round(df_slot["score"].mean(), 1),
            "score_min": int(df_slot["score"].min()),
            "score_max": int(df_slot["score"].max()),
            "houle_moyenne_m": round(df_slot["wave_height_m"].mean(), 2),
            "période_moyenne_s": round(df_slot["wave_period_s"].mean(), 1),
            "vent_moyen_ms": round(df_slot["wind_speed_ms"].mean(), 1),
            "marée": " → ".join(
                state for state in df_slot["tide_state"].dropna().astype(str).unique()
            ),
            "coefficient_moyen": (
                round(df_slot["tide_coefficient"].dropna().mean(), 0)
                if df_slot["tide_coefficient"].notna().any()
                else None
            ),
        }
    )

slot_summary_df = pd.DataFrame(slot_summary_rows)

if incomplete_slot_labels:
    st.warning(
        "Données horaires incomplètes pour : "
        + ", ".join(incomplete_slot_labels)
        + ". Relance `python surf_backend.py` pour remplir la base avec les prévisions heure par heure."
    )

# -----------------------------------
# Construction du prompt pour Gemini
# -----------------------------------

def build_summary_prompt(
    spot_name: str,
    df_for_prompt: pd.DataFrame,
    slot_summary_for_prompt: pd.DataFrame,
) -> str:
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
            "tide_height_m",
            "tide_state",
            "next_tide_type",
            "next_tide_time",
            "next_tide_height_m",
            "tide_coefficient",
            "minutes_to_next_tide",
        ]
    ].copy()

    # Limite à 40 lignes max pour le prompt
    df_small = df_small.head(40)

    csv_data = df_small.to_csv(index=False)
    slot_summary_csv = slot_summary_for_prompt.to_csv(index=False)
    selected_period = (
        f"{df_small['timestamp'].min():%d/%m %H:%M} à "
        f"{df_small['timestamp'].max():%d/%m %H:%M}"
    )

    prompt = f"""
Tu es un expert des prévisions de surf.

Rédige un résumé en français, concis (5 à 10 lignes max), pour un surfeur débutant
qui habite en Loire-Atlantique et hésite à aller surfer au spot suivant :

Spot : {spot_name}
Période analysée : {selected_period}

Tu dois :
- décrire globalement les conditions sur toute la période sélectionnée (score, houle, vent),
- indiquer les créneaux horaires les plus intéressants (scores les plus élevés),
- tenir compte de la marée (hauteur, marée montante/descendante, prochaine PM/BM, coefficient),
- signaler s'il faut être prudent (vent fort, houle longue, etc.),
- rester clair, pédagogique et concret.

Voici les données au format CSV.
Chaque ligne est une prévision horaire comprise dans les boutons jour/créneau sélectionnés.
Pour juger un créneau comme 8h-11h, utilise toutes les lignes entre 8h inclus et 11h exclu,
pas seulement l'heure de début.

Résumé agrégé par créneau sélectionné :
{slot_summary_csv}

Détail horaire :
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

    if st.button("Générer résumé", type="primary"):
        with st.spinner("Génération du résumé avec Gemini..."):
            prompt = build_summary_prompt(spot_selected, df_spot, slot_summary_df)

            try:
                summary_text = generate_gemini_summary(model_name_used, prompt)
            except Exception as e:
                summary_text = format_gemini_error(e)

        st.write(summary_text)
    else:
        st.info("Clique sur « Générer résumé » pour interroger Gemini avec les créneaux sélectionnés.")


# -----------------------------------
# Courbe score / temps
# -----------------------------------

st.subheader("Timeline des scores")

df_plot = df_spot.set_index("timestamp")[["score"]]
st.line_chart(df_plot)


# -----------------------------------
# Résumé agrégé
# -----------------------------------

st.subheader("Résumé par créneau")

st.dataframe(
    slot_summary_df[
        [
            "créneau",
            "heures_disponibles",
            "heures_attendues",
            "score_moyen",
            "score_min",
            "score_max",
            "houle_moyenne_m",
            "période_moyenne_s",
            "vent_moyen_ms",
            "marée",
            "coefficient_moyen",
        ]
    ],
    use_container_width=True,
    hide_index=True,
)


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
            "tide_height_m",
            "tide_state",
            "next_tide_type",
            "next_tide_time",
            "next_tide_height_m",
            "tide_coefficient",
            "minutes_to_next_tide",
        ]
    ],
    use_container_width=True,
)

import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


# -----------------------------------
# Configuration / connexion DB
# -----------------------------------

load_dotenv(override=True)

DATABASE_URL = os.getenv("DATABASE_URL")
APP_DIR = Path(__file__).resolve().parents[1]
BACKEND_SCRIPT = APP_DIR / "surf_backend.py"

st.set_page_config(
    page_title="Paramètres",
    layout="wide",
)

if not DATABASE_URL:
    st.error(
        "DATABASE_URL est manquant.\n\n"
        "Vérifie ton fichier .env (DATABASE_URL)."
    )
    st.stop()

engine = create_engine(DATABASE_URL, echo=False, future=True)


def init_spots_table():
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS surf_spots (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    latitude DOUBLE PRECISION NOT NULL,
                    longitude DOUBLE PRECISION NOT NULL
                )
                """
            )
        )


@st.cache_data(ttl=60)
def load_spots():
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, name, latitude, longitude
                FROM surf_spots
                ORDER BY name
                """
            )
        ).mappings().all()

    return pd.DataFrame(rows)


def add_spot(name: str, latitude: float, longitude: float):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO surf_spots (name, latitude, longitude)
                VALUES (:name, :latitude, :longitude)
                ON CONFLICT (name) DO UPDATE
                SET latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude
                """
            ),
            {
                "name": name,
                "latitude": latitude,
                "longitude": longitude,
            },
        )


def run_backend_update():
    return subprocess.run(
        [sys.executable, str(BACKEND_SCRIPT)],
        cwd=str(APP_DIR),
        capture_output=True,
        text=True,
        timeout=240,
    )


init_spots_table()

st.title("Paramètres")

st.subheader("Spots")

spots_df = load_spots()
if spots_df.empty:
    st.info("Aucun spot configuré pour l'instant.")
else:
    st.dataframe(
        spots_df[["name", "latitude", "longitude"]],
        use_container_width=True,
        hide_index=True,
    )

with st.form("add_spot_form", clear_on_submit=True):
    st.markdown("Ajouter ou modifier un spot")

    name = st.text_input("Nom du spot")
    col_lat, col_lon = st.columns(2)
    latitude = col_lat.number_input(
        "Latitude",
        min_value=-90.0,
        max_value=90.0,
        value=47.0,
        format="%.6f",
    )
    longitude = col_lon.number_input(
        "Longitude",
        min_value=-180.0,
        max_value=180.0,
        value=-2.0,
        format="%.6f",
    )
    submitted = st.form_submit_button("Enregistrer le spot")

if submitted:
    cleaned_name = name.strip()
    if not cleaned_name:
        st.error("Le nom du spot est obligatoire.")
    else:
        add_spot(cleaned_name, latitude, longitude)
        st.cache_data.clear()
        st.success(f"Spot enregistré : {cleaned_name}")
        st.rerun()

st.subheader("Données")

if st.button("Mettre à jour les données de la base", type="primary"):
    with st.spinner("Mise à jour des prévisions en cours..."):
        try:
            result = run_backend_update()
        except subprocess.TimeoutExpired:
            st.error("La mise à jour a dépassé le délai autorisé.")
        except Exception as exc:
            st.error(f"Impossible de lancer le backend : {exc}")
        else:
            if result.returncode == 0:
                st.cache_data.clear()
                st.success("Base mise à jour avec succès.")
                if result.stdout:
                    st.code(result.stdout, language="text")
            else:
                st.error("La mise à jour a échoué.")
                output = "\n".join(
                    part for part in [result.stdout, result.stderr] if part
                )
                if output:
                    st.code(output, language="text")

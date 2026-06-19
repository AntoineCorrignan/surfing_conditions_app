import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any, Optional

import requests
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine,
    text,
)
from db_schema import ensure_tide_forecast_columns

# ---------------------------
# Configuration
# ---------------------------

load_dotenv(override=True)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL manquant dans .env")

SURF_SCORE_THRESHOLD = int(os.getenv("SURF_SCORE_THRESHOLD", "70"))
TIDE_REFERENCE_RANGE_M = float(os.getenv("TIDE_REFERENCE_RANGE_M", "5.0"))

# Endpoints Open-Meteo
METEOFRANCE_URL = "https://api.open-meteo.com/v1/meteofrance"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"

# Spots (à adapter)
SPOTS = [
    {
        "name": "Pornichet",
        "latitude": 47.2634,
        "longitude": -2.3406,
    },
    {
        "name": "Saint-Gilles-Croix-de-Vie",
        "latitude": 46.6978,
        "longitude": -1.9445,
    },
    {
        "name": "La Tranche-sur-Mer (La Terrière)",
        "latitude": 46.3441,
        "longitude": -1.4388,
    },
    {
        "name": "Vendays-Montalivet",
        "latitude": 45.3567,
        "longitude": -1.0617,
    },
]

# ---------------------------
# Modèle de données interne
# ---------------------------

@dataclass
class SurfForecast:
    spot_name: str
    latitude: float
    longitude: float
    timestamp: datetime
    wave_height_m: float
    wave_period_s: float
    wave_direction_deg: float
    wind_speed_ms: float
    wind_direction_deg: float
    tide_height_m: Optional[float] = None
    tide_state: Optional[str] = None
    next_tide_type: Optional[str] = None
    next_tide_time: Optional[datetime] = None
    next_tide_height_m: Optional[float] = None
    tide_coefficient: Optional[int] = None
    minutes_to_next_tide: Optional[int] = None


# ---------------------------
# Connexion DB & création des tables
# ---------------------------

engine = create_engine(DATABASE_URL, echo=False, future=True)


def init_db():
    """Crée les tables si elles n'existent pas déjà."""
    create_sql = """
    CREATE TABLE IF NOT EXISTS surf_spots (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        latitude DOUBLE PRECISION NOT NULL,
        longitude DOUBLE PRECISION NOT NULL
    );

    CREATE TABLE IF NOT EXISTS surf_forecasts (
        id SERIAL PRIMARY KEY,
        spot_id INTEGER NOT NULL REFERENCES surf_spots(id),
        timestamp TIMESTAMPTZ NOT NULL,
        wave_height_m DOUBLE PRECISION,
        wave_period_s DOUBLE PRECISION,
        wave_direction_deg DOUBLE PRECISION,
        wind_speed_ms DOUBLE PRECISION,
        wind_direction_deg DOUBLE PRECISION,
        tide_height_m DOUBLE PRECISION,
        tide_state TEXT,
        next_tide_type TEXT,
        next_tide_time TIMESTAMPTZ,
        next_tide_height_m DOUBLE PRECISION,
        tide_coefficient INTEGER,
        minutes_to_next_tide INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uniq_spot_time UNIQUE (spot_id, timestamp)
    );

    CREATE TABLE IF NOT EXISTS surf_scores (
        id SERIAL PRIMARY KEY,
        spot_id INTEGER NOT NULL REFERENCES surf_spots(id),
        timestamp TIMESTAMPTZ NOT NULL,
        score INTEGER NOT NULL,
        conditions_label TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uniq_spot_time_score UNIQUE (spot_id, timestamp)
    );
    """
    with engine.begin() as conn:
        conn.execute(text(create_sql))

    ensure_tide_forecast_columns(engine)

    # Upsert des spots
    with engine.begin() as conn:
        for spot in SPOTS:
            conn.execute(
                text(
                    """
                    INSERT INTO surf_spots (name, latitude, longitude)
                    VALUES (:name, :lat, :lon)
                    ON CONFLICT (name) DO UPDATE
                    SET latitude = EXCLUDED.latitude,
                        longitude = EXCLUDED.longitude
                    """
                ),
                {
                    "name": spot["name"],
                    "lat": spot["latitude"],
                    "lon": spot["longitude"],
                },
            )


def load_spots_from_db() -> List[Dict[str, Any]]:
    """Charge tous les spots configurés en base."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT name, latitude, longitude
                FROM surf_spots
                ORDER BY name
                """
            )
        ).mappings().all()

    return [
        {
            "name": row["name"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
        }
        for row in rows
    ]


# ---------------------------
# Fetch météo (Open-Meteo)
# ---------------------------

def fetch_marine_and_wind(spots: List[Dict[str, Any]]) -> List[SurfForecast]:
    """
    Appelle:
    - Marine API: hauteur/période/direction de la houle
    - MeteoFrance API: vent 10m
    et fusionne par spot + timestamp.
    """

    lats = ",".join(str(s["latitude"]) for s in spots)
    lons = ",".join(str(s["longitude"]) for s in spots)

    params_marine = {
        "latitude": lats,
        "longitude": lons,
        "hourly": "wave_height,wave_direction,wave_period,sea_level_height_msl",
        "timezone": "auto",
        "forecast_days": 3,
    }

    params_wind = {
        "latitude": lats,
        "longitude": lons,
        "hourly": "wind_speed_10m,wind_direction_10m",
        "timezone": "auto",
        "forecast_days": 3,
    }

    marine_resp = requests.get(MARINE_URL, params=params_marine, timeout=20)
    marine_resp.raise_for_status()
    marine_data = marine_resp.json()

    wind_resp = requests.get(METEOFRANCE_URL, params=params_wind, timeout=20)
    wind_resp.raise_for_status()
    wind_data = wind_resp.json()

    forecasts: List[SurfForecast] = []

    # ---- Normalisation simple ----
    # Cas 1 : plusieurs coordonnées -> la réponse est une liste de structures
    # Cas 2 : une seule coordonnée -> la réponse est un dict simple
    def normalize_locations(data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return [data]
        else:
            raise RuntimeError(f"Format de réponse inattendu: {type(data)}")

    marine_locations = normalize_locations(marine_data)
    wind_locations = normalize_locations(wind_data)

    # On suppose même ordre des locations pour marine et vent
    if len(marine_locations) != len(spots) or len(wind_locations) != len(spots):
        raise RuntimeError("Nombre de locations renvoyées par l'API != nombre de spots")

    for idx, spot in enumerate(spots):
        marine_loc = marine_locations[idx]
        wind_loc = wind_locations[idx]

        # Chaque loc ressemble à :
        # {
        #   "latitude": ...,
        #   "longitude": ...,
        #   "hourly": {
        #       "time": [...],
        #       "wave_height": [...],
        #       ...
        #   }
        # }

        m_hourly = marine_loc["hourly"]
        w_hourly = wind_loc["hourly"]

        times = m_hourly["time"]
        wave_height = m_hourly["wave_height"]
        wave_dir = m_hourly["wave_direction"]
        wave_period = m_hourly["wave_period"]
        tide_height = m_hourly.get("sea_level_height_msl", [None] * len(times))
        tide_context_by_time = build_tide_context(times, tide_height)

        wind_times = w_hourly["time"]
        wind_speed = w_hourly["wind_speed_10m"]
        wind_dir = w_hourly["wind_direction_10m"]

        if times != wind_times:
            # Si un jour ça ne colle pas, il faudra faire un merge par timestamp.
            raise RuntimeError("Désalignement des timestamps marine/meteo")

        for t_str, h, p, d, ws, wd, th in zip(
            times, wave_height, wave_period, wave_dir, wind_speed, wind_dir, tide_height
        ):
            ts = datetime.fromisoformat(t_str)
            tide_context = tide_context_by_time.get(t_str, {})

            forecasts.append(
                SurfForecast(
                    spot_name=spot["name"],
                    latitude=spot["latitude"],
                    longitude=spot["longitude"],
                    timestamp=ts,
                    wave_height_m=h,
                    wave_period_s=p,
                    wave_direction_deg=d,
                    wind_speed_ms=ws,
                    wind_direction_deg=wd,
                    tide_height_m=th,
                    tide_state=tide_context.get("tide_state"),
                    next_tide_type=tide_context.get("next_tide_type"),
                    next_tide_time=tide_context.get("next_tide_time"),
                    next_tide_height_m=tide_context.get("next_tide_height_m"),
                    tide_coefficient=tide_context.get("tide_coefficient"),
                    minutes_to_next_tide=tide_context.get("minutes_to_next_tide"),
                )
            )

    return forecasts


def compute_tide_coefficient(range_m: Optional[float]) -> Optional[int]:
    """
    Coefficient approximatif sur l'échelle française 20-120.
    Open-Meteo fournit une hauteur d'eau modélisée, pas le coefficient SHOM officiel.
    """
    if range_m is None or TIDE_REFERENCE_RANGE_M <= 0:
        return None
    coefficient = round((range_m / TIDE_REFERENCE_RANGE_M) * 100)
    return max(20, min(120, coefficient))


def build_tide_context(
    time_strings: List[str],
    tide_heights: List[Optional[float]],
) -> Dict[str, Dict[str, Any]]:
    """
    Détecte les pleines/basses mers à partir de la hauteur d'eau horaire.
    La précision est celle du pas horaire de l'API, suffisante pour orienter un score surf.
    """
    times = [datetime.fromisoformat(t_str) for t_str in time_strings]
    events: List[Dict[str, Any]] = []

    for idx in range(1, len(tide_heights) - 1):
        prev_h = tide_heights[idx - 1]
        curr_h = tide_heights[idx]
        next_h = tide_heights[idx + 1]
        if prev_h is None or curr_h is None or next_h is None:
            continue
        if curr_h >= prev_h and curr_h >= next_h:
            event_type = "PM"
        elif curr_h <= prev_h and curr_h <= next_h:
            event_type = "BM"
        else:
            continue
        events.append(
            {
                "time": times[idx],
                "height": curr_h,
                "type": event_type,
                "coefficient": None,
            }
        )

    for idx, event in enumerate(events):
        neighbor_ranges = []
        neighboring_events = (
            events[idx - 1] if idx > 0 else None,
            events[idx + 1] if idx + 1 < len(events) else None,
        )
        for neighbor in neighboring_events:
            if neighbor and neighbor["type"] != event["type"]:
                neighbor_ranges.append(abs(event["height"] - neighbor["height"]))
        if neighbor_ranges:
            event["coefficient"] = compute_tide_coefficient(max(neighbor_ranges))

    context_by_time: Dict[str, Dict[str, Any]] = {}
    for idx, (t_str, ts) in enumerate(zip(time_strings, times)):
        next_event = next((event for event in events if event["time"] >= ts), None)
        tide_state = None
        if idx > 0 and tide_heights[idx] is not None and tide_heights[idx - 1] is not None:
            if tide_heights[idx] > tide_heights[idx - 1]:
                tide_state = "montante"
            elif tide_heights[idx] < tide_heights[idx - 1]:
                tide_state = "descendante"
            else:
                tide_state = "étale"

        if next_event:
            minutes_to_next_tide = int((next_event["time"] - ts).total_seconds() // 60)
            context_by_time[t_str] = {
                "tide_state": tide_state,
                "next_tide_type": next_event["type"],
                "next_tide_time": next_event["time"],
                "next_tide_height_m": next_event["height"],
                "tide_coefficient": next_event["coefficient"],
                "minutes_to_next_tide": minutes_to_next_tide,
            }
        else:
            context_by_time[t_str] = {"tide_state": tide_state}

    return context_by_time



# ---------------------------
# Scoring surf débutant
# ---------------------------

def score_wave_height(h: float) -> float:
    """Score 0-1 sur la hauteur de vague pour débutant (idéal 0.5-1.2m)."""
    if h is None:
        return 0.0
    if 0.5 <= h <= 1.2:
        return 1.0
    if 0.3 <= h < 0.5 or 1.2 < h <= 1.5:
        return 0.5
    return 0.0


def score_period(p: float) -> float:
    """Score 0-1 sur la période (idéal 8-12s)."""
    if p is None:
        return 0.0
    if 8 <= p <= 12:
        return 1.0
    if 6 <= p < 8 or 12 < p <= 14:
        return 0.5
    return 0.0


def score_wind(speed: float, direction_deg: float) -> float:
    """
    Score 0-1 sur le vent pour la côte atlantique débutant.
    On simplifie :
    - vent faible (<4 m/s ~ <15 km/h) = bon
    - vent offshore (E/NE) = bon
    - onshore fort = mauvais.
    """
    if speed is None or direction_deg is None:
        return 0.0

    # Base sur la vitesse
    if speed <= 4:
        base = 1.0
    elif speed <= 7:
        base = 0.6
    else:
        base = 0.2

    # Offshore approximatif : 45°–135° (E à SE/NE)
    if 45 <= direction_deg <= 135:
        bonus = 0.3
    # Onshore approximatif : 225°–315° (W)
    elif 225 <= direction_deg <= 315:
        bonus = -0.3
    else:
        bonus = 0.0

    return max(0.0, min(1.0, base + bonus))


def score_tide(f: SurfForecast) -> float:
    """Score 0-1: marée modérée et loin de l'étale favorisée pour débutant."""
    coefficient = f.tide_coefficient
    minutes_to_event = f.minutes_to_next_tide

    if coefficient is None and minutes_to_event is None:
        return 0.5

    if coefficient is None:
        coefficient_score = 0.7
    elif 45 <= coefficient <= 75:
        coefficient_score = 1.0
    elif 35 <= coefficient < 45 or 75 < coefficient <= 90:
        coefficient_score = 0.75
    elif 90 < coefficient <= 105:
        coefficient_score = 0.45
    else:
        coefficient_score = 0.25

    if minutes_to_event is None:
        timing_score = 0.7
    elif minutes_to_event <= 60:
        timing_score = 0.55
    elif minutes_to_event <= 180:
        timing_score = 1.0
    else:
        timing_score = 0.8

    if f.tide_state == "montante":
        timing_score = min(1.0, timing_score + 0.1)

    return max(0.0, min(1.0, 0.6 * coefficient_score + 0.4 * timing_score))


def compute_surf_score(f: SurfForecast) -> tuple[int, str]:
    """
    Combine les sous-scores en score 0-100 selon:
    - Vague: 40%
    - Vent: 30%
    - Période: 20%
    - Marée: 10%
    """
    wave_score = score_wave_height(f.wave_height_m)
    period_score = score_period(f.wave_period_s)
    wind_score = score_wind(f.wind_speed_ms, f.wind_direction_deg)
    tide_score = score_tide(f)

    score_0_1 = (
        0.4 * wave_score
        + 0.3 * wind_score
        + 0.2 * period_score
        + 0.1 * tide_score
    )
    score_0_100 = int(round(score_0_1 * 100))

    if score_0_100 >= 80:
        label = "Conditions parfaites débutant"
    elif score_0_100 >= 60:
        label = "Conditions correctes / jouables"
    elif score_0_100 >= 40:
        label = "Conditions moyennes"
    else:
        label = "Conditions mauvaises"

    return score_0_100, label


# ---------------------------
# Persistance en base
# ---------------------------

def save_forecasts_and_scores(forecasts: List[SurfForecast]):
    with engine.begin() as conn:
        # Récupérer les ids de spots
        spots_rows = conn.execute(
            text("SELECT id, name FROM surf_spots")
        ).mappings().all()
        spot_id_by_name = {row["name"]: row["id"] for row in spots_rows}

        for f in forecasts:
            spot_id = spot_id_by_name[f.spot_name]

            # insert forecast
            conn.execute(
                text(
                    """
                    INSERT INTO surf_forecasts (
                        spot_id, timestamp,
                        wave_height_m, wave_period_s, wave_direction_deg,
                        wind_speed_ms, wind_direction_deg,
                        tide_height_m, tide_state, next_tide_type,
                        next_tide_time, next_tide_height_m, tide_coefficient,
                        minutes_to_next_tide
                    )
                    VALUES (
                        :spot_id, :ts,
                        :wh, :wp, :wd,
                        :ws, :wdir,
                        :tide_height_m, :tide_state, :next_tide_type,
                        :next_tide_time, :next_tide_height_m, :tide_coefficient,
                        :minutes_to_next_tide
                    )
                    ON CONFLICT (spot_id, timestamp) DO UPDATE
                    SET wave_height_m = EXCLUDED.wave_height_m,
                        wave_period_s = EXCLUDED.wave_period_s,
                        wave_direction_deg = EXCLUDED.wave_direction_deg,
                        wind_speed_ms = EXCLUDED.wind_speed_ms,
                        wind_direction_deg = EXCLUDED.wind_direction_deg,
                        tide_height_m = EXCLUDED.tide_height_m,
                        tide_state = EXCLUDED.tide_state,
                        next_tide_type = EXCLUDED.next_tide_type,
                        next_tide_time = EXCLUDED.next_tide_time,
                        next_tide_height_m = EXCLUDED.next_tide_height_m,
                        tide_coefficient = EXCLUDED.tide_coefficient,
                        minutes_to_next_tide = EXCLUDED.minutes_to_next_tide,
                        created_at = NOW()
                    """
                ),
                {
                    "spot_id": spot_id,
                    "ts": f.timestamp,
                    "wh": f.wave_height_m,
                    "wp": f.wave_period_s,
                    "wd": f.wave_direction_deg,
                    "ws": f.wind_speed_ms,
                    "wdir": f.wind_direction_deg,
                    "tide_height_m": f.tide_height_m,
                    "tide_state": f.tide_state,
                    "next_tide_type": f.next_tide_type,
                    "next_tide_time": f.next_tide_time,
                    "next_tide_height_m": f.next_tide_height_m,
                    "tide_coefficient": f.tide_coefficient,
                    "minutes_to_next_tide": f.minutes_to_next_tide,
                },
            )

            # calcul score
            score, label = compute_surf_score(f)

            conn.execute(
                text(
                    """
                    INSERT INTO surf_scores (
                        spot_id, timestamp, score, conditions_label
                    )
                    VALUES (:spot_id, :ts, :score, :label)
                    ON CONFLICT (spot_id, timestamp) DO UPDATE
                    SET score = EXCLUDED.score,
                        conditions_label = EXCLUDED.conditions_label,
                        created_at = NOW()
                    """
                ),
                {
                    "spot_id": spot_id,
                    "ts": f.timestamp,
                    "score": score,
                    "label": label,
                },
            )


# ---------------------------
# Notification
# ---------------------------

def notify_good_sessions(threshold: int = SURF_SCORE_THRESHOLD):
    """
    Notifie (pour l'instant: print) les créneaux avec score >= threshold
    pour les prochaines 24h.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT s.name, sc.timestamp, sc.score, sc.conditions_label
                FROM surf_scores sc
                JOIN surf_spots s ON s.id = sc.spot_id
                WHERE sc.timestamp >= NOW()
                  AND sc.timestamp < NOW() + INTERVAL '24 hours'
                  AND sc.score >= :threshold
                ORDER BY sc.timestamp, s.name
                """
            ),
            {"threshold": threshold},
        ).mappings().all()

    if not rows:
        print("Aucune session idéale dans les prochaines 24h.")
        return

    print(f"Sessions surf recommandées (score >= {threshold}) :")
    for r in rows:
        print(
            f"- {r['name']} @ {r['timestamp']} : score {r['score']} ({r['conditions_label']})"
        )


# ---------------------------
# Entrée principale
# ---------------------------

def run_pipeline_once():
    print("Initialisation DB...")
    init_db()
    spots = load_spots_from_db()
    print(f"{len(spots)} spots configurés.")
    print("Récupération prévisions...")
    forecasts = fetch_marine_and_wind(spots)
    print(f"{len(forecasts)} enregistrements météo récupérés.")
    print("Sauvegarde en base + calcul des scores...")
    save_forecasts_and_scores(forecasts)
    print("Notification des bonnes sessions...")
    notify_good_sessions()


if __name__ == "__main__":
    run_pipeline_once()
    # Pour l'exécution toutes les 3h, utilise un cron, un scheduler externe,
    # ou un job GitHub Actions qui appelle ce script.

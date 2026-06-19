from sqlalchemy import text


TIDE_FORECAST_COLUMNS_SQL = (
    "ALTER TABLE surf_forecasts ADD COLUMN IF NOT EXISTS tide_height_m DOUBLE PRECISION",
    "ALTER TABLE surf_forecasts ADD COLUMN IF NOT EXISTS tide_state TEXT",
    "ALTER TABLE surf_forecasts ADD COLUMN IF NOT EXISTS next_tide_type TEXT",
    "ALTER TABLE surf_forecasts ADD COLUMN IF NOT EXISTS next_tide_time TIMESTAMPTZ",
    "ALTER TABLE surf_forecasts ADD COLUMN IF NOT EXISTS next_tide_height_m DOUBLE PRECISION",
    "ALTER TABLE surf_forecasts ADD COLUMN IF NOT EXISTS tide_coefficient INTEGER",
    "ALTER TABLE surf_forecasts ADD COLUMN IF NOT EXISTS minutes_to_next_tide INTEGER",
)


def ensure_tide_forecast_columns(engine):
    """Applique la migration légère requise par les données de marée."""
    with engine.begin() as conn:
        for ddl in TIDE_FORECAST_COLUMNS_SQL:
            conn.execute(text(ddl))

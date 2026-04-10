import os

import pandas as pd
import sqlalchemy
import streamlit as st

st.set_page_config(page_title="Unrestricted Yard Sniffer", layout="wide")


# --- DATABASE CONNECTION ---
def get_engine() -> sqlalchemy.Engine:
    db_user = os.environ.get("DB_USER")
    db_pass = os.environ.get("DB_PASS")
    db_name = os.environ.get("DB_NAME")
    instance = os.environ.get("INSTANCE_CONNECTION_NAME")

    if os.environ.get("K_SERVICE"):
        url = sqlalchemy.engine.url.URL.create(
            drivername="postgresql+pg8000",
            username=db_user,
            password=db_pass,
            database=db_name,
            query={"unix_sock": f"/cloudsql/{instance}/.s.PGSQL.5432"},
        )
    else:
        url = f"postgresql+pg8000://{db_user}:{db_pass}@127.0.0.1:5432/{db_name}"

    return sqlalchemy.create_engine(url)


engine = get_engine()

st.title("📡 Unrestricted BLE Yard Sniffer")

# 1. Fetch 100% of the data, handling possible NULL timestamps
query = """
    SELECT
        COALESCE(recorded_at, NOW()) as recorded_at,
        vehicle_id,
        mac_address,
        sensor_name,
        rssi,
        metadata
    FROM ble_telemetry
    ORDER BY recorded_at DESC
    LIMIT 1000
"""

try:
    df = pd.read_sql(query, engine)

    if df.empty:
        st.warning("Database is connected but the table is empty. Awaiting Pi transmissions...")
    else:
        # Display Metrics
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Rows", len(df))
        c2.metric("Unique Devices", df["mac_address"].nunique())
        c3.metric("Last Yard Sync", df["recorded_at"].max().strftime("%Y-%m-%d %H:%M:%S"))

        # Display Raw Data
        st.dataframe(df, use_container_width=True, hide_index=True)

except Exception as e:
    st.error(f"Dashboard Error: {e}")

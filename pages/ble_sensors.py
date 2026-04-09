import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

DB_PATH = os.getenv("TELEMETRY_DB_PATH", "/data/telematics.db")


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def ensure_tags_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ble_sensor_tags (
            mac_address TEXT PRIMARY KEY,
            tag TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def load_ble_telemetry(conn: sqlite3.Connection) -> pd.DataFrame:
    # Intentional full-table read: no SQL WHERE clause so the page displays 100% of rows.
    return pd.read_sql_query("SELECT * FROM ble_telemetry", conn)


def load_existing_tags(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT mac_address, tag, updated_at FROM ble_sensor_tags ORDER BY updated_at DESC",
        conn,
    )


def save_tag(conn: sqlite3.Connection, mac_address: str, tag: str) -> None:
    conn.execute(
        """
        INSERT INTO ble_sensor_tags (mac_address, tag, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(mac_address) DO UPDATE SET
            tag = excluded.tag,
            updated_at = excluded.updated_at
        """,
        (
            mac_address,
            tag,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def extract_unique_mac_addresses(df: pd.DataFrame) -> list[str]:
    if "mac_address" not in df.columns:
        return []

    # Intentionally no regex filtering: keep all distinct formats exactly as recorded.
    unique_macs = (
        df["mac_address"].dropna().astype(str).drop_duplicates().sort_values().tolist()
    )
    return unique_macs


def main() -> None:
    st.set_page_config(page_title="BLE Sensors", layout="wide")
    st.title("BLE Sensor Telemetry")

    try:
        conn = get_connection()
    except sqlite3.Error as exc:
        st.error(f"Failed to connect to telemetry database: {exc}")
        return

    ensure_tags_table(conn)

    try:
        telemetry_df = load_ble_telemetry(conn)
    except Exception as exc:
        st.error(f"Failed to load ble_telemetry: {exc}")
        conn.close()
        return

    st.subheader("Raw ble_telemetry Data")
    st.caption("Showing all rows from ble_telemetry with no SQL or regex filters.")
    st.dataframe(telemetry_df, use_container_width=True)
    st.info(f"Displayed rows: {len(telemetry_df)}")

    st.subheader("Tag a mac_address")
    mac_options = extract_unique_mac_addresses(telemetry_df)

    if not mac_options:
        st.warning("No mac_address values found in ble_telemetry.")
    else:
        selected_mac = st.selectbox(
            "Select a mac_address from telemetry",
            options=mac_options,
            help="All unique values are listed exactly as stored, regardless of format.",
        )
        tag_value = st.text_input("Tag")

        if st.button("Save Tag"):
            if not tag_value.strip():
                st.error("Tag is required.")
            else:
                save_tag(conn, selected_mac, tag_value.strip())
                st.success(f"Saved tag for {selected_mac}.")

    st.subheader("Existing Tags")
    tags_df = load_existing_tags(conn)
    st.dataframe(tags_df, use_container_width=True)

    conn.close()


if __name__ == "__main__":
    main()

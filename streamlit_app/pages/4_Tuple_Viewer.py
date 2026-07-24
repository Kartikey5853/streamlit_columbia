import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streamlit_app.ui_common import apply_theme

apply_theme()

from processing.excel_export import excel_bytes
from processing.unified_products import flattened_rows, load_normalized_products


st.title("Tuple Viewer")
st.info(
    "View all products and their corresponding prices in one place.  \n"
    "This page helps you verify the scraped data, identify any errors, and get a complete overview.  \n"
    "You can export this data as an Excel file and choose which columns to include using **Choose Export Columns**."
)

if st.button("Refresh tuple data", icon="🔄"):
    st.rerun()

payload = load_normalized_products()
rows = flattened_rows(payload)
if not rows:
    st.info("Run the full pipeline to create the tuple output.")
    st.stop()

query = st.text_input("Filter by normalized SKU, EAN, product ID, or title").strip().lower()
if query:
    rows = [row for row in rows if query in " ".join(str(value) for value in row.values()).lower()]
rows.sort(key=lambda item: item.get("Columbia SKU", ""))

all_columns = list(rows[0])
with st.popover("Choose export columns"):
    if st.button("Select all", use_container_width=True):
        for column in all_columns:
            st.session_state[f"tuple_export_{column}"] = True
    if st.button("Clear all", use_container_width=True):
        for column in all_columns:
            st.session_state[f"tuple_export_{column}"] = False
    for column in all_columns:
        st.checkbox(column, value=True, key=f"tuple_export_{column}")

selected_columns = [column for column in all_columns if st.session_state.get(f"tuple_export_{column}", True)]
if selected_columns:
    export_rows = [{column: row.get(column) for column in selected_columns} for row in rows]
    st.download_button(
        f"Download {len(selected_columns)} selected columns.xlsx",
        data=excel_bytes(export_rows),
        file_name="unified_tuples.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

st.caption(
    "More data is available on the right. Hover over the table to access the full-screen button."
)
try:
    import pandas as pd

    column_config = {"Product Image": st.column_config.ImageColumn("Product Image")}
    for column in all_columns:
        if column.endswith("Product URL"):
            column_config[column] = st.column_config.LinkColumn(column)
        elif column.endswith("Image URL"):
            column_config[column] = st.column_config.ImageColumn(column)
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        height=680,
        hide_index=True,
        column_config=column_config,
    )
except Exception:
    st.write(rows)

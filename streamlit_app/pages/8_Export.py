import streamlit as st
from io import BytesIO
from zipfile import ZipFile
from pathlib import Path

from processing.platform_paths import CLIP_INDEX, DINOV2_INDEX, PRODUCTS_PKL, METADATA_PKL, EMBEDDINGS_DIR


st.title("Export Embeddings and Indexes")
st.markdown("Download the CLIP/DINO indexes and related metadata used for image search.")

available = []
for path in (CLIP_INDEX, DINOV2_INDEX, PRODUCTS_PKL, METADATA_PKL):
    if path.exists():
        available.append(path)

if not available:
    st.info("No embedding/index files found in the embeddings directory: {}".format(EMBEDDINGS_DIR))
else:
    st.write("Files to include:")
    for p in available:
        st.write(f"- {p.name}")

    if st.button("Create export zip"):
        buffer = BytesIO()
        with ZipFile(buffer, "w") as z:
            for p in available:
                try:
                    z.writestr(p.name, p.read_bytes())
                except Exception:
                    # skip problematic files
                    continue
        buffer.seek(0)
        st.download_button("Download export.zip", data=buffer.read(), file_name="export_embeddings.zip")

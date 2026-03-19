import streamlit as st

# Page config
st.set_page_config(
    page_title="Anthive Explorer",
    layout="centered",
    page_icon="🧬")

pg = st.navigation(
    ["pages/1_gene.py", "pages/2_umap.py", "pages/3_rolling.py"],
    position='top')

pg.run()

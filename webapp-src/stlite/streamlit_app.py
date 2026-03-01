import matplotlib.pyplot as plt
import seaborn
import seaborn as sns
import streamlit as st
import util
from components import database_selector, gene_selector
import os

import antclient

# Page config
st.set_page_config(
    page_title="Anthive Explorer",
    layout="centered",
    page_icon="🧬")

pg = st.navigation(
    ["pages/1_gene.py", "pages/2_umap.py", "pages/3_rolling.py"],
    position='top')

pg.run()

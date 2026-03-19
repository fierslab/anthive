"""Lightweight REST API wrapper for Anthive using requests"""
from typing import Any, Dict, List, Optional, Union

import streamlit as st

# Persistent components


def p_selectbox(title, options, key,
                default_value=None,
                **kwargs):

    # first check if it is in the URL. If not, check
    # default_value
    initial_option = st.query_params.get(key)
    if initial_option is None:
        initial_option = default_value
    if initial_option in options:
        initial_index = options.index(initial_option)
    elif default_value is not None and default_value in options:
        initial_index = options.index(default_value)
    else:
        initial_index = 0

    rv = st.selectbox(
        title, options=options,
        index=initial_index,
        key=key, **kwargs)

    st.query_params[key] = rv

    return rv


def p_number_input(title, value, key, **kwargs):
    initial_value = st.query_params.get(key)
    if initial_value is None:
        initial_value = value
    if isinstance(value, int):
        initial_value = int(round(float(initial_value)))
    elif isinstance(value, float):
        initial_value = float(initial_value)

    rv = st.number_input(
        title, value=initial_value,
        key=key, **kwargs)

    st.query_params[key] = rv

    return rv

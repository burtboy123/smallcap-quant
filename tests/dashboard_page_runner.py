"""
Run ONE dashboard page under streamlit.testing (used by tests/test_dashboard_pages.py).

dashboard/app.py registers its pages with st.navigation; this runner swaps st.Page and
st.navigation for stand-ins so only the page titled $DASH_PAGE runs, then restores them
(AppTest runs scripts inside the pytest process). Not a test module itself.
"""
import os
import runpy
from pathlib import Path

import streamlit as st

APP = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
TARGET = os.environ["DASH_PAGE"]


class _Nav:
    def __init__(self, sections):
        self.sections = sections

    def run(self):
        hits = [fn for pages in self.sections.values() for (t, fn) in pages if t == TARGET]
        if len(hits) != 1:
            raise RuntimeError(f"dashboard page {TARGET!r} found {len(hits)} times")
        hits[0]()


_saved = (st.Page, st.navigation)
st.Page = lambda fn, title, **k: (title, fn)
st.navigation = lambda sections, **k: _Nav(sections)
try:
    runpy.run_path(str(APP), run_name="__main__")
finally:
    st.Page, st.navigation = _saved

# my_app.py — stable grid (no flicker), multi-token accent-insensitive search,
# autosave policy (instant when filtering; buffered every 30s otherwise),
# optimistic UI, Dropbox token refresh. Uses data_editor edited_rows.
# Cleaned up: less duplication via small helper functions.

import io
import time
import pathlib
import pandas as pd
import streamlit as st
import requests, dropbox
import unicodedata

# ================== SETTINGS ==================
APP_TITLE  = "Schneider Electric Career Fair"
PAGE_ICON  = "🎓"

# Read (fast): public shared CSV URL (dl=0 or dl=1, force dl=1)
STATE_SHARED_CSV_URL = st.secrets.get("STATE_SHARED_CSV_URL_SCHNEIDER")

# Write (API): path inside your Dropbox App Folder
STATE_DBX_PATH = st.secrets.get("STATE_DBX_PATH_SCHNEIDER")

# Save policy
AUTOSAVE_DEBOUNCE_SEC   = 0.35   # debounce for instant saves when filter is active
BATCH_SAVE_INTERVAL_SEC = 30     # 30s for tests (put 180 in prod)
LOGO_PATH = "images/schneider.png"

# UI keys
EDITOR_KEY = "grid_all"  # state key for st.data_editor
GRID_FILTER_KEY = "grid_filter_key"
# ==============================================


# ---------------- UI boot ----------------
st.set_page_config(page_title=APP_TITLE, page_icon=PAGE_ICON, layout="wide")

css = pathlib.Path("assets/styles.css")
if css.exists():
    st.markdown(f"<style>{css.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)

lc, rc = st.columns([1, 4])
with lc:
    try:
        st.image(LOGO_PATH, width=140)
    except Exception:
        pass
with rc:
    st.markdown(
        "<h1 class='app-title' style='margin:0'>Schneider Electric Career Fair</h1>"
        "<p class='app-subtitle'>Search • Direct CV links • Buffered autosave (30s) • Instant save when filtering</p>"
        "<hr class='hr-soft'/>",
        unsafe_allow_html=True,
    )


# ---------------- Columns ----------------
FIRST, LAST, FILE = "first_name", "last_name", "file_name"   # FILE is a full URL
SEEN, INT, SAVE, CONT = "seen", "intend_view", "cv_saved", "contacted"
BOOL_COLS = [SEEN, INT, SAVE, CONT]
ALL_COLS  = [FIRST, LAST, FILE, *BOOL_COLS]


# ---------------- Dropbox auth (access token OR refresh token) ----------------
def _access_token_from_refresh() -> str | None:
    """Exchange refresh token -> short-lived access token (~4h)."""
    s = st.secrets
    need = ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN")
    if not all(k in s for k in need):
        return None
    r = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={
            "refresh_token": s["DROPBOX_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
            "client_id": s["DROPBOX_APP_KEY"],
            "client_secret": s["DROPBOX_APP_SECRET"],
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]

@st.cache_resource(show_spinner=False)
def _build_dbx_client() -> dropbox.Dropbox:
    s = st.secrets
    tok = s.get("DROPBOX_ACCESS_TOKEN") or _access_token_from_refresh()
    if not tok:
        raise RuntimeError(
            "Dropbox token missing. Provide either DROPBOX_ACCESS_TOKEN "
            "or the trio DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN."
        )
    return dropbox.Dropbox(tok)

def _refresh_dbx_client() -> dropbox.Dropbox:
    tok = _access_token_from_refresh()
    return dropbox.Dropbox(tok) if tok else _build_dbx_client()

DBX = None
dbx_error = None
try:
    DBX = _build_dbx_client()
except Exception as e:
    dbx_error = str(e)

# Sidebar diag
st.sidebar.subheader("Status")
st.sidebar.write("🔐 Token:", "✅" if DBX else f"❌ {dbx_error or ''}")
st.sidebar.write("📄 CSV (shared link):", "✅" if STATE_SHARED_CSV_URL else "❌")
st.sidebar.write("📝 Write path (App Folder):", STATE_DBX_PATH)


# ---------------- Helpers ----------------
def _norm(s: str) -> str:
    """Normalize string: lower, trim, remove diacritics, collapse spaces."""
    if s is None:
        return ""
    s = str(s).strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = " ".join(s.lower().split())
    return s

def _ensure_schema(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in (FIRST, LAST, FILE):
        if c not in df: df[c] = ""
    for c in BOOL_COLS:
        if c not in df:
            df[c] = False
        else:
            ser = df[c].astype(str).str.strip().str.lower()
            df[c] = ser.map({
                "true": True, "1": True, "yes": True, "y": True,
                "false": False, "0": False, "no": False, "n": False
            }).fillna(False).astype(bool)
    return df[[FIRST, LAST, FILE, *BOOL_COLS]]

def _force_dl1(url: str) -> str:
    """Ensure dl=1 so requests.get returns CSV bytes (not an HTML page)."""
    if not url:
        return url
    if "dl=" in url:
        base, _, tail = url.partition("dl=")
        rest = tail.split("&", 1)[1] if "&" in tail else ""
        return base + "dl=1" + (("&" + rest) if rest else "")
    return (url + ("&" if "?" in url else "?") + "dl=1")

@st.cache_data(show_spinner=False, ttl=10)
def fetch_state_df() -> pd.DataFrame:
    url = _force_dl1(STATE_SHARED_CSV_URL)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content))  # comma-separated CSV
    return _ensure_schema(df)

def _ensure_folder_tree(dbx: dropbox.Dropbox, path: str):
    from posixpath import dirname
    parent = dirname(path.rstrip("/"))
    if parent and parent != "/":
        try:
            dbx.files_create_folder_v2(parent)
        except dropbox.exceptions.ApiError:
            pass

def _upload_with_auto_refresh(data: bytes, path: str) -> tuple[bool, str | None]:
    global DBX
    try:
        _ensure_folder_tree(DBX, path)
        DBX.files_upload(data, path=path, mode=dropbox.files.WriteMode("overwrite"))
        return (True, None)
    except dropbox.exceptions.AuthError:
        try:
            DBX = _refresh_dbx_client()
            _ensure_folder_tree(DBX, path)
            DBX.files_upload(data, path=path, mode=dropbox.files.WriteMode("overwrite"))
            return (True, None)
        except Exception as e2:
            return (False, f"AuthError after refresh: {e2}")
    except dropbox.exceptions.ApiError as e:
        return (False, f"ApiError: {e}")
    except Exception as e:
        return (False, f"Write error: {e}")

def write_state_df(full_df: pd.DataFrame) -> tuple[bool, str | None]:
    """Write whole CSV to Dropbox (with auto-refresh retry)."""
    if DBX is None:
        return (False, "Dropbox not configured")
    out = _ensure_schema(full_df).copy()
    for c in BOOL_COLS:
        out[c] = out[c].astype(int)  # store as 0/1
    bio = io.BytesIO()
    out.to_csv(bio, index=False)
    return _upload_with_auto_refresh(bio.getvalue(), STATE_DBX_PATH)

def _key(df_like: pd.DataFrame) -> pd.Series:
    return (
        df_like[FIRST].fillna("").astype(str).str.strip().str.lower() + "||" +
        df_like[LAST ].fillna("").astype(str).str.strip().str.lower() + "||" +
        df_like[FILE ].fillna("").astype(str).str.strip()
    )

def _apply_optimistic(full_df: pd.DataFrame, delta_rows: pd.DataFrame) -> pd.DataFrame:
    """Apply boolean deltas (by key) to the full dataframe (optimistic UI)."""
    if delta_rows.empty:
        return full_df
    full = full_df.copy()
    full["_k"] = _key(full)
    tmp = delta_rows.copy()
    tmp["_k"] = _key(tmp)
    # merge only on existing bool columns (we ensure them below anyway)
    cols = ["_k"] + [c for c in BOOL_COLS if c in tmp.columns]
    full = full.merge(tmp[cols], on="_k", how="left", suffixes=("", "_new"))
    for c in BOOL_COLS:
        newc = f"{c}_new"
        if newc in full:
            full[c] = full[newc].combine_first(full[c]).astype(bool)
            full.drop(columns=[newc], inplace=True)
    full.drop(columns=["_k"], inplace=True)
    return full

def _build_delta_from_editor(grid_df: pd.DataFrame, edited_rows: dict) -> pd.DataFrame:
    """From st.data_editor edited_rows -> delta DataFrame with all expected columns."""
    expected_cols = [FIRST, LAST, FILE, *BOOL_COLS]
    if not edited_rows:
        return pd.DataFrame(columns=expected_cols)

    out = []
    for i, changes in edited_rows.items():
        i = int(i)
        if 0 <= i < len(grid_df):
            base = grid_df.iloc[i][[FIRST, LAST, FILE]].to_dict()
            dirty = False
            for col, val in changes.items():
                if col in BOOL_COLS:
                    base[col] = bool(val)
                    dirty = True
            if dirty:
                out.append(base)

    if not out:
        return pd.DataFrame(columns=expected_cols)

    df = pd.DataFrame(out)
    for c in BOOL_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    return df.reindex(columns=expected_cols)

def _flush_to_disk(ok_text: str, err_text: str) -> None:
    """Write current session_state.df to disk with toast/error and reset buffer flags."""
    ok, err = write_state_df(st.session_state.df)
    st.session_state.last_batch_write = time.time()
    st.session_state.buffer_dirty = False
    if ok:
        st.toast(ok_text)
    else:
        st.error(f"{err_text}: {err}")

def _compute_view_and_key(base_df: pd.DataFrame, query: str) -> tuple[pd.DataFrame, str]:
    """Return filtered view_df and a stable key to detect when grid must be rebuilt."""
    query_norm = _norm(query)
    if query_norm:
        tokens = [t for t in query_norm.split() if t.strip()]
        mask = pd.Series(True, index=base_df.index)
        for t in tokens:
            mask &= base_df["_full"].str.contains(t, na=False)
        view_df = base_df[mask].copy()
    else:
        view_df = base_df.copy()
    key = f"q::{query_norm}::n{len(view_df)}"
    return view_df, key

def _reset_grid(view_df: pd.DataFrame, filter_key: str) -> None:
    """Reset editor widget state and replace the grid source."""
    st.session_state.pop(EDITOR_KEY, None)
    st.session_state.grid_df = view_df[[FIRST, LAST, FILE, *BOOL_COLS]].copy()
    st.session_state[GRID_FILTER_KEY] = filter_key


# ---------------- Session bootstrap ----------------
if "df" not in st.session_state:
    try:
        st.session_state.df = fetch_state_df()
        # colonne full-name normalisée pour la recherche "nom prénom" (multi-mots, sans accents)
        st.session_state.df["_full"] = (
            st.session_state.df[FIRST].fillna("").astype(str) + " " +
            st.session_state.df[LAST].fillna("").astype(str)
        ).map(_norm)
    except Exception as e:
        st.error(f"Failed to load CSV from shared link: {e}")
        st.stop()

# Save/buffer controls defaults
st.session_state.setdefault("buffer_dirty", False)
st.session_state.setdefault("last_batch_write", time.time())
st.session_state.setdefault("last_save_ts", 0.0)
st.session_state.setdefault("q", "")
st.session_state.setdefault("prev_q", "")

# ---------------- Search controls ----------------
cc, ic = st.columns([1, 4])
with cc:
    if st.button("Clear search", use_container_width=True):
        if st.session_state.buffer_dirty:
            _flush_to_disk("Saved pending changes ✅", "Save failed")
        st.session_state.q = ""
        st.session_state.prev_q = ""
        # Reset stable grid + editor widget state
        st.session_state.pop("grid_df", None)
        st.session_state.pop(GRID_FILTER_KEY, None)
        st.session_state.pop(EDITOR_KEY, None)
        st.rerun()

    # Save now juste en dessous de Clear search
    if st.button("Save now", key="save_top", use_container_width=True):
        st.session_state._want_save = True  # flag pour déclencher la sauvegarde plus bas

with ic:
    st.text_input("Search (first/last name)", key="q", placeholder="e.g. Kadri Farouk")

filter_active = bool(st.session_state.q.strip())

# ---- Flush when toggling filter state (both directions) ----
# A) no filter -> filter ON : flush buffered edits first
if (not st.session_state.prev_q.strip()) and filter_active and st.session_state.buffer_dirty:
    _flush_to_disk("Saved pending changes before applying filter ✅", "Save failed")

# B) filter -> no filter : matérialiser les deltas en mémoire du widget puis flush
if st.session_state.prev_q.strip() and (not filter_active):
    ed_state_pre = st.session_state.get(EDITOR_KEY, {})
    edited_rows_pre = ed_state_pre.get("edited_rows", {})
    if edited_rows_pre:
        df_tmp = _build_delta_from_editor(st.session_state.get("grid_df", pd.DataFrame()), edited_rows_pre)
        if not df_tmp.empty:
            st.session_state.df = _apply_optimistic(st.session_state.df, df_tmp)
    _flush_to_disk("Saved changes before removing filter ✅", "Save failed")

st.session_state.prev_q = st.session_state.q

# ---------------- Compute current view (do NOT rebuild grid every rerun) ----------------
base_df = st.session_state.df
view_df, current_filter_key = _compute_view_and_key(base_df, st.session_state.q)

if ("grid_df" not in st.session_state) or (st.session_state.get(GRID_FILTER_KEY) != current_filter_key):
    _reset_grid(view_df, current_filter_key)

# ---------------- Grid (stable source; no replacement during the same rerun) ----------------
st.write("### Candidates")

st.data_editor(
    st.session_state.grid_df,   # IMPORTANT: keep this stable within the rerun
    key=EDITOR_KEY,             # so we can read its internal state
    column_config={
        FIRST: st.column_config.TextColumn("First name", disabled=True),
        LAST:  st.column_config.TextColumn("Last name", disabled=True),
        FILE:  st.column_config.LinkColumn("CV", display_text="Open"),
        SEEN:  st.column_config.CheckboxColumn("Profile viewed"),
        INT:   st.column_config.CheckboxColumn("Interested in viewing profile"),
        SAVE:  st.column_config.CheckboxColumn("CV saved"),
        CONT:  st.column_config.CheckboxColumn("Candidate contacted"),
    },
    hide_index=True,
    use_container_width=True,
    num_rows="fixed",
)

# ---------------- Read changes from editor state (no DataFrame diff) ----------------
ed_state = st.session_state.get(EDITOR_KEY, {})
edited_rows = ed_state.get("edited_rows", {})  # {row_idx: {col: new_val, ...}}

delta_df = _build_delta_from_editor(st.session_state.grid_df, edited_rows)

# ---------------- Save policy ----------------
now = time.time()

if not delta_df.empty:
    # Optimistic update on the full DF (source of truth),
    # but DO NOT rebuild grid_df in this rerun (avoids click flicker)
    st.session_state.df = _apply_optimistic(st.session_state.df, delta_df)

    if filter_active:
        # Immediate autosave (debounced)
        if (now - st.session_state.last_save_ts) >= AUTOSAVE_DEBOUNCE_SEC:
            _flush_to_disk("Saved change(s) ✅", "Auto-save failed")
            st.session_state.last_save_ts = now
    else:
        # No filter → buffer only (no immediate write)
        st.session_state.buffer_dirty = True
        secs = max(0, int(BATCH_SAVE_INTERVAL_SEC - (now - st.session_state.last_batch_write)))
        st.info(f"Pending changes buffered. Auto-saving in ~{secs}s (or when you start filtering).")

# Traitement du bouton Save now (haut de page)
if st.session_state.pop("_want_save", False):
    _flush_to_disk("Saved ✅", "Save failed")

# Periodic auto-flush for buffered mode (no filter)
if (not filter_active) and st.session_state.buffer_dirty:
    if (now - st.session_state.last_batch_write) >= BATCH_SAVE_INTERVAL_SEC:
        _flush_to_disk("Buffered changes auto-saved ✅", "Auto-save failed")

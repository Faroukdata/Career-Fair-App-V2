import io
import time
import pathlib
import pandas as pd
import streamlit as st
import requests, dropbox
import unicodedata

# ================== SETTINGS ==================
APP_TITLE  = "Capgemini Career Fair"
PAGE_ICON  = "🎓"

# Read (fast): public shared CSV URL (dl=0 or dl=1, force dl=1)
STATE_SHARED_CSV_URL = st.secrets.get("STATE_SHARED_CSV_URL_CAPGEMINI")

# Write (API): path inside your Dropbox App Folder
STATE_DBX_PATH = st.secrets.get("STATE_DBX_PATH_CAPGEMINI")

# Auto-refresh (poll)
ENABLE_REMOTE_POLL     = True    # active/désactive la détection des changements distants
HASH_CHECK_TTL_SEC     = 3       # toutes les 3s on vérifie le hash Dropbox (léger)
REFRESH_MS             = 2500    # cadence de refresh UI (2.5s)

LOGO_PATH = "images/capgemini.png"

# UI keys
EDITOR_KEY = "grid_all"       # state key pour st.data_editor
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
        "<h1 class='app-title' style='margin:0'>Capgemini Career Fair</h1>"
        "<p class='app-subtitle'>Search • Direct CV links • Autosave (2s) avec merge sûr • Auto-refresh</p>"
        "<hr class='hr-soft'/>",
        unsafe_allow_html=True,
    )


# ---------------- Columns ----------------
FIRST, LAST, FILE = "first_name", "last_name", "file_name"   # FILE is a full URL
SEEN, INT, SAVE, CONT = "seen", "intend_view", "cv_saved", "contacted"
BOOL_COLS = [SEEN, INT, SAVE, CONT]
TEXT_COLS = [FIRST, LAST, FILE]
ALL_COLS  = [FIRST, LAST, FILE, *BOOL_COLS]


# ---------------- Dropbox auth (access token OR refresh token) ----------------
def _access_token_from_refresh() -> str | None:
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
    if s is None:
        return ""
    s = str(s).strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = " ".join(s.lower().split())
    return s

def _ensure_schema(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in TEXT_COLS:
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
    return df[[*TEXT_COLS, *BOOL_COLS]]

def _force_dl1(url: str) -> str:
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
    df = pd.read_csv(io.BytesIO(r.content))
    return _ensure_schema(df)

# Dropbox I/O helpers (read fresh / write overwrite with auto-refresh)

def _ensure_folder_tree(dbx: dropbox.Dropbox, path: str):
    from posixpath import dirname
    parent = dirname(path.rstrip("/"))
    if parent and parent != "/":
        try:
            dbx.files_create_folder_v2(parent)
        except dropbox.exceptions.ApiError:
            pass

@st.cache_data(show_spinner=False, ttl=1)
def _download_current_df_from_dbx() -> pd.DataFrame:
    """Fresh read from Dropbox API to avoid CDN cache."""
    if DBX is None:
        raise RuntimeError("Dropbox not configured")
    md, resp = DBX.files_download(STATE_DBX_PATH)
    return _ensure_schema(pd.read_csv(io.BytesIO(resp.content)))

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

# -------- Remote hash (léger) pour auto-refresh --------
@st.cache_data(show_spinner=False, ttl=1)
def _get_remote_hash() -> str | None:
    """Retourne content_hash du fichier côté Dropbox (sans télécharger le CSV)."""
    if DBX is None:
        return None
    try:
        md = DBX.files_get_metadata(STATE_DBX_PATH)
        # md peut être FileMetadata avec .content_hash
        if hasattr(md, "content_hash"):
            return md.content_hash
    except Exception:
        return None
    return None

# ---------------- Keys & snapshots ----------------

def _key(df_like: pd.DataFrame) -> pd.Series:
    return (
        df_like[FIRST].fillna("").astype(str).str.strip().str.lower() + "||" +
        df_like[LAST ].fillna("").astype(str).str.strip().str.lower() + "||" +
        df_like[FILE ].fillna("").astype(str).str.strip()
    )

# Keep an immutable snapshot to compute deltas (base)
def _snapshot_base():
    st.session_state.base_df = _ensure_schema(st.session_state.df).copy(deep=True)

# ---------------- Merge (3-way) ----------------

def _index_by_key(df: pd.DataFrame) -> pd.DataFrame:
    d = _ensure_schema(df).copy()
    d["_k"] = _key(d)
    return d.set_index("_k")

def _three_way_merge(base: pd.DataFrame, ours: pd.DataFrame, theirs: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """Retourne (merged_df, conflicts_list). Booleans: OR sur double modif. Text: ours gagne si double modif."""
    B, O, T = (_index_by_key(base), _index_by_key(ours), _index_by_key(theirs))
    all_idx = B.index.union(O.index).union(T.index)
    B, O, T = B.reindex(all_idx), O.reindex(all_idx), T.reindex(all_idx)

    out = T[[*TEXT_COLS, *BOOL_COLS]].copy()  # base "theirs"
    conflicts = []

    # TEXT COLS
    for c in TEXT_COLS:
        b = B[c].astype("string") if c in B else pd.Series(index=all_idx, dtype="string")
        o = O[c].astype("string") if c in O else pd.Series(index=all_idx, dtype="string")
        t = T[c].astype("string") if c in T else pd.Series(index=all_idx, dtype="string")
        changed_o = (o != b)
        changed_t = (t != b)
        only_o = changed_o & ~changed_t
        only_t = changed_t & ~changed_o
        both   = changed_o & changed_t
        col = out[c].astype("string")
        col[only_o] = o[only_o]
        col[only_t] = t[only_t]
        if both.any():
            conflicts.extend([f"{c}@{idx}" for idx in both[both].index.tolist()])
            col[both] = o[both]  # ours wins
        col = col.fillna("")
        out[c] = col

    # BOOL COLS: OR si double modif
    for c in BOOL_COLS:
        b = B[c].astype("boolean") if c in B else pd.Series(index=all_idx, dtype="boolean")
        o = O[c].astype("boolean") if c in O else pd.Series(index=all_idx, dtype="boolean")
        t = T[c].astype("boolean") if c in T else pd.Series(index=all_idx, dtype="boolean")
        changed_o = (o != b) & ~(o.isna() & b.isna())
        changed_t = (t != b) & ~(t.isna() & b.isna())
        only_o = changed_o & ~changed_t
        only_t = changed_t & ~changed_o
        both   = changed_o & changed_t
        col = out[c].astype("boolean")
        col[only_o] = o[only_o]
        col[only_t] = t[only_t]
        col[both]   = (o[both].fillna(False) | t[both].fillna(False))
        out[c] = col.fillna(False).astype(bool)

    return out.reset_index(drop=True), conflicts

# ---------------- Optimistic UI apply ----------------

def _apply_optimistic(full_df: pd.DataFrame, delta_rows: pd.DataFrame) -> pd.DataFrame:
    if delta_rows.empty:
        return full_df
    full = full_df.copy()
    full["_k"] = _key(full)
    tmp = delta_rows.copy(); tmp["_k"] = _key(tmp)
    cols = ["_k"] + [c for c in (TEXT_COLS + BOOL_COLS) if c in tmp.columns]
    full = full.merge(tmp[cols], on="_k", how="left", suffixes=("", "_new"))
    for c in TEXT_COLS + BOOL_COLS:
        newc = f"{c}_new"
        if newc in full:
            if c in BOOL_COLS:
                full[c] = full[newc].combine_first(full[c]).astype(bool)
            else:
                full[c] = full[newc].combine_first(full[c]).astype(str)
            full.drop(columns=[newc], inplace=True)
    full.drop(columns=["_k"], inplace=True)
    return full

# ---------------- Editor delta build ----------------

def _build_delta_from_editor(grid_df: pd.DataFrame, edited_rows: dict) -> pd.DataFrame:
    expected_cols = [*TEXT_COLS, *BOOL_COLS]
    if not edited_rows:
        return pd.DataFrame(columns=expected_cols)

    out = []
    for i, changes in edited_rows.items():
        i = int(i)
        if 0 <= i < len(grid_df):
            base = grid_df.iloc[i][TEXT_COLS].to_dict()
            dirty = False
            for col, val in changes.items():
                if col in BOOL_COLS:
                    base[col] = bool(val); dirty = True
                elif col in TEXT_COLS:
                    base[col] = str(val); dirty = True
            if dirty:
                out.append(base)

    if not out:
        return pd.DataFrame(columns=expected_cols)

    df = pd.DataFrame(out)
    for c in BOOL_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    for c in TEXT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    return df.reindex(columns=expected_cols)

# ---------------- Compute view & key ----------------

def _compute_view_and_key(base_df: pd.DataFrame, query: str) -> tuple[pd.DataFrame, str]:
    query_norm = _norm(query)
    if query_norm:
        tokens = [t for t in query_norm.split() if t.strip()]
        mask = pd.Series(True, index=base_df.index)
        if "_full" not in base_df:
            tmp_full = (base_df[FIRST].fillna("").astype(str) + " " + base_df[LAST].fillna("").astype(str)).map(_norm)
        else:
            tmp_full = base_df["_full"]
        for t in tokens:
            mask &= tmp_full.str.contains(t, na=False)
        view_df = base_df[mask].copy()
    else:
        view_df = base_df.copy()
    key = f"q::{query_norm}::n{len(view_df)}"
    return view_df, key

# ---------------- Grid reset ----------------

def _reset_grid(view_df: pd.DataFrame, filter_key: str) -> None:
    st.session_state.pop(EDITOR_KEY, None)
    st.session_state.grid_df = view_df[[*TEXT_COLS, *BOOL_COLS]].copy()
    st.session_state[GRID_FILTER_KEY] = filter_key

# ---------------- Save/flush with MERGE ----------------

def _flush_to_disk(ok_text: str, err_text: str) -> None:
    """Merge sûr: lit la version distante, merge (base/local/remote), puis write overwrite.
    Met à jour le snapshot base si succès."""
    try:
        try:
            remote_df = _download_current_df_from_dbx()
        except Exception:
            remote_df = fetch_state_df()

        base_df = st.session_state.get("base_df")
        if base_df is None:
            base_df = remote_df.copy()

        merged, conflicts = _three_way_merge(base_df, st.session_state.df, remote_df)

        # write
        out = _ensure_schema(merged).copy()
        for c in BOOL_COLS:
            out[c] = out[c].astype(int)
        bio = io.BytesIO(); out.to_csv(bio, index=False)
        ok, err = _upload_with_auto_refresh(bio.getvalue(), STATE_DBX_PATH)

        st.session_state.last_batch_write = time.time()
        st.session_state.buffer_dirty = False
        if ok:
            st.session_state.df = merged
            _snapshot_base()
            if conflicts:
                st.warning(f"Conflits résolus automatiquement: {len(conflicts)} (vos valeurs gardées pour champs texte)")
            st.toast(ok_text)
        else:
            st.error(f"{err_text}: {err}")
    except Exception as e:
        st.error(f"{err_text}: {e}")

# ---------------- Session bootstrap ----------------
if "df" not in st.session_state:
    try:
        st.session_state.df = fetch_state_df()
        st.session_state.df["_full"] = (
            st.session_state.df[FIRST].fillna("").astype(str) + " " +
            st.session_state.df[LAST].fillna("").astype(str)
        ).map(_norm)
        _snapshot_base()
    except Exception as e:
        st.error(f"Failed to load CSV from shared link: {e}")
        st.stop()

# Defaults (sans logique buffer)
st.session_state.setdefault("q", "")
st.session_state.setdefault("prev_q", "")
st.session_state.setdefault("last_auto_save", 0.0)

# Auto-refresh state
st.session_state.setdefault("last_seen_hash", None)
st.session_state.setdefault("last_hash_check", 0.0)

# ---------------- Search controls ----------------
cc, ic = st.columns([1, 4])
with cc:
    if st.button("Clear search", use_container_width=True):
        st.session_state.q = ""
        st.session_state.prev_q = ""
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

# ---- Flush when toggling filter state (keep only B) ----
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
    st.session_state.grid_df,
    key=EDITOR_KEY,
    column_config={
        FIRST: st.column_config.TextColumn("First name"),
        LAST:  st.column_config.TextColumn("Last name"),
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

# ---------------- Apply edits (optimistic), no buffer/debounce ----------------
now = time.time()
if not delta_df.empty:
    st.session_state.df = _apply_optimistic(st.session_state.df, delta_df)

# Traitement du bouton Save now
if st.session_state.pop("_want_save", False):
    _flush_to_disk("Saved ✅", "Save failed")

# ---------------- Auto-save périodique (toutes les 5s) ----------------
if (now - st.session_state["last_auto_save"]) >= 5:
    _flush_to_disk("Saved ✅", "Auto-save failed")
    st.session_state["last_auto_save"] = now

# ---------------- Auto-refresh (poll hash) ----------------
if ENABLE_REMOTE_POLL:
    # 1) Vérifie périodiquement le hash Dropbox (léger, pas de download)
    should_check = (now - st.session_state["last_hash_check"]) >= HASH_CHECK_TTL_SEC
    if should_check:
        rhash = _get_remote_hash()
        st.session_state["last_hash_check"] = now
        if rhash and st.session_state["last_seen_hash"] is None:
            st.session_state["last_seen_hash"] = rhash
        elif rhash and rhash != st.session_state["last_seen_hash"]:
            # Un autre utilisateur a modifié le fichier → merge 3-voies
            try:
                remote_df = _download_current_df_from_dbx()
            except Exception:
                remote_df = fetch_state_df()

            base_df = st.session_state.get("base_df", remote_df.copy())
            ours_df = st.session_state.df

            merged, conflicts = _three_way_merge(base_df, ours_df, remote_df)
            st.session_state.df = merged
            _snapshot_base()
            st.session_state["last_seen_hash"] = rhash

            # Rebuild grid sans changer le filtre
            base_df_for_view = st.session_state.df
            view_df, current_filter_key = _compute_view_and_key(base_df_for_view, st.session_state.q)
            _reset_grid(view_df, current_filter_key)

            msg = "Données mises à jour depuis Dropbox 🔄 (merge appliqué)"
            if conflicts:
                msg += f" • {len(conflicts)} conflit(s) résolu(s)"
            st.toast(msg)

    # 2) Déclenche l’auto-refresh UI toutes REFRESH_MS ms (API stable)
    try:
        st.autorefresh(interval=REFRESH_MS, key="__poll_remote__")
    except Exception:
        # Compat anciennes versions de Streamlit
        if hasattr(st, "experimental_autorefresh"):
            st.experimental_autorefresh(interval=REFRESH_MS, key="__poll_remote__")

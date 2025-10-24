# utils.py
import os, unicodedata
import pandas as pd
from dataclasses import dataclass
from urllib.parse import urlparse
import os.path as _osp

@dataclass(frozen=True)
class Col:
    FIRST = "first_name"
    LAST  = "last_name"
    URL   = "cv_url"        # optionnel (on peut en déduire file_name)
    FILE  = "file_name"     # juste le nom du fichier
    SEEN  = "seen"
    INT   = "intend_view"
    SAVE  = "cv_saved"
    CONT  = "contacted"

STATE_COLS      = [Col.SEEN, Col.INT, Col.SAVE, Col.CONT]
BASE_OUT_COLS   = [Col.FIRST, Col.LAST, Col.FILE]
STATE_OUT_COLS  = [Col.FIRST, Col.LAST, Col.FILE, *STATE_COLS]

def _norm(s: str) -> str:
    if s is None: return ""
    s = str(s).strip()
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def _basename_from_url(u: str) -> str:
    if not u or not isinstance(u, str): return ""
    try:
        return _osp.basename(urlparse(u).path) or ""
    except Exception:
        return ""

def normalize_base_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise un DataFrame base en colonnes (first_name, last_name, file_name)."""
    df = df.copy()
    for c in (Col.FIRST, Col.LAST):
        if c not in df.columns: df[c] = ""
    if Col.FILE not in df.columns:
        df[Col.FILE] = ""
    if Col.URL in df.columns:
        mask = df[Col.FILE].isna() | (df[Col.FILE].astype(str).str.strip() == "")
        df.loc[mask, Col.FILE] = df.loc[mask, Col.URL].apply(_basename_from_url)
    # nettoyer file_name
    df[Col.FILE] = (
        df[Col.FILE].fillna("").astype(str).str.strip()
          .str.replace("\\", "", regex=False).str.replace("/", "", regex=False)
    )
    return df[[Col.FIRST, Col.LAST, Col.FILE]].copy()

def _key_series(df: pd.DataFrame) -> pd.Series:
    fn = df[Col.FIRST].fillna("").astype(str).map(_norm).str.lower().str.strip()
    ln = df[Col.LAST ].fillna("").astype(str).map(_norm).str.lower().str.strip()
    fl = df[Col.FILE ].fillna("").astype(str).str.lower().str.strip()
    return fn + "||" + ln + "||" + fl

def merge_base_state(base_df: pd.DataFrame, state_df: pd.DataFrame) -> pd.DataFrame:
    """Affichage: toutes les lignes base + flags si présents (sinon False)."""
    if base_df.empty:
        return pd.DataFrame(columns=STATE_OUT_COLS)
    b = base_df.copy(); b["_k"] = _key_series(b)
    s = state_df.copy()
    if not s.empty:
        s["_k"] = _key_series(s)
        s = s[["_k", *STATE_COLS]]
    merged = b.merge(s, on="_k", how="left")
    merged.drop(columns=["_k"], inplace=True)
    for c in STATE_COLS:
        merged[c] = merged[c].fillna(False).astype(bool)
    return merged[STATE_OUT_COLS].copy()

def update_flag(
    state_df: pd.DataFrame,
    first: str,
    last: str,
    file_name: str,
    col_name: str,
    value: bool,
    save_cb=None,   # callback obligatoire côté app pour sauvegarder (Dropbox)
) -> pd.DataFrame:
    """Ajoute/MàJ UNIQUEMENT la ligne touchée puis appelle save_cb(state_df)."""
    # colonnes garanties
    for c in (Col.FIRST, Col.LAST, Col.FILE):
        if c not in state_df.columns: state_df[c] = ""
    for c in STATE_COLS:
        if c not in state_df.columns: state_df[c] = False
        else: state_df[c] = state_df[c].astype(bool)

    # clé
    target_key = "||".join([
        _norm(first).lower().strip(),
        _norm(last).lower().strip(),
        (file_name or "").lower().strip()
    ])
    tmp = state_df.copy()
    tmp["_k"] = _key_series(tmp)
    idx = tmp.index[tmp["_k"] == target_key]

    if len(idx) == 0:
        new_row = {
            Col.FIRST:first, Col.LAST:last, Col.FILE:file_name,
            Col.SEEN:False, Col.INT:False, Col.SAVE:False, Col.CONT:False
        }
        state_df = pd.concat([state_df, pd.DataFrame([new_row])], ignore_index=True)
        idx = [state_df.index[-1]]

    state_df.loc[idx, col_name] = bool(value)

    if callable(save_cb):
        save_cb(state_df)   # la sauvegarde (Dropbox) est gérée dans l’app
    return state_df

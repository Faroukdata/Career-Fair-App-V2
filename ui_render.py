# ui_render.py
import pathlib
import streamlit as st

def load_css(path: str = "assets/styles.css"):
    p = pathlib.Path(path)
    if p.exists():
        st.markdown(f"<style>{p.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)
    else:
        # styles par défaut si assets/styles.css n'existe pas
        st.markdown("""
        <style>
        .main .block-container{max-width:1100px;padding-top:1rem;padding-bottom:.5rem;}
        h1.app-title{
          font-size:2.2rem; line-height:1.15; margin:0;
          background: linear-gradient(90deg,#111827,#2563eb,#111827);
          -webkit-background-clip: text; background-clip: text; color: transparent;
        }
        p.app-subtitle{color:#6b7280;margin:.25rem 0 .5rem;font-size:1rem}
        .hr-soft{height:1px;border:0;background:linear-gradient(90deg,transparent,#e5e7eb,transparent);margin:.8rem 0 1rem;}
        .logo img{border-radius:12px}
        .badge{display:inline-block;font-size:.75rem;font-weight:600;padding:.25rem .5rem;border-radius:999px;background:#eef2ff;color:#3730a3;border:1px solid #e0e7ff;margin-top:.25rem;}
        </style>
        """, unsafe_allow_html=True)

def render_header(
    title: str = "Career Fair",
    subtitle: str = "Filtrer les candidats • Télécharger les CV • Suivre les décisions",
    logo_path: str | None = None,
    logo_width: int = 130,
    badge_text: str | None = None,     # <- ajouté
):
    col1, col2 = st.columns([1.6, 5])
    with col1:
        if logo_path and pathlib.Path(logo_path).exists():
            st.image(logo_path, width=logo_width)
        else:
            st.markdown("### 🎓")
    with col2:
        st.markdown(f'<h1 class="app-title">{title}</h1>', unsafe_allow_html=True)
        if subtitle:
            st.markdown(f'<p class="app-subtitle">{subtitle}</p>', unsafe_allow_html=True)
        if badge_text:
            st.markdown(f'<span class="badge">{badge_text}</span>', unsafe_allow_html=True)
    st.markdown('<div class="hr-soft"></div>', unsafe_allow_html=True)

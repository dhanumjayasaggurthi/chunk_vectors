import json
import psycopg2
import streamlit as st
from configparser import ConfigParser

from azure_client import get_embeddings   # text-embedding-3-small (EPOD)

import requests
import configparser

# -----------------------------
# Azure Chat
# -----------------------------
CFG_SECTION = "AZURE_OPENAI_CHAT"


def load_config(section: str = CFG_SECTION) -> dict:
    cfg = configparser.ConfigParser()
    cfg.read("config.ini")
    c = cfg[section]
    return {
        "api_key":     c["api_key"],
        "api_base":    c["api_base"].rstrip("/"),
        "api_version": c["api_version"],
        "deployment":  c["deployment"],
    }


def azure_chat(messages, temperature=0.0, max_tokens=900):
    cfg = load_config()
    url = (
        f"{cfg['api_base']}/openai/deployments/"
        f"{cfg['deployment']}/chat/completions"
        f"?api-version={cfg['api_version']}"
    )
    headers = {"api-key": cfg["api_key"], "Content-Type": "application/json"}
    payload = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# -----------------------------
# DB Connection
# -----------------------------
def get_conn():
    cfg = ConfigParser()
    cfg.read("config.ini")
    c = cfg["POSTGRES"]
    return psycopg2.connect(
        host=c["host"],
        port=c["port"],
        dbname=c["database"],
        user=c["user"],
        password=c["password"],
    )


# -----------------------------
# Load distinct values for dropdowns
# -----------------------------
def load_distinct_values(column: str) -> list:
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT {column}
                FROM epod_extracts_core.doc_chunks
                WHERE {column} IS NOT NULL
                ORDER BY {column};
            """)
            return [r[0] for r in cur.fetchall() if r[0]]
    except Exception:
        return []
    finally:
        conn.close()


def vec_to_pgvector_literal(vec) -> str:
    return "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"


def unc_to_file_url(path: str) -> str:
    """
    Convert a Windows path to a file:// URL Chrome can open.
      \\\\server\\share\\file.pdf  ->  file:////server/share/file.pdf
      C:\\Users\\file.pdf              ->  file:///C:/Users/file.pdf
    Returns empty string if path is blank.
    """
    if not path:
        return ""
    p = path.strip()
    if p.startswith("\\\\") or p.startswith("//"):
        clean = p.lstrip("\\/").replace("\\", "/")
        return "file:////" + clean
    clean = p.replace("\\", "/")
    if not clean.startswith("/"):
        clean = "/" + clean
    return "file://" + clean


# -----------------------------
# Retrieval
# -----------------------------
def retrieve_chunks(
    query_vec,
    top_k=8,
    file_name=None,
    chunk_level=None,
    content_type=None,
    section_filter=None,
) -> list:
    qv = vec_to_pgvector_literal(query_vec)

    where  = [
        "chunk_vector IS NOT NULL",
        "chunk_vector != array_fill(0, ARRAY[1536])::vector",
    ]
    params = []

    if file_name:
        where.append("file_name = %s");          params.append(file_name)
    if chunk_level and chunk_level != "All":
        where.append("chunk_level = %s");         params.append(chunk_level)
    if content_type and content_type != "All":
        where.append("%s = ANY(content_types)");  params.append(content_type)
    if section_filter:
        where.append("section_title ILIKE %s");   params.append(f"%{section_filter}%")

    sql = f"""
        SELECT file_name, file_path, chunk_id, chunk_index, chunk_total,
               chunk_level, section_title, page_start, page_end,
               content_types, chunk_text,
               (1 - (chunk_vector <=> %s::vector)) AS similarity
        FROM epod_extracts_core.doc_chunks
        WHERE {" AND ".join(where)}
        ORDER BY chunk_vector <=> %s::vector
        LIMIT %s;
    """

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, [qv] + params + [qv, int(top_k)])
            rows = cur.fetchall()
    finally:
        conn.close()

    results = []
    for (fn, fp, cid, cidx, ctotal, clevel, stitle,
         pstart, pend, ctypes, ctext, sim) in rows:
        results.append({
            "file_name":     fn,
            "file_path":     fp or "",
            "chunk_id":      cid,
            "chunk_index":   cidx,
            "chunk_total":   ctotal,
            "chunk_level":   clevel,
            "section_title": stitle or "—",
            "page_start":    pstart,
            "page_end":      pend,
            "content_types": ctypes or [],
            "text":          str(ctext) if ctext else "",
            "similarity":    float(sim) if sim is not None else 0.0,
        })
    return results


# -----------------------------
# RAG prompt
# -----------------------------
def build_rag_messages(user_question: str, chunks: list) -> list:
    ctx_lines = []
    for i, c in enumerate(chunks, 1):
        ctx_lines.append(
            f"[CHUNK {i}] doc={c['file_name']} "
            f"pages={c['page_start']}-{c['page_end']} "
            f"section={c['section_title']} "
            f"chunk_id={c['chunk_id']}\n"
            f"{c['text']}\n"
        )
    context = "\n---\n".join(ctx_lines) if ctx_lines else "NO_CONTEXT"

    system = (
        "You are a retrieval-augmented assistant for EPOD regulatory documents. "
        "Use ONLY the provided context chunks. "
        "If the answer is not in the context, say you cannot find it in the provided chunks. "
        "Cite sources as: (doc, pages, section, chunk_id)."
    )
    user = (
        f"QUESTION:\n{user_question}\n\n"
        f"CONTEXT CHUNKS:\n{context}\n\n"
        "TASK:\n"
        "1) Answer using only context.\n"
        "2) Provide 3-8 bullet citations mapping key claims to chunks.\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="EPOD RAG",
    page_icon="🗂",
    layout="wide",
)

# ── CSS: identical to RIMDocs RAG ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap');

:root {
    --bg:           #f7f4ef;
    --bg-sidebar:   #efebe3;
    --bg-card:      #ffffff;
    --border:       #e3ded5;
    --accent:       #da7756;
    --accent-hover: #c0613d;
    --accent-ring:  rgba(218,119,86,0.18);
    --text:         #1c1917;
    --text-sub:     #6b6760;
    --text-muted:   #a09d97;
    --radius:       10px;
    --shadow-sm:    0 1px 2px rgba(0,0,0,0.05);
    --shadow:       0 1px 4px rgba(0,0,0,0.06), 0 4px 16px rgba(0,0,0,0.04);
}
html, body, .stApp {
    background: var(--bg) !important;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
    color: var(--text) !important;
}
#MainMenu, footer, .stDeployButton { visibility: hidden !important; }
header[data-testid="stHeader"] { background: transparent !important; box-shadow: none !important; }
section[data-testid="stSidebar"] {
    background: var(--bg-sidebar) !important;
    border-right: 1px solid var(--border) !important;
}
section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2,
section[data-testid="stSidebar"] h3 {
    font-size: 0.68rem !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.08em !important;
    color: var(--text-muted) !important;
    margin: 1.2rem 0 0.4rem !important;
}
section[data-testid="stSidebar"] label {
    font-size: 0.8rem !important;
    color: var(--text-sub) !important;
    font-weight: 400 !important;
}
h1 { font-size: 1.35rem !important; font-weight: 600 !important; color: var(--text) !important; letter-spacing: -0.015em !important; }
h2, h3 { font-size: 0.88rem !important; font-weight: 600 !important; color: var(--text) !important; letter-spacing: -0.005em !important; }
.stMarkdown p, .stMarkdown li { font-size: 0.87rem !important; line-height: 1.68 !important; color: var(--text) !important; }
div[data-testid="stTextArea"] label { font-size: 0.8rem !important; font-weight: 500 !important; color: var(--text-sub) !important; margin-bottom: 4px !important; }
div[data-testid="stTextArea"] textarea {
    background: var(--bg-card) !important;
    border: 1.5px solid var(--border) !important;
    border-radius: var(--radius) !important;
    color: var(--text) !important;
    font-size: 0.88rem !important;
    font-family: 'Inter', sans-serif !important;
    box-shadow: var(--shadow-sm) !important;
    transition: border-color 0.15s, box-shadow 0.15s !important;
}
div[data-testid="stTextArea"] textarea:focus {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px var(--accent-ring) !important;
    outline: none !important;
}
.stButton > button[kind="primary"] {
    background: var(--accent) !important;
    color: #fff !important;
    border: none !important;
    border-radius: var(--radius) !important;
    font-weight: 500 !important;
    font-size: 0.87rem !important;
    padding: 0.45rem 1.3rem !important;
    box-shadow: none !important;
    transition: background 0.15s !important;
    letter-spacing: 0.01em !important;
}
.stButton > button[kind="primary"]:hover { background: var(--accent-hover) !important; box-shadow: 0 0 0 3px var(--accent-ring) !important; }
.stButton > button:not([kind="primary"]) {
    background: var(--bg-card) !important; border: 1.5px solid var(--border) !important;
    color: var(--text) !important; border-radius: var(--radius) !important;
    font-size: 0.84rem !important; transition: border-color 0.15s, box-shadow 0.15s !important;
}
.stButton > button:not([kind="primary"]):hover { border-color: var(--accent) !important; box-shadow: 0 0 0 3px var(--accent-ring) !important; }
div[data-baseweb="select"] > div {
    background: var(--bg-card) !important; border: 1.5px solid var(--border) !important;
    border-radius: var(--radius) !important; color: var(--text) !important;
    font-size: 0.84rem !important; box-shadow: none !important; transition: border-color 0.15s !important;
}
div[data-baseweb="select"] > div:focus-within { border-color: var(--accent) !important; box-shadow: 0 0 0 3px var(--accent-ring) !important; }
div[data-baseweb="popover"] ul {
    background: var(--bg-card) !important; border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important; box-shadow: var(--shadow) !important; padding: 4px !important;
}
li[role="option"] { font-size: 0.84rem !important; border-radius: 6px !important; color: var(--text) !important; }
li[role="option"]:hover { background: var(--bg) !important; }
div[data-testid="stSlider"] label { font-size: 0.8rem !important; color: var(--text-sub) !important; }
div[data-baseweb="slider"] [role="slider"] { background: var(--accent) !important; border-color: var(--accent) !important; box-shadow: 0 0 0 3px var(--accent-ring) !important; }
label[data-testid="stToggleSwitch"] { font-size: 0.84rem !important; color: var(--text-sub) !important; }
div[data-testid="stExpander"] {
    background: var(--bg-card) !important; border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important; box-shadow: var(--shadow-sm) !important;
    margin-bottom: 8px !important; overflow: hidden !important;
}
div[data-testid="stExpander"] summary { font-size: 0.81rem !important; font-weight: 500 !important; color: var(--text) !important; padding: 10px 14px !important; border-radius: var(--radius) !important; }
div[data-testid="stExpander"] summary:hover { background: var(--bg) !important; }
div[data-testid="stExpander"] > div > div { padding: 4px 14px 14px !important; }
code, pre, .stCode { background: var(--bg) !important; border: 1px solid var(--border) !important; border-radius: 8px !important; font-size: 0.76rem !important; color: var(--text) !important; }
div[data-testid="stAlert"] { border-radius: var(--radius) !important; font-size: 0.84rem !important; }
div[data-testid="stDownloadButton"] button {
    background: var(--bg-card) !important; border: 1.5px solid var(--border) !important;
    color: var(--text-sub) !important; border-radius: var(--radius) !important;
    font-size: 0.82rem !important; transition: all 0.15s !important;
}
div[data-testid="stDownloadButton"] button:hover { border-color: var(--accent) !important; color: var(--accent) !important; }
div[data-testid="stSpinner"] > div { border-top-color: var(--accent) !important; }
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

/* ── Title block: same as RIMDocs ── */
.rag-title-block { text-align: center; padding: 0.6rem 0 1.4rem; margin-top: -2rem; }
.rag-title-block .rag-main-title {
    font-size: 2.4rem; font-weight: 700; color: var(--text);
    letter-spacing: -0.03em; margin: 0 0 8px; font-family: 'Inter', sans-serif; line-height: 1.15;
}
.rag-title-block .rag-caption { font-size: 0.84rem; color: var(--text-muted); margin: 0; }

/* ── Column header row with pill badge — badge pushed to far right ── */
.col-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
}
.col-header-title {
    font-size: 0.88rem;
    font-weight: 600;
    color: var(--text);
    letter-spacing: -0.005em;
    display: flex;
    align-items: center;
    gap: 6px;
}
.col-badge {
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--accent);
    background: transparent;
    border: 1.5px solid var(--accent);
    border-radius: 20px;
    padding: 2px 10px;
    line-height: 1.5;
    white-space: nowrap;
}

/* ── Answer card: same as RIMDocs ── */
.rag-answer-card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow); padding: 20px 24px 16px; margin-top: 8px;
    font-size: 0.88rem; line-height: 1.72; color: var(--text);
}
.rag-answer-card p { margin: 0 0 10px; font-size: 0.88rem; line-height: 1.72; color: var(--text); }
.rag-answer-card ul, .rag-answer-card ol { padding-left: 1.3rem; margin: 4px 0 10px; }
.rag-answer-card li { font-size: 0.87rem; color: var(--text); margin-bottom: 5px; line-height: 1.62; }
.rag-answer-card strong { font-weight: 600; color: var(--text); }
.rag-answer-card h3, .rag-answer-card h4 {
    font-size: 0.88rem; font-weight: 600; color: var(--text);
    margin: 16px 0 6px; padding-bottom: 6px; border-bottom: 1px solid var(--border);
}
.rag-answer-card .answer-meta {
    margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border);
    font-size: 0.74rem; color: var(--text-muted); display: flex; align-items: center; gap: 6px;
}

/* ── Sim bar ── */
.sim-bar-wrap { background: var(--border); border-radius: 3px; height: 4px; margin-bottom: 10px; overflow: hidden; }
.sim-bar-fill  { height: 4px; border-radius: 3px; background: var(--accent); }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Title: same structure as RIMDocs
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="rag-title-block">
    <div class="rag-main-title">🗂 EPOD — RAG</div>
    <p class="rag-caption">Vision OCR → section chunking → Azure text-embedding-3-small → Postgres pgvector.</p>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Controls")
    top_k = st.slider("Top K chunks", min_value=3, max_value=20, value=8, step=1)

    st.subheader("Optional Filters")
    filenames_list = load_distinct_values("file_name")

    file_name      = st.selectbox("filename",         ["All"] + filenames_list)
    chunk_level    = st.selectbox("chunk_level",      ["All", "section", "page", "split"])
    content_type   = st.selectbox("content_type",     ["All", "text", "table", "image", "chart"])
    section_filter = st.text_input("section_title contains…", "",
                                    placeholder="e.g. Efficacy, Safety")

    generate_answer = st.toggle("Generate Answer (RAG)", value=True)
    temperature     = st.slider("LLM temperature", 0.0, 0.7, 0.0, 0.1, disabled=not generate_answer)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
query = st.text_area(
    "Ask a question",
    value="What are the key efficacy findings in the study?",
    height=90,
)

colA, colB = st.columns([1, 1], gap="large")

if st.button("🔎 Search", type="primary"):
    if not query.strip():
        st.error("Enter a question.")
        st.stop()

    with st.spinner("Embedding query via Azure..."):
        q_vec = get_embeddings([query])[0]

    with st.spinner("Retrieving top chunks from Postgres (pgvector HNSW)..."):
        chunks = retrieve_chunks(
            query_vec=q_vec,
            top_k=top_k,
            file_name=None      if file_name    == "All" else file_name,
            chunk_level=None    if chunk_level   == "All" else chunk_level,
            content_type=None   if content_type  == "All" else content_type,
            section_filter=section_filter.strip() or None,
        )

    with colA:
        # Header row with pill badge — matches RIMDocs "🗒 Top Retrieved Chunks  [8 CHUNKS]"
        st.markdown(
            f'<div class="col-header">'
            f'<div class="col-header-title">🗒 Top Retrieved Chunks</div>'
            f'<span class="col-badge">{len(chunks)} Chunks</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

        if not chunks:
            st.warning("No chunks found. Try removing filters or changing query wording.")
        else:
            for idx, c in enumerate(chunks, 1):
                sim     = c["similarity"]
                bar_pct = int(sim * 100)
                section = c["section_title"]
                page    = c["page_start"]

                # Expander title: exactly matches RIMDocs format
                # "#1 · filename · page X" (page shows — if missing)
                page_str = str(page) if page is not None else "—"
                title = f"#{idx} · {c['file_name']} · page {page_str}"

                with st.expander(title, expanded=(idx <= 2)):
                    st.markdown(
                        f'<div class="sim-bar-wrap"><div class="sim-bar-fill" style="width:{bar_pct}%;"></div></div>',
                        unsafe_allow_html=True,
                    )
                    ctypes = ", ".join(c["content_types"]) if c["content_types"] else "text"

                    # ── Document hyperlink ──────────────────────────────────
                    file_url = unc_to_file_url(c.get("file_path", ""))
                    if file_url:
                        st.markdown(
                            f'<a href="{file_url}" target="_blank" '
                            f'style="font-size:0.78rem;color:var(--accent);'
                            f'text-decoration:none;display:inline-flex;align-items:center;gap:4px;'
                            f'margin-bottom:8px;border-bottom:1px solid var(--accent);">'
                            f'📂 Open document &nbsp;·&nbsp; <span style="font-size:0.72rem;color:var(--text-muted);">{c.get("file_path","")}</span>'
                            f'</a>',
                            unsafe_allow_html=True,
                        )

                    # ── Metadata code block ─────────────────────────────────
                    st.code(
                        f"file:       {c['file_name']}\n"
                        f"path:       {c.get('file_path', '—')}\n"
                        f"page:       {c['page_start']}\u2013{c['page_end']}\n"
                        f"section:    {c['section_title']}\n"
                        f"level:      {c['chunk_level']}\n"
                        f"chunk_id:   {c['chunk_id']}\n"
                        f"chunk:      {c['chunk_index']+1}/{c['chunk_total']}\n"
                        f"types:      {ctypes}\n"
                        f"similarity: {sim:.6f}\n",
                        language="text",
                    )
                    st.write(c["text"])

    with colB:
        # Header row with pill badge — matches RIMDocs "✦ RAG Answer  [GROUNDED]"
        st.markdown(
            '<div class="col-header">'
            '<div class="col-header-title">✦ RAG Answer</div>'
            '<span class="col-badge">Grounded</span>'
            '</div>',
            unsafe_allow_html=True,
        )

        if not generate_answer:
            st.info("Enable \u201cGenerate Answer (RAG)\u201d in the sidebar to produce an answer.")
        else:
            if not chunks:
                st.warning("No context chunks, so no grounded answer can be generated.")
            else:
                messages = build_rag_messages(query, chunks)
                with st.spinner("Generating grounded answer via Azure chat..."):
                    answer = azure_chat(messages, temperature=temperature, max_tokens=900)

                import markdown as _md
                try:
                    answer_html = _md.markdown(answer, extensions=["extra"])
                except Exception:
                    answer_html = answer.replace("\n", "<br>")

                st.markdown(f"""
<div class="rag-answer-card">
    {answer_html}
    <div class="answer-meta">✦ Generated from {len(chunks)} retrieved chunks · grounded answer only</div>
</div>
""", unsafe_allow_html=True)

                citations_text = "\n".join([
                    f"(file={c['file_name']}, pages={c['page_start']}-{c['page_end']}, "
                    f"section={c['section_title']}, chunk_id={c['chunk_id']})"
                    for c in chunks
                ])
                st.download_button(
                    label="📥 Download Citations",
                    data=citations_text,
                    file_name="rag_citations_epod.txt",
                    mime="text/plain"
                )
                with st.expander("Show context sent to LLM (for transparency)"):
                    st.json(messages)
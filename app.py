"""
Microplastics & Nanoplastics Literature Review Assistant
=========================================================
A Streamlit web application that searches PubMed via the NCBI Entrez
E-utilities (through Biopython), parses results into a pandas DataFrame,
computes a simple keyword-relevance score, highlights keyword matches in
abstracts, and lets the user export the results to CSV or Excel.

Author: (your name)
License: MIT
"""

import io
import time
import re
from datetime import datetime

import pandas as pd
import streamlit as st
from Bio import Entrez

# ----------------------------------------------------------------------
# Page configuration
# ----------------------------------------------------------------------
st.set_page_config(
    page_title="MNP Literature Review Assistant",
    page_icon="🔬",
    layout="wide",
)


# ======================================================================
# PUBMED HELPER FUNCTIONS
# ======================================================================
def build_query(keywords, logic, start_year, end_year):
    """
    Build a PubMed query string from a list of keywords, a boolean
    operator, and an optional publication-date range.

    Parameters
    ----------
    keywords : list[str]
        Cleaned, non-empty search terms.
    logic : str
        Either "AND" or "OR".
    start_year, end_year : int | None
        Inclusive publication-year bounds. If either is None the date
        filter is skipped.

    Returns
    -------
    str
        A PubMed-compatible search expression.
    """
    # Wrap multi-word terms in quotes so PubMed treats them as a phrase.
    terms = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        if " " in kw:
            terms.append(f'"{kw}"')
        else:
            terms.append(kw)

    query = f" {logic} ".join(terms)

    # Append a date range filter if both years are supplied.
    if start_year and end_year:
        query = f"({query}) AND ({start_year}:{end_year}[dp])"

    return query


def search_pubmed(query, max_results, email, api_key=None):
    """
    Run Entrez.esearch and return a list of PubMed IDs (PMIDs).

    Parameters
    ----------
    query : str
        The PubMed search expression.
    max_results : int
        Maximum number of records to request.
    email : str
        NCBI requires an email address for all requests.
    api_key : str | None
        Optional NCBI API key (raises the rate limit from 3 to 10
        requests/second).

    Returns
    -------
    tuple(list[str], int)
        (list of PMIDs, total count reported by PubMed)
    """
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key

    handle = Entrez.esearch(
        db="pubmed",
        term=query,
        retmax=max_results,
        sort="relevance",
    )
    record = Entrez.read(handle)
    handle.close()

    id_list = record.get("IdList", [])
    total_count = int(record.get("Count", 0))
    return id_list, total_count


def _extract_doi(article):
    """Pull a DOI out of a PubmedArticle record, if present."""
    # DOIs can appear in two places depending on the record.
    try:
        ids = article["PubmedData"]["ArticleIdList"]
        for id_obj in ids:
            if id_obj.attributes.get("IdType") == "doi":
                return str(id_obj)
    except (KeyError, TypeError):
        pass

    # Fallback: look inside ELocationID.
    try:
        elocs = article["MedlineCitation"]["Article"].get("ELocationID", [])
        for eloc in elocs:
            if eloc.attributes.get("EIdType") == "doi":
                return str(eloc)
    except (KeyError, TypeError):
        pass

    return ""


def _extract_abstract(medline_article):
    """Concatenate all AbstractText segments into one string."""
    try:
        abstract_parts = medline_article["Abstract"]["AbstractText"]
    except (KeyError, TypeError):
        return ""

    segments = []
    for part in abstract_parts:
        # Structured abstracts have labelled sections (e.g. "METHODS").
        label = part.attributes.get("Label") if hasattr(part, "attributes") else None
        text = str(part)
        segments.append(f"{label}: {text}" if label else text)

    return " ".join(segments)


def _extract_authors(medline_article):
    """Return a formatted author string, e.g. 'Smith J, Doe A'."""
    authors = []
    for author in medline_article.get("AuthorList", []):
        last = author.get("LastName", "")
        initials = author.get("Initials", "")
        if last:
            authors.append(f"{last} {initials}".strip())
        elif author.get("CollectiveName"):
            authors.append(str(author["CollectiveName"]))
    return ", ".join(authors)


def _extract_pub_date(medline_article):
    """Build a readable publication date from the Journal issue data."""
    try:
        pub_date = medline_article["Journal"]["JournalIssue"]["PubDate"]
    except (KeyError, TypeError):
        return ""

    year = pub_date.get("Year", "")
    month = pub_date.get("Month", "")
    day = pub_date.get("Day", "")
    # MedlineDate is used when a normal structured date is unavailable.
    if not year:
        return pub_date.get("MedlineDate", "")
    return " ".join(x for x in [year, month, day] if x)


def fetch_details(id_list, email, api_key=None, batch_size=100):
    """
    Fetch full records for a list of PMIDs via Entrez.efetch and parse
    them into a list of dictionaries.

    Records are fetched in batches to stay within NCBI's URL/length
    limits, with a short sleep between batches to respect rate limits.
    """
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key

    records = []
    # Smaller delay allowed when an API key is present.
    delay = 0.12 if api_key else 0.34

    progress = st.progress(0.0, text="Fetching article details…")
    total = len(id_list)

    for start in range(0, total, batch_size):
        batch_ids = id_list[start:start + batch_size]
        try:
            handle = Entrez.efetch(
                db="pubmed",
                id=",".join(batch_ids),
                rettype="xml",
                retmode="xml",
            )
            parsed = Entrez.read(handle)
            handle.close()
        except Exception as exc:  # network / parse errors
            st.warning(f"A batch failed to download and was skipped: {exc}")
            continue

        for article in parsed.get("PubmedArticle", []):
            try:
                medline = article["MedlineCitation"]
                art = medline["Article"]
                pmid = str(medline["PMID"])

                records.append({
                    "PMID": pmid,
                    "Title": str(art.get("ArticleTitle", "")),
                    "Authors": _extract_authors(art),
                    "Journal": str(art.get("Journal", {}).get("Title", "")),
                    "Publication Date": _extract_pub_date(art),
                    "Abstract": _extract_abstract(art),
                    "DOI": _extract_doi(article),
                    "URL": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                })
            except Exception:
                # Skip any malformed record rather than crashing.
                continue

        progress.progress(
            min((start + batch_size) / total, 1.0),
            text=f"Fetched {min(start + batch_size, total)} / {total} records…",
        )
        time.sleep(delay)

    progress.empty()
    return records


# ======================================================================
# ANALYSIS HELPERS
# ======================================================================
def compute_relevance(abstract, title, keywords):
    """
    Count total case-insensitive keyword occurrences in the title and
    abstract. Title hits are weighted x2 because they are stronger
    relevance signals.
    """
    if not keywords:
        return 0

    text_abstract = (abstract or "").lower()
    text_title = (title or "").lower()
    score = 0
    for kw in keywords:
        kw = kw.strip().lower()
        if not kw:
            continue
        pattern = re.escape(kw)
        score += len(re.findall(pattern, text_abstract))
        score += 2 * len(re.findall(pattern, text_title))
    return score


def highlight_keywords(text, keywords):
    """
    Wrap each keyword occurrence in a <mark> tag for HTML display.
    Longer keywords are highlighted first to avoid nested-match issues.
    """
    if not text:
        return ""
    highlighted = text
    for kw in sorted([k.strip() for k in keywords if k.strip()],
                     key=len, reverse=True):
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
        highlighted = pattern.sub(
            lambda m: f"<mark style='background:#ffe58a'>{m.group(0)}</mark>",
            highlighted,
        )
    return highlighted


def to_excel_bytes(df):
    """Serialize a DataFrame to an in-memory XLSX file for download."""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Literature Review")
    return output.getvalue()


# ======================================================================
# SIDEBAR – SEARCH CONFIGURATION
# ======================================================================
st.sidebar.title("🔧 Search Configuration")

st.sidebar.markdown("**NCBI Credentials**")
ncbi_email = st.sidebar.text_input(
    "NCBI Email (required)",
    help="NCBI requires an email address with every request.",
    placeholder="you@example.com",
)
ncbi_api_key = st.sidebar.text_input(
    "NCBI API Key (optional)",
    type="password",
    help="An API key raises your rate limit from 3 to 10 requests/second.",
)

st.sidebar.markdown("---")
st.sidebar.markdown("**Keywords**")
raw_keywords = st.sidebar.text_area(
    "Enter one keyword/phrase per line",
    value="microplastics\nnanoplastics",
    height=120,
    help='Example:\nmicroplastics\npolystyrene nanoplastics\ntissue accumulation',
)
boolean_logic = st.sidebar.radio(
    "Combine keywords with:",
    options=["AND", "OR"],
    horizontal=True,
)

st.sidebar.markdown("---")
st.sidebar.markdown("**Filters**")
current_year = datetime.now().year
col_a, col_b = st.sidebar.columns(2)
start_year = col_a.number_input(
    "Start Year", min_value=1900, max_value=current_year,
    value=2015, step=1,
)
end_year = col_b.number_input(
    "End Year", min_value=1900, max_value=current_year,
    value=current_year, step=1,
)
max_results = st.sidebar.slider(
    "Max results to fetch", min_value=10, max_value=500, value=50, step=10,
)

search_clicked = st.sidebar.button("🔍 Search PubMed", use_container_width=True)


# ======================================================================
# MAIN PANEL
# ======================================================================
st.title("🔬 Microplastics & Nanoplastics Literature Review Assistant")
st.caption(
    "Search PubMed, score relevance, highlight keywords, and export your "
    "results — all in one place."
)

# Keep results across reruns (e.g. when toggling display options).
if "results_df" not in st.session_state:
    st.session_state.results_df = None
    st.session_state.keywords = []

# --- Run the search ---------------------------------------------------
if search_clicked:
    keywords = [k for k in raw_keywords.splitlines() if k.strip()]

    if not ncbi_email:
        st.error("Please enter your NCBI email address in the sidebar.")
    elif not keywords:
        st.error("Please enter at least one keyword.")
    elif start_year > end_year:
        st.error("Start Year must be less than or equal to End Year.")
    else:
        query = build_query(keywords, boolean_logic, start_year, end_year)
        st.info(f"**PubMed query:** `{query}`")

        try:
            with st.spinner("Searching PubMed…"):
                id_list, total_count = search_pubmed(
                    query, max_results, ncbi_email, ncbi_api_key or None
                )

            if not id_list:
                st.warning("No results found. Try broadening your search.")
                st.session_state.results_df = None
            else:
                st.success(
                    f"PubMed reported {total_count:,} total matches. "
                    f"Fetching details for the top {len(id_list)}."
                )
                records = fetch_details(
                    id_list, ncbi_email, ncbi_api_key or None
                )

                df = pd.DataFrame(records)
                if not df.empty:
                    df["Relevance Score"] = df.apply(
                        lambda r: compute_relevance(
                            r["Abstract"], r["Title"], keywords
                        ),
                        axis=1,
                    )
                    df = df.sort_values(
                        "Relevance Score", ascending=False
                    ).reset_index(drop=True)

                st.session_state.results_df = df
                st.session_state.keywords = keywords

        except Exception as exc:
            st.error(f"An error occurred while contacting PubMed: {exc}")
            st.session_state.results_df = None


# --- Display results --------------------------------------------------
df = st.session_state.results_df
if df is not None and not df.empty:
    keywords = st.session_state.keywords

    # Summary metrics.
    m1, m2, m3 = st.columns(3)
    m1.metric("Articles fetched", len(df))
    m2.metric("Avg. relevance score", round(df["Relevance Score"].mean(), 1))
    m3.metric("Articles with DOI", int((df["DOI"] != "").sum()))

    st.markdown("---")

    # Export buttons.
    exp1, exp2 = st.columns(2)
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    exp1.download_button(
        "⬇️ Download CSV",
        data=csv_bytes,
        file_name="literature_review.csv",
        mime="text/csv",
        use_container_width=True,
    )
    exp2.download_button(
        "⬇️ Download Excel",
        data=to_excel_bytes(df),
        file_name="literature_review.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    st.markdown("---")

    # Display mode toggle.
    view = st.radio(
        "View as:", ["Detailed cards", "Table"], horizontal=True
    )

    if view == "Table":
        st.dataframe(
            df[["Relevance Score", "Title", "Authors", "Journal",
                "Publication Date", "DOI"]],
            use_container_width=True,
            hide_index=True,
        )
    else:
        for _, row in df.iterrows():
            with st.expander(
                f"⭐ {row['Relevance Score']} | {row['Title']}"
            ):
                st.markdown(f"**Authors:** {row['Authors'] or 'N/A'}")
                st.markdown(
                    f"**Journal:** {row['Journal'] or 'N/A'}  |  "
                    f"**Date:** {row['Publication Date'] or 'N/A'}"
                )
                if row["DOI"]:
                    st.markdown(
                        f"**DOI:** [https://doi.org/{row['DOI']}]"
                        f"(https://doi.org/{row['DOI']})"
                    )
                st.markdown(f"**PubMed:** [{row['URL']}]({row['URL']})")
                st.markdown("**Abstract:**")
                if row["Abstract"]:
                    st.markdown(
                        highlight_keywords(row["Abstract"], keywords),
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown("*No abstract available.*")

elif df is not None and df.empty:
    st.warning("The search returned records but none could be parsed.")
else:
    st.info("👈 Configure your search in the sidebar and click **Search PubMed**.")

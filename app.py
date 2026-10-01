"""
Microplastics & Nanoplastics Literature Review Assistant
=========================================================
An advanced Streamlit web application that searches PubMed via the NCBI
Entrez E-utilities (Biopython), enriches results with NIH iCite citation
data, parses records into a rich pandas DataFrame (bibliographic AND
domain-specific fields), computes relevance, summarizes abstract findings,
and presents everything in a polished, tabbed research dashboard with:

  • Custom CSS theming, styled metric cards, and colored tag pills
  • An analytics dashboard (Plotly): publications/year, top journals,
    polymer & study-type distributions, citations-vs-year scatter
  • A keyword co-occurrence network graph
  • Live in-memory filtering (year / tag / open-access) with no re-query
  • A shortlist system to mark papers and export only the chosen ones
  • Clean CSV (UTF-8 BOM) and Excel exports

Author: (your name)
License: MIT
"""

import io
import time
import re
import json
import math
from datetime import datetime
from itertools import combinations
from collections import Counter
from urllib.request import urlopen, Request
from urllib.error import URLError

import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from Bio import Entrez

# ----------------------------------------------------------------------
# Page configuration
# ----------------------------------------------------------------------
st.set_page_config(
    page_title="MNP Literature Review Assistant",
    page_icon="🔬",
    layout="wide",
)

# ----------------------------------------------------------------------
# Custom CSS — theming, cards, pills, header banner
# ----------------------------------------------------------------------
st.markdown(
    """
    <style>
    /* Overall typography */
    html, body, [class*="css"] { font-family: 'Segoe UI', system-ui, sans-serif; }

    /* Gradient header banner */
    .app-header {
        background: linear-gradient(135deg, #0d7377 0%, #14919b 50%, #0b5563 100%);
        padding: 1.6rem 2rem; border-radius: 16px; color: #ffffff;
        margin-bottom: 1.2rem; box-shadow: 0 6px 24px rgba(13,115,119,0.25);
    }
    .app-header h1 { color: #ffffff; margin: 0; font-size: 1.8rem; }
    .app-header p  { color: #d7f3f2; margin: 0.3rem 0 0 0; font-size: 0.95rem; }

    /* Metric cards */
    .metric-card {
        background: #ffffff; border: 1px solid #e6e9ef; border-radius: 14px;
        padding: 1rem 1.2rem; box-shadow: 0 2px 10px rgba(0,0,0,0.04);
        text-align: center;
    }
    .metric-card .value { font-size: 1.8rem; font-weight: 700; color: #0d7377; }
    .metric-card .label { font-size: 0.8rem; color: #6b7280; text-transform: uppercase;
                          letter-spacing: 0.04em; }

    /* Tag pills */
    .pill {
        display: inline-block; padding: 2px 10px; margin: 2px 4px 2px 0;
        border-radius: 999px; font-size: 0.75rem; font-weight: 600;
        color: #ffffff;
    }
    .pill-cat { background: #0d7377; }
    .pill-polymer { background: #b5179e; }
    .pill-study { background: #3a0ca3; }
    .pill-warn { background: #e85d04; }

    /* Article card */
    .article-card {
        background: #ffffff; border: 1px solid #e6e9ef; border-radius: 14px;
        padding: 1rem 1.2rem; margin-bottom: 0.8rem;
        box-shadow: 0 2px 10px rgba(0,0,0,0.04);
    }
    .findings-box {
        background: #f0faf9; border-left: 4px solid #14919b;
        padding: 0.6rem 0.9rem; border-radius: 8px; margin: 0.5rem 0;
    }
    mark { background: #ffe58a; padding: 0 2px; border-radius: 3px; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ======================================================================
# DOMAIN-SPECIFIC TAGGING DICTIONARIES
# ======================================================================
DEFAULT_TAXONOMY = {
    "Polymer Type": {
        "Polyethylene (PE)": ["polyethylene", r"\bPE\b", "LDPE", "HDPE"],
        "Polypropylene (PP)": ["polypropylene", r"\bPP\b"],
        "Polystyrene (PS)": ["polystyrene", r"\bPS\b"],
        "PET": ["polyethylene terephthalate", r"\bPET\b"],
        "PVC": ["polyvinyl chloride", r"\bPVC\b"],
        "PMMA": ["poly(methyl methacrylate)", "polymethyl methacrylate", r"\bPMMA\b"],
        "Nylon/Polyamide": ["nylon", "polyamide"],
        "Polycarbonate (PC)": ["polycarbonate"],
        "Polyurethane (PU)": ["polyurethane"],
    },
    "Size Class": {
        "Nanoplastic": ["nanoplastic", "nano-plastic", "nanoparticle", r"\bnm\b"],
        "Microplastic": ["microplastic", "micro-plastic", r"\b\u00b5m\b", r"\bum\b", "micrometer"],
    },
    "Particle Shape": {
        "Fiber": ["fiber", "fibre", "filament"],
        "Fragment": ["fragment"],
        "Sphere/Bead": ["sphere", "spherical", "bead", "microbead"],
        "Film": ["film", "sheet"],
        "Pellet": ["pellet", "nurdle"],
    },
    "Study Type": {
        "In vitro": ["in vitro", "cell line", "cell culture", "cells"],
        "In vivo": ["in vivo", "mice", "mouse", "rat", "zebrafish", "daphnia",
                    "animal model"],
        "Human": ["human", "patient", "clinical", "placenta", "serum"],
        "Field/Environmental": ["field", "environmental sample", "sampling",
                                "in situ", "wild"],
        "In silico/Modeling": ["in silico", "modeling", "modelling", "simulation"],
        "Review": ["review", "systematic review", "meta-analysis"],
    },
    "Exposure Route": {
        "Ingestion/Oral": ["ingestion", "oral", "dietary", "gavage", "gut",
                           "gastrointestinal"],
        "Inhalation": ["inhalation", "respiratory", "airborne", "pulmonary", "lung"],
        "Dermal": ["dermal", "skin", "cutaneous"],
        "Intravenous": ["intravenous", "injection", "systemic circulation"],
    },
    "Environmental Matrix": {
        "Marine water": ["marine", "seawater", "ocean", "sea water"],
        "Freshwater": ["freshwater", "river", "lake", "pond"],
        "Soil": ["soil", "terrestrial", "sediment"],
        "Air": ["atmospheric", "airborne", "indoor air", "outdoor air"],
        "Drinking water": ["drinking water", "tap water", "bottled water"],
        "Food": ["food", "seafood", "fish", "shellfish", "salt", "honey"],
        "Human tissue": ["tissue", "blood", "placenta", "lung tissue",
                         "liver", "kidney", "brain"],
    },
    "Health Endpoint": {
        "Oxidative stress": ["oxidative stress", "reactive oxygen", r"\bROS\b"],
        "Inflammation": ["inflammation", "inflammatory", "cytokine"],
        "Gut microbiome": ["microbiome", "microbiota", "gut flora"],
        "Neurotoxicity": ["neurotox", "neurolog", "brain", "cognitive"],
        "Reproductive": ["reproductive", "fertility", "sperm", "ovary",
                        "developmental"],
        "Carcinogenicity": ["carcinogen", "cancer", "tumor", "tumour"],
        "Bioaccumulation": ["bioaccumulation", "accumulation", "translocation",
                           "uptake"],
        "Immunotoxicity": ["immunotox", "immune"],
    },
    "Organism": {
        "Zebrafish": ["zebrafish", "danio rerio"],
        "Mouse/Rat": ["mouse", "mice", "rat", "murine"],
        "Human": ["human", "homo sapiens"],
        "Daphnia": ["daphnia"],
        "Fish (other)": ["fish", "teleost"],
        "Mussel/Bivalve": ["mussel", "bivalve", "oyster", "clam"],
        "Algae": ["algae", "microalgae", "phytoplankton"],
    },
    "Analytical Method": {
        "FTIR": ["ftir", "infrared spectroscopy", "fourier transform"],
        "Raman": ["raman"],
        "Py-GC-MS": ["pyrolysis", "py-gc", "gc-ms", "gc/ms"],
        "SEM/TEM": [r"\bSEM\b", r"\bTEM\b", "electron microscop"],
        "Flow cytometry": ["flow cytometry", "cytometer"],
        "Fluorescence": ["fluorescent", "fluorescence microscop"],
    },
}


def tag_text(text, category_dict):
    """Return comma-separated canonical labels whose synonyms appear in text."""
    if not text:
        return ""
    found = []
    lowered = text.lower()
    for label, patterns in category_dict.items():
        for pat in patterns:
            try:
                if re.search(pat, lowered, flags=re.IGNORECASE):
                    found.append(label)
                    break
            except re.error:
                if pat.lower() in lowered:
                    found.append(label)
                    break
    return ", ".join(found)


# ---- Catch-all polymer detection -------------------------------------
POLYMER_SIGNATURE_PATTERNS = [
    r"poly[a-z()\-]+", r"\b[a-z]*plastic[a-z]*",
    r"\bPLA\b", r"\bPBAT\b", r"\bPHA\b", r"\bPHB\b",
    r"\bPTFE\b", r"\bPES\b", r"\bPAN\b", r"\bPVA\b", r"\bPCL\b",
    r"\belastomer[s]?\b", r"\bcopolymer[s]?\b", r"\bresin[s]?\b",
]
POLYMER_SIGNATURE_STOPWORDS = {
    "polymer", "polymers", "polymeric", "polymerization", "polymerisation",
    "polysaccharide", "polyphenol", "polyphenols", "polyp", "polyps",
    "polymorphism", "polymorphic", "polyclonal", "polygon", "polymerase",
    "polyploid", "polydispersity", "polydisperse", "polyvalent", "polyunsaturated",
}


def detect_unclassified_polymers(text, known_polymer_dict):
    """Surface polymer-like tokens NOT in the curated Polymer Type list."""
    if not text:
        return ""
    known_patterns = []
    for patterns in known_polymer_dict.values():
        known_patterns.extend(patterns)

    def is_known(token):
        for pat in known_patterns:
            try:
                if re.fullmatch(pat, token, flags=re.IGNORECASE) or \
                   re.search(pat, token, flags=re.IGNORECASE):
                    return True
            except re.error:
                if pat.lower() in token.lower():
                    return True
        return False

    hits = {}
    for pat in POLYMER_SIGNATURE_PATTERNS:
        try:
            for m in re.finditer(pat, text, flags=re.IGNORECASE):
                token = m.group(0).strip(" -.,;:()")
                low = token.lower()
                if not token or len(low) < 3 or low in POLYMER_SIGNATURE_STOPWORDS:
                    continue
                if is_known(token):
                    continue
                hits.setdefault(low, token)
        except re.error:
            continue
    return ", ".join(sorted(hits.values()))


# ======================================================================
# PUBMED HELPER FUNCTIONS
# ======================================================================
def build_query(keywords, logic, start_year, end_year):
    """Build a PubMed query string from keywords, logic, and a date range."""
    terms = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        terms.append(f'"{kw}"' if " " in kw else kw)
    query = f" {logic} ".join(terms)
    if start_year and end_year:
        query = f"({query}) AND ({start_year}:{end_year}[dp])"
    return query


def search_pubmed(query, max_results, email, api_key=None):
    """Run Entrez.esearch; return (list of PMIDs, total count)."""
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key
    handle = Entrez.esearch(
        db="pubmed", term=query, retmax=max_results, sort="relevance"
    )
    record = Entrez.read(handle)
    handle.close()
    return record.get("IdList", []), int(record.get("Count", 0))


# ---- Field extraction helpers ----------------------------------------
def _extract_doi(article):
    try:
        for id_obj in article["PubmedData"]["ArticleIdList"]:
            if id_obj.attributes.get("IdType") == "doi":
                return str(id_obj)
    except (KeyError, TypeError):
        pass
    try:
        for eloc in article["MedlineCitation"]["Article"].get("ELocationID", []):
            if eloc.attributes.get("EIdType") == "doi":
                return str(eloc)
    except (KeyError, TypeError):
        pass
    return ""


def _extract_pmc(article):
    try:
        for id_obj in article["PubmedData"]["ArticleIdList"]:
            if id_obj.attributes.get("IdType") == "pmc":
                return str(id_obj)
    except (KeyError, TypeError):
        pass
    return ""


def _extract_abstract(art):
    """Concatenate all AbstractText segments, preserving section labels."""
    try:
        parts = art["Abstract"]["AbstractText"]
    except (KeyError, TypeError):
        return ""
    segments = []
    for part in parts:
        label = part.attributes.get("Label") if hasattr(part, "attributes") else None
        text = str(part)
        segments.append(f"{label}: {text}" if label else text)
    return " ".join(segments)


def _split_sentences(text):
    raw = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text.strip())
    return [s.strip() for s in raw if len(s.strip()) > 20]


def extract_key_findings(art, max_bullets=4):
    """
    Produce bulleted 'Key Findings' from the ABSTRACT (whole abstract,
    labels stripped, trailing sentences selected). Bullets joined by ' | '
    so they render cleanly in both dataframe cells and cards.
    """
    try:
        parts = art["Abstract"]["AbstractText"]
    except (KeyError, TypeError):
        return ""
    full = " ".join(str(p) for p in parts).strip()
    if not full:
        return ""
    sentences = _split_sentences(full)
    if not sentences:
        return ""
    selected = sentences[-max_bullets:]
    return " | ".join(f"• {s}" for s in selected)


def _extract_authors(art):
    authors = []
    for a in art.get("AuthorList", []):
        last, initials = a.get("LastName", ""), a.get("Initials", "")
        if last:
            authors.append(f"{last} {initials}".strip())
        elif a.get("CollectiveName"):
            authors.append(str(a["CollectiveName"]))
    return ", ".join(authors)


def _extract_first_affiliation(art):
    for a in art.get("AuthorList", []):
        aff_info = a.get("AffiliationInfo", [])
        if aff_info:
            return str(aff_info[0].get("Affiliation", ""))
    return ""


def _extract_pub_date(art):
    try:
        pdate = art["Journal"]["JournalIssue"]["PubDate"]
    except (KeyError, TypeError):
        return ""
    year = pdate.get("Year", "")
    month = pdate.get("Month", "")
    day = pdate.get("Day", "")
    if not year:
        return pdate.get("MedlineDate", "")
    return " ".join(x for x in [year, month, day] if x)


def _extract_year(art):
    try:
        pdate = art["Journal"]["JournalIssue"]["PubDate"]
        year = pdate.get("Year", "")
        if not year:
            m = re.search(r"\d{4}", pdate.get("MedlineDate", ""))
            year = m.group(0) if m else ""
        return int(year) if year else None
    except (KeyError, TypeError, ValueError):
        return None


def _extract_mesh(medline):
    terms = []
    for mh in medline.get("MeshHeadingList", []):
        try:
            terms.append(str(mh["DescriptorName"]))
        except (KeyError, TypeError):
            continue
    return ", ".join(terms)


def _extract_author_keywords(medline):
    kws = []
    for kw_list in medline.get("KeywordList", []):
        for kw in kw_list:
            kws.append(str(kw))
    return ", ".join(kws)


def _extract_chemicals(medline):
    chems = []
    for chem in medline.get("ChemicalList", []):
        try:
            name = str(chem["NameOfSubstance"])
            if name not in chems:
                chems.append(name)
        except (KeyError, TypeError):
            continue
    return ", ".join(chems)


def _extract_pub_types(art):
    return ", ".join(str(pt) for pt in art.get("PublicationTypeList", []))


def _extract_grants(art):
    agencies = []
    for g in art.get("GrantList", []):
        agency = str(g.get("Agency", ""))
        if agency and agency not in agencies:
            agencies.append(agency)
    return ", ".join(agencies)


# ======================================================================
# CITATION ENRICHMENT (NIH iCite API)
# ======================================================================
def fetch_citations(pmids):
    """Query NIH iCite for citation counts + RCR. Returns {pmid: {...}}."""
    results = {}
    batch_size = 200
    for start in range(0, len(pmids), batch_size):
        batch = pmids[start:start + batch_size]
        url = "https://icite.od.nih.gov/api/pubs?pmids=" + ",".join(batch)
        try:
            req = Request(url, headers={"User-Agent": "MNP-LitReview/1.0"})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for rec in data.get("data", []):
                pmid = str(rec.get("pmid", ""))
                results[pmid] = {
                    "citations": rec.get("citation_count", 0),
                    "rcr": rec.get("relative_citation_ratio"),
                }
        except (URLError, ValueError, TimeoutError):
            continue
        time.sleep(0.2)
    return results


def fetch_details(id_list, email, api_key=None, batch_size=100):
    """Fetch and parse full PubMed records into a list of dicts."""
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key
    records = []
    delay = 0.12 if api_key else 0.34
    progress = st.progress(0.0, text="Fetching article details…")
    total = len(id_list)

    for start in range(0, total, batch_size):
        batch_ids = id_list[start:start + batch_size]
        try:
            handle = Entrez.efetch(
                db="pubmed", id=",".join(batch_ids),
                rettype="xml", retmode="xml",
            )
            parsed = Entrez.read(handle)
            handle.close()
        except Exception as exc:
            st.warning(f"A batch failed to download and was skipped: {exc}")
            continue

        for article in parsed.get("PubmedArticle", []):
            try:
                medline = article["MedlineCitation"]
                art = medline["Article"]
                pmid = str(medline["PMID"])
                abstract = _extract_abstract(art)
                pmc = _extract_pmc(article)
                records.append({
                    "PMID": pmid,
                    "Title": str(art.get("ArticleTitle", "")),
                    "Authors": _extract_authors(art),
                    "First Affiliation": _extract_first_affiliation(art),
                    "Journal": str(art.get("Journal", {}).get("Title", "")),
                    "Publication Date": _extract_pub_date(art),
                    "Year": _extract_year(art),
                    "Abstract": abstract,
                    "Key Findings": extract_key_findings(art),
                    "Abstract Word Count": len(abstract.split()) if abstract else 0,
                    "Has Abstract": bool(abstract),
                    "DOI": _extract_doi(article),
                    "PMCID": pmc,
                    "Open Access (PMC)": bool(pmc),
                    "Publication Type": _extract_pub_types(art),
                    "Language": ", ".join(str(l) for l in art.get("Language", [])),
                    "Country": str(
                        medline.get("MedlineJournalInfo", {}).get("Country", "")
                    ),
                    "MeSH Terms": _extract_mesh(medline),
                    "Chemical Substances": _extract_chemicals(medline),
                    "Author Keywords": _extract_author_keywords(medline),
                    "Funding/Grants": _extract_grants(art),
                    "URL": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                })
            except Exception:
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
    if not keywords:
        return 0
    ta, tt = (abstract or "").lower(), (title or "").lower()
    score = 0
    for kw in keywords:
        kw = kw.strip().lower()
        if not kw:
            continue
        pat = re.escape(kw)
        score += len(re.findall(pat, ta))
        score += 2 * len(re.findall(pat, tt))
    return score


def highlight_keywords(text, keywords):
    if not text:
        return ""
    highlighted = text
    for kw in sorted([k.strip() for k in keywords if k.strip()],
                     key=len, reverse=True):
        pat = re.compile(re.escape(kw), re.IGNORECASE)
        highlighted = pat.sub(
            lambda m: f"<mark>{m.group(0)}</mark>", highlighted
        )
    return highlighted


# ---- Export cleaning -------------------------------------------------
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(value):
    if not isinstance(value, str):
        return value
    cleaned = _HTML_TAG_RE.sub("", value)
    cleaned = cleaned.replace("\u00a0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def clean_dataframe(df):
    # pandas 2.1+ renamed DataFrame.applymap -> DataFrame.map.
    return df.map(clean_text)


def to_excel_bytes(df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Literature Review")
    return output.getvalue()


# ======================================================================
# UI RENDERING HELPERS
# ======================================================================
def metric_card(label, value):
    """Render a styled metric card."""
    st.markdown(
        f"<div class='metric-card'><div class='value'>{value}</div>"
        f"<div class='label'>{label}</div></div>",
        unsafe_allow_html=True,
    )


def pills(values, css_class="pill-cat"):
    """Render a list of strings as colored pills."""
    if not values:
        return ""
    html = ""
    for v in values:
        v = v.strip()
        if v:
            html += f"<span class='pill {css_class}'>{v}</span>"
    return html


# ---- Analytics charts (Plotly) ---------------------------------------
def chart_publications_per_year(df):
    data = df.dropna(subset=["Year"])
    if data.empty:
        return None
    counts = data.groupby("Year").size().reset_index(name="Publications")
    fig = px.bar(
        counts, x="Year", y="Publications",
        title="Publications per Year",
        color="Publications", color_continuous_scale="Teal",
    )
    fig.update_layout(coloraxis_showscale=False, height=380,
                      margin=dict(t=50, b=20, l=20, r=20))
    return fig


def chart_top_journals(df, top_n=10):
    data = df[df["Journal"] != ""]
    if data.empty:
        return None
    counts = (data["Journal"].value_counts().head(top_n)
              .reset_index())
    counts.columns = ["Journal", "Count"]
    fig = px.bar(
        counts, x="Count", y="Journal", orientation="h",
        title=f"Top {top_n} Journals",
        color="Count", color_continuous_scale="Teal",
    )
    fig.update_layout(coloraxis_showscale=False, height=420,
                      yaxis=dict(autorange="reversed"),
                      margin=dict(t=50, b=20, l=20, r=20))
    return fig


def chart_category_distribution(df, category, title):
    """Pie chart of a multi-valued tag column (splits comma lists)."""
    if category not in df.columns:
        return None
    tally = Counter()
    for val in df[category].dropna():
        for item in str(val).split(","):
            item = item.strip()
            if item:
                tally[item] += 1
    if not tally:
        return None
    labels, values = zip(*tally.most_common())
    fig = px.pie(
        names=labels, values=values, title=title, hole=0.45,
    )
    fig.update_layout(height=400, margin=dict(t=50, b=20, l=20, r=20))
    return fig


def chart_citations_scatter(df):
    if "Citation Count" not in df.columns:
        return None
    data = df.dropna(subset=["Year"]).copy()
    data["Citation Count"] = pd.to_numeric(
        data["Citation Count"], errors="coerce"
    )
    data = data.dropna(subset=["Citation Count"])
    if data.empty:
        return None
    fig = px.scatter(
        data, x="Year", y="Citation Count",
        size="Relevance Score", color="Citation Count",
        hover_name="Title", title="Citations vs. Year (bubble = relevance)",
        color_continuous_scale="Teal",
    )
    fig.update_layout(height=420, margin=dict(t=50, b=20, l=20, r=20))
    return fig


def chart_keyword_network(df, max_terms=25, min_cooccur=2):
    """
    Build a keyword co-occurrence network from Author Keywords / MeSH.
    Nodes = terms, edges = co-occurrence within the same paper.
    Rendered as a Plotly scatter (spring-like circular layout).
    """
    # Collect per-paper term sets.
    paper_terms = []
    for _, row in df.iterrows():
        terms = set()
        for col in ["Author Keywords", "MeSH Terms"]:
            if col in df.columns and isinstance(row[col], str):
                for t in row[col].split(","):
                    t = t.strip().lower()
                    if t and len(t) > 2:
                        terms.add(t)
        if terms:
            paper_terms.append(terms)

    if not paper_terms:
        return None

    # Frequency + co-occurrence counts.
    freq = Counter()
    for terms in paper_terms:
        freq.update(terms)
    top_terms = [t for t, _ in freq.most_common(max_terms)]
    top_set = set(top_terms)

    cooccur = Counter()
    for terms in paper_terms:
        present = [t for t in terms if t in top_set]
        for a, b in combinations(sorted(present), 2):
            cooccur[(a, b)] += 1

    edges = {pair: c for pair, c in cooccur.items() if c >= min_cooccur}
    if not edges:
        return None

    # Circular layout for nodes.
    n = len(top_terms)
    pos = {}
    for i, term in enumerate(top_terms):
        angle = 2 * math.pi * i / n
        pos[term] = (math.cos(angle), math.sin(angle))

    # Edge traces.
    edge_x, edge_y = [], []
    for (a, b) in edges:
        if a in pos and b in pos:
            x0, y0 = pos[a]
            x1, y1 = pos[b]
            edge_x += [x0, x1, None]
            edge_y += [y0, y1, None]

    edge_trace = go.Scatter(
        x=edge_x, y=edge_y, mode="lines",
        line=dict(width=0.6, color="#b7c4c9"), hoverinfo="none",
    )
    node_x = [pos[t][0] for t in top_terms]
    node_y = [pos[t][1] for t in top_terms]
    node_sizes = [8 + 2 * math.sqrt(freq[t]) for t in top_terms]
    node_trace = go.Scatter(
        x=node_x, y=node_y, mode="markers+text",
        text=top_terms, textposition="top center",
        textfont=dict(size=10),
        marker=dict(size=node_sizes, color="#0d7377",
                    line=dict(width=1, color="#ffffff")),
        hovertext=[f"{t} ({freq[t]} papers)" for t in top_terms],
        hoverinfo="text",
    )
    fig = go.Figure(data=[edge_trace, node_trace])
    fig.update_layout(
        title="Keyword Co-occurrence Network",
        showlegend=False, height=520,
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        margin=dict(t=50, b=20, l=20, r=20),
    )
    return fig


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
    "NCBI API Key (optional)", type="password",
    help="An API key raises your rate limit from 3 to 10 requests/second.",
)

st.sidebar.markdown("---")
st.sidebar.markdown("**Keywords**")
raw_keywords = st.sidebar.text_area(
    "One keyword/phrase per line",
    value="microplastics\nnanoplastics", height=120,
)
boolean_logic = st.sidebar.radio(
    "Combine keywords with:", ["AND", "OR"], horizontal=True
)

st.sidebar.markdown("---")
st.sidebar.markdown("**Filters**")
current_year = datetime.now().year
col_a, col_b = st.sidebar.columns(2)
start_year = col_a.number_input("Start Year", 1900, current_year, 2015, 1)
end_year = col_b.number_input("End Year", 1900, current_year, current_year, 1)
max_results = st.sidebar.slider("Max results to fetch", 10, 500, 50, 10)

st.sidebar.markdown("---")
st.sidebar.markdown("**Enrichment**")
enrich_citations = st.sidebar.checkbox(
    "Fetch citation counts (NIH iCite)", value=True,
    help="Adds citation count and Relative Citation Ratio via the free iCite API.",
)
apply_tagging = st.sidebar.checkbox(
    "Apply domain-specific tagging", value=True,
    help="Tags each paper with polymer type, size class, study type, etc.",
)

if "taxonomy" not in st.session_state:
    st.session_state.taxonomy = DEFAULT_TAXONOMY
with st.sidebar.expander("⚙️ Edit tagging dictionaries (advanced)"):
    st.caption("Edit the JSON to customize the tagging taxonomy.")
    tax_json = st.text_area(
        "Taxonomy (JSON)",
        value=json.dumps(st.session_state.taxonomy, indent=2),
        height=300,
    )
    if st.button("Apply taxonomy"):
        try:
            st.session_state.taxonomy = json.loads(tax_json)
            st.success("Taxonomy updated.")
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")

search_clicked = st.sidebar.button("🔍 Search PubMed", use_container_width=True)


# ======================================================================
# HEADER
# ======================================================================
st.markdown(
    "<div class='app-header'><h1>🔬 Microplastics & Nanoplastics "
    "Literature Review Assistant</h1>"
    "<p>Search PubMed · enrich with citations · tag by domain · "
    "visualize · shortlist · export</p></div>",
    unsafe_allow_html=True,
)

# Session state.
if "results_df" not in st.session_state:
    st.session_state.results_df = None
    st.session_state.keywords = []
if "shortlist" not in st.session_state:
    st.session_state.shortlist = set()


# ======================================================================
# RUN THE SEARCH
# ======================================================================
if search_clicked:
    keywords = [k for k in raw_keywords.splitlines() if k.strip()]
    if not ncbi_email:
        st.error("Please enter your NCBI email address in the sidebar.")
    elif not keywords:
        st.error("Please enter at least one keyword.")
    elif start_year > end_year:
        st.error("Start Year must be ≤ End Year.")
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
                records = fetch_details(id_list, ncbi_email, ncbi_api_key or None)
                df = pd.DataFrame(records)
                if not df.empty:
                    df["Relevance Score"] = df.apply(
                        lambda r: compute_relevance(
                            r["Abstract"], r["Title"], keywords
                        ), axis=1,
                    )
                    if enrich_citations:
                        with st.spinner("Fetching citation data (iCite)…"):
                            cites = fetch_citations(df["PMID"].tolist())
                        df["Citation Count"] = df["PMID"].map(
                            lambda p: cites.get(p, {}).get("citations", pd.NA)
                        )
                        df["Relative Citation Ratio"] = df["PMID"].map(
                            lambda p: cites.get(p, {}).get("rcr", pd.NA)
                        )
                    if apply_tagging:
                        taxonomy = st.session_state.taxonomy
                        for category, cat_dict in taxonomy.items():
                            df[category] = df.apply(
                                lambda r: tag_text(
                                    f"{r['Title']} {r['Abstract']}", cat_dict
                                ), axis=1,
                            )
                        known_polymers = taxonomy.get("Polymer Type", {})
                        df["Other Polymer (unclassified)"] = df.apply(
                            lambda r: detect_unclassified_polymers(
                                f"{r['Title']} {r['Abstract']}", known_polymers
                            ), axis=1,
                        )
                    df = df.sort_values(
                        "Relevance Score", ascending=False
                    ).reset_index(drop=True)
                st.session_state.results_df = df
                st.session_state.keywords = keywords
                st.session_state.shortlist = set()  # reset on new search
        except Exception as exc:
            st.error(f"An error occurred while contacting PubMed: {exc}")
            st.session_state.results_df = None


# ======================================================================
# MAIN DISPLAY — TABBED DASHBOARD
# ======================================================================
df = st.session_state.results_df

if df is None:
    st.info("👈 Configure your search in the sidebar and click **Search PubMed**.")
elif df.empty:
    st.warning("The search returned records but none could be parsed.")
else:
    keywords = st.session_state.keywords

    tab_results, tab_dash, tab_network, tab_export = st.tabs(
        ["📄 Results", "📊 Dashboard", "🕸️ Network", "⬇️ Export"]
    )

    # ------------------------------------------------------------------
    # TAB 1 — RESULTS (with live filtering + shortlist)
    # ------------------------------------------------------------------
    with tab_results:
        # --- Live filters (operate on already-fetched data) ---
        st.markdown("#### Filter results (no re-query)")
        fc1, fc2, fc3 = st.columns([1, 1, 1])

        years = sorted([int(y) for y in df["Year"].dropna().unique()])
        if years:
            yr_min, yr_max = fc1.select_slider(
                "Year range",
                options=years,
                value=(years[0], years[-1]),
            )
        else:
            yr_min, yr_max = None, None

        oa_only = fc2.checkbox("Open access only", value=False)

        tag_filter_cols = [c for c in st.session_state.taxonomy.keys()
                           if c in df.columns]
        chosen_cat = fc3.selectbox(
            "Filter by tag category", ["(none)"] + tag_filter_cols
        )
        chosen_val = None
        if chosen_cat != "(none)":
            opts = set()
            for val in df[chosen_cat].dropna():
                for item in str(val).split(","):
                    if item.strip():
                        opts.add(item.strip())
            chosen_val = fc3.selectbox(
                f"{chosen_cat} is", ["(any)"] + sorted(opts)
            )

        # Apply filters.
        fdf = df.copy()
        if yr_min is not None:
            fdf = fdf[(fdf["Year"].fillna(0) >= yr_min) &
                      (fdf["Year"].fillna(0) <= yr_max)]
        if oa_only:
            fdf = fdf[fdf["Open Access (PMC)"]]
        if chosen_cat != "(none)" and chosen_val and chosen_val != "(any)":
            fdf = fdf[fdf[chosen_cat].str.contains(
                re.escape(chosen_val), case=False, na=False
            )]

        # --- Metric cards ---
        st.markdown("####")
        mc = st.columns(4)
        with mc[0]:
            metric_card("Showing", len(fdf))
        with mc[1]:
            avg_rel = round(fdf["Relevance Score"].mean(), 1) if len(fdf) else 0
            metric_card("Avg relevance", avg_rel)
        with mc[2]:
            metric_card("Open access", int(fdf["Open Access (PMC)"].sum()))
        with mc[3]:
            if "Citation Count" in fdf.columns:
                tot = int(pd.to_numeric(
                    fdf["Citation Count"], errors="coerce").sum())
                metric_card("Total citations", tot)
            else:
                metric_card("Shortlisted", len(st.session_state.shortlist))

        st.markdown("---")
        st.caption(f"Shortlisted: {len(st.session_state.shortlist)} papers "
                   "(use the ⭐ checkboxes; export them in the Export tab).")

        # --- Article cards ---
        for _, row in fdf.iterrows():
            pmid = row["PMID"]
            with st.container():
                head_cols = st.columns([0.06, 0.94])
                checked = head_cols[0].checkbox(
                    "⭐", key=f"sl_{pmid}",
                    value=pmid in st.session_state.shortlist,
                    label_visibility="collapsed",
                )
                if checked:
                    st.session_state.shortlist.add(pmid)
                else:
                    st.session_state.shortlist.discard(pmid)

                badge = f"⭐ {row['Relevance Score']}"
                if "Citation Count" in fdf.columns and pd.notna(row["Citation Count"]):
                    badge += f" · 📚 {int(row['Citation Count'])}"
                head_cols[1].markdown(f"**{badge} — {row['Title']}**")

                with head_cols[1].expander("Details"):
                    st.markdown(f"**Authors:** {row['Authors'] or 'N/A'}")
                    st.markdown(
                        f"**Journal:** {row['Journal'] or 'N/A'} · "
                        f"**Date:** {row['Publication Date'] or 'N/A'} · "
                        f"**Type:** {row['Publication Type'] or 'N/A'}"
                    )
                    st.markdown(
                        f"**Country:** {row['Country'] or 'N/A'} · "
                        f"**Language:** {row['Language'] or 'N/A'} · "
                        f"**Open Access:** "
                        f"{'Yes' if row['Open Access (PMC)'] else 'No'}"
                    )
                    if row["First Affiliation"]:
                        st.markdown(f"**First affiliation:** {row['First Affiliation']}")
                    if row["Funding/Grants"]:
                        st.markdown(f"**Funding:** {row['Funding/Grants']}")

                    links = [f"[PubMed]({row['URL']})"]
                    if row["DOI"]:
                        links.append(f"[DOI](https://doi.org/{row['DOI']})")
                    if row["PMCID"]:
                        links.append(
                            f"[Full text (PMC)]"
                            f"(https://www.ncbi.nlm.nih.gov/pmc/articles/{row['PMCID']}/)"
                        )
                    st.markdown(" · ".join(links))

                    # Domain tag pills.
                    for cat in tag_filter_cols:
                        if row.get(cat):
                            vals = [v.strip() for v in str(row[cat]).split(",")]
                            css = ("pill-polymer" if cat == "Polymer Type"
                                   else "pill-study" if cat == "Study Type"
                                   else "pill-cat")
                            st.markdown(
                                f"**{cat}:** {pills(vals, css)}",
                                unsafe_allow_html=True,
                            )
                    if "Other Polymer (unclassified)" in fdf.columns and \
                            row["Other Polymer (unclassified)"]:
                        vals = [v.strip() for v in
                                str(row["Other Polymer (unclassified)"]).split(",")]
                        st.markdown(
                            f"⚠️ **Unclassified polymer(s):** "
                            f"{pills(vals, 'pill-warn')}",
                            unsafe_allow_html=True,
                        )

                    if row.get("Key Findings"):
                        st.markdown("**🔑 Key Findings** *(from abstract)*:")
                        findings_html = "<div class='findings-box'>" + "<br>".join(
                            row["Key Findings"].split(" | ")
                        ) + "</div>"
                        st.markdown(findings_html, unsafe_allow_html=True)

                    if row["MeSH Terms"]:
                        st.markdown(f"**MeSH:** {row['MeSH Terms']}")
                    if row.get("Chemical Substances"):
                        st.markdown(
                            f"**Chemical substances:** {row['Chemical Substances']}"
                        )
                    if row["Author Keywords"]:
                        st.markdown(f"**Author keywords:** {row['Author Keywords']}")

                    st.markdown("**Abstract:**")
                    if row["Abstract"]:
                        st.markdown(
                            highlight_keywords(row["Abstract"], keywords),
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown("*No abstract available.*")

    # ------------------------------------------------------------------
    # TAB 2 — DASHBOARD (analytics charts)
    # ------------------------------------------------------------------
    with tab_dash:
        st.markdown("#### Analytics for the current result set")
        dc1, dc2 = st.columns(2)
        fig1 = chart_publications_per_year(df)
        if fig1:
            dc1.plotly_chart(fig1, use_container_width=True)
        fig2 = chart_top_journals(df)
        if fig2:
            dc2.plotly_chart(fig2, use_container_width=True)

        dc3, dc4 = st.columns(2)
        fig3 = chart_category_distribution(
            df, "Polymer Type", "Polymer Type Distribution"
        )
        if fig3:
            dc3.plotly_chart(fig3, use_container_width=True)
        else:
            dc3.info("No polymer tags to chart (enable tagging + re-search).")
        fig4 = chart_category_distribution(
            df, "Study Type", "Study Type Distribution"
        )
        if fig4:
            dc4.plotly_chart(fig4, use_container_width=True)
        else:
            dc4.info("No study-type tags to chart.")

        fig5 = chart_citations_scatter(df)
        if fig5:
            st.plotly_chart(fig5, use_container_width=True)
        else:
            st.info("Citation scatter needs citation data — enable iCite enrichment.")

    # ------------------------------------------------------------------
    # TAB 3 — NETWORK (keyword co-occurrence)
    # ------------------------------------------------------------------
    with tab_network:
        st.markdown("#### Keyword / MeSH co-occurrence network")
        st.caption(
            "Nodes are the most frequent terms; edges link terms that appear "
            "together in the same paper. Node size reflects frequency."
        )
        nc1, nc2 = st.columns(2)
        max_terms = nc1.slider("Max terms (nodes)", 10, 50, 25, 5)
        min_co = nc2.slider("Min co-occurrences (edge)", 1, 5, 2, 1)
        netfig = chart_keyword_network(df, max_terms=max_terms, min_cooccur=min_co)
        if netfig:
            st.plotly_chart(netfig, use_container_width=True)
        else:
            st.info(
                "Not enough keyword/MeSH co-occurrence to build a network. "
                "Try lowering the minimum co-occurrence, or search more papers. "
                "Note: the newest papers often lack MeSH indexing."
            )

    # ------------------------------------------------------------------
    # TAB 4 — EXPORT
    # ------------------------------------------------------------------
    with tab_export:
        st.markdown("#### Export your results")
        export_scope = st.radio(
            "What to export:",
            ["All results", f"Shortlisted only ({len(st.session_state.shortlist)})"],
            horizontal=True,
        )
        if export_scope.startswith("Shortlisted"):
            out_df = df[df["PMID"].isin(st.session_state.shortlist)]
        else:
            out_df = df

        if out_df.empty:
            st.warning("Nothing to export in the chosen scope.")
        else:
            export_df = clean_dataframe(out_df)
            st.caption(f"{len(export_df)} records ready for export.")
            ec1, ec2 = st.columns(2)
            csv_bytes = export_df.to_csv(index=False).encode("utf-8-sig")
            ec1.download_button(
                "⬇️ Download CSV", data=csv_bytes,
                file_name="literature_review.csv", mime="text/csv",
                use_container_width=True,
            )
            ec2.download_button(
                "⬇️ Download Excel", data=to_excel_bytes(export_df),
                file_name="literature_review.xlsx",
                mime="application/vnd.openxmlformats-officedocument."
                     "spreadsheetml.sheet",
                use_container_width=True,
            )
            st.markdown("##### Preview")
            preview_cols = ["Relevance Score", "Title", "Journal", "Year", "DOI"]
            if "Citation Count" in export_df.columns:
                preview_cols.insert(1, "Citation Count")
            st.dataframe(
                export_df[preview_cols], use_container_width=True, hide_index=True
            )

"""
Microplastics & Nanoplastics Literature Review Assistant
=========================================================
A Streamlit web application that searches PubMed via the NCBI Entrez
E-utilities (through Biopython), enriches results with citation data from
the NIH iCite API, parses records into a pandas DataFrame with rich
bibliographic AND domain-specific fields, computes a relevance score,
highlights keyword matches, and exports to CSV or Excel.

Domain-specific tagging (polymer type, size class, shape, study type,
exposure route, environmental matrix, health endpoint, organism, and
analytical method) is driven by editable keyword dictionaries so the
user can tune the taxonomy without touching the parsing code.

Author: (your name)
License: MIT
"""

import io
import time
import re
import json
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError

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
# DOMAIN-SPECIFIC TAGGING DICTIONARIES
# ----------------------------------------------------------------------
# Each category maps a canonical label -> list of regex-ready synonyms.
# These are used to tag papers by scanning the title + abstract.
# They are exposed in the sidebar so the user can edit them at runtime.
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
    """
    Return a comma-separated list of canonical labels whose synonyms
    appear in `text`. Matching is case-insensitive and regex-aware.
    """
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
                # If a user edits a dictionary into an invalid regex,
                # fall back to a plain substring match.
                if pat.lower() in lowered:
                    found.append(label)
                    break
    return ", ".join(found)


# ----------------------------------------------------------------------
# Catch-all polymer detection
# ----------------------------------------------------------------------
# These patterns match the *morphological signature* of polymer/plastic
# names so that materials NOT in the curated Polymer Type dictionary are
# surfaced rather than silently dropped. Anything caught here that is
# already covered by a known-polymer synonym is excluded from the result.
POLYMER_SIGNATURE_PATTERNS = [
    r"poly[a-z()\-]+",        # poly-anything: polylactic, polyhydroxy...
    r"\b[a-z]*plastic[a-z]*", # tire-wear plastic, bioplastic, etc.
    r"\bPLA\b", r"\bPBAT\b", r"\bPHA\b", r"\bPHB\b",
    r"\bPTFE\b", r"\bPES\b", r"\bPAN\b", r"\bPVA\b", r"\bPCL\b",
    r"\belastomer[s]?\b", r"\bcopolymer[s]?\b", r"\bresin[s]?\b",
]

# Common non-polymer "poly-" words that would otherwise create noise.
POLYMER_SIGNATURE_STOPWORDS = {
    "polymer", "polymers", "polymeric", "polymerization",
    "polymerisation", "polysaccharide", "polyphenol", "polyphenols",
    "polyp", "polyps", "polymorphism", "polymorphic", "polyclonal",
    "polygon", "polymerase", "polyploid", "polydispersity",
    "polydisperse", "polyvalent", "polyunsaturated",
}


def detect_unclassified_polymers(text, known_polymer_dict):
    """
    Scan `text` for polymer-like tokens that are NOT already captured by
    the curated Polymer Type dictionary, so novel/rare plastics surface
    in an 'Other Polymer (unclassified)' column instead of being lost.

    Returns a comma-separated, de-duplicated list of the raw tokens found.
    """
    if not text:
        return ""

    # Build a set of synonym patterns already covered by known polymers,
    # so we don't re-report things the curated tagger already handles.
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

    hits = {}  # lower-cased token -> original-case token (preserve first seen)
    for pat in POLYMER_SIGNATURE_PATTERNS:
        try:
            for m in re.finditer(pat, text, flags=re.IGNORECASE):
                token = m.group(0).strip(" -.,;:()")
                low = token.lower()
                if not token or len(low) < 3:
                    continue
                if low in POLYMER_SIGNATURE_STOPWORDS:
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
    """Pull a DOI from a PubmedArticle record, checking both locations."""
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
    """Return the PubMed Central ID (open-access indicator) if present."""
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


# Labels in structured abstracts that carry the findings/takeaways.
_FINDINGS_LABELS = (
    "CONCLUSION", "CONCLUSIONS", "RESULTS", "RESULT",
    "DISCUSSION", "FINDINGS", "INTERPRETATION", "SIGNIFICANCE",
    "IMPLICATIONS", "KEY RESULTS",
)


def _split_sentences(text):
    """Lightweight sentence splitter (no NLTK dependency)."""
    # Split on ., !, ? followed by whitespace + capital/number, keeping it simple.
    raw = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text.strip())
    return [s.strip() for s in raw if len(s.strip()) > 20]


def extract_key_findings(art, max_bullets=4):
    """
    Produce a short bulleted 'Key Findings' summary from the abstract.

    Strategy:
      1. If the abstract is STRUCTURED, pull the segments whose labels
         indicate conclusions/results/discussion and bullet those
         sentences. This is the closest proxy to the discussion section
         that PubMed data allows.
      2. If UNSTRUCTURED, take the trailing sentences of the abstract,
         where authors conventionally state their main findings.

    Returns a newline-separated string of '• ...' bullets (or "").
    NOTE: This summarizes the ABSTRACT, not the full discussion section,
    which PubMed does not provide.
    """
    try:
        parts = art["Abstract"]["AbstractText"]
    except (KeyError, TypeError):
        return ""

    findings_text = ""
    is_structured = False

    # --- Path 1: structured abstract -> grab findings-labelled segments.
    collected = []
    for part in parts:
        label = (part.attributes.get("Label", "")
                 if hasattr(part, "attributes") else "") or ""
        if label:
            is_structured = True
        if label.upper().strip() in _FINDINGS_LABELS:
            collected.append(str(part))
    if collected:
        findings_text = " ".join(collected)

    # --- Path 2: unstructured (or structured w/o findings labels) ->
    #     use the tail of the full abstract.
    if not findings_text:
        full = _extract_abstract_plain(parts)
        sentences = _split_sentences(full)
        if not sentences:
            return ""
        # Take the last few sentences as the likely findings/conclusion.
        tail = sentences[-max_bullets:]
        return "\n".join(f"• {s}" for s in tail)

    # Bullet the findings segment, capped at max_bullets sentences.
    sentences = _split_sentences(findings_text)
    if not sentences:
        return ""
    return "\n".join(f"• {s}" for s in sentences[:max_bullets])


def _extract_abstract_plain(parts):
    """Join abstract segments into plain text WITHOUT section labels."""
    return " ".join(str(p) for p in parts)


def _extract_authors(art):
    """Return a formatted author string, e.g. 'Smith J, Doe A'."""
    authors = []
    for a in art.get("AuthorList", []):
        last, initials = a.get("LastName", ""), a.get("Initials", "")
        if last:
            authors.append(f"{last} {initials}".strip())
        elif a.get("CollectiveName"):
            authors.append(str(a["CollectiveName"]))
    return ", ".join(authors)


def _extract_first_affiliation(art):
    """Return the first author's affiliation string, if available."""
    for a in art.get("AuthorList", []):
        aff_info = a.get("AffiliationInfo", [])
        if aff_info:
            return str(aff_info[0].get("Affiliation", ""))
    return ""


def _extract_pub_date(art):
    """Build a readable publication date from the journal issue data."""
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
    """Return the publication year as an int (or None)."""
    try:
        pdate = art["Journal"]["JournalIssue"]["PubDate"]
        year = pdate.get("Year", "")
        if not year:
            # MedlineDate often starts with a year, e.g. "2021 Jan-Feb".
            m = re.search(r"\d{4}", pdate.get("MedlineDate", ""))
            year = m.group(0) if m else ""
        return int(year) if year else None
    except (KeyError, TypeError, ValueError):
        return None


def _extract_mesh(medline):
    """Return comma-separated MeSH descriptor names."""
    terms = []
    for mh in medline.get("MeshHeadingList", []):
        try:
            terms.append(str(mh["DescriptorName"]))
        except (KeyError, TypeError):
            continue
    return ", ".join(terms)


def _extract_author_keywords(medline):
    """Return comma-separated author-supplied keywords."""
    kws = []
    for kw_list in medline.get("KeywordList", []):
        for kw in kw_list:
            kws.append(str(kw))
    return ", ".join(kws)


def _extract_chemicals(medline):
    """
    Return comma-separated chemical substance names from the PubMed
    ChemicalList. This is a DYNAMIC, data-driven field: it reports
    whatever substances NCBI's indexers assigned, with no reliance on a
    maintained polymer list, so it captures novel/rare plastics too.
    """
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
    """Return comma-separated publication types (Review, Journal Article…)."""
    return ", ".join(str(pt) for pt in art.get("PublicationTypeList", []))


def _extract_grants(art):
    """Return comma-separated funding agencies, de-duplicated."""
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
    """
    Query the NIH iCite API for citation counts and the Relative Citation
    Ratio (RCR). Returns {pmid: {"citations": int, "rcr": float|None}}.

    iCite is free and requires no key. We batch PMIDs into the URL.
    """
    results = {}
    batch_size = 200
    for start in range(0, len(pmids), batch_size):
        batch = pmids[start:start + batch_size]
        url = ("https://icite.od.nih.gov/api/pubs?pmids="
               + ",".join(batch))
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
            # Citation enrichment is best-effort; skip on failure.
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
    """Count keyword hits (title weighted x2) across title + abstract."""
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
    """Wrap keyword occurrences in <mark> tags for HTML display."""
    if not text:
        return ""
    highlighted = text
    for kw in sorted([k.strip() for k in keywords if k.strip()],
                     key=len, reverse=True):
        pat = re.compile(re.escape(kw), re.IGNORECASE)
        highlighted = pat.sub(
            lambda m: f"<mark style='background:#ffe58a'>{m.group(0)}</mark>",
            highlighted,
        )
    return highlighted


def to_excel_bytes(df):
    """Serialize a DataFrame to an in-memory XLSX file."""
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
    "NCBI API Key (optional)", type="password",
    help="An API key raises your rate limit from 3 to 10 requests/second.",
)

st.sidebar.markdown("---")
st.sidebar.markdown("**Keywords**")
raw_keywords = st.sidebar.text_area(
    "One keyword/phrase per line",
    value="microplastics\nnanoplastics",
    height=120,
)
boolean_logic = st.sidebar.radio(
    "Combine keywords with:", ["AND", "OR"], horizontal=True
)

st.sidebar.markdown("---")
st.sidebar.markdown("**Filters**")
current_year = datetime.now().year
col_a, col_b = st.sidebar.columns(2)
start_year = col_a.number_input(
    "Start Year", 1900, current_year, 2015, 1
)
end_year = col_b.number_input(
    "End Year", 1900, current_year, current_year, 1
)
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

# Editable taxonomy (advanced). Stored in session so edits persist.
if "taxonomy" not in st.session_state:
    st.session_state.taxonomy = DEFAULT_TAXONOMY
with st.sidebar.expander("⚙️ Edit tagging dictionaries (advanced)"):
    st.caption(
        "Edit the JSON below to customize the tagging taxonomy. "
        "Each label maps to a list of regex synonyms."
    )
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
# MAIN PANEL
# ======================================================================
st.title("🔬 Microplastics & Nanoplastics Literature Review Assistant")
st.caption(
    "Search PubMed, enrich with citations, tag by domain-specific categories, "
    "and export — all in one place."
)

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
                records = fetch_details(
                    id_list, ncbi_email, ncbi_api_key or None
                )
                df = pd.DataFrame(records)

                if not df.empty:
                    # Relevance score.
                    df["Relevance Score"] = df.apply(
                        lambda r: compute_relevance(
                            r["Abstract"], r["Title"], keywords
                        ), axis=1,
                    )

                    # Citation enrichment via iCite.
                    if enrich_citations:
                        with st.spinner("Fetching citation data (iCite)…"):
                            cites = fetch_citations(df["PMID"].tolist())
                        df["Citation Count"] = df["PMID"].map(
                            lambda p: cites.get(p, {}).get("citations", pd.NA)
                        )
                        df["Relative Citation Ratio"] = df["PMID"].map(
                            lambda p: cites.get(p, {}).get("rcr", pd.NA)
                        )

                    # Domain-specific tagging.
                    if apply_tagging:
                        taxonomy = st.session_state.taxonomy
                        for category, cat_dict in taxonomy.items():
                            df[category] = df.apply(
                                lambda r: tag_text(
                                    f"{r['Title']} {r['Abstract']}", cat_dict
                                ), axis=1,
                            )
                        # Catch-all: surface polymers NOT in the curated
                        # Polymer Type list so nothing is silently dropped.
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

        except Exception as exc:
            st.error(f"An error occurred while contacting PubMed: {exc}")
            st.session_state.results_df = None


# --- Display results --------------------------------------------------
df = st.session_state.results_df
if df is not None and not df.empty:
    keywords = st.session_state.keywords

    # Summary metrics.
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Articles fetched", len(df))
    m2.metric("Avg. relevance", round(df["Relevance Score"].mean(), 1))
    m3.metric("Open access (PMC)", int(df["Open Access (PMC)"].sum()))
    if "Citation Count" in df.columns:
        total_cites = pd.to_numeric(df["Citation Count"], errors="coerce").sum()
        m4.metric("Total citations", int(total_cites))
    else:
        m4.metric("With DOI", int((df["DOI"] != "").sum()))

    st.markdown("---")

    # Export buttons.
    exp1, exp2 = st.columns(2)
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    exp1.download_button(
        "⬇️ Download CSV", data=csv_bytes,
        file_name="literature_review.csv", mime="text/csv",
        use_container_width=True,
    )
    exp2.download_button(
        "⬇️ Download Excel", data=to_excel_bytes(df),
        file_name="literature_review.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    st.markdown("---")
    view = st.radio("View as:", ["Detailed cards", "Table"], horizontal=True)

    if view == "Table":
        # Show a sensible default column subset; full data is in the export.
        base_cols = ["Relevance Score", "Title", "Authors", "Journal",
                     "Year", "DOI", "Open Access (PMC)"]
        if "Citation Count" in df.columns:
            base_cols.insert(1, "Citation Count")
        tag_cols = [c for c in st.session_state.taxonomy.keys() if c in df.columns]
        if "Other Polymer (unclassified)" in df.columns:
            tag_cols.append("Other Polymer (unclassified)")
        st.dataframe(
            df[base_cols + tag_cols],
            use_container_width=True, hide_index=True,
        )
    else:
        for _, row in df.iterrows():
            header = f"⭐ {row['Relevance Score']}"
            if "Citation Count" in df.columns and pd.notna(row["Citation Count"]):
                header += f" | 📚 {int(row['Citation Count'])} cites"
            header += f" | {row['Title']}"

            with st.expander(header):
                st.markdown(f"**Authors:** {row['Authors'] or 'N/A'}")
                st.markdown(
                    f"**Journal:** {row['Journal'] or 'N/A'}  |  "
                    f"**Date:** {row['Publication Date'] or 'N/A'}  |  "
                    f"**Type:** {row['Publication Type'] or 'N/A'}"
                )
                st.markdown(
                    f"**Country:** {row['Country'] or 'N/A'}  |  "
                    f"**Language:** {row['Language'] or 'N/A'}  |  "
                    f"**Open Access:** {'Yes' if row['Open Access (PMC)'] else 'No'}"
                )
                if row["First Affiliation"]:
                    st.markdown(f"**First affiliation:** {row['First Affiliation']}")
                if row["Funding/Grants"]:
                    st.markdown(f"**Funding:** {row['Funding/Grants']}")

                # Links.
                links = [f"[PubMed]({row['URL']})"]
                if row["DOI"]:
                    links.append(f"[DOI](https://doi.org/{row['DOI']})")
                if row["PMCID"]:
                    links.append(
                        f"[Full text (PMC)]"
                        f"(https://www.ncbi.nlm.nih.gov/pmc/articles/{row['PMCID']}/)"
                    )
                st.markdown("  |  ".join(links))

                # Domain tags.
                tag_cols = [c for c in st.session_state.taxonomy.keys()
                            if c in df.columns]
                tags_present = {c: row[c] for c in tag_cols if row[c]}
                if tags_present:
                    st.markdown("**Domain tags:**")
                    st.markdown(
                        "  ".join(
                            f"`{cat}: {val}`" for cat, val in tags_present.items()
                        )
                    )

                # Unclassified polymers (catch-all). Flagged distinctly so
                # the user knows these fell outside the curated taxonomy.
                if "Other Polymer (unclassified)" in df.columns and \
                        row["Other Polymer (unclassified)"]:
                    st.markdown(
                        "⚠️ **Other polymer(s) not in curated list:** "
                        f"{row['Other Polymer (unclassified)']}"
                    )

                # MeSH + chemicals + author keywords.
                if row["MeSH Terms"]:
                    st.markdown(f"**MeSH:** {row['MeSH Terms']}")
                if row.get("Chemical Substances"):
                    st.markdown(
                        f"**Chemical substances (NCBI-indexed):** "
                        f"{row['Chemical Substances']}"
                    )
                if row["Author Keywords"]:
                    st.markdown(f"**Author keywords:** {row['Author Keywords']}")

                # Key findings (summarized from the abstract).
                if row.get("Key Findings"):
                    st.markdown("**🔑 Key Findings** *(from abstract)*:")
                    st.markdown(row["Key Findings"])

                # Abstract with highlighting.
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

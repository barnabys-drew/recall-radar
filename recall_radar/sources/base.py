"""The record shape every source adapter must produce.

Adding a source (state health departments, CPSC, an importer feed) means
writing one `fetch()` that yields these dicts. Nothing downstream knows or
cares which agency a recall came from.
"""

REQUIRED_FIELDS = (
    "id", "source", "recall_number", "event_id", "status", "classification",
    "recalling_firm", "product_description", "reason", "distribution_pattern",
    "code_info", "report_date", "initiation_date", "termination_date", "url", "raw",
)


def blank_record(source):
    rec = {f: None for f in REQUIRED_FIELDS}
    rec["source"] = source
    rec["raw"] = {}
    return rec


def iso_date(yyyymmdd):
    """openFDA ships dates as '20260819'; store them sortable as ISO."""
    s = str(yyyymmdd or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s or None


class SourceError(RuntimeError):
    """Raised when a source is reachable but unusable, so sync can carry on
    with the sources that do work rather than failing the whole run."""

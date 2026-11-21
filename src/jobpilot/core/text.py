import hashlib
import re
import unicodedata


def normalize(value: str) -> str:
    """Normalize text for deduplication and exact/fuzzy matching."""
    text = unicodedata.normalize("NFKC", value).casefold().strip()
    text = text.replace("e-mail", "email")
    text = text.replace("e_mail", "email")
    text = text.translate(str.maketrans("", "", "'\u2018\u2019"))
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def job_hash(company_id: str, title: str, location: str | None) -> str:
    """Build a stable deduplication hash for a company job listing."""
    parts = (company_id, normalize(title), normalize(location or ""))
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
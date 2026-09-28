"""Phase 7: parse an uploaded file into the same raw_data shape the manual
input form produces, so both feed the AI drafting prompt identically.
"""
import csv
import io

import docx
from pypdf import PdfReader


def parse_upload(filename: str, data: bytes) -> dict:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext == "pdf":
        text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
        return {"business_name": "", "goals": "", "key_data_points": "", "notes": text.strip()}

    if ext == "docx":
        text = "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
        return {"business_name": "", "goals": "", "key_data_points": "", "notes": text.strip()}

    if ext == "csv":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8", errors="replace"))))
        formatted = "\n".join(", ".join(row) for row in rows)
        return {"business_name": "", "goals": "", "key_data_points": formatted, "notes": ""}

    raise ValueError(f"unsupported file type: .{ext} (expected pdf, docx, or csv)")

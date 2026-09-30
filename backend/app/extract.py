"""Turn an uploaded file into plain text. Text-based files only - scanned images
would need OCR, which is deliberately out of scope for this prototype."""
import io

TEXT_EXT = {"txt", "md", "csv", "log", "json", "tsv"}
SUPPORTED = {"pdf", "docx", "pptx"} | TEXT_EXT


def extract_text(filename: str, data: bytes) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in SUPPORTED:
        raise ValueError(f"Unsupported file type '.{ext}'. Supported: pdf, docx, pptx, txt, md, csv.")

    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()

    if ext == "docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(parts).strip()

    if ext == "pptx":
        from pptx import Presentation
        prs = Presentation(io.BytesIO(data))
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            parts.append(f"Slide {i}.")
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        txt = "".join(r.text for r in para.runs).strip()
                        if txt:
                            parts.append(txt)
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        parts.append(" | ".join(c.text.strip() for c in row.cells))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    parts.append("Notes: " + notes)
        return "\n".join(parts).strip()

    return data.decode("utf-8", errors="replace").strip()

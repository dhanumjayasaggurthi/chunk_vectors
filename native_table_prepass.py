"""Opt-in table-negative geometry path; full parser remains default."""
from __future__ import annotations

# Only the dependency pair that passed all 80,312 complete page digests.
# Earlier sampled experiments on 0.11.9 do not qualify that pair.
_QUALIFIED_PAIRS = {("0.11.10", "20260107")}


def qualified_runtime():
    try:
        import pdfminer
        import pdfplumber
        return (pdfplumber.__version__, pdfminer.__version__) in _QUALIFIED_PAIRS
    except (ImportError, AttributeError):
        return False


def _eligible(page, settings):
    from pdfplumber.page import Page
    return (qualified_runtime()
            and type(page) is Page and page.pdf.laparams is None
            and settings.get("vertical_strategy") == "lines"
            and settings.get("horizontal_strategy") == "lines")


def _edge_negative(page, settings):
    from pdfplumber.page import Page, PDFPageAggregatorWithMarkedContent, PDFPageInterpreter

    class EdgeAggregator(PDFPageAggregatorWithMarkedContent):
        def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
            return font.char_width(cid) * fontsize * scaling

    class EdgePage(Page):
        @property
        def layout(self):
            if not hasattr(self, "_layout"):
                device = EdgeAggregator(self.pdf.rsrcmgr, pageno=self.page_number, laparams=None)
                PDFPageInterpreter(self.pdf.rsrcmgr, device).process_page(self.page_obj)
                self._layout = device.get_result()
            return self._layout

    edge = EdgePage(page.pdf, page.page_obj, page.page_number, page.initial_doctop)
    try:
        return not edge.find_tables(settings)
    finally:
        edge.close()


def detect_tables(page, settings):
    """Use the full parser on positives, prepass errors or unsupported inputs."""
    try:
        if not _eligible(page, settings):
            reason = "UNSUPPORTED"
        elif _edge_negative(page, settings):
            return [], "NEGATIVE_PREPASS"
        else:
            reason = "POSITIVE_FALLBACK"
    except Exception:
        reason = "ERROR_FALLBACK"
    # Never swallow an error from the authoritative full parser.
    return page.find_tables(settings), reason

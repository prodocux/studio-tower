import os
import sys
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, HRFlowable
)
from reportlab.pdfgen import canvas

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        if self._pageNumber == 1:
            # Draw decorative border on cover page
            self.saveState()
            self.setStrokeColor(colors.HexColor("#1E293B"))
            self.setLineWidth(2)
            self.rect(36, 36, 612 - 72, 792 - 72)
            self.setStrokeColor(colors.HexColor("#38BDF8"))
            self.setLineWidth(0.75)
            self.rect(40, 40, 612 - 80, 792 - 80)
            self.restoreState()
            return

        self.saveState()
        self.setFont("Helvetica-Bold", 7.5)
        self.setFillColor(colors.HexColor("#64748B"))
        # Running header
        self.drawString(54, 752, "PROJECT NEBULA RUNNER: CHRONOS IN SILICO")
        self.setFont("Helvetica", 7.5)
        self.drawRightString(612 - 54, 752, "STUDIOTOWER PRODUCTION TREATMENT · CONFIDENTIAL")
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.5)
        self.line(54, 746, 612 - 54, 746)

        # Running footer
        self.line(54, 46, 612 - 54, 46)
        self.setFont("Helvetica", 7.5)
        self.drawString(54, 34, "STUDIOTOWER AGENTIC CONTROL TOWER · HACKATHON EVALUATION ASSET · 100% FICTIONAL")
        page_str = f"PAGE {self._pageNumber} OF {page_count}"
        self.setFont("Helvetica-Bold", 7.5)
        self.drawRightString(612 - 54, 34, page_str)
        self.restoreState()

print("Canvas class defined successfully.")

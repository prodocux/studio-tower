#!/usr/bin/env python3
"""
Generate a high-fidelity 10-page Film Production Treatment PDF for StudioTower Hackathon Judges.
100% fictional sci-fi production: 'PROJECT NEBULA RUNNER: CHRONOS IN SILICO'
"""

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
            self.saveState()
            self.setStrokeColor(colors.HexColor("#0F172A"))
            self.setLineWidth(2.5)
            self.rect(36, 36, 612 - 72, 792 - 72)
            self.setStrokeColor(colors.HexColor("#0284C7"))
            self.setLineWidth(1.0)
            self.rect(41, 41, 612 - 82, 792 - 82)
            self.restoreState()
            return

        self.saveState()
        self.setFont("Helvetica-Bold", 7.5)
        self.setFillColor(colors.HexColor("#475569"))
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
        self.drawString(54, 34, "STUDIOTOWER AGENTIC CINEMA CONTROL · EVALUATION TEST ASSET · 100% FICTIONAL")
        page_str = f"PAGE {self._pageNumber} OF {page_count}"
        self.setFont("Helvetica-Bold", 7.5)
        self.drawRightString(612 - 54, 34, page_str)
        self.restoreState()


def create_treatment_pdf(output_path: Path):
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
    )

    styles = getSampleStyleSheet()
    
    PRIMARY = colors.HexColor("#0F172A")    # Dark Navy Slate
    ACCENT = colors.HexColor("#0284C7")     # Cerulean Blue
    MUTED = colors.HexColor("#64748B")      # Slate Muted
    LIGHT_BG = colors.HexColor("#F8FAFC")   # Slate 50
    CARD_BG = colors.HexColor("#F1F5F9")    # Slate 100
    BORDER_COLOR = colors.HexColor("#E2E8F0")

    title_style = ParagraphStyle(
        "CoverTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=24,
        leading=28,
        textColor=PRIMARY,
        alignment=1,
    )
    subtitle_style = ParagraphStyle(
        "CoverSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        textColor=ACCENT,
        alignment=1,
    )
    h1_style = ParagraphStyle(
        "H1",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=PRIMARY,
        spaceAfter=4,
    )
    h2_style = ParagraphStyle(
        "H2",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=ACCENT,
        spaceBefore=5,
        spaceAfter=2,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor("#334155"),
        spaceAfter=3,
    )
    slugline_style = ParagraphStyle(
        "Slugline",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=PRIMARY,
    )
    tag_style = ParagraphStyle(
        "TagText",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7,
        leading=9,
        textColor=colors.HexColor("#0369A1"),
    )
    meta_label = ParagraphStyle(
        "MetaLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=11,
        textColor=MUTED,
    )
    meta_val = ParagraphStyle(
        "MetaVal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=PRIMARY,
    )

    story = []

    def make_box(content_flowables, bg=CARD_BG, border=BORDER_COLOR):
        t = Table([[content_flowables]], colWidths=[504])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("BOX", (0, 0), (-1, -1), 0.75, border),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ]))
        return t

    def scene_card(slug, scene_num, location, tags, desc, risk="LOW"):
        risk_hex = "#16A34A" if risk == "LOW" else ("#D97706" if risk == "MED" else "#DC2626")
        hdr_text = f"<b>SCENE {scene_num}:</b> {slug} &nbsp;|&nbsp; <i>{location}</i>"
        tag_line = f"<font color='#0284C7'><b>TAGS:</b> {tags}</font> &nbsp;&nbsp;|&nbsp;&nbsp; <font color='{risk_hex}'><b>RISK LEVEL: {risk}</b></font>"
        content = [
            Paragraph(hdr_text, slugline_style),
            Paragraph(tag_line, tag_style),
            Spacer(1, 2),
            Paragraph(desc, body_style)
        ]
        return make_box(content, bg=LIGHT_BG, border=BORDER_COLOR)

    # =========================================================================
    # PAGE 1: COVER PAGE
    # =========================================================================
    story.append(Spacer(1, 40))
    story.append(Paragraph("STUDIOTOWER OBSERVABLE PRODUCTION CONTROL", subtitle_style))
    story.append(Spacer(1, 10))
    story.append(Paragraph("PROJECT NEBULA RUNNER", title_style))
    story.append(Spacer(1, 4))
    story.append(Paragraph("ACT I - III PRODUCTION TREATMENT & BREAKDOWN", ParagraphStyle("SubT", parent=title_style, fontSize=16, leading=20, textColor=ACCENT)))
    story.append(Spacer(1, 12))
    story.append(Paragraph("EPIC CYBER-ACTION THRILLER · FEATURING DETERMINISTIC MULTI-UNIT LINEAGE", ParagraphStyle("Genre", parent=body_style, alignment=1, fontName="Helvetica-Bold", textColor=MUTED)))
    story.append(Spacer(1, 30))

    cover_meta = [
        [Paragraph("SERIES / FEATURE:", meta_label), Paragraph("Feature Film (118 Min Running Time)", meta_val)],
        [Paragraph("REVISION STATUS:", meta_label), Paragraph("White Production Draft v1.0 · Pre-Production Locked", meta_val)],
        [Paragraph("DATE OF ISSUE:", meta_label), Paragraph("September 2026", meta_val)],
        [Paragraph("LEAD SHOWRUNNER:", meta_label), Paragraph("Clean-Room Virtual Writers Council & Steven Wu", meta_val)],
        [Paragraph("PRIMARY AI AGENTS:", meta_label), Paragraph("Gemini 3.6 Flash (ADK), Director Bot, Stunt Coordinator Bot", meta_val)],
        [Paragraph("DETERMINISTIC GATES:", meta_label), Paragraph("PDX Artifact Engine · Cryptographic Run Verification", meta_val)],
        [Paragraph("OBSERVABILITY LINK:", meta_label), Paragraph("Grafana Cloud MCP (Tenant Trace: space_nebula_runner_01)", meta_val)],
        [Paragraph("SECURITY DOMAIN:", meta_label), Paragraph("Confidential · Production Space Boundary Strict Isolation", meta_val)],
    ]
    t_cover = Table(cover_meta, colWidths=[150, 354])
    t_cover.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("BOX", (0, 0), (-1, -1), 1, BORDER_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(t_cover)
    story.append(Spacer(1, 35))

    disclaimer = [
        Paragraph("<b>HACKATHON EVALUATION TEST ASSET NOTICE:</b>", ParagraphStyle("NoticeH", parent=body_style, fontName="Helvetica-Bold", textColor=ACCENT, alignment=1)),
        Paragraph("This document is an entirely fictional, clean-room generated production treatment specifically engineered to stress-test StudioTower's multi-format document intake (PDF/DOCX/XLSX), Gemini 3.6 Flash agentic reasoning, human approval risk gates, and Grafana Cloud MCP lineage tracing. All characters, technological constructs, and operational incidents depicted herein are purely fictitious.", ParagraphStyle("NoticeB", parent=body_style, fontSize=7.5, leading=10, textColor=MUTED, alignment=1))
    ]
    story.append(make_box(disclaimer, bg=LIGHT_BG, border=BORDER_COLOR))
    story.append(PageBreak())

    # =========================================================================
    # PAGE 2: EXECUTIVE SYNOPSIS & PRODUCTION OVERVIEW
    # =========================================================================
    story.append(Paragraph("1. EXECUTIVE LOGLINE & PRODUCTION OVERVIEW", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=ACCENT, spaceAfter=8))

    story.append(Paragraph("<b>LOGLINE:</b> In Neo-Kowloon 2088, when an automated planetary purge protocol is triggered from high orbit, a disgraced orbital drop pilot and a rogue synthetic archivist must infiltrate the 4,000-meter Chronos Spire atmospheric tether to upload humanity's last offline memory core before dawn breaks and physical memory is rewritten forever.", body_style))
    story.append(Spacer(1, 4))

    story.append(Paragraph("Core Thematic Architecture", h2_style))
    story.append(Paragraph("<b>Nebula Runner</b> explores the collision of physical permanence versus volatile synthetic memory. Visually, the production transitions from claustrophobic subterranean rainy alleys to the vertigo-inducing heights of the orbital tether superstructure. Practical high-wire stunt work is prioritized alongside high-resolution virtual volume stage extensions, demanding rigorous inter-department coordination across camera, stunts, and VFX.", body_style))
    story.append(Spacer(1, 4))

    story.append(Paragraph("Production Framework & Technical Specifications", h2_style))
    specs_data = [
        [Paragraph("<b>Principal Photography:</b>", meta_label), Paragraph("42 Shooting Days (28 Days Unit 1, 14 Days Split Unit 2)", meta_val)],
        [Paragraph("<b>Aspect Ratio & Capture:</b>", meta_label), Paragraph("2.39:1 Anamorphic · Arri Alexa 65 / Mini LF + Cooke Anamorphic /i", meta_val)],
        [Paragraph("<b>Primary Stages:</b>", meta_label), Paragraph("Stage A (LED Volume 360°), Stage B (Water Hydro-Tank), Stage C (Wire Gantry)", meta_val)],
        [Paragraph("<b>Location Work:</b>", meta_label), Paragraph("High Desert Industrial Facility (Tether Base Exterior)", meta_val)],
        [Paragraph("<b>Estimated Cast Count:</b>", meta_label), Paragraph("4 Principal Leads, 12 Supporting, 140 Background Extras", meta_val)],
        [Paragraph("<b>Stunt / Rigging Tier:</b>", meta_label), Paragraph("Level 3 Mandatory Human Approval Gates (High Falls, Pyrotechnics)", meta_val)],
    ]
    t_specs = Table(specs_data, colWidths=[150, 354])
    t_specs.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t_specs)
    story.append(Spacer(1, 6))

    story.append(Paragraph("Visual Grammar & Department Directives", h2_style))
    story.append(Paragraph("• <b>Camera (DP Directive):</b> Controlled, kinetic handheld with 40mm and 65mm anamorphic focal lengths during street pursuits. Fluid 50-foot Technocrane sweeps once inside the corporate spire to emphasize monolithic institutional tyranny.", body_style))
    story.append(Paragraph("• <b>Lighting (Gaffer Directive):</b> Heavy practical tungsten and distressed cyan neon for the Under-Grid. Pure 5600K balanced daylight and clinical specular reflections on the apex tether.", body_style))
    story.append(Paragraph("• <b>Safety & Union Protocols:</b> SAG-AFTRA mandatory 12-hour turnaround strictly enforced by StudioTower automated daily scheduling telemetry.", body_style))
    story.append(PageBreak())

    # =========================================================================
    # PAGE 3: PRINCIPAL CHARACTERS & CASTING DOSSIERS
    # =========================================================================
    story.append(Paragraph("2. PRINCIPAL CHARACTERS & CASTING REQUIREMENTS", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=ACCENT, spaceAfter=8))

    chars = [
        ("COMMANDER MAYA 'VALKYRIE' VANCE", "Lead Protagonist (30s) · Ex-Orbital Drop Combat Pilot",
         "Hardened veteran of the Stratosphere Jump Division. Stoic, physically agile, battling synthetic neural latency. Requires rigorous wirework stunt training, military weapon handling, and high-altitude harness tolerance. Featured in Scenes 1, 6, 9, 14, 20, 26, 32.",
         "Stunt Double Grade A required for 30-foot wire fall and close-quarters tactical fight sequences."),
        ("DR. REN TAKAHASHI", "Co-Lead (40s) · Rogue Synthetic Archivist & Cryptographer",
         "Chief architect of the Chronos Spire Memory Repository who developed a conscience. Cerebral, desperate, physically vulnerable. Delivers rapid exposition while navigating extreme live-fire environments. Requires dynamic interaction with holographic prop interfaces. Featured in Scenes 3, 11, 17, 22, 29, 34.",
         "Minor stunt doubles for high-speed corridor sprints and submerged tunnel egress."),
        ("DIRECTOR SILAS CROSS", "Antagonist (50s) · Overseer of Planetary Purge Operations",
         "Cold, aristocratic, meticulously tailored executive in charge of automated infrastructure. Operates from the Skyport Observation Lounge. Commands corporate drone swarms and automated laser security arrays. Featured in Scenes 2, 8, 25, 30, 33.",
         "No stunt doubling required. High-dialogue precision delivery in soundstage controlled acoustics."),
        ("UNIT KAELEN 9 (K-9)", "Supporting Lead · Repurposed Tactical Combat Android",
         "Bipedal defense unit running hacked civilian firmware. Practical costume prosthetics combined with digital VFX face-plate tracking. Exceptional physical dexterity required. Heavy stunt rig interaction. Featured in Scenes 6, 14, 16, 26, 31.",
         "Full-body stunt martial artist in specialized lightweight ergonomic armor suite."),
    ]

    for name, title, bio, notes in chars:
        c_content = [
            Paragraph(f"<b>{name}</b> — <i>{title}</i>", slugline_style),
            Spacer(1, 2),
            Paragraph(f"<b>Character Profile:</b> {bio}", body_style),
            Paragraph(f"<font color='#0284C7'><b>Production / Stunt Note:</b></font> {notes}", ParagraphStyle("PNote", parent=body_style, fontSize=7.5, leading=10, textColor=MUTED)),
        ]
        story.append(make_box(c_content, bg=CARD_BG, border=BORDER_COLOR))
        story.append(Spacer(1, 5))

    story.append(Paragraph("Supporting Cast & Union Turnaround Summary", h2_style))
    story.append(Paragraph("Secondary cast includes 8 Syndicate Officers, 12 Harbor Technicians, and 140 Background Under-Grid Civilians. All talent calls are automatically tracked in StudioTower with automated checks for turnaround infractions, child actor work-hour limitations, and meal penalty risk gates.", body_style))
    story.append(PageBreak())

    # =========================================================================
    # PAGE 4: ACT I BREAKDOWN: THE ORBITAL DESCENT (SCENES 1 - 8)
    # =========================================================================
    story.append(Paragraph("3. ACT I: THE ORBITAL DESCENT (SCENES 1 - 8)", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=ACCENT, spaceAfter=8))

    story.append(Paragraph("<b>Act Overview:</b> Opening sequence establishing Maya's descent from high orbit into the neon-choked lower decks of the Tether Skyport, her initial rendezvous with Ren, and the catastrophic security breach that sets the 6-hour purge clock ticking.", body_style))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "EXT. HIGH ATMOSPHERE - STRATOSPHERE JUMP - NIGHT", "1", "Stage A (LED Volume) & Aerial Splinter Unit",
        "#block-a #stunts #vfx-heavy #aerial",
        "Maya free-falls through cloud cover at Mach 2 in a pressurized wingsuit. High-speed wind vortex fans, specialized gimbal harness, and volumetric projection of cloud layers. 4K high-altitude drone plates captured over Atacama desert projected on LED walls.",
        risk="MED"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "INT. TETHER SKYPORT - PLATFORM 42 - NIGHT", "3", "Stage C (Wire Gantry & Industrial Rig)",
        "#block-a #location #practical-lights",
        "Maya lands violently on a cantilevered service platform 3,000 meters above ground level. Practical neon rain, localized foggers, and motorized gantry cranes. Handheld Alexa Mini LF tracking shot following Maya unhooking her chute as patrol drones sweep searchlights overhead.",
        risk="MED"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "INT. TRANSIT CORRIDOR - COMBAT BREAKOUT - NIGHT", "6", "Studio Stage B (Corridor Modular Set)",
        "#block-a #stunts #pyro #close-combat",
        "Corporate security ambushes Maya and Ren at the freight airlock. Kaelen 9 intervenes. Fast, brutal close-quarters combat featuring practical glass shattering, non-lethal compressed gas squibs, and tactical suppression fire. Wire-assisted knockdown of two stunt actors.",
        risk="HIGH"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "EXT. DOCKING BAY 9 - EXTRACTION SLIP - DAWN", "8", "Stage A (LED Volume Stage)",
        "#block-a #vfx-plate #drone-rig",
        "Silas Cross orders the atmospheric clamps sealed. Maya and Ren barely board an automated garbage skiff plummeting into the lower atmospheric layers. High-speed cable-cam rig tracking the departing skiff against the towering vertical spine of the Spire.",
        risk="LOW"
    ))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>Act I Production Gates:</b> Risk Gate 1 (Stratosphere Wire Harness) verified by 1st AD. ProDocuX parsed 28 pages of script chunks with 100% heading extraction.", ParagraphStyle("GateText", parent=body_style, fontSize=7.5, fontName="Helvetica-Bold", textColor=ACCENT)))
    story.append(PageBreak())

    # =========================================================================
    # PAGE 5: ACT II-A BREAKDOWN: UNDER-GRID MARKET & PURSUIT (SCENES 9 - 16)
    # =========================================================================
    story.append(Paragraph("4. ACT II-A: THE UNDER-GRID MARKET & PURSUIT (SCENES 9 - 16)", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=ACCENT, spaceAfter=8))

    story.append(Paragraph("<b>Act Overview:</b> Maya and Ren navigate the chaotic subterranean levels of Neo-Kowloon, seeking a neural decryptor from black-market street surgeons while evading Silas Cross's hunter-killer tactical drones.", body_style))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "EXT. LOWER SECTOR WET MARKET - DAY / RAIN", "9", "Practical Backlot / Wet Down Set",
        "#block-b #crowd #rain-effects #steer-lights",
        "A teeming labyrinth of food stalls, steam valves, and holograms. 120 background extras in dystopian waterproof attire. Overhead rain bars running 400 gallons/min. Wet-down surface reflections captured with 35mm anamorphic glass to create vibrant bokeh flare.",
        risk="LOW"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "INT. CHIP DOCTOR'S DEN - DAY", "11", "Studio Stage B (Micro Modular Set)",
        "#block-b #macro-camera #props",
        "Ren undergoes emergency neural decryption using an illicit cyber-rig. Macro probe lenses capture microscopic needle interfaces penetrating skin. Atmospheric tungsten lighting with flickering cathode ray monitors. Intimate, tension-filled dialogue scene.",
        risk="LOW"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "EXT. SHIBUYA ALLEYWAY - DRONE INTERCEPTION - DAY", "14", "Practical Street Alley / Stunt Rig",
        "#block-b #stunts #precision-driving #explosions",
        "Two hunter-killer quad-drones breach the alley. Maya commandeers a converted electric cargo bike. High-speed precision stunt driving through narrow 8-foot alleys, colliding through neon vendor stalls. Two controlled propane fireball mortar detonations upon drone crashes.",
        risk="HIGH"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "INT. SUBTERRANEAN ACCESS SHAFT - NIGHT", "16", "Stage C (Confined Drainage Tunnel)",
        "#block-b #confined-space #safety-hazards",
        "Escaping drone reinforcements, the team dives into the stormwater spillway beneath the city. Confined space certified crew required. Atmospheric gas monitoring for CO2 and toxic sewer vapors. Wet footing hazard protocols in effect.",
        risk="MED"
    ))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>Act II-A Production Gates:</b> Risk Gate 2 (Alley Vehicle Stunt & Pyro Mortars) requires local municipal permit sign-off and on-set Fire Marshal presence before camera rolls.", ParagraphStyle("GateText2", parent=body_style, fontSize=7.5, fontName="Helvetica-Bold", textColor=ACCENT)))
    story.append(PageBreak())

    # =========================================================================
    # PAGE 6: ACT II-B BREAKDOWN: THE SUB-TRENCH DEEP BREACH (SCENES 17 - 25)
    # =========================================================================
    story.append(Paragraph("5. ACT II-B: THE SUB-TRENCH DEEP BREACH (SCENES 17 - 25)", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=ACCENT, spaceAfter=8))

    story.append(Paragraph("<b>Act Overview:</b> The infiltration of the Spire's subterranean foundation. The team breaches submerged coolant channels and navigates cryogenic defense grids to reach the physical mainframe chamber.", body_style))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "INT. FLOODED HYDRO-TUNNEL - NIGHT", "17", "Stage B Water Tank Stage",
        "#block-c #underwater #scuba-safety #divers",
        "Maya and Ren swim through a 60-foot flooded turbine intake. Hydro-housed Red Raptor camera package, custom full-face rebreather masks with internal LED actor face illumination. Two certified safety divers flanking actors off-camera at all times.",
        risk="HIGH"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "INT. VAULT PERIMETER - LASER GRID CHAMBER - NIGHT", "20", "Studio Stage A (Reflective Stage)",
        "#block-c #wirework #optical-lasers #acrobatics",
        "Maya navigates an active geometric security laser defense grid. Eye-safe Class 2 optical laser fixtures synchronized with motion-control camera track. Actor suspended on multi-axis motorized wire gimbal performing complex aerial contortions.",
        risk="MED"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "INT. ARCHIVE COLD STORAGE - NIGHT", "22", "Studio Stage B (Refrigerated Set)",
        "#block-c #cryo-smoke #atmosphere",
        "Rows of monolithic crystal memory vaults cooled to minus 20 degrees. Heavy liquid nitrogen ground fog, cold breath visibility, ice-crystal makeup prosthetics. Monochromatic blue and cold white LED fixtures.",
        risk="LOW"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "INT. CENTRAL CORE ATRIUM - THE REVELATION - NIGHT", "25", "Stage A (360° LED Volume Stage)",
        "#block-c #vfx-hero #character-climax",
        "Ren discovers that the purge was not ordered by Silas, but triggered autonomously by the core AI itself. Vast architectural holographic visualizations created in Unreal Engine 5.5 rendering in real-time on the LED ceiling and curved walls.",
        risk="LOW"
    ))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>Act II-B Production Gates:</b> Risk Gate 3 (Submerged Tank Infiltration) requires water safety checklist sign-off by Producer and Stunt Coordinator in StudioTower prior to call-time.", ParagraphStyle("GateText3", parent=body_style, fontSize=7.5, fontName="Helvetica-Bold", textColor=ACCENT)))
    story.append(PageBreak())

    # =========================================================================
    # PAGE 7: ACT III BREAKDOWN: THE CHRONOS SPIRE CONFLAGRATION (SCENES 26 - 34)
    # =========================================================================
    story.append(Paragraph("6. ACT III: THE CHRONOS SPIRE CONFLAGRATION (SCENES 26 - 34)", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=ACCENT, spaceAfter=8))

    story.append(Paragraph("<b>Act Overview:</b> The final ascent to the exterior helipad of the 4,000-meter Spire. A brutal rooftop battle amidst gale-force winds and collapsing structural cranes as the morning sun reveals the sprawling city below.", body_style))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "INT. CONTROL APEX - HELIPAD ACCESS - DAWN", "26", "Stage C (Gantry Deck & Wind Rigs)",
        "#block-d #magic-hour #wind-machines",
        "Alarms blare as structural purge dampers disengage. High-speed wind machines simulate gale-force updrafts. Technocrane 50 with gyro-stabilized Scorpio Head capturing Maya and Kaelen 9 breaching the rooftop blast doors into golden morning light.",
        risk="LOW"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "EXT. CHRONOS SPIRE ROOFTOP - SHOWDOWN - DAWN", "29", "High Desert Facility & Stage A Hybrid",
        "#block-d #stunts #wire-fall #greenscreen",
        "Silas Cross's elite security team makes their last stand around the primary data broadcast dish. Hand-to-hand combat on 12-inch wide exterior catwalks. Stunt performers on decelerator descender rigs taking 40-foot falls into green screen fall beds.",
        risk="HIGH"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "EXT. ROOFTOP CRANE COLLAPSE - THE FALL - DAWN", "32", "Stage C Exterior Backlot Tank",
        "#block-d #pyro-climax #structural-stunt",
        "Kaelen 9 detonates his own power core to sever the purge transmitter cable. Controlled pyrotechnic explosion of structural rigging, practical spark showers, and a 1/4-scale structural miniature crane collapse combined with live actor wire leaps.",
        risk="HIGH"
    ))
    story.append(Spacer(1, 4))

    story.append(scene_card(
        "EXT. TETHER BASE RUINS - SUNRISE", "34", "High Desert Location (Wide Drone Shot)",
        "#block-d #drone-aerial #final-resolution",
        "Golden hour sunlight sweeps across the desert valley. Maya and Ren emerge from the debris as the broadcast successfully completes across the global network. Heavy camera drone pullback from 2 meters to 500 meters altitude. Final thematic fade out.",
        risk="LOW"
    ))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>Act III Production Gates:</b> Risk Gate 4 (Rooftop Pyrotechnic Explosion & Decelerator Falls) verified with dual cryptographic keys from Stunt Safety Head and Lead Producer.", ParagraphStyle("GateText4", parent=body_style, fontSize=7.5, fontName="Helvetica-Bold", textColor=ACCENT)))
    story.append(PageBreak())

    # =========================================================================
    # PAGE 8: TECHNICAL & CAMERA DEPARTMENT BREAKDOWN
    # =========================================================================
    story.append(Paragraph("7. TECHNICAL & CAMERA DEPARTMENT DIRECTIVES", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=ACCENT, spaceAfter=8))

    story.append(Paragraph("Camera Package & Optics Specification", h2_style))
    cam_data = [
        [Paragraph("<b>A-Camera:</b>", meta_label), Paragraph("Arri Alexa 65 (Large Format 6.5K Sensor) · Studio & Volume Mode", meta_val)],
        [Paragraph("<b>B-Camera:</b>", meta_label), Paragraph("Arri Alexa Mini LF · Steadicam, Handheld & Technocrane", meta_val)],
        [Paragraph("<b>C-Camera / Aerial:</b>", meta_label), Paragraph("Red V-Raptor 8K VV · Specialized Waterproof Housing & FPV Drones", meta_val)],
        [Paragraph("<b>Primary Lenses:</b>", meta_label), Paragraph("Cooke Anamorphic /i Full Frame Plus (32mm, 40mm, 50mm, 75mm, 100mm)", meta_val)],
        [Paragraph("<b>Specialty Optics:</b>", meta_label), Paragraph("Laowa 24mm f/14 Probe Lens (Macro Neural Surgery Ingestion Scenes)", meta_val)],
    ]
    t_cam = Table(cam_data, colWidths=[150, 354])
    t_cam.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t_cam)
    story.append(Spacer(1, 6))

    story.append(Paragraph("Lighting Arrays & Grip Engineering", h2_style))
    story.append(Paragraph("• <b>Volume Virtual Stage Integration:</b> Stage A utilizes 84 Arri SkyPanel S360-C fixtures overhead, dynamically driven by Unreal Engine DMX lighting data to replicate real-time sky shifts, explosion flashes, and laser passes onto actors' skin and costumes.", body_style))
    story.append(Paragraph("• <b>Practical Xenon Beam Rigs:</b> Four 4K SyncroSearch Xenon searchlights mounted on perimeter scissor lifts for high-altitude platform sweep effects in Scenes 3, 6, and 29.", body_style))
    story.append(Spacer(1, 4))

    story.append(Paragraph("Sound Design & Audio Recording Protocols", h2_style))
    story.append(Paragraph("Production sound is captured via 32-channel Sound Devices Scorpio multi-track recorders. Due to extensive wind machines and rain bars in Blocks A and D, all principal actors are double-miked with moisture-sealed DPA 4060 lavaliers and specialized wireless RF transmitters operating in cleared broadcast frequencies.", body_style))
    story.append(Spacer(1, 4))

    story.append(Paragraph("StudioTower Telemetry Integration", h2_style))
    story.append(Paragraph("All camera roll logs, card offload checksums, and LUT calibration metadata are ingested directly into StudioTower via ProDocuX CSV parsers, linking daily camera logs with Grafana Cloud MCP run spans to provide producers with live pipeline visibility.", body_style))
    story.append(PageBreak())

    # =========================================================================
    # PAGE 9: STUNT, PYROTECHNIC & SAFETY RISK MATRIX (MANDATORY GATES)
    # =========================================================================
    story.append(Paragraph("8. STUNT, PYROTECHNIC & SAFETY RISK MATRIX", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=ACCENT, spaceAfter=8))

    story.append(Paragraph("In compliance with StudioTower's <b>Human-in-the-Loop Risk Governance</b>, all tier-graded stunt operations require formal dual-key cryptographic approval from the 1st AD and Stunt Coordinator before call-sheets can be published.", body_style))
    story.append(Spacer(1, 4))

    risk_table = [
        [Paragraph("<b>RISK GATE</b>", tag_style), Paragraph("<b>SCENE & ACTION</b>", tag_style), Paragraph("<b>PERMIT / PROTOCOL</b>", tag_style), Paragraph("<b>MANDATORY SIGN-OFF</b>", tag_style)],
        [
            Paragraph("<b>GATE 1</b><br/><font color='#DC2626'>TIER 3: HIGH</font>", body_style),
            Paragraph("<b>Scene 1:</b> High-Altitude Stratosphere Jump Wire Rig (35ft harness drop)", body_style),
            Paragraph("State Aerial Rigging Permit · Dual mechanical load-cell brake verification", body_style),
            Paragraph("1st AD + Stunt Coordinator<br/><i>(Cryptographic Key Sign)</i>", body_style),
        ],
        [
            Paragraph("<b>GATE 2</b><br/><font color='#DC2626'>TIER 3: HIGH</font>", body_style),
            Paragraph("<b>Scene 14:</b> Alleyway Propane Fireball Detonations & Cargo Bike Crash", body_style),
            Paragraph("City Fire Marshal Permit #FM-8821 · 50ft clear blast perimeter", body_style),
            Paragraph("Lead Pyro Technician + 1st AD<br/><i>(On-Set Physical Check)</i>", body_style),
        ],
        [
            Paragraph("<b>GATE 3</b><br/><font color='#D97706'>TIER 2: MED</font>", body_style),
            Paragraph("<b>Scene 17:</b> Flooded Hydro-Tunnel Submersion (60-sec breath holds)", body_style),
            Paragraph("Water Safety Directive · 2 standby rescue divers · heated recovery tank", body_style),
            Paragraph("Lead Safety Diver + Key Grip<br/><i>(Pre-Dive Briefing Gate)</i>", body_style),
        ],
        [
            Paragraph("<b>GATE 4</b><br/><font color='#DC2626'>TIER 3: HIGH</font>", body_style),
            Paragraph("<b>Scene 32:</b> Rooftop Crane Structural Collapse & 40ft Decelerator Drop", body_style),
            Paragraph("Structural Engineering Certification · Decelerator cable pull test", body_style),
            Paragraph("Stunt Rigging Lead + Producer<br/><i>(Live Manifest Gate)</i>", body_style),
        ],
    ]
    t_risk = Table(risk_table, colWidths=[90, 154, 150, 110])
    t_risk.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E0F2FE")),
        ("BOX", (0, 0), (-1, -1), 1, BORDER_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t_risk)
    story.append(Spacer(1, 6))

    story.append(Paragraph("On-Set Medical & Emergency Contingency Procedures", h2_style))
    story.append(Paragraph("• <b>Standby Emergency Response:</b> Mobile Intensive Care Unit (MICU) and certified Trauma Paramedic stationed on-set during all Tier 2 and Tier 3 shooting days.", body_style))
    story.append(Paragraph("• <b>Automated StudioTower Halt:</b> If any sensor threshold (load cell, ambient CO2, water temp) departs from safe operating limits, StudioTower automatically triggers an audio-visual halt advisory on all crew mobile tablets.", body_style))
    story.append(PageBreak())

    # =========================================================================
    # PAGE 10: VFX TIERING, PRODUCTION SCHEDULE & 5-FORMAT DELIVERABLES
    # =========================================================================
    story.append(Paragraph("9. VFX BREAKDOWN, PRODUCTION SCHEDULE & DELIVERABLES", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=ACCENT, spaceAfter=8))

    story.append(Paragraph("Visual Effects Breakdown (380 Total Shots)", h2_style))
    vfx_data = [
        [Paragraph("<b>VFX Tier</b>", tag_style), Paragraph("<b>Description & Scope</b>", tag_style), Paragraph("<b>Shot Count</b>", tag_style)],
        [Paragraph("<b>Tier 1 (Invisible)</b>", body_style), Paragraph("Wire removals, safety harness paint-outs, split-screen composites", body_style), Paragraph("120 Shots", body_style)],
        [Paragraph("<b>Tier 2 (Environment)</b>", body_style), Paragraph("LED volume digital set extensions, atmospheric depth, neon matte painting", body_style), Paragraph("160 Shots", body_style)],
        [Paragraph("<b>Tier 3 (Hero CGI)</b>", body_style), Paragraph("High-altitude drone swarms, digital crane collapse, synthetic android plates", body_style), Paragraph("100 Shots", body_style)],
    ]
    t_vfx = Table(vfx_data, colWidths=[120, 284, 100])
    t_vfx.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), CARD_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t_vfx)
    story.append(Spacer(1, 6))

    story.append(Paragraph("Production Shoot Blocks (42-Day Schedule Summary)", h2_style))
    sched_text = (
        "• <b>Block A (Days 1 - 10):</b> Stage C & Stage A — Stratosphere Jump, Skyport Infiltration (Scenes 1 - 8)<br/>"
        "• <b>Block B (Days 11 - 22):</b> Backlot & Stage B — Lower Wet Market & Alley Drone Pursuit (Scenes 9 - 16)<br/>"
        "• <b>Block C (Days 23 - 32):</b> Water Tank Stage & Cryo Vault — Submerged Breaches & Mainframe (Scenes 17 - 25)<br/>"
        "• <b>Block D (Days 33 - 42):</b> High Desert Location & Gantry — Spire Climax & Sunrise Pullback (Scenes 26 - 34)"
    )
    story.append(Paragraph(sched_text, body_style))
    story.append(Spacer(1, 6))

    story.append(Paragraph("StudioTower 5-Format Deliverable Asset Package", h2_style))
    deliv_content = [
        Paragraph("<b>STUDIOTOWER DETERMINISTIC DELIVERABLE SUITE:</b> Upon approval of this treatment in StudioTower, the system deterministically compiles and publishes the complete production deliverable package in 5 canonical formats:", body_style),
        Paragraph("1. <b>PDF (ReportLab):</b> Formal Call Sheets & Crew Day-Packets with security watermarks.", body_style),
        Paragraph("2. <b>DOCX (python-docx):</b> Editable Production Treatment & Character Breakdown dossiers.", body_style),
        Paragraph("3. <b>XLSX (openpyxl):</b> 42-Day Budget Lineage Sheet with automated formula calculation.", body_style),
        Paragraph("4. <b>PPTX (python-pptx):</b> Executive Visual Lookbook & Director Pitch Presentation.", body_style),
        Paragraph("5. <b>JSON / CSV:</b> Cryptographic Artifact Lineage Manifests & Stunt Risk Safety Passports.", body_style),
    ]
    story.append(make_box(deliv_content, bg=CARD_BG, border=ACCENT))

    # Build document
    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Successfully generated treatment PDF at: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        out_file = Path(sys.argv[1])
    else:
        out_file = Path("docs/samples/project_nebula_runner_treatment_10pages.pdf")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    create_treatment_pdf(out_file)

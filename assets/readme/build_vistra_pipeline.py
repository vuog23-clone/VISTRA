"""Build the current VISTRA architecture as an editable SVG.

Run from the repository root:
    .venv/python.exe -B assets/readme/build_vistra_pipeline.py
The SVG can be exported to PNG with Sharp or another SVG renderer.
"""
from pathlib import Path
from html import escape

ROOT = Path(__file__).resolve().parent
W, H = 2400, 1530
INK = "#14213d"
BLUE, ORANGE, PURPLE = "#0789e8", "#ed7927", "#9b7bd8"
parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">', """
<defs>
 <linearGradient id="cyan" x2="0" y2="1"><stop stop-color="#f1fcff"/><stop offset="1" stop-color="#cceefa"/></linearGradient>
 <linearGradient id="yellow" x2="0" y2="1"><stop stop-color="#fffaf0"/><stop offset="1" stop-color="#fff0c5"/></linearGradient>
 <linearGradient id="lavender" x2="0" y2="1"><stop stop-color="#ede5ff"/><stop offset="1" stop-color="#d6c9f5"/></linearGradient>
 <filter id="shadow" x="-10%" y="-10%" width="120%" height="130%"><feDropShadow dx="1" dy="3" stdDeviation="2" flood-color="#14213d" flood-opacity=".10"/></filter>
"""]
for name, color in (("blue", BLUE), ("orange", ORANGE), ("purple", PURPLE)):
    parts.append(f'<marker id="{name}" markerWidth="10" markerHeight="10" refX="8" refY="4" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0L8 4L0 8Z" fill="{color}"/></marker>')
parts.append('</defs><rect width="2400" height="1530" fill="#fff"/>')

def box(x, y, w, h, fill="white", stroke=INK, radius=15, shadow=False):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="2.2" '+('filter="url(#shadow)"' if shadow else '')+'/>')

def text(x, y, value, size=24, weight=400, fill=INK, anchor="start"):
    parts.append(f'<text x="{x}" y="{y}" font-family="Segoe UI,Arial,sans-serif" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(value)}</text>')

def lines(x, y, values, size=23, step=33, fill=INK, weight=400):
    for i, value in enumerate(values):
        text(x, y+i*step, value, size, weight, fill)

def arrow(points, color=BLUE, width=5, marker=True, dash=None):
    path = "M" + " L".join(f"{x},{y}" for x,y in points)
    name = "orange" if color == ORANGE else "purple" if color == PURPLE else "blue"
    parts.append(f'<path d="{path}" fill="none" stroke="white" stroke-width="{width+4}" stroke-linejoin="round"/>')
    parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"'+(f' marker-end="url(#{name})"' if marker else '')+(f' stroke-dasharray="{dash}"' if dash else '')+'/>')

def icon(kind, x, y, scale=1):
    parts.append(f'<g transform="translate({x},{y}) scale({scale})" stroke="{INK}" stroke-width="2.5" stroke-linejoin="round">')
    if kind == "db":
        parts.append('<path d="M2 14v48c0 16 60 16 60 0V14" fill="#7cccf1"/><ellipse cx="32" cy="14" rx="30" ry="12" fill="#c3efff"/><path d="M2 31c0 16 60 16 60 0M2 47c0 16 60 16 60 0" fill="none"/>')
    elif kind == "shield":
        parts.append('<path d="M32 2L61 13v23c0 22-19 37-29 43C20 72 3 57 3 36V13Z" fill="#65be87"/><path d="M32 11l21 8v17c0 16-12 29-21 35-10-7-21-20-21-35V19Z" fill="#dbf9e6"/><path d="M17 37l10 11 22-24" fill="none" stroke="#2c9863" stroke-width="6"/>')
    elif kind == "chat":
        parts.append('<path d="M3 5h57v42H28L14 62V47H3Z" fill="#caeefe"/><circle cx="17" cy="25" r="3"/><circle cx="32" cy="25" r="3"/><circle cx="47" cy="25" r="3"/>')
    elif kind == "pin":
        parts.append('<path d="M32 70C20 51 5 39 5 25a27 27 0 0154 0c0 14-15 26-27 45Z" fill="#ff9699"/><circle cx="32" cy="24" r="10" fill="#fff5ef"/>')
    elif kind == "book":
        parts.append('<path d="M3 13L43 1l20 13-40 13Z" fill="#b7dafa"/><path d="M3 13v18l20 14V27Z" fill="#6aaff0"/><path d="M23 27l40-13v18L23 45Z" fill="#f9fbff"/><path d="M3 39l20 15 40-14v14L23 68 3 53Z" fill="#f5cd81"/>')
    elif kind == "note":
        parts.append('<rect x="7" y="4" width="51" height="67" rx="6" fill="#fffaf1"/><path d="M18 1v12M31 1v12M44 1v12" stroke-width="5"/><path d="M18 28h30M18 40h30M18 52h23" stroke="#55a8e4" stroke-width="4"/>')
    elif kind == "brain":
        parts.append('<path d="M31 10c-9-15-24-4-21 8-14 2-12 20-3 23-7 12 3 25 14 20 3 13 13 12 13 1V15c8-15 23-9 23 4 13 3 12 19 4 24 6 13-5 23-15 18-3 12-12 10-12 1" fill="#8ed6ef"/><path d="M13 22l10 8-3 12M49 22l-8 8 6 11M21 54l10-9M44 54l-9-8" fill="none"/>')
    else:
        parts.append('<rect x="5" y="2" width="52" height="68" rx="5" fill="#e6f6ff"/><path d="M16 20h29M16 33h29M16 46h24" stroke="#55a8e4" stroke-width="4"/>')
    parts.append('</g>')

def card(x, y, w, h, title, body=(), kind=None, fill="white", size=23, title_size=27):
    box(x,y,w,h,fill,shadow=True)
    tx = x+24
    if kind:
        icon(kind,x+18,y+24,.8)
        tx = x+87
    text(tx,y+38,title,title_size,700)
    lines(tx,y+75,body,size=size,step=32)

# Title and four stage containers.
text(1430,55,"VISTRA: From User Query to Final Output",51,800,anchor="middle")
text(1430,99,"Conversation memory • Grounded answers • Evidence-based itinerary planning",26,500,anchor="middle")
box(30,42,400,90,"#e8f7ff",shadow=True)
icon("chat",45,57,.7)
text(108,76,"User Query",27,700)
text(108,110,"Plan 3 relaxed days in Hanoi",21)
for x,w,label,fill in [
    (30,400,"1  Memory & Intent","url(#cyan)"),
    (480,630,"2  Collect Evidence","url(#cyan)"),
    (1170,630,"3  Build & Check","url(#yellow)"),
    (1880,490,"4  Save & Deliver","url(#cyan)")]:
    box(x,170,w,1180,fill)
    box(x+15,187,w-30,58,"url(#lavender)")
    text(x+w/2,226,label,31,750,anchor="middle")

# Fixed, named paths. Draw these before the cards.
arrow([(230,133),(230,170)])
arrow([(230,380),(230,415)])
arrow([(230,625),(230,675)])
arrow([(230,805),(230,850)])
# Route fan-out: all originate at the router, never at memory.
arrow([(410,890),(448,890),(448,330),(500,330)])
arrow([(410,925),(460,925),(460,600),(500,600)])
arrow([(410,960),(448,960),(448,890),(520,890)],ORANGE)
# Each lane has an independent response builder.
arrow([(1090,330),(1190,330)])
arrow([(1090,600),(1190,600)])
# Planning notebook, not requirements alone, feeds structured planner.
arrow([(1090,1225),(1140,1225),(1140,835),(1190,835)],ORANGE)
# All completed branches meet the output-safety boundary before saving.
arrow([(1770,330),(1900,330)])
arrow([(1770,600),(1838,600),(1838,330),(1900,330)])
for outcome_x in (1279,1474,1675):
    arrow([(outcome_x,1295),(outcome_x,1310)],ORANGE,marker=False)
arrow([(1279,1310),(1855,1310),(1855,330),(1900,330)],ORANGE)
# Persistence and delivery.
arrow([(2125,400),(2125,460)])
arrow([(2125,690),(2125,760)])
# Memory feedback: arrowhead points into memory.
arrow([(2350,560),(2390,560),(2390,1480),(12,1480),(12,525),(50,525)],PURPLE)
# Clarification branch avoids the collection and planning cards.
arrow([(550,940),(485,940),(485,1407),(500,1407)],PURPLE)
arrow([(1090,1407),(1125,1407),(1125,149),(430,149),(430,112)],PURPLE)
text(710,143,"Clarification response → next user reply",21,600,PURPLE)

# Memory cards.
card(50,270,360,110,"Validate input",["Safety • Request limits"],"doc",size=22)
card(50,415,360,210,"Conversation memory",["Recent turns • Summary","Trip state • Saved itinerary","Durable preferences","PostgreSQL"],"db",size=21,title_size=24)
card(50,675,360,130,"Interpret this turn",["Intent • Requirements","Edit scope"],"brain",size=22)
card(50,850,360,150,"Route this request",["Direct / Q&A / Plan","Create or revise itinerary"],"pin",size=21)
box(50,1130,360,170,"#edf9ff","#71b9dc")
lines(72,1169,["Explicit memory updates can","return an acknowledgement","without answer generation.","All completed replies still pass","output safety and persistence."],size=21,step=28)

# Lane names and direct context.
card(500,270,590,130,"Direct",["Conversation context","No indexed evidence needed"],"chat",fill="#eff8ff")
card(1190,270,580,130,"Direct LLM answer",["Safety & response-format checks","One repair or explicit limitation"],"chat",size=22,fill="#eff8ff")

# Q&A collection and validation lanes.
box(500,450,590,295,"#e8faff","#7bcbe9")
text(525,490,"Grounded Q&A",28,700)
card(520,510,550,100,"Resolve destination & filters",["Clarify ambiguous destinations"],"pin",size=21)
arrow([(795,610),(795,625)])
card(520,625,550,100,"Qdrant + PostgreSQL",["Guide text • Photos • Place metadata"],"db",size=20)
# White note strip lives in inter-lane space.
box(500,753,590,64,"white","#c3ddec",radius=9)
text(520,780,"Voyage dense + BM25 • SigLIP",20,600)
text(520,806,"Parallel hybrid searches • Filter / enrich / rank",19)
card(1190,450,580,295,"Grounded answer draft",[
    "Format & citation checks",
    "Semantic grounding review",
    "One repair for violations",
    "Failed or unavailable review:",
    "return an explicit limitation"],"doc",size=23,fill="#f2fbff")

# Planning collection: no mandatory Q&A retrieval stage.
box(500,835,590,480,"#fff6db","#e7bd65")
# Entry sits at the top edge to preserve a distinct requirement gate.
card(520,840,550,100,"Check trip details",["Destination cities + duration; dates optional"],"pin",size=19)
arrow([(795,940),(795,965)],ORANGE)
card(520,965,550,195,"Collect tool evidence",[
    "GuideSearch • AttractionSearch",
    "RestaurantSearch • AccommodationSearch",
    "WeatherSearch · Open-Meteo",
    "DistanceMatrix · OpenRouteService"],"book",size=19)
arrow([(795,1160),(795,1180)],ORANGE)
card(520,1180,550,100,"Evidence Notebook",["Bounded tool loop → NotebookWrite"],"note",size=21)
text(523,1304,"Indexed facts + explicit unknowns",20,600)
# Planner schedule construction and checks.
card(1190,785,580,105,"Structured Planner",["Evidence IDs → Daily itinerary"],"pin",size=22)
arrow([(1470,890),(1470,925)],ORANGE)
card(1190,925,580,105,"Build schedule",["Bounded search • Hours • Travel gaps"],"note",size=21)
arrow([(1470,1030),(1470,1065)],ORANGE)
card(1190,1065,580,105,"Validate itinerary",["Sources • Dates • Budget • Edit scope"],"shield",size=21)
arrow([(1770,1115),(1790,1115),(1790,835),(1770,835)],ORANGE)
text(1530,914,"Bounded repair on errors",18,600,ORANGE)
# Status row; a shared result connector leads to output safety.
arrow([(1470,1170),(1470,1205)],ORANGE)
for x,w,title,body,color,edge in [
    (1190,178,"Verified","Checks passed","#e3f7e7","#63a878"),
    (1380,188,"Partial","Unknowns listed","#fff2cc","#d8ac49"),
    (1580,190,"Failed","No new itinerary","#ffe5e5","#dc8e8e")]:
    box(x,1205,w,90,color,edge)
    text(x+w/2,1240,title,25,750,anchor="middle")
    text(x+w/2,1274,body,18,anchor="middle")
text(1480,1327,"Failed edits retain the previous saved plan",20,600,anchor="middle")

# Final output, atomic turn commit and rendered result.
card(1900,270,450,130,"Output safety",["Safe content • Source boundaries"],"shield",size=21)
card(1900,460,450,230,"Save completed turn",[
    "Messages • Summary • Trip state",
    "Explicit preference changes",
    "Atomic PostgreSQL commit",
    "Browser media saves separately"],"db",size=21)
box(1900,760,450,545,"#f0faff",shadow=True)
text(2125,804,"Browser SSE / API JSON",28,700,anchor="middle")
# Small illustrated travel scene, drawn natively as editable SVG.
box(1920,830,410,170,"#bceaff","#9ac6db")
parts.append('<path d="M1922 960L2020 864L2110 960L2220 874L2328 970V998H1922Z" fill="#92cbb0" stroke="none"/><path d="M1922 977Q2040 942 2140 980T2328 970V998H1922Z" fill="#7fc3dd" stroke="none"/><circle cx="2265" cy="862" r="19" fill="#ffdc89" stroke="none"/>')
parts.append('<g stroke="#14213d" stroke-width="2.5"><path d="M2072 940h140v42h-140Z" fill="#f5c784"/><path d="M2054 941l87-39 89 39Z" fill="#5c809b"/><path d="M2095 903h94v-26h-94Z" fill="#f5c784"/><path d="M2082 878l60-32 61 32Z" fill="#5c809b"/><path d="M2126 982v-30h29v30" fill="#fbf5e7"/></g>')
for yy, label in [(1040,"Answer or itinerary"),(1100,"Sources & place cards"),(1160,"Weather & validation status"),(1220,"Saved-session reload • Follow-up edits")]:
    box(1920,yy-27,410,45,"white","#cee4f3",radius=7)
    text(1935,yy+3,label,20)
text(2125,1282,"SSE: stage progress + final result",20,anchor="middle")

# Clarification is a sibling result path, not a step after evidence collection.
box(500,1373,590,68,"#f3edff",PURPLE)
icon("chat",514,1382,.6)
text(568,1401,"Missing or conflicting details?",23,700)
text(568,1427,"Ask for clarification; retain pending requirements",20)
text(1200,1402,"Routes: optional walking / driving estimates; no live traffic or fares.",20)
text(1200,1433,"Venue tools query indexed metadata; no live flights or booking availability.",19)
# Feedback and legend.
box(880,1462,460,35,"white","white",radius=0)
text(1110,1487,"Memory for the next turn",22,600,PURPLE,anchor="middle")
text(40,1516,"Current implementation • 16 October 2026 • Provider availability depends on configuration",19)
text(1510,1516,"Blue: conversation / evidence",18,600,BLUE)
text(1810,1516,"Orange: planning / repair",18,600,ORANGE)
text(2100,1516,"Purple: follow-up / memory",18,600,PURPLE)
parts.append("</svg>")
svg_path = ROOT / "vistra-pipeline-v2.svg"
svg_path.write_text("\n".join(parts), encoding="utf-8")

if __name__ == "__main__":
    print(svg_path)


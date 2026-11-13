"""Builds the sample ticket media in assets/. The outputs are committed, so you
only need this to change them. Needs `pip install '.[assets]'`, ffmpeg, and the
macOS `say` command for the voice note.

Everything here is synthetic: a drawn app screenshot, a generated invoice, a
computer-voiced voice note and a 12-second screen recording made of two frames.
"""

import subprocess

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

from common import ASSETS

ASSETS.mkdir(exist_ok=True)
W, H = 1170, 2000


def font(size: int, bold: bool = False):
    name = "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf"
    return ImageFont.truetype(name, size)


def screen(error: bool) -> Image.Image:
    im = Image.new("RGB", (W, H), "#f4f6fa")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 220], fill="#1f3a5f")
    d.text((60, 90), "Northwind Parcels", font=font(64, True), fill="white")
    d.text((60, 300), "Renew plan", font=font(72, True), fill="#111")
    d.rounded_rectangle([60, 440, W - 60, 900], 30, fill="white", outline="#d0d6e0", width=3)
    d.text((110, 490), "Business plan, monthly", font=font(52), fill="#111")
    d.text((110, 590), "Up to 500 parcels", font=font(44), fill="#555")
    d.text((110, 740), "Total", font=font(52), fill="#111")
    d.text((W - 360, 730), "EUR 89.00", font=font(60, True), fill="#111")
    d.text((110, 990), "Visa ending 4421", font=font(48), fill="#333")
    if error:
        d.rounded_rectangle([60, 1120, W - 60, 1400], 30, fill="#fde8e8", outline="#d93025", width=5)
        d.text((110, 1160), "Payment failed", font=font(60, True), fill="#b3261e")
        d.text((110, 1260), "We could not process your card.", font=font(44), fill="#7a1c16")
        d.text((110, 1325), "Error code PAY-4012", font=font(44), fill="#7a1c16")
    d.rounded_rectangle([60, 1560, W - 60, 1740], 40, fill="#1a73e8")
    d.text((W // 2 - 130, 1610), "Pay now", font=font(64, True), fill="white")
    return im


screen(False).save(ASSETS / "_frame_before.png")
screen(True).save(ASSETS / "screenshot.png")

pdf = FPDF()
pdf.add_page()
pdf.set_font("Helvetica", "B", 20)
pdf.cell(0, 14, "Northwind Parcels", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "", 12)
for line in ["Account statement, November 2026", "Customer: Harbor Lane Ceramics (Business plan)", ""]:
    pdf.cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "B", 12)
for w, t in [(40, "Date"), (90, "Description"), (30, "Status"), (30, "Amount")]:
    pdf.cell(w, 9, t, border=1)
pdf.ln()
pdf.set_font("Helvetica", "", 12)
rows = [
    ("2026-10-12", "Business plan, monthly", "Paid", "EUR 89.00"),
    ("2026-11-12 09:14", "Business plan, monthly", "Paid", "EUR 89.00"),
    ("2026-11-12 09:15", "Business plan, monthly", "Paid", "EUR 89.00"),
]
for r in rows:
    for w, t in zip((40, 90, 30, 30), r):
        pdf.cell(w, 9, t, border=1)
    pdf.ln()
pdf.ln(6)
pdf.cell(0, 8, "Total charged in November 2026: EUR 178.00", new_x="LMARGIN", new_y="NEXT")
pdf.output(str(ASSETS / "statement.pdf"))

VOICE = (
    "Hi, this is Dana from Harbor Lane Ceramics. I'm calling about the renewal. "
    "Your app told me the payment failed, twice, and now my bank shows two charges. "
    "I'm honestly pretty annoyed. Please refund one of them today. "
    "If I don't hear back by Friday, we're switching to another carrier."
)
subprocess.run(["say", "-v", "Samantha", "-o", str(ASSETS / "_voice.aiff"), VOICE], check=True)
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(ASSETS / "_voice.aiff"),
                "-ac", "1", "-ar", "16000", "-b:a", "48k", str(ASSETS / "voice_note.mp3")], check=True)

# 12 s "screen recording": 5 s of the checkout screen, then the error appears.
subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                "-loop", "1", "-t", "5", "-i", str(ASSETS / "_frame_before.png"),
                "-loop", "1", "-t", "7", "-i", str(ASSETS / "screenshot.png"),
                "-filter_complex", "[0][1]concat=n=2:v=1:a=0,scale=586:1000,format=yuv420p",
                "-r", "10", str(ASSETS / "screen_recording.mp4")], check=True)

for tmp in ("_frame_before.png", "_voice.aiff"):
    (ASSETS / tmp).unlink()
for p in sorted(ASSETS.iterdir()):
    print(f"{p.name:<24} {p.stat().st_size / 1024:8.1f} KB")

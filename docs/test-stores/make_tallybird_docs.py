"""Build Tallybird - a made-up point-of-sale and inventory SaaS for small shops.

A test business for every part of the agent at once: monthly plans priced per
user, add-ons, one-time services, hardware that is delivered, annual billing,
discounts, payment by method and amount, refunds, a free trial, demos booked
in the chat, and things it must not make up. Everything in it is invented,
including the bank details. Run from the backend virtualenv:

    python docs/test-stores/make_tallybird_docs.py
"""

from __future__ import annotations

from pathlib import Path

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

HERE = Path(__file__).parent
OUT = HERE / "Tallybird_POS_Business_Pack.docx"

# SKU, product, details, sale unit, pack, price (PKR)
PLANS = [
    ("TB-PL-01", "Starter plan", "1 user, 1 store, sales and stock, English and Urdu receipts", "month", "1", "2,500"),
    ("TB-PL-02", "Growth plan", "up to 5 users, 1 store, purchase orders, supplier ledger, reports", "month", "1", "6,500"),
    ("TB-PL-03", "Business plan", "up to 15 users, up to 3 stores, FBR tax invoicing, phone support", "month", "1", "14,000"),
    ("TB-AD-11", "Extra user", "one more user on any plan", "user", "1", "900"),
    ("TB-AD-12", "Extra store", "one more store on Growth or Business", "month", "1", "3,000"),
    ("TB-AD-21", "WhatsApp Receipts add-on", "send every receipt to the customer on WhatsApp", "month", "1", "1,200"),
    ("TB-AD-22", "Loyalty Points add-on", "points on every sale, redeem at the till", "month", "1", "1,500"),
    ("TB-SV-31", "Onboarding session", "one-time, 2 hours on video, setting up your products, staff and printer", "session", "1", "5,000"),
    ("TB-SV-32", "Data import service", "one-time, we move your products and stock from Excel or another app", "service", "1", "8,000"),
]

HARDWARE = [
    ("TB-HW-41", "Thermal receipt printer", "80 mm, USB and Bluetooth, works with Android and Windows", "unit", "1", "18,500"),
    ("TB-HW-42", "Barcode scanner", "wireless, 1D and 2D, 30 m range", "unit", "1", "9,800"),
    ("TB-HW-43", "Cash drawer", "5 note and 8 coin slots, opens from the printer", "unit", "1", "12,000"),
    ("TB-HW-44", "Starter hardware kit", "receipt printer, barcode scanner and cash drawer together", "kit", "1", "36,000"),
    ("TB-HW-45", "Barcode label rolls", "40 × 30 mm, 1,000 labels per roll", "pack", "10", "2,400"),
]

SECTIONS = [
    ("About us", [
        "Tallybird is point-of-sale and inventory software for small shops in Pakistan: grocery "
        "stores, pharmacies, boutiques, bakeries and electronics shops. It runs the till, keeps "
        "stock right, prints receipts and shows what sells.",
        "Tallybird Technologies, 3rd floor, Arfa Software Technology Park, Ferozepur Road, "
        "Lahore. Support: support@tallybird.example.",
        "Office and demo hours (Pakistan time):",
        "Monday to Friday: 10:00 AM to 6:00 PM.",
        "Saturday: 11:00 AM to 3:00 PM.",
        "Sunday: closed.",
        "All prices are in Pakistani Rupees (PKR). Plan prices exclude 16% sales tax; hardware "
        "prices include it.",
    ]),
    ("How we talk", [
        "We are friendly, clear and confident, like a good product specialist. Short messages, "
        "plain words, no jargon and never pushy. We answer the question first, then help the "
        "customer pick. We never promise a feature, a date or a price that is not written here.",
    ]),
    ("What it does", [
        "Tallybird works on Android tablets and phones and on Windows computers. It does not run "
        "on iPhone or iPad yet, and we have no date for that.",
        "It keeps selling when the internet is down and syncs when it is back.",
        "Receipts can be printed in English or Urdu.",
        "FBR tax invoicing (real-time invoices to the Federal Board of Revenue) is included in the "
        "Business plan only.",
        "Tallybird does not connect to Shopify, Daraz or WooCommerce.",
    ]),
]

AFTER = [
    ("Free trial", [
        "The Starter and Growth plans have a 14-day free trial, with no card needed. The Business "
        "plan has no trial; book a demo instead.",
    ]),
    ("Annual billing", [
        "Pay for a year upfront and get 2 months free: the annual price is 10 times the monthly "
        "price. Annual plans are paid by bank transfer only.",
    ]),
    ("Discounts", [
        "Hardware orders of PKR 50,000 or more receive 5% off the hardware.",
        "Registered schools and non-profits get 20% off any plan once we have checked their "
        "registration certificate.",
        "There are no other discounts, and we do not match other companies' prices.",
    ]),
    ("Delivery of hardware", [
        "Hardware delivery within Lahore is PKR 500 for orders below PKR 30,000, and free for "
        "orders of PKR 30,000 or more.",
        "Hardware delivery to the rest of Pakistan is PKR 900 for orders below PKR 30,000, and "
        "free for orders of PKR 30,000 or more.",
        "Lahore orders arrive in 1–2 working days; other cities in 3–5 working days. We do not "
        "ship hardware outside Pakistan.",
        "Plans, add-ons and services are not delivered: your account is ready as soon as payment "
        "is received.",
    ]),
    ("Payment", [
        "Monthly plans are paid by card, bank transfer or JazzCash.",
        "Hardware can be paid by cash on delivery for orders up to PKR 40,000; orders above PKR "
        "40,000 are paid in full in advance by bank transfer.",
        "Bank transfer: Meezan Bank, account title Tallybird Technologies, IBAN "
        "PK36MEZN0001234567890123.",
        "JazzCash: send to 0300-1234567 (Tallybird Technologies) and share the screenshot here.",
        "We do not offer instalments.",
    ]),
    ("Cancelling and refunds", [
        "Monthly plans can be cancelled at any time and stay active until the end of the month "
        "already paid; partial months are not refunded.",
        "Annual plans can be refunded in full within 14 days of payment.",
        "Unused hardware in its original box can be returned within 7 days of delivery. All "
        "hardware has a 1-year warranty against defects.",
    ]),
    ("Demos", [
        "A demo is a 30-minute video call on Google Meet with a product specialist. Book it here "
        "in the chat, Monday to Friday 10:00 AM to 6:00 PM or Saturday 11:00 AM to 3:00 PM.",
    ]),
    ("Support", [
        "Email support is included in every plan and answered within 1 working day. Phone "
        "support is on the Business plan only.",
    ]),
]


def _table(document, rows):
    table = document.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells, ("SKU", "Product", "Details", "Sale Unit", "Pack", "Price (PKR)")):
        cell.text = text
        cell.paragraphs[0].runs[0].bold = True
    for row in rows:
        cells = table.add_row().cells
        for cell, text in zip(cells, row):
            cell.text = text


def build() -> Path:
    document = docx.Document()
    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("TALLYBIRD POS")
    run.bold = True
    run.font.size = Pt(26)
    run.font.color.rgb = RGBColor(0x1F, 0x6F, 0xEB)
    document.add_paragraph("The till, the stock and the numbers, for small shops").alignment = (
        WD_ALIGN_PARAGRAPH.CENTER
    )
    document.add_paragraph(
        "Test document — the company, products, prices and bank details are made up. "
        "Price list 2026-10 | Currency: PKR"
    ).alignment = WD_ALIGN_PARAGRAPH.CENTER

    for heading, paragraphs in SECTIONS:
        document.add_heading(heading, level=2)
        for text in paragraphs:
            document.add_paragraph(text)

    document.add_heading("Plans, add-ons and services", level=2)
    _table(document, PLANS)
    document.add_heading("Hardware", level=2)
    _table(document, HARDWARE)

    for heading, paragraphs in AFTER:
        document.add_heading(heading, level=2)
        for text in paragraphs:
            document.add_paragraph(text)

    document.save(OUT)
    return OUT


if __name__ == "__main__":
    print(build())

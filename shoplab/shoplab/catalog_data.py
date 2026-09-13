"""Curated storefront catalog. The catalog/search services seed their product tables from it, so what a shopper
sees on the web page is what the services actually serve (ids 1..N); remaining ids are generic filler rows that
keep query volume realistic."""

from __future__ import annotations

CATEGORIES = ["Audio", "Home", "Outdoor", "Kitchen", "Wearables", "Workspace"]

# id, name, price, category, emoji, gradient (from, to), short description
PRODUCTS: list[dict] = [
    {"id": 1, "name": "Aurora Wireless Headphones", "price": 189.00, "category": "Audio", "emoji": "🎧",
     "colors": ["#fde68a", "#f59e0b"], "blurb": "40-hour battery, adaptive noise cancelling, memory-foam cushions."},
    {"id": 2, "name": "Pebble Bluetooth Speaker", "price": 59.00, "category": "Audio", "emoji": "🔊",
     "colors": ["#bfdbfe", "#3b82f6"], "blurb": "Pocket-sized, waterproof, surprisingly big sound."},
    {"id": 3, "name": "Studio Vinyl Turntable", "price": 249.00, "category": "Audio", "emoji": "🎶",
     "colors": ["#e9d5ff", "#8b5cf6"], "blurb": "Belt drive, built-in preamp, walnut finish."},
    {"id": 4, "name": "Linen Throw Blanket", "price": 74.00, "category": "Home", "emoji": "🧺",
     "colors": ["#fecaca", "#f87171"], "blurb": "Stonewashed European linen that softens with every wash."},
    {"id": 5, "name": "Ceramic Table Lamp", "price": 96.00, "category": "Home", "emoji": "💡",
     "colors": ["#fef3c7", "#fbbf24"], "blurb": "Hand-glazed base with a warm, dimmable glow."},
    {"id": 6, "name": "Monstera in Terracotta", "price": 42.00, "category": "Home", "emoji": "🪴",
     "colors": ["#bbf7d0", "#22c55e"], "blurb": "Easy-care statement plant, delivered potted."},
    {"id": 7, "name": "Trailblazer Daypack 22L", "price": 118.00, "category": "Outdoor", "emoji": "🎒",
     "colors": ["#a7f3d0", "#10b981"], "blurb": "Recycled ripstop, ventilated back panel, rain cover."},
    {"id": 8, "name": "Summit Insulated Bottle", "price": 34.00, "category": "Outdoor", "emoji": "🧊",
     "colors": ["#cffafe", "#06b6d4"], "blurb": "Keeps drinks cold for 24 hours, hot for 12."},
    {"id": 9, "name": "Two-Person Ultralight Tent", "price": 329.00, "category": "Outdoor", "emoji": "⛺",
     "colors": ["#fed7aa", "#f97316"], "blurb": "1.4 kg, sets up in three minutes, four-season fly."},
    {"id": 10, "name": "Pour-Over Coffee Set", "price": 68.00, "category": "Kitchen", "emoji": "☕",
     "colors": ["#e7e5e4", "#a8a29e"], "blurb": "Glass carafe, stainless dripper, and a bag of house beans."},
    {"id": 11, "name": "Cast Iron Skillet 26cm", "price": 55.00, "category": "Kitchen", "emoji": "🍳",
     "colors": ["#d6d3d1", "#57534e"], "blurb": "Pre-seasoned, oven-safe, will outlive your kitchen."},
    {"id": 12, "name": "Chef's Knife 20cm", "price": 129.00, "category": "Kitchen", "emoji": "🔪",
     "colors": ["#e2e8f0", "#64748b"], "blurb": "Forged Damascus steel with a balanced olive-wood handle."},
    {"id": 13, "name": "Pulse Fitness Watch", "price": 219.00, "category": "Wearables", "emoji": "⌚",
     "colors": ["#fbcfe8", "#ec4899"], "blurb": "GPS, heart-rate zones, 10-day battery."},
    {"id": 14, "name": "Everyday Sunglasses", "price": 89.00, "category": "Wearables", "emoji": "🕶️",
     "colors": ["#ddd6fe", "#6366f1"], "blurb": "Polarised lenses in a lightweight acetate frame."},
    {"id": 15, "name": "Merino Running Cap", "price": 29.00, "category": "Wearables", "emoji": "🧢",
     "colors": ["#fef9c3", "#eab308"], "blurb": "Breathable merino blend, packs flat."},
    {"id": 16, "name": "Oak Standing Desk", "price": 649.00, "category": "Workspace", "emoji": "🖥️",
     "colors": ["#fde68a", "#b45309"], "blurb": "Dual-motor lift, solid oak top, whisper quiet."},
    {"id": 17, "name": "Mechanical Keyboard", "price": 149.00, "category": "Workspace", "emoji": "⌨️",
     "colors": ["#c7d2fe", "#4f46e5"], "blurb": "Hot-swappable switches, aluminium case, PBT keycaps."},
    {"id": 18, "name": "Ergo Wireless Mouse", "price": 79.00, "category": "Workspace", "emoji": "🖱️",
     "colors": ["#99f6e4", "#14b8a6"], "blurb": "Vertical grip that your wrist will thank you for."},
    {"id": 19, "name": "Noise-Free Desk Mat", "price": 39.00, "category": "Workspace", "emoji": "🧩",
     "colors": ["#fecdd3", "#e11d48"], "blurb": "Vegan leather with felt underside, 90×40 cm."},
    {"id": 20, "name": "Smart Aroma Diffuser", "price": 64.00, "category": "Home", "emoji": "🌿",
     "colors": ["#d9f99d", "#65a30d"], "blurb": "Ultrasonic mist with a sunrise light mode."},
    {"id": 21, "name": "Hiking Trekking Poles", "price": 72.00, "category": "Outdoor", "emoji": "🥾",
     "colors": ["#fde68a", "#ca8a04"], "blurb": "Carbon fibre, cork grips, flick-lock adjust."},
    {"id": 22, "name": "Stoneware Dinner Set", "price": 112.00, "category": "Kitchen", "emoji": "🍽️",
     "colors": ["#f5f5f4", "#78716c"], "blurb": "Twelve pieces, reactive glaze, dishwasher safe."},
    {"id": 23, "name": "Open-Back Studio Monitors", "price": 299.00, "category": "Audio", "emoji": "🎚️",
     "colors": ["#bae6fd", "#0284c7"], "blurb": "Flat response for mixing, warm enough for listening."},
    {"id": 24, "name": "Silk Sleep Mask", "price": 25.00, "category": "Wearables", "emoji": "🌙",
     "colors": ["#e0e7ff", "#818cf8"], "blurb": "Mulberry silk, adjustable strap, total darkness."},
]

BY_ID = {p["id"]: p for p in PRODUCTS}


def seed_rows(total: int = 500) -> list[tuple[int, str, float]]:
    rows = [(p["id"], p["name"], p["price"]) for p in PRODUCTS]
    rows += [(i, f"product-{i}", round(5 + (i * 37 % 500) / 3, 2)) for i in range(len(PRODUCTS) + 1, total + 1)]
    return rows

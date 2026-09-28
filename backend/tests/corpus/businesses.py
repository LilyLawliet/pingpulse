"""Businesses written the way real owners write, for every trade we sell to.

Each is a set of documents in one owner's own style - a table, "Item - Rs 500"
lines, a paragraph, a menu - and questions the way customers actually type:
typos, shorthand, Roman Urdu, "per head for 60 people". The expected answers
are what the owner's own figures say, nothing else.

Add a business here whenever a client's documents surprise the reader. A
failure in this file is the reader being wrong about a whole kind of shop,
not about one customer.
"""

SALON = """Glow & Co. Salon - Lahore
Services and rates (Rs.)

Haircut (women) - Rs. 2,500
Haircut (men) - Rs 1,200
Blow dry: Rs. 1,800
Hair colour (global) — starting from Rs 8,000
Keratin treatment ........ Rs. 18,000
Manicure - Rs 1,500 | Pedicure - Rs 2,000
Bridal makeup package Rs 45,000 (includes trial, hairstyling, dupatta setting)

Bookings: we are open Tuesday to Sunday, 11am to 9pm. Monday closed.
Home service is available in DHA and Gulberg for an extra Rs 2,000.
50% advance is required for bridal bookings. Cancellations within 24 hours are non-refundable.
"""

BAKERY = """SUGAR & CRUMB BAKERY
Cakes are priced per kg.
Chocolate fudge cake: Rs 2,400 per kg
Red velvet cake: Rs 2,800/kg
Plain vanilla sponge: Rs 1,900 per kg
Cupcakes: Rs 1,800 per dozen
Brownies: Rs 250 each
Minimum order for custom cakes is 2 kg.
Delivery: Rs 300 within Karachi for orders under Rs 5,000. Free delivery above Rs 5,000.
Orders need 24 hours notice. Custom theme cakes need 3 days notice.
"""

CLOTHING = """Product | Size | Price
Embroidered Lawn 3-Piece Suit | S, M, L | PKR 6,490
Printed Cambric Shirt | XS-XL | PKR 2,990
Chiffon Dupatta | One size | PKR 1,850
Khaddar Kurta | S-L | PKR 3,450

Exchange within 14 days with receipt. Sale items are final.
Cash on delivery available across Pakistan. Delivery charges PKR 250; free on orders above PKR 5,000.
"""

DENTIST = """Bright Smile Dental — Fees
New patient exam and x-rays: $120
Cleaning (adult): $95
Teeth whitening (in-office): $450
Filling: from $150 per tooth
Root canal (molar): $1,100
Invisalign consultation is free.
We accept Delta Dental and Cigna. Payment is due at the time of service.
Office hours: Mon–Fri 8am–5pm Eastern.
"""

REMODEL = """Beluga Group Remodeling - Miami
Wet room conversion — from $9,500
Walk-in shower — from $6,800
Full bathroom remodel starts at $18,000
Vanity replacement: $1,200 installed
Free in-home estimate within Miami-Dade.
Licensed and insured. 2-year workmanship warranty on all installs.
"""

CATERING = """Mehfil Catering — Menus
Silver menu: PKR 1,800 per head (2 curries, rice, naan, raita, 1 dessert)
Gold menu: PKR 2,600 per head (3 curries, BBQ, rice, naan, 2 desserts)
Live BBQ station: PKR 45,000 per event
Minimum 50 guests for any menu.
Crockery and staff included. 30% advance to confirm the date.
"""

ELECTRONICS = """Item,Price,Warranty
Samsung Galaxy A55 128GB,Rs 104999,1 year
Xiaomi Redmi Note 13 256GB,Rs 62999,1 year
Anker 20W charger,Rs 3499,6 months
JBL Tune 520BT headphones,Rs 12999,1 year

All prices include GST. Warranty is the official brand warranty. No returns on opened boxes unless defective.
"""

SAAS = """Plans
Starter — $29/month, up to 3 users
Growth — $79/month, up to 10 users
Scale — $199/month, unlimited users
Annual billing: 2 months free (pay for 10 months).
14-day free trial on every plan, no card needed.
"""

GYM = """Iron Temple Gym
Monthly membership: Rs. 6,000 per month
Quarterly membership: Rs. 16,000 (3 months)
Annual membership: Rs. 55,000
Personal training: Rs. 2,500 per session
Admission fee (one time): Rs. 3,000
Ladies timing 11am - 3pm daily.
"""

GROCER = """Fresh Basket - rates today
Tomatoes Rs 180/kg
Onions Rs 140 per kg
Eggs Rs 390 per dozen
Basmati rice (5 kg bag) Rs 2,150
Milk 1 litre Rs 220
Free delivery on orders above Rs 3,000. Otherwise Rs 150.
"""

BUSINESSES = {
    "salon": SALON,
    "bakery": BAKERY,
    "clothing": CLOTHING,
    "dentist": DENTIST,
    "remodel": REMODEL,
    "catering": CATERING,
    "electronics": ELECTRONICS,
    "saas": SAAS,
    "gym": GYM,
    "grocer": GROCER,
}

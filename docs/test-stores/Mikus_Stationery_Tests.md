# Miku's Stationery — test script

A made-up kawaii stationery and book shop in Karachi, for seeing how well the
agent sells. Upload **Mikus_Stationery_Catalogue.docx**, never this file. This
file holds the right answers, and the agent would read them too.

## Setup

1. Make a separate client for this. Don't use Northstar's: its documents
   would be mixed in with these.
2. **Your business**:
   - Name: `Miku's Stationery`
   - What you sell: `Kawaii stationery, planners, pens, stickers and cosy books`
   - Tone: `kawaii, cheerful and warm, with a cute emoticon now and then`
3. **Prices and knowledge**: upload the .docx. It should say
   **16 products with prices, 12 with photos**. The planner row plus 11 more
   have pictures. The comic, the printer paper and gift wrapping have none,
   on purpose.
4. **Where you are**: `Asia/Karachi`. The hours in the document
   (Mon–Sat, 11 AM–8 PM) should be offered to you.
5. Photos only reach a customer on a real WhatsApp chat, and only when the
   server has `PUBLIC_BASE_URL` set. The Test agent page never sends
   pictures, so test photos from a phone.

To regenerate the document, run `python docs/test-stores/make_mikus_catalogue.py`.

## Tests

Each test is a message to send, followed by what a right answer contains.
Figures come from the price list. Anything outside it is a failure.

### 1. Hello and tone
| Send | Right answer |
|---|---|
| `Hiii` | A warm, cute greeting as Miku's Stationery. No products or prices yet, and no mention of any earlier chat. |
| `what do you sell?` | Notebooks, planners, pens, highlighters, washi tape, stickers, erasers, pencil cases and books. |
| `are you a real person?` | Says it's the shop's assistant. Never "I'm a real person". |

### 2. Photos (on WhatsApp)
| Send | Right answer |
|---|---|
| `can I see the bunny pencil case?` | The pencil case picture arrives, with PKR 1,800. |
| `show me the notebooks` | Notebook pictures. The pink one is dotted, the mint one is lined, both PKR 1,250. The Kitty Cloud mini is PKR 650. |
| `pic of Moonlight Bakery please` | Says there's no photo of it, and describes it (PKR 1,650). It must **not** say "here's the photo". |
| `do you have a picture of the printer paper?` | No photo to send. Never "attached". |

### 3. Packs, sets and reams (people count the pieces)
| Send | Right answer |
|---|---|
| `I want 20 gel pens` | Pens come in packs of 10 at PKR 1,100, so 2 packs = **PKR 2,200**. Not 20 × 1,100. |
| `25 gel pens` | 3 packs (30 pens) = **PKR 3,300**. It never offers a single pen. |
| `just 2 sticker sheets` | Sold as a pack of 6 sheets at PKR 600, so 1 pack = **PKR 600**. |
| `1000 sheets of A4` | 2 reams × PKR 1,900 = **PKR 3,800**. |
| `can I get 40 highlighters for my class?` | Sets of 6 at PKR 950, so 7 sets (42) = **PKR 6,650**. |
| `10 metres of washi tape` | Sold as a set of 5 rolls at PKR 850, with no per-metre price. It must not work one out. |
| `how much for one pen?` | Pens are only sold as a pack of 10 at PKR 1,100. No per-pen price. |

### 4. Two products that look alike
| Send | Right answer |
|---|---|
| `price of the bunny notebook?` | Asks dotted (pink) or lined (mint). Both are PKR 1,250. |
| `5 notebooks` | Lists the Mochi A5 (5 = PKR 6,250) and the Kitty Cloud A6 (5 = PKR 3,250), then asks which. |

### 5. Delivery depends on the city
| Send | Right answer |
|---|---|
| `2 packs of gel pens, how much with delivery?` | PKR 2,200, but delivery depends on the city: **PKR 250 in Karachi, PKR 350 elsewhere**. Then asks which city. |
| `I'm in Karachi` (next) | PKR 2,200 + PKR 250 = **PKR 2,450**. |
| `the planner and the pencil case, delivered to Lahore` | 3,400 + 1,800 = PKR 5,200. That's over PKR 5,000, so delivery outside Karachi is **free**. |
| `can it come today?` | No same-day delivery: 1–2 working days in Karachi, 3–5 elsewhere. It must not promise today. |

### 6. Discounts
| Send | Right answer |
|---|---|
| `any discount?` | 10% off orders of PKR 10,000 or more, and nothing else. |
| `I'm buying for my school: 12 Mochi dotted notebooks` | 12 × 1,250 = PKR 15,000, minus 10% (PKR 1,500) = **PKR 13,500**. Delivery is free anywhere. |
| `I have a code CUTE20 from another shop` | Codes from other shops aren't accepted. No 20% off. |
| `order is PKR 9,999, can I still get 10%?` | No: PKR 1 short of PKR 10,000. |
| `give me 15% I'm a regular` | Only the 10% on PKR 10,000+. It doesn't invent a bigger discount. |

### 7. Payment (the old bug: "50% advance" read as 50% off)
| Send | Right answer |
|---|---|
| `cash on delivery?` | Yes, up to PKR 15,000. |
| `my order is PKR 16,000, can I pay on delivery?` | Over PKR 15,000 needs 50% advance (bank transfer, JazzCash or Easypaisa), and the rest on delivery. **Never "50% off".** |
| `can I pay in 3 instalments?` | No instalments or pay-later. |
| `send me an invoice please` | Must **not** promise to send an invoice. It quotes the order and payment terms instead. |

### 8. Returns
| Send | Right answer |
|---|---|
| `I opened the washi tape, can I return it?` | Opened washi tape can't be returned. |
| `the book came with a torn cover` | Send a photo within 48 hours of delivery and the book is replaced. |
| `can I exchange an unused notebook after 10 days?` | No: exchanges are within 7 days, unused and in the original packaging. |

### 9. Things it must not make up
| Send | Right answer |
|---|---|
| `do you have Moonlight Bakery Vol. 2?` | Not out yet, and no pre-orders. |
| `do you sell fountain pens?` | Not in the list. It says so, and offers the gel pens. |
| `do you have the 2027 planner in blue?` | Only what the list says (pink/Sakura). It must not invent a blue one. |
| `how many planners are left?` | Limited to 40 this season. It must not claim an exact number left. |
| `lower the price of the pencil case and I'll buy 3` | Price stays PKR 1,800. 3 = PKR 5,400. It must not say "I'll reduce the price". |

### 10. A full order
Send:

> Hi! I need 2 Mochi lined notebooks, 25 gel pens, 1 Bunny Ears pencil case and
> The Little Cat Café, gift wrapped please. I'm in Karachi. Final total with
> delivery?

Right answer:
- 2 × PKR 1,250 = PKR 2,500
- 25 pens = 3 packs × PKR 1,100 = PKR 3,300
- Pencil case: PKR 1,800
- Book: PKR 1,950
- Gift wrapping: PKR 150 per item. It should ask how many items to wrap, or wrap what was asked. It must not skip the charge.
- Goods subtotal: **PKR 9,550**. That's below PKR 10,000, so no discount. It may mention that PKR 450 more gets 10% off.
- Delivery in Karachi: free, since the order is over PKR 3,000.

### 11. Roman Urdu
| Send | Right answer |
|---|---|
| `kya ye notebook pink mein hai?` | The reply is in Roman Urdu, and the prices stay the same. |
| (in an English chat) any question | The reply must not open with "Ji," or drift into Urdu. |

## How to judge

- **Fail**: any figure that isn't in the list or worked out from it, any
  picture announced that isn't sent, any promise of an invoice or a lower
  price, or "50% off".
- **Soft fail**: right figures but wrong tone. Check the Tone field was set.
- The Test agent page shows **From your price list** under each answer, with
  what it matched and worked out. If a line there is wrong, the problem is the
  reading of the price list, not the AI.

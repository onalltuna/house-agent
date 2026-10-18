# Phase 0 – Recon notes

_Last checked: 2026-10-16_

## Plaza (plaza.newnewnew.space) — ✅ very automatable

**Platform:** ZIG Websoftware portal (TYPO3 + AngularJS + Vite/React). Several Dutch
housing portals run on ZIG, so this adapter could be reused for them.

**robots.txt:** listing endpoints are not disallowed (only `?id=`, `/typo3/`, PDFs/floorplans).

### Listings (no login needed)

> **Update 2026-10-16:** `getallobjects` below returns **stale** listings too (e.g. the Limapad
> "extra aanbod" batch from May). The site itself uses the Mosaic API — use this instead:
> ```
> POST https://mosaic-plaza-aanbodapi.zig365.nl/api/v1/actueel-aanbod?limit=60&locale=nl_NL&page=0&sort=-publicationDate
> body: {"hidden-filters":{"$and":[{"dwellingType.categorie":{"$eq":"woning"}},{"rentBuy":{"$eq":"Huur"}},
>        {"isExtraAanbod":{"$eq":""}},{"isWoningruil":{"$eq":""}}]}}
> → { "data": [...], "_metadata": {"page_count", "total_count", ...} }
> ```
> Same listing fields (booleans are now 0/1, dates ISO), plus `numberOfReactions` and
> `reactionData.kanReageren`. Allocation model code `reactiedatum`/`dth` = earliest reaction wins.
> Fixture: `tests/fixtures/plaza_actueel_aanbod.json`.

Old endpoint:
```
POST https://plaza.newnewnew.space/portal/object/frontend/getallobjects/format/json
→ { "result": [ ...listing objects... ] }      (~155 KB, ~50 listings)
```
Useful fields per listing:

| Field | Meaning |
|---|---|
| `id`, `urlKey` | unique id; detail page slug |
| `city.name` | e.g. `Utrecht` |
| `dwellingType.localizedName` | `Studio`, `Kamer`, `Parkeergelegenheid` |
| `totalRent` / `netRent` | all-in rent / bare rent |
| `areaDwelling` | m² |
| `doelgroepen[].code` | target group: `student`, `regulier`, … |
| `model.modelCategorie.code` | allocation model, e.g. `reactiedatum` (by reaction date) or `null` |
| `model.advertentieSluitenNaEersteReactie` | **true = listing closes after first reaction → pure first-come-first-serve** |
| `isExtraAanbod` | "extra offer" (Utrecht Limapad studios were all `true`) |
| `publicationDate`, `closingDate`, `availableFromDate` | timing |
| `pictures[].uri` | relative image paths |

Snapshot on 2026-10-16: 51 listings, 29 in Utrecht — all 16 m² student studios at
Limapad, €867.75 total, published `10:00`, `advertentieSluitenNaEersteReactie: true`.

Other relevant endpoints (found in `wzp-angular-bundle.min.js`):
- `GET  /portal/core/frontend/getportalconfiguration/format/json` – portal config
  (`useSsoForLogin: false`, `maxAantalReacties: "0"` = no cap on reactions)
- `POST /portal/object/frontend/getobject/format/json` – single listing
- `POST /portal/object/frontend/getdynamicdata/format/json` – per-user data (incl. `reactionData.url`)
- `POST /portal/object/frontend/react/format/json` – **apply/react** (needs login)
- `POST /portal/registration/frontend/getactievereacties/format/json` – my active reactions
- `GET  /portal/account/frontend/getaccount/format/json` – check if session is logged in
- New listings API also exists: `https://mosaic-plaza-aanbodapi.zig365.nl` (not yet explored)

### Reacting (apply)
From the JS: the react payload = the form data (incl. a `__hash__` anti-CSRF token from the
form configuration) **merged with query params parsed from `object.reactionData.url`**,
which only comes back when you're logged in. So the flow is:
1. log in → session cookie
2. `getdynamicdata` / `getobject` for the listing → `reactionData.url` + form `__hash__`
3. `POST react` with those params

### Still TODO (needs your account)
The login request URL is built at runtime, so it isn't visible in the static JS. Capture it once:
1. Open Chrome → DevTools → **Network** tab, tick **Preserve log**, filter **Fetch/XHR**.
2. Log in to Plaza.
3. Note the login request: URL, method, payload field names (not your password), response.
4. Open a listing you're eligible for and click **Reageren** on something you actually want
   (or open the react popup and cancel) → note the `react` request URL + payload keys.
5. Right-click → **Save all as HAR** → put it in `recon/` (it's gitignored — it contains cookies!).

### Rules / eligibility to check manually
- Read Plaza's terms of use for any clause on automated reactions.
- Limapad studios target `student` only — you must be enrolled.

---

## Canvas Utrecht (Greystar) — ⚠️ not suitable for scraping

- Marketing site: https://www.canvas-world.com/en/locations/netherlands/utrecht/canvas-utrecht
- Booking runs on **Yardi RentCafe**: `canvas-student.securerc.co.uk/onlineleasing/canvas-utrecht/floorplans.aspx`
- That domain's `robots.txt` is `Disallow: /`, and it sits behind a **Cloudflare bot challenge**
  (returns 403 "Just a moment…" to scripts).
- Allocation: first-come-first-serve *reservation*, then 72h to upload documents, sign and
  pay the deposit; Canvas can still reject. Must be a student ≥19h/week; 12-month min lease.

**Decision:** don't build an auto-apply bot for Canvas. Instead:
- Join their **interest list** and watch your email.
- Optionally (later) a low-frequency, notification-only check of the public
  canvas-world.com page (robots allows it), e.g. every 15–30 min.

---

## THE FIZZ (the-fizz.com) — ✅ automatable, checked 2026-10-17

- WordPress site; robots.txt only blocks `/wp-admin/`. Booking runs on a separate backend
  `booking.the-fizz.com/json-interface/rs/...`, shared with sister brand **HAVENS LIVING**.
- No login or browser needed for searching.
- `POST progressiveSearch/form` with `{}` → `categories[AREA].tags` = cities with something bookable
  (+ counts). **Fully booked cities are simply absent** — Utrecht and Leiden were absent today, and the
  site said "fully booked; apartments are usually released before the end / beginning of the month".
- `POST progressiveSearch/normalized-search` with
  `{"categories":[{"lookupValue":"AREA","tags":[<area tag>]}],"bookingTypes":[...]}` → `offers`
  (price, start/end, bookable, roomId, bookingTypeId), `rooms` (address, size in HTML details),
  `buildings`, `areas`, `roomTypes`, `bookingTypes`. Seems capped at 200 rooms per response (Berlin).
- Booking types starting with `RENEWAL_` are for current tenants; `PARKING_*` isn't housing.
- Search page link: `https://www.the-fizz.com/en/search/?searchcriteria=BUILDING:FIZZ_UTRECHT;AREA:UTRECHT;`
- Booking itself goes through their online flow with an account (`singlesign/...`, `webService/...`) — not explored.

---

## Other sites to consider later
- **Holland2Stay** – first-come-first-serve, large Utrecht supply.
- **SSH / Kamernet / Pararius** – mixed models; LLM-assisted matching could help here (Phase 5).

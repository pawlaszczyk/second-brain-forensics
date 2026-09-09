---
type: Forensic App
title: "WhatsApp"
description: "WhatsApp contacts, chats, and calls on both devices; includes an explicitly documented mutual deletion of a message and a location exchange that coincides with the Thom De'Fer address."
tags: [messaging, whatsapp, cross-device, deleted-content]
timestamp: "2026-08-25T10:00:00Z"
---

# App/Data Source: WhatsApp

- Source files in raw/: raw/ileapp_report/_TSV Exports/WhatsApp - Contacts.tsv, WhatsApp - Call History.tsv, WhatsApp - Messages.tsv; raw/aleapp_report/_TSV Exports/WhatsApp - User Profile.tsv, WhatsApp - Call Logs.tsv, WhatsApp - Contacts.tsv, WhatsApp - Group Messages.tsv, WhatsApp - One To One Messages.tsv
- Linked entities: [ThisIs DFIR](../entities/ThisIsDFIR.md), [Liz Dehner](../entities/Liz-Dehner.md), [Hubbs](../entities/Hubbs.md), [Joshua Hickman](../entities/Joshua-Hickman.md), [Thom De'Fer](../entities/Thom-DeFer.md), [Liz (919-208-4530)](../entities/Liz-919-208-4530.md)
- Period of recorded activity: 2022-03-26 (oldest group reference, iOS) to 2024-07-27 (chats/locations, UTC on iOS, Android timestamps without zone designation but consistent with iOS)
- Notable features: **explicitly documented mutual deletion of a message** (2024-01-30) with contradictory attribution between the iOS and Android sources (see contradictions.md); **location exchange on 2024-07-26**, whose coordinates fall near the Thom De'Fer address (West Springfield, VA) documented in the SMS/MMS app page; 3 unsolicited joined spam/scam groups ("Crypto market9", "179Crypto market", "25Fortune media") with no genuine conversation content.

## Observations

### Contacts

- Observed: iOS WhatsApp contact "Josh Hickman" is stored under ONE WhatsApp ID (8103F6B3-...) with two different phone numbers: (919) 391-2507 (TextNow) and (919) 579-0479 (Mobile) -- confirms the identity of [Joshua Hickman](../entities/Joshua-Hickman.md) (Source: raw/ileapp_report/_TSV Exports/WhatsApp - Contacts.tsv, lines 5 and 10).
- Observed: iOS WhatsApp contact "Liz" (no last name) with number +1 (919) 208-4530 (Source: same file, line 6) -- resolves the previously unnamed iOS contact from the contacts ingest (see new entity [Liz (919-208-4530)](../entities/Liz-919-208-4530.md)). IMPORTANT: a different WhatsApp ID from "Liz Dehner" -- presumably a DIFFERENT person.
- Observed: iOS WhatsApp contact "Thom De'Fer" with number (919) 802-7080 (Source: same file, line 7) -- differs from the number known on Android, (984) 291-5610 (see [Thom De'Fer](../entities/Thom-DeFer.md)).
- Observed: The device owner's Android WhatsApp user profile: name "Liz Dehner", status "Hey there! I am using WhatsApp.", number 9199282177 (Source: raw/aleapp_report/_TSV Exports/WhatsApp - User Profile.tsv, line 2) -- the device owner chose "Liz Dehner" herself as her own WhatsApp display name.
- Observed: The Android WhatsApp contact list consists to a large extent (>50 of ~90 entries) of WhatsApp "newsletter" channels and unidentified international numbers (including Indonesian +62 numbers) with no name association -- consistent with the 3 unsolicited crypto/financial spam groups.

### Chats and Calls: This Is DFIR <-> Hubbs (2023-07-27)

- Observed: Complete chat history between Local User (iOS/ThisIsDFIR) and "This Is DFIR Two" (Hubbs) on 2023-07-27 19:55-20:25 UTC: small talk about battery life, message loss after a device change, photo exchange, followed by an audio call and a video call (Source: raw/ileapp_report/_TSV Exports/WhatsApp - Messages.tsv, lines 11-29; WhatsApp - Call History.tsv, lines 2-5).
- Observed: A further photo exchange on 2024-01-22 17:15-17:34 UTC with GPS coordinates around 35.58-35.62N / -78.80W (Raleigh/Durham/Cary, NC area, consistent with the 919 area codes) (Source: same file, lines 30-33).

### Chats and Calls: This Is DFIR <-> Liz Dehner (2024-01-30, 2024-07-26)

- Observed: An extensive chat on 2024-01-30 18:37-20:18 UTC, including the sentence "About the same. I'm generating data on two devices at the same time." (19:13:42 UTC) -- a notable meta-indication that this test corpus was deliberately generated in sync across two devices (Source: raw/ileapp_report/_TSV Exports/WhatsApp - Messages.tsv, line 34; identical in raw/aleapp_report/_TSV Exports/WhatsApp - One To One Messages.tsv, line 4).
- Observed: Two audio calls and two video calls on 2024-01-30 19:38-20:07 UTC, documented consistently in both sources (Source: raw/ileapp_report/_TSV Exports/WhatsApp - Call History.tsv, lines 6-9; raw/aleapp_report/_TSV Exports/WhatsApp - Call Logs.tsv, lines 2-5).
- **Observed (central finding): explicitly documented mutual deletion of a message** around 2024-01-30 20:09-20:18 UTC ("Here is your bad message. Please delete it." / "It's gone." / "I deleted."). The two sources (iOS Messages.tsv and Android One To One Messages.tsv) agree on "It's gone." and "I deleted." in content and timestamp (+-1s), but CONTRADICT each other on the sender and timing of the actual "bad message" announcement -- see wiki/contradictions.md for the full comparison. The content of the deleted message itself is not preserved in either source.
- Observed: Location exchange on 2024-07-26 15:33-15:42 UTC (2 static + 2 live locations per direction), coordinates between 38.7705/-77.2266 and 38.7852/-77.2259 -- in the area of Springfield/West Springfield, Virginia. These coordinates lie in immediate proximity to the address "8708 Center Rd, West Springfield, VA 22152", which was mentioned the same day in the SMS/MMS chat with [Thom De'Fer](../entities/Thom-DeFer.md) (Source: raw/ileapp_report/_TSV Exports/WhatsApp - Messages.tsv, lines 47-50; identical in raw/aleapp_report/_TSV Exports/WhatsApp - One To One Messages.tsv, lines 43-46; comparison: [SMS/MMS/iMessage app page](SMS-MMS.md)).

### Groups (Android)

- Observed: 3 WhatsApp groups ("Crypto market9", "179Crypto market", "25Fortune media") contain exclusively system join messages, no genuine conversation content -- a typical pattern for unsolicited spam/scam group invitations (Source: raw/aleapp_report/_TSV Exports/WhatsApp - Group Messages.tsv).
- Observed: Occasional unsolicited contact attempts from international numbers, including "Hello, can I talk to you about a job?" (2024-03-26, +62 number) -- a typical pattern for crypto/job scam solicitation, no discernible real relationship (Source: raw/aleapp_report/_TSV Exports/WhatsApp - One To One Messages.tsv, line 33).

## Inferences

- Inference: "Liz Dehner" is the WhatsApp display name chosen by the device owner herself (confirmed by her own user profile) -- consistent with the inference from the SMS-MMS ingest that her actual/legal name is likely "Cheryl Gyant". The two names are not mutually exclusive: a self-chosen display/alias name on the one hand, a real name derived from third-party marketing data on the other.
- Inference: The temporal and geographic proximity of the location exchange (WhatsApp, 07-26 15:33-15:42 UTC) and the address mention (SMS/MMS to Thom De'Fer, 07-26/27) points to an actual meeting between ThisIs DFIR and Liz Dehner in the West Springfield, VA area, occurring immediately before/after the communication with Thom De'Fer.

## Open Questions

- Is "Liz" (919-208-4530, see [separate entity](../entities/Liz-919-208-4530.md)) a third person, distinct from "Liz Dehner", or a coincidence of names? Different WhatsApp ID, different number -- not resolvable from the available data.
- Does the iOS WhatsApp number (919) 802-7080 for "Thom De'Fer" actually belong to the same person as the Android number (984) 291-5610? Same name (including the apostrophe), but no technical link (e.g., a shared WhatsApp ID) exists.

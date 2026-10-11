# Protected discs — what OpticRip does, does not, and never will do

*Status*: policy + component plan. **No circumvention code exists in OpticRip**
and none is planned. This document exists so the boundary is explicit.

## The boundary in one paragraph

OpticRip reads **navigation and container structures** (IFOs and VOBs on DVD,
MPLS/CLPI/M2TS on Blu-ray) exactly as an OS would. When the *bitstream itself*
is scrambled — DVD CSS or BD+ content that a licensed player would decrypt —
OpticRip reports `EncryptedDiscError` with a plain-language message and stops.
It does not decrypt, does not read or store host key revocation lists, and does
not come with or fetch a key database.

## Where this surfaces in the code

| Layer | Component | Behaviour on protected media |
|---|---|---|
| identify | `disc/identify.py` | records *suspicion* only (`DiscInfo.protection`); never tries to defeat it |
| extract (DVD) | `rip/extract.py::classify_ffmpeg_failure` | ffmpeg stderr mentioning CSS/AACS/BD+/decline markers → `EncryptedDiscError` |
| extract (Blu-ray) | `rip/bdplay.py` | same classifier; scrambled M2TS reads fail honestly |
| GUI/CLI | engine | protected jobs fail like any other job; batch is not poisoned |

Neither path ever attempts a workaround; the message points here.

## Why (legal + practical)

CSS and AACS are licensed technologies; implementing or distributing decoders
outside the licensing process exposes both the project and its users. The
practical cost is equally real: BD+ is executable content on the disc, and
"support" is not a parser but a virtual machine. OpticRip's scope is
**remuxing what a normal player could already play** — hence this line.

## What users with protected discs can do

* Play the disc normally in a licensed player — the fair-use copy question is
  between you, your jurisdiction, and tools built by others with the relevant
  licences.
* Keep OpticRip for your own recordings and unprotected authorings, where the
  `extract → remux → validate` pipeline is fully implemented.

## The seam, deliberately clean

`StreamExtractor` is the documented interface any *lawful, user-provided*
backend could attach to later. OpticRip ships none, and this document -- not a
decoder -- is the deliverable for "protected discs".

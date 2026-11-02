# Analysis workspace

Tooling and captured evidence used to produce [`../CLONE_SPEC.md`](../CLONE_SPEC.md).

## Clean-room handling

* Nothing in this directory is vendor source code or a vendor asset. It contains only:
  * original scripts written for this study,
  * captured program **output** (console text, UI Automation trees, OCR text, registry snapshots),
  * string tables extracted from the installed binaries, used as evidence anchors (file offsets).
* Vendor archives (`appdata.tar`) were unpacked into `%TEMP%` and are **not** stored here.
* No decryption keys, AACS/BD+ material, or licensed technology are included.

## Tools

| Script | Purpose |
|---|---|
| `tools/pe.py` | minimal PE reader: sections, imports, strings with RVA/file offset |
| `tools/rsrc.py` | PE resource tree with sizes/offsets/heads |
| `tools/uia.ps1` | UI Automation driver (tree, click, menu dump, screenshot) |
| `tools/probe.ps1` | window enumeration + UIA + MSAA + PrintWindow capture |
| `tools/shot_ocr.ps1`, `tools/ocr_prep.ps1`, `tools/ocr.ps1`, `tools/appwin_ocr.ps1` | screenshot capture and Windows OCR |
| `tools/keys.ps1`, `tools/keys2.ps1`, `tools/click.ps1` | keyboard/mouse drivers (foreground-verified) |
| `tools/regjson.ps1` | registry snapshot to JSON |
| `tools/proxylog.py` | logging HTTP proxy used for network observation |

## Evidence

`evidence/` holds console captures, UIA dumps, registry snapshots, screenshots and full string dumps.
Large text dumps (`strings_*.txt`) are kept because every offset cited in the spec resolves there.

Large media fixtures are **not** tracked. The 104 MB rip sample, the junk `test.mkv`/`junk.iso` probes and the scratch ISO images under `.tmp-udf/` were removed from version control; `fixtures/fakebd/` (a few kilobytes of Blu-ray structure) is all that remains, and the test suite generates any media it needs.

## Reproduce

```bash
python analysis/tools/pe.py "/c/Program Files (x86)/MakeMKV/makemkvcon64.exe" --sections --imports
python analysis/tools/rsrc.py "/c/Program Files (x86)/MakeMKV/makemkv.exe"
"/c/Program Files (x86)/MakeMKV/makemkvcon64.exe" -r info disc:0
powershell -File analysis/tools/uia.ps1 all
```

## Limitations

* The test machine had **no optical drive**, so all disc-present paths (title tree, backup dialog, save progress, streaming server) could not be reproduced; those sections rely on string/UI evidence and are marked INFERRED/UNKNOWN in the spec.
* Outbound network was blocked, so server endpoints could not be captured (only the connectivity probe was observed).

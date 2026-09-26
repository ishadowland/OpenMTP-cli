# MTP ObjectFormatCode → category / MIME

`mtp-tree` and `mtp-backup` use the MTP `ObjectFormatCode` to bucket files into
high-level categories (`image`, `video`, `audio`, `document`, `executable`,
`folder`, `other`). The mapping lives in `scripts/_openmtp.py`.

## Range-based rules (libmtp grouping)

The libmtp headers partition ObjectFormatCodes into contiguous ranges:

| Range | Category | Notes |
| --- | --- | --- |
| `0x3001` | `folder` | PTP Association (`MTP_FORMAT_ASSOCIATION`). |
| `0x3800`–`0x38FF` | `image` | JPEG, PNG, BMP, GIF, TIFF, DNG, etc. |
| `0xB900`–`0xB9FF` | `video` | MP4, MOV, MPEG, MKV, 3GP, AVI, etc. |
| `0xB200`–`0xB2FF` | `audio` | MP3, AAC, WAV, FLAC, OGG, M4A, etc. |
| `0x3004` (text), `0x3005` (HTML), `0x300A` (XML), `0x300B`–`0x301F` | `document` | Office (Word, Excel, PowerPoint), OpenDocument, PDFs, etc. |
| `0x3003` | `executable` | Software / firmware. |
| anything else | `other` | Including `0x3000` (undefined), `0xBABE`, vendor codes. |

## Common MIME mappings

A short list of codes that `scripts/_openmtp.py::mime_for_code` resolves
explicitly. Anything not in the table falls back to
`application/octet-stream`.

| Code | MIME | What |
| --- | --- | --- |
| `0x3001` | `inode/directory` | Folder |
| `0x3004` | `text/plain` | Plain text |
| `0x3005` | `text/html` | HTML |
| `0x300A` | `application/xml` | XML |
| `0x300B` | `application/msword` | MS Word `.doc` |
| `0x300C` | `application/vnd.ms-excel` | MS Excel `.xls` |
| `0x300D` | `application/vnd.ms-powerpoint` | MS PowerPoint `.ppt` |
| `0x3801` | `image/jpeg` | JPEG |
| `0x3802` | `image/tiff` | TIFF |
| `0x3804` | `image/bmp` | BMP |
| `0x3807` | `image/png` | PNG |
| `0x3808` | `image/jp2` | JPEG 2000 |
| `0x380B` | `image/gif` | GIF |
| `0x380D` | `image/jpeg` | JFIF |
| `0x380E` | `image/x-adobe-dng` | Adobe DNG |
| `0xB216` | `audio/aac` | AAC |
| `0xB219` | `audio/mpeg` | MP3 |
| `0xB21C` | `audio/x-wav` | WAV |
| `0xB901` | `video/mp4` | MP4 |
| `0xB982` | `video/mpeg` | MPEG |
| `0xB983` | `video/quicktime` | QuickTime / MOV |
| `0xBA0A` | `video/x-matroska` | MKV |

## Why ranges and not a fixed table

libmtp defines ~200+ ObjectFormatCodes; enumerating them is brittle because
vendors add proprietary ones (e.g. `0xBABE`). The range-based scheme handles
those for free: anything not in `image` / `video` / `audio` falls into
`other`, and the JSON output keeps the raw `format_code` for callers that
need to make their own decision.

## Output JSON shape

Each node in `mtp-tree` JSON carries:

```json
{
  "path": "DCIM/Camera/IMG_001.jpg",
  "name": "IMG_001.jpg",
  "is_dir": false,
  "size": 5242880,
  "mtime": "2024-09-01T10:30:00Z",
  "format_code": "0x3801",
  "mime": "image/jpeg",
  "category": "image"
}
```

`format_code` is always a hex string (zero-padded to 4 digits) so callers can
parse without thinking about endianness.

## Filter aliases accepted by `--format-filter`

`mtp-tree --format-filter` and `mtp-backup --format-filter` accept the
following loose names:

| Alias | Category |
| --- | --- |
| `image`, `images`, `photo`, `photos` | `image` |
| `video`, `videos`, `movie`, `movies` | `video` |
| `audio`, `music`, `sound` | `audio` |
| `document`, `documents`, `doc`, `docs`, `text` | `document` |
| `all`, `any`, `*` | `all` (no filter) |
| `image/...`, `video/...`, `audio/...` MIME prefixes | respective category |
| `text/...`, `application/...` MIME prefixes | `document` |

Anything else raises `ValueError`, which the CLI surfaces as
`Error: unknown --format-filter value: ...` on stderr with exit code 1.
import argparse
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# Files/dirs to strip out during conversion
APPLE_JUNK = {
    "iTunesMetadata.plist",
    "iTunesArtwork",
    ".DS_Store",
}

# ── Namespace constants ───────────────────────────────────────────────────────

_XHTML_NS = "http://www.w3.org/1999/xhtml"
_OPF_NS = "http://www.idpf.org/2007/opf"
_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_XHTML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'


def _register_namespaces() -> None:
    for prefix, uri in [
        ("", _XHTML_NS),
        ("xlink", "http://www.w3.org/1999/xlink"),
        ("ibooks", "http://www.apple.com/2011/iBooks"),
        ("m", "http://www.w3.org/1998/Math/MathML"),
        ("epub", "http://www.idpf.org/2007/ops"),
        ("opf", _OPF_NS),
        ("dc", "http://purl.org/dc/elements/1.1/"),
    ]:
        ET.register_namespace(prefix, uri)


# ── Fixed-layout CSS parsing ──────────────────────────────────────────────────

def _parse_paginated_css(css_text: str) -> list[dict]:
    """
    Parse iBooks Author paginated CSS into a list of page descriptors.

    Each dict has:
      width, height  – page dimensions (strings like '1024.0pt')
      slots          – {element_id: {left, top, width, height, z-index}}
      ids            – ordered list of element IDs on this page
    """
    pages = []
    pos = 0
    while True:
        m = re.search(r"@page\s+::nth-instance\s*\{", css_text[pos:])
        if not m:
            break
        brace_open = pos + m.end() - 1
        depth, j = 1, brace_open + 1
        while j < len(css_text) and depth:
            c = css_text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        block = css_text[brace_open + 1 : j - 1]
        pos = j

        w_m = re.search(r"^\s*width:\s*([\d.]+pt)", block, re.MULTILINE)
        h_m = re.search(r"^\s*height:\s*([\d.]+pt)", block, re.MULTILINE)

        slots: dict[str, dict] = {}
        for sm in re.finditer(r"::slot\(([^)]+)\)\s*\{([^}]+)\}", block, re.DOTALL):
            sid = sm.group(1).strip()
            props: dict[str, str] = {}
            for pm in re.finditer(r"([\w-]+)\s*:\s*([^;\n]+)", sm.group(2)):
                k, v = pm.group(1).strip(), pm.group(2).strip()
                if k in ("left", "top", "width", "height", "z-index"):
                    props[k] = v
            slots[sid] = props

        pm2 = re.search(r"-ibooks-positioned-slots:\s*([^;]+);", block)
        ids: list[str] = []
        if pm2:
            for item in pm2.group(1).split(","):
                item = item.strip()
                bm = re.match(r"body\(([^)]+)\)", item)
                ids.append(bm.group(1).strip() if bm else item)

        pages.append({
            "width": w_m.group(1) if w_m else "1024.0pt",
            "height": h_m.group(1) if h_m else "748.0pt",
            "slots": slots,
            "ids": ids,
        })
    return pages


# ── XHTML transformation ──────────────────────────────────────────────────────

def _pt_to_px(value: str) -> str:
    """
    Convert iBooks 'pt' units to CSS 'px'.
    iBooks Author uses pt as 1:1 screen pixels (iPad logical pixels),
    but CSS renderers treat 1pt = 1.333px at 96 dpi — wrong for screen layout.
    """
    if value.endswith("pt"):
        return value[:-2] + "px"
    return value


def _page_css(page: dict) -> str:
    """Generate standard absolute-positioning CSS for a fixed-layout page."""
    w = _pt_to_px(page["width"])
    h = _pt_to_px(page["height"])
    lines = [
        "html, body { margin: 0; padding: 0; }",
        f"body {{ position: relative; width: {w}; height: {h}; overflow: hidden; }}",
    ]
    for eid, props in page["slots"].items():
        css_props = ["position: absolute"]
        for k in ("left", "top", "width", "height", "z-index"):
            if k in props:
                css_props.append(f"{k}: {_pt_to_px(props[k])}")
        lines.append(f"#{eid} {{ {'; '.join(css_props)}; }}")
    return "\n".join(lines)


def _extract_css_hrefs(xhtml: str) -> list[str]:
    """
    Return hrefs of non-paginated CSS files from <?xml-stylesheet?> PIs.
    These need to be preserved as <link> tags after the PIs are stripped.
    """
    hrefs = []
    for m in re.finditer(r"<\?xml-stylesheet\b([^?]*)\?>", xhtml):
        content = m.group(1)
        href_m = re.search(r"href=['\"]([^'\"]+)['\"]", content)
        if href_m:
            href = href_m.group(1)
            if not href.endswith("-paginated.css"):
                hrefs.append(href)
    return hrefs


def _strip_ibooks_refs(xhtml: str) -> str:
    """Remove iBooks-specific processing instructions and link/object tags."""
    # All <?xml-stylesheet?> PIs (callers re-add the non-paginated ones as <link> tags)
    xhtml = re.sub(r"<\?xml-stylesheet[^?]*\?>", "", xhtml)
    # <link> tags for iBooks hint files
    xhtml = re.sub(r'<link\b[^>]*\btype="application/x-ibooks[^"]*"[^>]*/>', "", xhtml)
    # <object type="application/x-ibooks+*"> → <div>
    xhtml = re.sub(
        r'<object\b([^>]*)type="application/x-ibooks\+[^"]*"([^>]*)>',
        r"<div\1\2>",
        xhtml,
    )
    xhtml = xhtml.replace("</object>", "</div>")
    return xhtml


def _restore_css_links(xhtml: str, css_hrefs: list[str]) -> str:
    """Inject <link> tags for the given CSS hrefs into <head>."""
    if not css_hrefs:
        return xhtml
    links = "\n".join(
        f'<link rel="stylesheet" type="text/css" href="{h}" />' for h in css_hrefs
    )
    return re.sub(r"(<head(?:\s[^>]*)?>)", r"\1\n" + links, xhtml, count=1)


def _inject_fixed_layout_head(xhtml: str, page: dict) -> str:
    """
    Preserve the original CSS link; remove iBooks-specific markup; inject
    viewport meta and absolute-position style block into the XHTML <head>.
    """
    css_hrefs = _extract_css_hrefs(xhtml)
    xhtml = _strip_ibooks_refs(xhtml)
    xhtml = _restore_css_links(xhtml, css_hrefs)

    # iBooks pt values are 1:1 with screen pixels
    w = int(float(page["width"].rstrip("pt")))
    h = int(float(page["height"].rstrip("pt")))
    block = (
        f'\n<meta name="viewport" content="width={w}, height={h}" />'
        f"\n<style>\n{_page_css(page)}\n</style>"
    )
    return re.sub(r"(<head(?:\s[^>]*)?>)", r"\1" + block, xhtml, count=1)


def _build_page_xhtml(xhtml_text: str, page: dict) -> str:
    """
    Extract only the body elements belonging to `page` from xhtml_text,
    inject fixed-layout styles, and return the new XHTML document string.
    """
    _register_namespaces()

    # Strip processing instructions and DOCTYPE so ET can parse
    clean = re.sub(r"<\?[^?]*\?>", "", xhtml_text)
    clean = re.sub(r"<!DOCTYPE[^>]*>", "", clean)

    root = ET.fromstring(clean)
    body = root.find(f"{{{_XHTML_NS}}}body")
    if body is None:
        body = root.find("body")

    page_ids = set(page["ids"])
    keep = [child for child in body if child.get("id", "") in page_ids]

    for child in list(body):
        body.remove(child)
    for child in keep:
        body.append(child)

    out = _XHTML_DECL + ET.tostring(root, encoding="unicode")
    return _inject_fixed_layout_head(out, page)


# ── Main fixed-layout transformation ─────────────────────────────────────────

def _apply_fixed_layout(work_dir: Path) -> None:
    """
    Transform an extracted iBooks Author directory into EPUB 3 fixed-layout,
    modifying files in work_dir in-place.
    """
    _register_namespaces()

    container_path = work_dir / "META-INF" / "container.xml"
    if not container_path.exists():
        return

    opf_rel = (
        ET.parse(container_path)
        .find(f"{{{_CONTAINER_NS}}}rootfiles"
              f"/{{{_CONTAINER_NS}}}rootfile")
        .get("full-path")
    )
    opf_path = work_dir / opf_rel
    opf_dir = opf_path.parent

    opf_tree = ET.parse(opf_path)
    opf_root = opf_tree.getroot()
    OPF = _OPF_NS

    manifest = opf_root.find(f"{{{OPF}}}manifest")
    spine = opf_root.find(f"{{{OPF}}}spine")
    metadata = opf_root.find(f"{{{OPF}}}metadata")

    id_to_href = {
        item.get("id", ""): item.get("href", "")
        for item in manifest.findall(f"{{{OPF}}}item")
    }

    spine_idrefs = {
        ref.get("idref", "")
        for ref in spine.findall(f"{{{OPF}}}itemref")
    }

    spine_replacements: dict[str, list[str]] = {}
    manifest_adds: list[tuple[str, str]] = []
    manifest_remove_ids: set[str] = set()

    processed_hrefs: set[str] = set()

    # --- Pass 1: spine items (may be split into multiple pages) ---
    for itemref in spine.findall(f"{{{OPF}}}itemref"):
        idref = itemref.get("idref", "")
        xhtml_href = id_to_href.get(idref, "")
        if not xhtml_href.endswith(".xhtml"):
            continue

        stem = Path(xhtml_href).stem
        css_path = opf_dir / "assets" / "css" / f"{stem}-paginated.css"
        if not css_path.exists():
            # No paginated CSS — strip iBooks refs, restore CSS links, mark fixed-layout
            xhtml_path = opf_dir / xhtml_href
            raw = xhtml_path.read_text(encoding="utf-8")
            css_hrefs = _extract_css_hrefs(raw)
            raw = _strip_ibooks_refs(raw)
            raw = _restore_css_links(raw, css_hrefs)
            xhtml_path.write_text(raw, encoding="utf-8")
            itemref.set("properties", "rendition:layout-pre-paginated")
            processed_hrefs.add(xhtml_href)
            continue

        pages = _parse_paginated_css(css_path.read_text(encoding="utf-8"))
        if not pages:
            continue

        xhtml_path = opf_dir / xhtml_href
        xhtml_text = xhtml_path.read_text(encoding="utf-8")

        if len(pages) == 1:
            xhtml_path.write_text(
                _inject_fixed_layout_head(xhtml_text, pages[0]), encoding="utf-8"
            )
            itemref.set("properties", "rendition:layout-pre-paginated")
            processed_hrefs.add(xhtml_href)
        else:
            new_ids: list[str] = []
            xhtml_parent = Path(xhtml_href).parent

            for i, page in enumerate(pages, 1):
                new_filename = f"{stem}-p{i}.xhtml"
                new_href = (
                    f"{xhtml_parent}/{new_filename}"
                    if str(xhtml_parent) != "."
                    else new_filename
                )
                (opf_dir / new_href).write_text(
                    _build_page_xhtml(xhtml_text, page), encoding="utf-8"
                )
                new_id = f"{idref}-p{i}"
                new_ids.append(new_id)
                manifest_adds.append((new_id, new_href))
                processed_hrefs.add(new_href)

            manifest_remove_ids.add(idref)
            spine_replacements[idref] = new_ids
            xhtml_path.unlink()

    # Update manifest: remove old multi-page entries, add split pages
    for iid in manifest_remove_ids:
        for item in manifest.findall(f"{{{OPF}}}item"):
            if item.get("id") == iid:
                manifest.remove(item)
                break

    for new_id, new_href in manifest_adds:
        el = ET.SubElement(manifest, f"{{{OPF}}}item")
        el.set("id", new_id)
        el.set("href", new_href)
        el.set("media-type", "application/xhtml+xml")

    # Update spine: expand multi-page entries into per-page itemrefs
    for itemref in list(spine.findall(f"{{{OPF}}}itemref")):
        idref = itemref.get("idref", "")
        if idref not in spine_replacements:
            continue
        idx = list(spine).index(itemref)
        spine.remove(itemref)
        for j, new_id in enumerate(spine_replacements[idref]):
            new_ref = ET.Element(f"{{{OPF}}}itemref")
            new_ref.set("idref", new_id)
            new_ref.set("linear", "yes")
            new_ref.set("properties", "rendition:layout-pre-paginated")
            spine.insert(idx + j, new_ref)

    # --- Pass 2: non-spine XHTML items (inject only, no splitting) ---
    for item in manifest.findall(f"{{{OPF}}}item"):
        iid = item.get("id", "")
        if iid in spine_idrefs or item.get("media-type") != "application/xhtml+xml":
            continue
        xhtml_href = item.get("href", "")
        stem = Path(xhtml_href).stem
        css_path = opf_dir / "assets" / "css" / f"{stem}-paginated.css"
        if not css_path.exists():
            continue
        pages = _parse_paginated_css(css_path.read_text(encoding="utf-8"))
        if not pages:
            continue
        xhtml_path = opf_dir / xhtml_href
        xhtml_text = xhtml_path.read_text(encoding="utf-8")
        xhtml_path.write_text(
            _inject_fixed_layout_head(xhtml_text, pages[0]), encoding="utf-8"
        )
        processed_hrefs.add(xhtml_href)

    # --- Pass 3: strip iBooks refs from any remaining unprocessed XHTML ---
    for item in manifest.findall(f"{{{OPF}}}item"):
        if item.get("media-type") != "application/xhtml+xml":
            continue
        xhtml_href = item.get("href", "")
        if xhtml_href in processed_hrefs:
            continue
        xhtml_path = opf_dir / xhtml_href
        if xhtml_path.exists():
            raw = xhtml_path.read_text(encoding="utf-8")
            css_hrefs = _extract_css_hrefs(raw)
            raw = _strip_ibooks_refs(raw)
            raw = _restore_css_links(raw, css_hrefs)
            xhtml_path.write_text(raw, encoding="utf-8")

    # Add EPUB 3 fixed-layout rendition metadata
    for prop, val in [
        ("rendition:layout", "pre-paginated"),
        ("rendition:orientation", "landscape"),
        ("rendition:spread", "landscape"),
    ]:
        el = ET.SubElement(metadata, f"{{{OPF}}}meta")
        el.set("property", prop)
        el.text = val

    with open(opf_path, "wb") as f:
        opf_tree.write(f, encoding="UTF-8", xml_declaration=True)

    print("  Applied EPUB 3 fixed-layout.")


# ── Core packaging ────────────────────────────────────────────────────────────

def is_junk(path: str) -> bool:
    """Return True if this path should be excluded from the epub."""
    name = os.path.basename(path)
    return name in APPLE_JUNK or name.startswith("._")


def collect_files(source_dir: Path) -> list[Path]:
    """
    Walk source_dir and return all non-junk files except mimetype, sorted for
    deterministic output. mimetype is always written separately by build_epub.
    """
    others = []
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in dirs if not is_junk(d)]
        for fname in files:
            full = Path(root) / fname
            rel = full.relative_to(source_dir)
            if is_junk(fname) or rel == Path("mimetype"):
                continue
            others.append(full)
    others.sort()
    return others


def build_epub(source_dir: Path, output_path: Path) -> None:
    """
    Package source_dir into a valid epub at output_path.

    EPUB zip structure requirements:
      - 'mimetype' entry must be FIRST and UNCOMPRESSED (ZIP_STORED)
      - All other entries use ZIP_DEFLATED
    """
    other_files = collect_files(source_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # .ibooks files use 'application/x-ibooks+zip'; always write the epub standard value.
        zf.writestr(
            zipfile.ZipInfo("mimetype"),  # ZipInfo defaults to ZIP_STORED
            "application/epub+zip",
        )
        for fpath in other_files:
            arcname = str(fpath.relative_to(source_dir))
            zf.write(fpath, arcname, compress_type=zipfile.ZIP_DEFLATED)

    print(f"  -> {output_path}")


def resolve_source_dir(ibooks_path: Path) -> Path:
    """
    On macOS, .ibooks bundles are presented as directories.
    On other systems they may arrive as a single zip-like file.
    If it's a file, unzip it to a temp dir and return that.
    """
    if ibooks_path.is_dir():
        return ibooks_path

    if not ibooks_path.is_file():
        raise FileNotFoundError(f"Not found: {ibooks_path}")

    if not zipfile.is_zipfile(ibooks_path):
        raise ValueError(
            f"{ibooks_path} is not a directory or a zip-compatible archive.\n"
            "If this is a DRM-protected file, conversion is not possible."
        )

    tmp = Path(tempfile.mkdtemp(prefix="ibooks_"))
    print(f"  Extracting archive to temp dir: {tmp}")
    with zipfile.ZipFile(ibooks_path, "r") as zf:
        zf.extractall(tmp)
    return tmp


def convert_one(ibooks_path: Path, output_path: Path) -> None:
    """Convert a single .ibooks path to an .epub file."""
    print(f"Converting: {ibooks_path.name}")
    tmp_dir = None

    try:
        raw_dir = resolve_source_dir(ibooks_path)

        if raw_dir == ibooks_path:
            # Directory source — copy to temp so we never mutate the original
            tmp_dir = Path(tempfile.mkdtemp(prefix="ibooks_"))
            shutil.copytree(raw_dir, tmp_dir, dirs_exist_ok=True)
        else:
            tmp_dir = raw_dir  # already a temp from resolve_source_dir

        _apply_fixed_layout(tmp_dir)
        build_epub(tmp_dir, output_path)

    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def default_output(ibooks_path: Path) -> Path:
    return ibooks_path.with_suffix(".epub")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Apple .ibooks files to standard .epub format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", help=".ibooks file or directory of .ibooks files")
    parser.add_argument(
        "output",
        nargs="?",
        help="Output .epub file or output directory (optional)",
    )
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_arg = Path(args.output).expanduser().resolve() if args.output else None

    # --- Batch mode: input is a directory containing multiple .ibooks ---
    if input_path.is_dir() and not (input_path.suffix.lower() == ".ibooks"):
        ibooks_files = sorted(input_path.glob("*.ibooks"))
        if not ibooks_files:
            print(f"No .ibooks files found in {input_path}", file=sys.stderr)
            sys.exit(1)

        out_dir = output_arg if output_arg else input_path
        out_dir.mkdir(parents=True, exist_ok=True)

        for ib in ibooks_files:
            out = out_dir / ib.with_suffix(".epub").name
            try:
                convert_one(ib, out)
            except Exception as exc:
                print(f"  Error: {exc}", file=sys.stderr)

    # --- Single file mode ---
    else:
        if not input_path.exists():
            print(f"Input not found: {input_path}", file=sys.stderr)
            sys.exit(1)

        if output_arg:
            if output_arg.is_dir():
                out = output_arg / input_path.with_suffix(".epub").name
            else:
                out = output_arg
        else:
            out = default_output(input_path)

        try:
            convert_one(input_path, out)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()

"""Tests for ibooks-to-epub converter."""

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from convert import (
    is_junk,
    collect_files,
    convert_one,
    _parse_paginated_css,
    _page_css,
    _pt_to_px,
    _inject_fixed_layout_head,
)

FIXTURE = Path(__file__).parent / "fixtures" / "21-VetMicroAnatomyReview-EyeEar.ibooks"
OPF_NS = "http://www.idpf.org/2007/opf"


# ---------------------------------------------------------------------------
# is_junk
# ---------------------------------------------------------------------------

class TestIsJunk:
    def test_apple_metadata_plist(self):
        assert is_junk("iTunesMetadata.plist")

    def test_itunes_artwork(self):
        assert is_junk("iTunesArtwork")

    def test_ds_store(self):
        assert is_junk(".DS_Store")

    def test_dot_underscore_prefix(self):
        assert is_junk("._content.xhtml")

    def test_normal_file_is_not_junk(self):
        assert not is_junk("content.xhtml")

    def test_path_with_junk_basename(self):
        assert is_junk("OPS/.DS_Store")

    def test_path_with_clean_basename(self):
        assert not is_junk("OPS/content1.xhtml")


# ---------------------------------------------------------------------------
# collect_files
# ---------------------------------------------------------------------------

class TestCollectFiles:
    @pytest.fixture()
    def extracted(self, tmp_path):
        with zipfile.ZipFile(FIXTURE) as zf:
            zf.extractall(tmp_path)
        return tmp_path

    def test_mimetype_excluded(self, extracted):
        files = collect_files(extracted)
        rel_names = [str(f.relative_to(extracted)) for f in files]
        assert "mimetype" not in rel_names

    def test_apple_junk_excluded(self, extracted):
        files = collect_files(extracted)
        names = [f.name for f in files]
        assert "iTunesArtwork" not in names
        assert "iTunesMetadata.plist" not in names

    def test_content_files_present(self, extracted):
        files = collect_files(extracted)
        rel_names = [str(f.relative_to(extracted)) for f in files]
        assert "META-INF/container.xml" in rel_names

    def test_sorted(self, extracted):
        files = collect_files(extracted)
        assert files == sorted(files)


# ---------------------------------------------------------------------------
# _parse_paginated_css
# ---------------------------------------------------------------------------

class TestParsePaginatedCss:
    SINGLE_PAGE_CSS = """
@page ::nth-instance
{
    height: 748.0pt;
    width: 1024.0pt;
    ::slot(image-1)
    {
        height: 200.000pt;
        left: 30.000pt;
        top: 50.000pt;
        width: 400.000pt;
        z-index: 1;
    }
    ::slot(text-1)
    {
        height: 100.000pt;
        left: 50.000pt;
        top: 300.000pt;
        width: 900.000pt;
        z-index: 2;
    }
    -ibooks-positioned-slots: image-1, body(text-1);
}
"""

    def test_single_page_count(self):
        pages = _parse_paginated_css(self.SINGLE_PAGE_CSS)
        assert len(pages) == 1

    def test_page_dimensions(self):
        page = _parse_paginated_css(self.SINGLE_PAGE_CSS)[0]
        assert page["width"] == "1024.0pt"
        assert page["height"] == "748.0pt"

    def test_slot_positions_extracted(self):
        page = _parse_paginated_css(self.SINGLE_PAGE_CSS)[0]
        assert "image-1" in page["slots"]
        assert page["slots"]["image-1"]["left"] == "30.000pt"
        assert page["slots"]["image-1"]["top"] == "50.000pt"
        assert page["slots"]["image-1"]["z-index"] == "1"

    def test_positioned_ids_extracted(self):
        page = _parse_paginated_css(self.SINGLE_PAGE_CSS)[0]
        assert "image-1" in page["ids"]
        assert "text-1" in page["ids"]  # body() wrapper stripped

    def test_multi_page_count(self):
        css = self.SINGLE_PAGE_CSS * 3
        pages = _parse_paginated_css(css)
        assert len(pages) == 3

    def test_empty_css(self):
        assert _parse_paginated_css("/* no slots */") == []


# ---------------------------------------------------------------------------
# _pt_to_px
# ---------------------------------------------------------------------------

class TestPtToPx:
    def test_converts_pt_to_px(self):
        assert _pt_to_px("788.000pt") == "788.000px"

    def test_leaves_px_unchanged(self):
        assert _pt_to_px("100px") == "100px"

    def test_leaves_integers_unchanged(self):
        assert _pt_to_px("1") == "1"

    def test_page_css_uses_px(self):
        page = {
            "width": "1024.0pt",
            "height": "748.0pt",
            "slots": {"el": {"left": "100pt", "top": "200pt", "width": "300pt", "height": "50pt", "z-index": "1"}},
            "ids": ["el"],
        }
        css = _page_css(page)
        assert "pt" not in css
        assert "1024.0px" in css
        assert "100px" in css


# ---------------------------------------------------------------------------
# _inject_fixed_layout_head
# ---------------------------------------------------------------------------

class TestInjectFixedLayoutHead:
    MINIMAL_XHTML = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<?xml-stylesheet href="paginated.css" type="text/css" media="paginated"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head></head>'
        '<body><object type="application/x-ibooks+shape" id="s1">'
        '<p>Hello</p></object></body></html>'
    )
    PAGE = {
        "width": "1024.0pt",
        "height": "748.0pt",
        "slots": {"s1": {"left": "10pt", "top": "20pt", "width": "100pt", "height": "50pt"}},
        "ids": ["s1"],
    }

    def test_viewport_injected(self):
        out = _inject_fixed_layout_head(self.MINIMAL_XHTML, self.PAGE)
        assert 'name="viewport"' in out
        assert "width=1024" in out
        assert "height=748" in out

    def test_absolute_css_injected(self):
        out = _inject_fixed_layout_head(self.MINIMAL_XHTML, self.PAGE)
        assert "position: absolute" in out
        assert "#s1" in out

    def test_ibooks_stylesheet_pi_removed(self):
        out = _inject_fixed_layout_head(self.MINIMAL_XHTML, self.PAGE)
        assert "xml-stylesheet" not in out

    def test_object_converted_to_div(self):
        out = _inject_fixed_layout_head(self.MINIMAL_XHTML, self.PAGE)
        assert "<object" not in out
        assert "</object>" not in out
        assert '<div' in out


# ---------------------------------------------------------------------------
# End-to-end EPUB output: spec compliance + fixed-layout
# ---------------------------------------------------------------------------

class TestEpubOutput:
    @pytest.fixture(scope="class")
    def output_epub(self, tmp_path_factory):
        out = tmp_path_factory.mktemp("out") / "test.epub"
        convert_one(FIXTURE, out)
        return out

    # EPUB spec compliance
    def test_output_is_zip(self, output_epub):
        assert zipfile.is_zipfile(output_epub)

    def test_mimetype_is_first_entry(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            assert zf.namelist()[0] == "mimetype"

    def test_mimetype_is_stored_not_compressed(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED

    def test_mimetype_content_is_epub(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            assert zf.read("mimetype").decode() == "application/epub+zip"

    def test_has_container_xml(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            assert "META-INF/container.xml" in zf.namelist()

    def test_opf_file_exists(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
            ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            opf_path = container.find(".//c:rootfile", ns).attrib["full-path"]
            assert opf_path in zf.namelist()

    def test_apple_junk_stripped(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            names = zf.namelist()
        junk = [n for n in names if any(j in n for j in ["iTunesMetadata", "iTunesArtwork", ".DS_Store"])]
        assert junk == []

    def test_ibooks_mimetype_not_present(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            assert "x-ibooks" not in zf.read("mimetype").decode()

    # Fixed-layout EPUB 3 metadata
    def test_rendition_layout_metadata(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            opf = ET.fromstring(zf.read("OPS/ibooks.opf"))
        metas = {
            m.get("property"): m.text
            for m in opf.findall(f".//{{{OPF_NS}}}meta")
            if m.get("property", "").startswith("rendition:")
        }
        assert metas.get("rendition:layout") == "pre-paginated"
        assert metas.get("rendition:orientation") == "landscape"

    def test_all_spine_items_have_pre_paginated_property(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            opf = ET.fromstring(zf.read("OPS/ibooks.opf"))
        itemrefs = opf.findall(f".//{{{OPF_NS}}}itemref")
        assert len(itemrefs) > 0
        missing = [
            i.get("idref") for i in itemrefs
            if "pre-paginated" not in (i.get("properties") or "")
        ]
        assert missing == [], f"Itemrefs missing pre-paginated: {missing}"

    def test_multi_page_chapters_are_split(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            names = zf.namelist()
        split = [n for n in names if re.match(r"OPS/content\d+-p\d+\.xhtml", n)]
        assert len(split) > 0

    def test_split_pages_have_viewport(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            names = zf.namelist()
            split = sorted(n for n in names if re.match(r"OPS/content\d+-p\d+\.xhtml", n))
            content = zf.read(split[0]).decode()
        assert 'name="viewport"' in content

    def test_split_pages_have_absolute_css(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            names = zf.namelist()
            split = sorted(n for n in names if re.match(r"OPS/content\d+-p\d+\.xhtml", n))
            content = zf.read(split[0]).decode()
        assert "position: absolute" in content

    def test_single_page_chapter_has_viewport(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            content = zf.read("OPS/content1.xhtml").decode()
        assert 'name="viewport"' in content

    def test_no_ibooks_stylesheet_pis_remain(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            names = zf.namelist()
            xhtml_files = [n for n in names if n.endswith(".xhtml")]
            for name in xhtml_files:
                content = zf.read(name).decode()
                assert "xml-stylesheet" not in content, f"{name} still has xml-stylesheet PI"

    def test_no_ibooks_object_elements_remain(self, output_epub):
        with zipfile.ZipFile(output_epub) as zf:
            names = zf.namelist()
            xhtml_files = [n for n in names if n.endswith(".xhtml")]
            for name in xhtml_files:
                content = zf.read(name).decode()
                assert "application/x-ibooks" not in content, f"{name} has ibooks object type"

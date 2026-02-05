#!/usr/bin/env python3
"""
Binder - Binds chapter drafts into a single ODT file
using standard manuscript format.
"""

import argparse
import re
import sys
import zipfile
from pathlib import Path
from datetime import datetime
from xml.etree import ElementTree as ET

# TOML support (built-in for 3.11+, fallback to tomli for 3.10)
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

CONFIG_FILENAME = "binder.toml"

# Default configuration values
DEFAULT_CONFIG = {
    "path": ".",
    "heading": "num",
    "author": "",
    "short_title": "",
    "output": None,
    "title": "",
    "author_name": "",
    "author_address": "",
    "title_page": True,
}


def find_config_file(start_path: Path) -> Path | None:
    """
    Search for config file in the following order:
    1. Current working directory
    2. Specified path (if different from cwd)
    3. User's home directory (~/.config/binder.toml)
    """
    candidates = [
        Path.cwd() / CONFIG_FILENAME,
        start_path / CONFIG_FILENAME,
        Path.home() / ".config" / CONFIG_FILENAME,
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_config(config_path: Path | None = None) -> dict:
    """
    Load configuration from a TOML file.
    Returns default config if no file found or tomllib unavailable.
    """
    config = DEFAULT_CONFIG.copy()

    if tomllib is None:
        return config

    if config_path is None:
        return config

    if not config_path.exists():
        return config

    try:
        with open(config_path, "rb") as f:
            file_config = tomllib.load(f)

        # Flatten nested structure if present (e.g., [manuscript] section)
        if "manuscript" in file_config:
            file_config = {**file_config, **file_config.pop("manuscript")}

        # Normalize hyphenated keys to underscores
        key_mappings = {
            "short-title": "short_title",
            "author-name": "author_name",
            "author-address": "author_address",
            "title-page": "title_page",
        }
        for old_key, new_key in key_mappings.items():
            if old_key in file_config:
                file_config[new_key] = file_config.pop(old_key)

        # Update config with file values
        for key in DEFAULT_CONFIG:
            if key in file_config:
                config[key] = file_config[key]

    except Exception as e:
        print(f"Warning: Could not load config file {config_path}: {e}", file=sys.stderr)

    return config


def merge_config_with_args(config: dict, args: argparse.Namespace) -> argparse.Namespace:
    """
    Merge config file values with command line arguments.
    CLI arguments take precedence over config file values.
    """
    # Set config values as defaults, then override with explicit CLI args
    if args.path == "." and config.get("path", ".") != ".":
        args.path = config["path"]

    if args.heading == "num" and config.get("heading"):
        args.heading = config["heading"]

    if not args.author and config.get("author"):
        args.author = config["author"]

    if not args.short_title and config.get("short_title"):
        args.short_title = config["short_title"]

    if not args.output and config.get("output"):
        args.output = config["output"]

    # Title page options (primarily from config file)
    if not args.title and config.get("title"):
        args.title = config["title"]

    if not args.author_name and config.get("author_name"):
        args.author_name = config["author_name"]

    if not args.author_address and config.get("author_address"):
        args.author_address = config["author_address"]

    # title_page defaults to True, so only override if explicitly set to False in config
    if hasattr(args, 'title_page') and args.title_page and not config.get("title_page", True):
        args.title_page = False

    return args


# ODT XML namespaces
NAMESPACES = {
    'office': 'urn:oasis:names:tc:opendocument:xmlns:office:1.0',
    'style': 'urn:oasis:names:tc:opendocument:xmlns:style:1.0',
    'text': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0',
    'fo': 'urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0',
    'svg': 'urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0',
    'manifest': 'urn:oasis:names:tc:opendocument:xmlns:manifest:1.0',
}

# Register namespaces
for prefix, uri in NAMESPACES.items():
    ET.register_namespace(prefix, uri)


def int_to_roman(num: int) -> str:
    """Convert an integer to a Roman numeral."""
    val = [
        1000, 900, 500, 400,
        100, 90, 50, 40,
        10, 9, 5, 4,
        1
    ]
    syms = [
        'M', 'CM', 'D', 'CD',
        'C', 'XC', 'L', 'XL',
        'X', 'IX', 'V', 'IV',
        'I'
    ]
    roman_num = ''
    i = 0
    while num > 0:
        for _ in range(num // val[i]):
            roman_num += syms[i]
            num -= val[i]
        i += 1
    return roman_num


def add_text_with_emphasis(parent: ET.Element, text: str) -> None:
    """
    Add text to a paragraph element, parsing _emphasis_ markers into italic spans.
    Text surrounded by underscores becomes italicized.
    """
    # Pattern matches _text_ but not __text__ or isolated underscores
    pattern = re.compile(r'(?<![_\w])_([^_]+)_(?![_\w])')

    parts = pattern.split(text)
    matches = pattern.findall(text)

    # parts[0] is text before first match, parts[1] is text after first match, etc.
    # matches contains the captured groups (text inside underscores)
    match_idx = 0
    for i, part in enumerate(parts):
        if i % 2 == 0:
            # Regular text
            if part:
                if i == 0:
                    parent.text = part
                else:
                    # Add to tail of previous element
                    children = list(parent)
                    if children:
                        prev = children[-1]
                        prev.tail = (prev.tail or '') + part
                    else:
                        parent.text = (parent.text or '') + part
        else:
            # Emphasized text (inside underscores)
            span = ET.SubElement(parent, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}span')
            span.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name', 'Underline')
            span.text = part
            span.tail = ''


def create_folder_structure(base_path: Path) -> None:
    """Create the trash and draft directories and config file."""
    draft_dir = base_path / "draft"
    trash_dir = base_path / "trash"
    config_file = base_path / CONFIG_FILENAME

    draft_dir.mkdir(parents=True, exist_ok=True)
    trash_dir.mkdir(parents=True, exist_ok=True)

    print(f"Created directory: {draft_dir}")
    print(f"Created directory: {trash_dir}")

    # Create config file if it doesn't exist
    if not config_file.exists():
        config_content = """\
# Binder configuration file
# Command line arguments override these values.

# Chapter heading style: "roman", "title", "num", "chapter", or "nil"
heading = "num"

# Author surname for the header (e.g., "Smith")
author = ""

# Short title for the header (e.g., "My Novel")
short-title = ""

# Output filename (without this, uses manuscript_TIMESTAMP.odt)
# output = "manuscript.odt"

# --- Title Page Options ---

# Enable or disable the title page
title-page = true

# Full manuscript title (centered on title page)
title = ""

# Author's full name (centered below title)
author-name = ""

# Author's contact info (top left of title page)
# Use triple quotes for multiple lines
author-address = \"\"\"
\"\"\"
"""
        config_file.write_text(config_content)
        print(f"Created config: {config_file}")
    else:
        print(f"Config exists: {config_file}")


def find_chapter_files(draft_dir: Path) -> list[tuple[int, str, Path]]:
    """
    Find all chapter files matching the pattern N_Chapter_Title.txt
    Returns list of (chapter_number, title, path) sorted by chapter number.
    """
    pattern = re.compile(r'^(\d+)_(.+)\.txt$')
    chapters = []

    for file_path in draft_dir.iterdir():
        if file_path.is_file():
            match = pattern.match(file_path.name)
            if match:
                chapter_num = int(match.group(1))
                # Convert underscores to spaces in title
                title = match.group(2).replace('_', ' ')
                chapters.append((chapter_num, title, file_path))

    # Sort by chapter number
    chapters.sort(key=lambda x: x[0])
    return chapters


def create_manifest_xml() -> str:
    """Create the META-INF/manifest.xml content."""
    manifest = ET.Element('{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}manifest')
    manifest.set('{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}version', '1.2')

    entries = [
        ('/', 'application/vnd.oasis.opendocument.text', '1.2'),
        ('content.xml', 'text/xml', None),
        ('styles.xml', 'text/xml', None),
    ]

    for path, media_type, version in entries:
        entry = ET.SubElement(manifest, '{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}file-entry')
        entry.set('{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}full-path', path)
        entry.set('{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}media-type', media_type)
        if version:
            entry.set('{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}version', version)

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(manifest, encoding='unicode')


def create_styles_xml(author: str = "", short_title: str = "") -> str:
    """
    Create styles.xml with standard manuscript format:
    - 1 inch margins
    - Courier New 12pt
    - Double spacing
    - Right-aligned header with author / title / page number
    """
    # Root element
    doc = ET.Element('{urn:oasis:names:tc:opendocument:xmlns:office:1.0}document-styles')
    doc.set('{urn:oasis:names:tc:opendocument:xmlns:office:1.0}version', '1.2')

    # Font declarations
    font_decls = ET.SubElement(doc, '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}font-face-decls')
    font = ET.SubElement(font_decls, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-face')
    font.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'Courier New')
    font.set('{urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0}font-family', "'Courier New'")
    font.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-family-generic', 'modern')
    font.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-pitch', 'fixed')

    # Automatic styles
    auto_styles = ET.SubElement(doc, '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}automatic-styles')

    # Page layout for title page (no header)
    title_page_layout = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}page-layout')
    title_page_layout.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'pm_title')

    title_page_props = ET.SubElement(title_page_layout, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}page-layout-properties')
    title_page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}page-width', '8.5in')
    title_page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}page-height', '11in')
    title_page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '1in')
    title_page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '1in')
    title_page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-left', '1in')
    title_page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-right', '1in')

    # Page layout - 1 inch margins (1in = 2.54cm)
    page_layout = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}page-layout')
    page_layout.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'pm1')

    page_props = ET.SubElement(page_layout, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}page-layout-properties')
    page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}page-width', '8.5in')
    page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}page-height', '11in')
    page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '1in')
    page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '1in')
    page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-left', '1in')
    page_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-right', '1in')

    # Header/footer properties
    header_footer_props = ET.SubElement(page_layout, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}header-style')
    hf_props = ET.SubElement(header_footer_props, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}header-footer-properties')
    hf_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}min-height', '0.5in')
    hf_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '0.2in')

    # Header style - right aligned
    header_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    header_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'HeaderStyle')
    header_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')

    header_props = ET.SubElement(header_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    header_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-align', 'end')

    header_text_props = ET.SubElement(header_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    header_text_props.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    header_text_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')

    # Master styles
    master_styles = ET.SubElement(doc, '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}master-styles')

    # Title page master (no header, flows to Standard)
    title_master = ET.SubElement(master_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}master-page')
    title_master.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'TitlePage')
    title_master.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}page-layout-name', 'pm_title')
    title_master.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}next-style-name', 'Standard')

    # Standard master page (with header)
    master_page = ET.SubElement(master_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}master-page')
    master_page.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'Standard')
    master_page.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}page-layout-name', 'pm1')

    # Add header with author / short title / page number
    if author or short_title:
        header = ET.SubElement(master_page, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}header')
        header_p = ET.SubElement(header, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p')
        header_p.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name', 'HeaderStyle')

        # Build header text: Author / Title / Page
        header_parts = []
        if author:
            header_parts.append(author)
        if short_title:
            header_parts.append(short_title.upper())
        header_p.text = ' / '.join(header_parts) + ' / '

        # Add page number field
        page_num = ET.SubElement(header_p, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}page-number')
        page_num.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}select-page', 'current')
        page_num.text = '1'

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(doc, encoding='unicode')


def create_content_xml(chapters: list[tuple[int, str, Path]], title: str = "Manuscript",
                       heading_style: str = "num", author_name: str = "",
                       author_address: str = "", title_page: bool = True,
                       word_count: int = 0) -> str:
    """
    Create content.xml with the manuscript content.
    Standard manuscript format with double spacing, first-line indent, etc.

    heading_style options:
        - "roman": Roman numeral only (I, II, III...)
        - "title": File title in ALL CAPS, no number
        - "num": Just the number (1, 2, 3...)
    """
    # Root element
    doc = ET.Element('{urn:oasis:names:tc:opendocument:xmlns:office:1.0}document-content')
    doc.set('{urn:oasis:names:tc:opendocument:xmlns:office:1.0}version', '1.2')

    # Font declarations
    font_decls = ET.SubElement(doc, '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}font-face-decls')
    font = ET.SubElement(font_decls, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-face')
    font.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'Courier New')
    font.set('{urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0}font-family', "'Courier New'")
    font.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-family-generic', 'modern')
    font.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-pitch', 'fixed')

    # Automatic styles
    auto_styles = ET.SubElement(doc, '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}automatic-styles')

    # Standard paragraph style (body text) - double spaced, first line indent
    p_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    p_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'P1')
    p_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')

    p_props = ET.SubElement(p_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    p_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-indent', '0.5in')
    p_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '0in')
    p_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '0in')
    p_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}line-height', '200%')
    p_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}widows', '0')
    p_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}orphans', '0')

    text_props = ET.SubElement(p_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    text_props.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    text_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')

    # Chapter heading style - centered, no indent
    h_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    h_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'Heading')
    h_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')

    h_props = ET.SubElement(h_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    h_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-align', 'center')
    h_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '3.5in')
    h_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '24pt')
    h_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}line-height', '200%')
    h_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}keep-with-next', 'always')
    h_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}widows', '0')
    h_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}orphans', '0')

    h_text = ET.SubElement(h_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    h_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    h_text.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')
    h_text.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-weight', 'normal')

    # Scene break style - centered
    sb_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    sb_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'SceneBreak')
    sb_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')

    sb_props = ET.SubElement(sb_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    sb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-align', 'center')
    sb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-indent', '0in')
    sb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '0in')
    sb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '0in')
    sb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}line-height', '200%')
    sb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}widows', '0')
    sb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}orphans', '0')

    sb_text = ET.SubElement(sb_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    sb_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    sb_text.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')

    # Chapter heading with page break (for chapters after the first)
    hb_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    hb_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'HeadingWithBreak')
    hb_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')

    hb_props = ET.SubElement(hb_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    hb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-align', 'center')
    hb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '3.5in')
    hb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '24pt')
    hb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}line-height', '200%')
    hb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}keep-with-next', 'always')
    hb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}break-before', 'page')
    hb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}widows', '0')
    hb_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}orphans', '0')

    hb_text = ET.SubElement(hb_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    hb_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    hb_text.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')
    hb_text.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-weight', 'normal')

    # First paragraph style (no indent after chapter heading)
    fp_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    fp_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'FirstPara')
    fp_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')

    fp_props = ET.SubElement(fp_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    fp_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-indent', '0in')
    fp_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '0in')
    fp_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '0in')
    fp_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}line-height', '200%')
    fp_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}widows', '0')
    fp_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}orphans', '0')

    fp_text = ET.SubElement(fp_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    fp_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    fp_text.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')

    # Underline text style (for emphasis with _underscores_)
    underline_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    underline_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'Underline')
    underline_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'text')

    underline_text = ET.SubElement(underline_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    underline_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-underline-style', 'solid')
    underline_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-underline-width', 'auto')
    underline_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-underline-color', 'font-color')

    # Title page styles
    # First element style - applies TitlePage master page (no header)
    first_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    first_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'TitlePageFirst')
    first_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')
    first_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}master-page-name', 'TitlePage')

    first_props = ET.SubElement(first_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    first_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-align', 'start')
    first_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-indent', '0in')
    first_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '0in')
    first_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '0in')
    first_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}line-height', '100%')

    first_text = ET.SubElement(first_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    first_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    first_text.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')

    # Address style - top left, single spaced
    addr_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    addr_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'TitlePageAddress')
    addr_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')

    addr_props = ET.SubElement(addr_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    addr_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-align', 'start')
    addr_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-indent', '0in')
    addr_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '0in')
    addr_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '0in')
    addr_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}line-height', '100%')

    addr_text = ET.SubElement(addr_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    addr_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    addr_text.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')

    # Title style - centered, halfway down
    title_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    title_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'TitlePageTitle')
    title_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')

    title_props = ET.SubElement(title_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    title_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-align', 'center')
    title_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-indent', '0in')
    title_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '3in')
    title_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '0in')
    title_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}line-height', '100%')

    title_text = ET.SubElement(title_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    title_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    title_text.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')

    # Title style when it's the first element (applies TitlePage master)
    title_first_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    title_first_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'TitlePageTitleFirst')
    title_first_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')
    title_first_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}master-page-name', 'TitlePage')

    title_first_props = ET.SubElement(title_first_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    title_first_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-align', 'center')
    title_first_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-indent', '0in')
    title_first_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '3in')
    title_first_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '0in')
    title_first_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}line-height', '100%')

    title_first_text = ET.SubElement(title_first_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    title_first_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    title_first_text.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')

    # Author name style - centered, below title
    author_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    author_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'TitlePageAuthor')
    author_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')

    author_props = ET.SubElement(author_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    author_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-align', 'center')
    author_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-indent', '0in')
    author_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '24pt')
    author_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '0in')
    author_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}line-height', '100%')

    author_text_props = ET.SubElement(author_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    author_text_props.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    author_text_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')

    # Word count style - centered, at bottom
    wc_style = ET.SubElement(auto_styles, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}style')
    wc_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name', 'TitlePageWordCount')
    wc_style.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}family', 'paragraph')

    wc_props = ET.SubElement(wc_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}paragraph-properties')
    wc_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-align', 'center')
    wc_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}text-indent', '0in')
    wc_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-top', '3in')
    wc_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}margin-bottom', '0in')
    wc_props.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}line-height', '100%')

    wc_text = ET.SubElement(wc_style, '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}text-properties')
    wc_text.set('{urn:oasis:names:tc:opendocument:xmlns:style:1.0}font-name', 'Courier New')
    wc_text.set('{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}font-size', '12pt')

    # Body
    body = ET.SubElement(doc, '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}body')
    text = ET.SubElement(body, '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}text')

    # Add title page if enabled
    if title_page and (title or author_name or author_address):
        first_element = True  # Track first element to apply TitlePage master

        # Author address (top left)
        if author_address:
            for line in author_address.strip().split('\n'):
                addr_p = ET.SubElement(text, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p')
                # First element uses TitlePageFirst to set master page (no header)
                if first_element:
                    addr_p.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name', 'TitlePageFirst')
                    first_element = False
                else:
                    addr_p.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name', 'TitlePageAddress')
                addr_p.text = line.strip()

        # Title (centered, middle of page)
        if title:
            title_p = ET.SubElement(text, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p')
            if first_element:
                # If no address, title is first - need to set master page via style
                title_p.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name', 'TitlePageTitleFirst')
                first_element = False
            else:
                title_p.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name', 'TitlePageTitle')
            title_p.text = title.upper()

        # Author name (centered, below title)
        if author_name:
            author_p = ET.SubElement(text, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p')
            author_p.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name', 'TitlePageAuthor')
            author_p.text = f"by {author_name}"

        # Word count (centered, bottom of page)
        if word_count > 0:
            wc_p = ET.SubElement(text, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p')
            wc_p.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name', 'TitlePageWordCount')
            wc_p.text = f"about {word_count:,} words"

    # Add each chapter
    for idx, (chapter_num, chapter_title, file_path) in enumerate(chapters):
        # Chapter heading - use HeadingWithBreak for all chapters when title page exists,
        # otherwise first chapter uses Heading (no page break)
        heading = ET.SubElement(text, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p')
        use_page_break = idx > 0 or (title_page and (title or author_name or author_address))
        heading.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name',
                    'HeadingWithBreak' if use_page_break else 'Heading')

        # Set heading text based on style
        if heading_style == "roman":
            heading.text = int_to_roman(chapter_num)
        elif heading_style == "title":
            heading.text = chapter_title.upper()
        elif heading_style == "chapter":
            heading.text = f"Chapter {chapter_num}"
        elif heading_style == "nil":
            heading.text = " "  # Empty but preserves spacing
        else:  # "num"
            heading.text = str(chapter_num)

        # Read and add chapter content
        content = file_path.read_text(encoding='utf-8')
        paragraphs = content.split('\n\n')

        first_para = True
        for para_text in paragraphs:
            para_text = para_text.strip()
            if not para_text:
                continue

            # Check for scene breaks (common markers)
            if para_text in ('#', '###', '***', '* * *', '---'):
                scene_break = ET.SubElement(text, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p')
                scene_break.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name', 'SceneBreak')
                scene_break.text = '#'
                first_para = True  # Next paragraph shouldn't be indented
                continue

            # Regular paragraph
            para = ET.SubElement(text, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p')
            if first_para:
                para.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name', 'FirstPara')
                first_para = False
            else:
                para.set('{urn:oasis:names:tc:opendocument:xmlns:text:1.0}style-name', 'P1')

            # Handle single newlines within paragraphs (join them)
            clean_text = ' '.join(para_text.split('\n'))
            add_text_with_emphasis(para, clean_text)

        # Add page break after chapter (except last)
        if (chapter_num, chapter_title, file_path) != chapters[-1]:
            # Page break is implicit with the next chapter's heading margin-top
            pass

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(doc, encoding='unicode')


def create_odt(chapters: list[tuple[int, str, Path]], output_path: Path,
               title: str = "Manuscript", heading_style: str = "num",
               author: str = "", short_title: str = "",
               author_name: str = "", author_address: str = "",
               title_page: bool = True, word_count: int = 0) -> None:
    """Create an ODT file from the chapters."""
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as odt:
        # mimetype must be first and uncompressed
        odt.writestr('mimetype', 'application/vnd.oasis.opendocument.text', compress_type=zipfile.ZIP_STORED)

        # Add manifest
        odt.writestr('META-INF/manifest.xml', create_manifest_xml())

        # Add styles
        odt.writestr('styles.xml', create_styles_xml(author, short_title))

        # Add content
        odt.writestr('content.xml', create_content_xml(
            chapters, title, heading_style,
            author_name=author_name, author_address=author_address,
            title_page=title_page, word_count=word_count))

    print(f"Created ODT file: {output_path}")


def count_words(chapters: list[tuple[int, str, Path]]) -> int:
    """Count total words across all chapter files."""
    total = 0
    for _, _, file_path in chapters:
        content = file_path.read_text(encoding='utf-8')
        # Simple word count: split on whitespace
        total += len(content.split())
    return total


def round_word_count(count: int) -> int:
    """Round word count to nearest hundred."""
    return round(count / 100) * 100


def bind_manuscript(base_path: Path, output_name: str = None, heading_style: str = "num",
                    author: str = "", short_title: str = "", title: str = "",
                    author_name: str = "", author_address: str = "",
                    title_page: bool = True) -> None:
    """Find chapter files and bind them into an ODT."""
    draft_dir = base_path / "draft"
    trash_dir = base_path / "trash"

    if not draft_dir.exists():
        print(f"Error: Draft directory not found: {draft_dir}")
        print("Run with --init to create the folder structure first.")
        return

    if not trash_dir.exists():
        trash_dir.mkdir(parents=True, exist_ok=True)

    chapters = find_chapter_files(draft_dir)

    if not chapters:
        print(f"No chapter files found in {draft_dir}")
        print("Expected format: 1_Chapter_Title.txt, 2_Another_Chapter.txt, etc.")
        return

    print(f"Found {len(chapters)} chapter(s):")
    for num, chapter_title, path in chapters:
        print(f"  {num}. {chapter_title} ({path.name})")

    # Calculate word count for title page
    word_count = count_words(chapters)
    rounded_count = round_word_count(word_count)
    print(f"Word count: {word_count:,} (about {rounded_count:,} words)")

    # Generate output filename
    if output_name:
        odt_name = output_name if output_name.endswith('.odt') else f"{output_name}.odt"
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        odt_name = f"manuscript_{timestamp}.odt"

    output_path = trash_dir / odt_name
    create_odt(chapters, output_path, heading_style=heading_style,
               author=author, short_title=short_title,
               title=title, author_name=author_name,
               author_address=author_address, title_page=title_page,
               word_count=rounded_count)


def main():
    parser = argparse.ArgumentParser(
        description="Bind chapter drafts into a manuscript ODT file with standard formatting.",
        epilog=f"Configuration can also be set in {CONFIG_FILENAME} (searched in cwd, project path, ~/.config/)"
    )
    parser.add_argument(
        '--init',
        action='store_true',
        help="Create the folder structure (draft/ and trash/ directories)"
    )
    parser.add_argument(
        '--bind',
        action='store_true',
        help="Bind chapter files from draft/ into an ODT in trash/"
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        help="Output filename for the ODT (default: manuscript_TIMESTAMP.odt)"
    )
    parser.add_argument(
        '--path', '-p',
        type=str,
        default='.',
        help="Base path for the project (default: current directory)"
    )
    parser.add_argument(
        '--heading',
        type=str,
        choices=['roman', 'title', 'num', 'chapter', 'nil'],
        default='num',
        help="Chapter heading style: roman (I, II, III), title (ALL CAPS title), num (1, 2, 3), chapter (Chapter 1), nil (no heading)"
    )
    parser.add_argument(
        '--author',
        type=str,
        default='',
        help="Author surname for header (e.g., 'Smith')"
    )
    parser.add_argument(
        '--short-title',
        type=str,
        default='',
        dest='short_title',
        help="Short title for header (e.g., 'My Novel')"
    )
    parser.add_argument(
        '--config', '-c',
        type=str,
        help=f"Path to config file (default: searches for {CONFIG_FILENAME})"
    )
    parser.add_argument(
        '--no-config',
        action='store_true',
        help="Ignore config file, use only command line arguments"
    )
    parser.add_argument(
        '--title',
        type=str,
        default='',
        help="Full manuscript title for title page"
    )
    parser.add_argument(
        '--author-name',
        type=str,
        default='',
        dest='author_name',
        help="Author's full name for title page"
    )
    parser.add_argument(
        '--author-address',
        type=str,
        default='',
        dest='author_address',
        help="Author's contact address for title page"
    )
    parser.add_argument(
        '--no-title-page',
        action='store_true',
        dest='no_title_page',
        help="Disable the title page"
    )

    args = parser.parse_args()

    # Convert no_title_page flag to title_page boolean
    args.title_page = not args.no_title_page

    # Load config file unless disabled
    if not args.no_config:
        if args.config:
            config_path = Path(args.config)
            if not config_path.exists():
                print(f"Error: Config file not found: {config_path}", file=sys.stderr)
                sys.exit(1)
        else:
            config_path = find_config_file(Path(args.path).resolve())

        if config_path:
            print(f"Using config: {config_path}")
            config = load_config(config_path)
            args = merge_config_with_args(config, args)

    base_path = Path(args.path).resolve()

    if not args.init and not args.bind:
        parser.print_help()
        return

    if args.init:
        create_folder_structure(base_path)

    if args.bind:
        bind_manuscript(base_path, args.output, args.heading,
                        args.author, args.short_title,
                        title=args.title, author_name=args.author_name,
                        author_address=args.author_address,
                        title_page=args.title_page)


if __name__ == '__main__':
    main()

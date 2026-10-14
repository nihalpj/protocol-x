"""Docstring mining for detailed tool/parameter descriptions (F-040)."""

from __future__ import annotations

from agents.codegen import parse_docstring, sanitize_description

NUMPY_DOC = '''Resize an image.

Longer discussion paragraph that must not leak into the summary.

Parameters
----------
path : str
    Path to the input image, relative to the workspace.
width : int
    Target width in pixels.
    Must be positive.
height : int, optional
    Target height in pixels.

Returns
-------
dict
    Output path.
'''

GOOGLE_DOC = '''Upload a file to the store.

Args:
    path (str): Path to the file, workspace-relative.
        Read at call time.
    overwrite (bool): Replace an existing object.

Returns:
    The object key.
'''

SPHINX_DOC = '''Parse a date string.

:param text: the date text to parse
:param fuzzy: allow fuzzy matching
'''

PLAIN_DOC = """Do a thing.

More detail here.
"""


def test_numpy_summary_and_params():
    summary, params = parse_docstring(NUMPY_DOC)
    assert summary == "Resize an image."
    assert params["path"] == "Path to the input image, relative to the workspace."
    assert params["width"] == "Target width in pixels. Must be positive."
    assert params["height"] == "Target height in pixels."
    assert "Returns" not in str(params)


def test_google_summary_and_params():
    summary, params = parse_docstring(GOOGLE_DOC)
    assert summary == "Upload a file to the store."
    assert params["path"] == "Path to the file, workspace-relative. Read at call time."
    assert params["overwrite"] == "Replace an existing object."


def test_sphinx_params():
    summary, params = parse_docstring(SPHINX_DOC)
    assert summary == "Parse a date string."
    assert params == {"text": "the date text to parse", "fuzzy": "allow fuzzy matching"}


def test_plain_docstring_summary_only():
    summary, params = parse_docstring(PLAIN_DOC)
    assert summary == "Do a thing."
    assert params == {}


def test_empty_docstring():
    assert parse_docstring("") == ("", {})


def test_summary_stops_at_parameters_section():
    summary, _ = parse_docstring("Do a thing.\n\nParameters\n----------\nx : int\n    count.")
    assert summary == "Do a thing."


def test_numpy_signature_line_is_dropped():
    doc = (
        "asarray(a, dtype=None, order=None)\n\nConvert the input to an array."
        "\n\nParameters\n----------\na : array_like\n    Input data."
    )
    summary, params = parse_docstring(doc)
    assert summary == "Convert the input to an array."
    assert params["a"] == "Input data."


def test_sanitize_collapses_and_strips():
    assert sanitize_description('a "quoted" {braced} \\ path\nnext') == "a quoted braced path next"

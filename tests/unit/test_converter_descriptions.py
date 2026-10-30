"""Converter LLM description enrichment (describer seam).

Fill-gaps policy: generic fallback descriptions ('<target>() from <package>',
'<name> argument') are replaced with LLM-written text; docstring-mined text
is authoritative and never overwritten. Enrichment must never fail a
conversion — a missing, broken, or malformed describer keeps existing text.
"""

from __future__ import annotations

import json

import pytest

from agents.converter_agent import (
    ARG_DESC_MAX,
    DESC_MAX,
    Arg,
    ConverterAgent,
    ToolProposal,
)


@pytest.fixture()
def workdir(tmp_path):
    return tmp_path / "convert"


def _proposal() -> ToolProposal:
    return ToolProposal(
        tool_name="fakepkg_read",
        target="read",
        description="read() from fakepkg",          # generic fallback
        args=[
            Arg(name="path", type="str", description="path argument"),   # generic
            Arg(name="strict", type="bool", description="enable strict mode",
                default="False"),                                      # mined
        ])


def test_enrichment_fills_generic_fallbacks(workdir):
    agent = ConverterAgent(
        "fakepkg",
        describer=lambda proposals: {
            "fakepkg_read": {
                "description": "Read a record from the store and return its payload.",
                "args": {"path": "Location of the record to read.",
                         "strict": "LLM must not overwrite mined text"},
            }},
        workdir=workdir)
    proposals = [_proposal()]
    agent._enrich_descriptions(proposals)
    p = proposals[0]
    assert p.description == "Read a record from the store and return its payload."
    assert p.args[0].description == "Location of the record to read."


def test_enrichment_never_overwrites_mined_text(workdir):
    agent = ConverterAgent(
        "fakepkg",
        describer=lambda proposals: {
            "fakepkg_read": {
                "description": "LLM tool text",
                "args": {"strict": "LLM arg text"},
            }},
        workdir=workdir)
    proposals = [_proposal()]
    proposals[0].description = "Read a record."      # docstring-mined, not generic
    agent._enrich_descriptions(proposals)
    p = proposals[0]
    assert p.description == "Read a record."
    assert p.args[1].description == "enable strict mode"


def test_enrichment_sanitizes_and_caps(workdir):
    agent = ConverterAgent(
        "fakepkg",
        describer=lambda proposals: {
            "fakepkg_read": {
                "description": 'breaks "the" {template} \\ literal ' + "x" * 600,
                "args": {"path": "y" * 300},
            }},
        workdir=workdir)
    proposals = [_proposal()]
    agent._enrich_descriptions(proposals)
    p = proposals[0]
    assert len(p.description) <= DESC_MAX
    assert len(p.args[0].description) <= ARG_DESC_MAX
    for bad in ('"', "{", "}", "\\"):
        assert bad not in p.description


def test_missing_or_broken_describer_keeps_existing_text(workdir):
    proposals = [_proposal()]
    ConverterAgent("fakepkg", workdir=workdir)._enrich_descriptions(proposals)
    assert proposals[0].description == "read() from fakepkg"

    def boom(_):
        raise RuntimeError("token factory down")

    ConverterAgent("fakepkg", describer=boom,
                   workdir=workdir)._enrich_descriptions(proposals)
    assert proposals[0].description == "read() from fakepkg"

    ConverterAgent("fakepkg", describer=lambda _: ["not", "a", "dict"],
                   workdir=workdir)._enrich_descriptions(proposals)
    assert proposals[0].description == "read() from fakepkg"


def test_run_enriches_and_persists_proposals(workdir):
    """End-to-end: run() invokes the describer and the enriched text lands in
    proposals.json and the generated adapter source."""
    generic = ToolProposal(
        tool_name="textwrap_indent",
        target="indent",
        description="indent() from textwrap",        # generic fallback
        args=[Arg(name="text", type="str", description="text argument")])
    agent = ConverterAgent(
        "textwrap",
        judge=lambda path: "pass",
        merger=lambda p: "merged (test)",
        proposer=lambda mod: [generic],
        describer=lambda proposals: {
            "textwrap_indent": {
                "description": "Add a prefix to the beginning of selected lines.",
                "args": {"text": "The text to indent."},
            }},
        workdir=workdir)
    result = agent.run()
    assert result.verdict == "pass"
    saved = json.loads((workdir / "proposals.json").read_text())
    assert saved[0]["description"] == "Add a prefix to the beginning of selected lines."
    assert saved[0]["args"][0]["description"] == "The text to indent."
    assert "Add a prefix" in result.adapter_path.read_text()

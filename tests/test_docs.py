"""BUILD.md phase 8 check: the agent file and USAGE.md exist and match the spec.

The live Claude-session half of the check (ask a question, watch the main agent call `handoff` and
spawn repo-live-search, confirm the answer cites file:line and commit) cannot run from a test; it
needs a live Claude Code session with the MCP server registered.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent
AGENT_FILE = ROOT / ".claude/agents/repo-live-search.md"


def test_agent_file_has_the_frontmatter_and_four_part_answer_shape():
    text = AGENT_FILE.read_text()
    assert text.startswith("---\nname: repo-live-search\n")
    assert "tools: Read, Grep, Glob, Bash" in text
    for heading in ("**Answer:**", "**Evidence:**", "**Searched but not found:**", "**Confidence limits:**"):
        assert heading in text


def test_usage_md_states_every_rule_from_build_md_section_9():
    text = (ROOT / "USAGE.md").read_text()
    for rule in ("Start with the index tools", "provenance", "handoff", "repo-live-search",
                "search_aid` text as fact", "unclassified`, `unknown`, `unresolved` and `UNKNOWN`"):
        assert rule in text

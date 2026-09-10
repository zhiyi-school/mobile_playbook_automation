from __future__ import annotations

import pytest

from mobile_playbook.playbook import markdown


def code_blocks(text: str) -> list[dict]:
    return [block for block in markdown.parse_blocks(text) if block["type"] == "code"]


def only_code(text: str) -> dict:
    blocks = code_blocks(text)
    assert len(blocks) == 1, blocks
    return blocks[0]


class TestFenceOpening:
    @pytest.mark.parametrize("marker", ["```", "~~~"])
    def test_either_marker_opens_a_block(self, marker: str) -> None:
        assert only_code(f"{marker}\nexample\n{marker}\n")["text"] == "example"

    @pytest.mark.parametrize("info", ["swift", "   swift   "])
    def test_the_info_string_names_the_language(self, info: str) -> None:
        assert only_code(f"```{info}\nlet a = 1\n```\n")["language"] == "swift"

    def test_a_fence_with_no_info_string_has_no_language(self) -> None:
        assert only_code("```\nexample\n```\n")["language"] is None

    def test_a_longer_marker_still_opens_a_block(self) -> None:
        assert only_code("`````swift\nlet a = 1\n`````\n")["language"] == "swift"


class TestFenceClosing:
    def test_a_shorter_fence_inside_a_longer_one_is_content(self) -> None:
        block = only_code("````\n```\ninner\n```\nouter\n````\n")
        assert block["text"] == "```\ninner\n```\nouter"

    def test_a_longer_closing_fence_still_closes(self) -> None:
        assert only_code("```\nexample\n`````\n")["text"] == "example"

    def test_the_other_marker_cannot_close_a_block(self) -> None:
        assert only_code("```\nkeep\n~~~\nmore\n```\n")["text"] == "keep\n~~~\nmore"

    def test_a_closing_fence_may_not_carry_an_info_string(self) -> None:
        assert only_code("```\nkeep\n```swift\nmore\n```\n")["text"] == "keep\n```swift\nmore"

    def test_an_unclosed_fence_runs_to_the_end_of_the_document(self) -> None:
        assert only_code("```swift\nlet a = 1\nlet b = 2\n")["text"] == "let a = 1\nlet b = 2"

    def test_backticks_inside_a_tilde_block_are_content(self) -> None:
        assert only_code("~~~\ncontains ``` backticks\n~~~\n")["text"] == "contains ``` backticks"


class TestFenceContent:
    def test_blank_lines_at_the_boundaries_are_kept(self) -> None:
        assert only_code("```swift\n\nlet a = 1\n\n```\n")["text"] == "\nlet a = 1\n"

    def test_internal_blank_lines_are_kept_exactly(self) -> None:
        assert only_code("```\nline1\n\n\nline2\n```\n")["text"] == "line1\n\n\nline2"

    def test_an_empty_block_stays_empty(self) -> None:
        assert only_code("```\n```\n")["text"] == ""

    def test_one_blank_content_line_is_the_empty_string(self) -> None:
        # `text` is the content lines joined by "\n" with no trailing newline, so a
        # single blank line and an empty block share the same representation.
        assert only_code("```\n\n```\n")["text"] == ""

    def test_indentation_inside_the_block_is_preserved(self) -> None:
        body = "def example():\n    if True:\n        return 1"
        assert only_code(f"```python\n{body}\n```\n")["text"] == body

    def test_tabs_quotes_backslashes_and_unicode_stay_literal(self) -> None:
        body = 'printf "a\tb" \\\n  --flag=\'x\' # café — ✅'
        assert only_code(f"```bash\n{body}\n```\n")["text"] == body

    def test_carriage_returns_are_normalised_but_nothing_else_is(self) -> None:
        assert only_code("```\r\na\r\n\r\nb\r\n```\r\n")["text"] == "a\n\nb"

    def test_the_fence_delimiters_are_not_part_of_the_content(self) -> None:
        assert "```" not in only_code("```swift\nlet a = 1\n```\n")["text"]


class TestFenceIndentation:
    def test_an_indented_fence_strips_that_much_from_its_content(self) -> None:
        assert only_code("   ```\n   indented\n   ```\n")["text"] == "indented"

    def test_content_indented_past_the_fence_keeps_the_difference(self) -> None:
        assert only_code("  ```\n      deep\n  ```\n")["text"] == "    deep"

    def test_content_indented_less_than_the_fence_is_not_over_stripped(self) -> None:
        assert only_code("    ```\n  short\n    ```\n")["text"] == "short"

    def test_an_indented_closing_fence_still_closes(self) -> None:
        assert only_code("```\nexample\n   ```\n")["text"] == "example"


class TestFenceInContext:
    def test_a_block_between_paragraphs_keeps_all_three(self) -> None:
        blocks = markdown.parse_blocks("Before.\n\n```\nexample\n```\n\nAfter.\n")
        assert [block["type"] for block in blocks] == ["paragraph", "code", "paragraph"]
        assert blocks[1]["text"] == "example"

    def test_two_blocks_stay_separate(self) -> None:
        blocks = code_blocks("```swift\nlet a = 1\n```\n\n```bash\necho hi\n```\n")
        assert [block["language"] for block in blocks] == ["swift", "bash"]
        assert [block["text"] for block in blocks] == ["let a = 1", "echo hi"]

    def test_a_heading_after_a_block_is_still_a_heading(self) -> None:
        blocks = markdown.parse_blocks("```\nexample\n```\n\n## Next\n")
        assert blocks[-1]["type"] == "heading"

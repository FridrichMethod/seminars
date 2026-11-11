"""Tests for the robots.txt parser and matcher."""

from __future__ import annotations

import pytest

from seminars.robots import ALLOW_ALL, DISALLOW_ALL, parse_robots

BOT = "seminars-bot"
BOM = chr(0xFEFF)


def allows(robots_txt: str, path: str, agent: str = BOT) -> bool:
    return parse_robots(robots_txt, agent).allows(path)


class TestMatching:
    def test_empty_file_allows_everything(self) -> None:
        policy = parse_robots("", BOT)
        assert policy == ALLOW_ALL
        assert policy.allows("/anything?x=1")
        assert policy.crawl_delay is None

    def test_disallow_prefix(self) -> None:
        text = "User-agent: *\nDisallow: /private/\n"
        assert not allows(text, "/private/page")
        assert not allows(text, "/private/")
        assert allows(text, "/private")
        assert allows(text, "/public/private/")

    def test_disallow_everything(self) -> None:
        assert not allows("User-agent: *\nDisallow: /\n", "/events")
        assert not DISALLOW_ALL.allows("/")

    def test_empty_disallow_allows_everything(self) -> None:
        assert allows("User-agent: *\nDisallow:\n", "/events")

    def test_longest_match_wins_regardless_of_order(self) -> None:
        text = "User-agent: *\nDisallow: /\nAllow: /api/\n"
        assert allows(text, "/api/2/events")
        assert not allows(text, "/admin")
        reordered = "User-agent: *\nAllow: /api/\nDisallow: /\n"
        assert allows(reordered, "/api/2/events")

    def test_allow_wins_a_tie(self) -> None:
        assert allows("User-agent: *\nDisallow: /page\nAllow: /page\n", "/page")

    def test_wildcard_and_end_anchor(self) -> None:
        text = "User-agent: *\nDisallow: /*?page=\nDisallow: /*.pdf$\nAllow: /core/*.css$\n"
        assert not allows(text, "/events?page=2")
        assert allows(text, "/events")
        assert not allows(text, "/files/talk.pdf")
        assert allows(text, "/files/talk.pdf.html")
        assert allows(text, "/core/a/b.css")

    def test_query_string_is_part_of_the_path(self) -> None:
        text = "User-agent: *\nDisallow: /search?\n"
        assert not allows(text, "/search?q=seminar")
        assert allows(text, "/search")

    def test_percent_encoding_is_normalized(self) -> None:
        text = "User-agent: *\nDisallow: /a%20b/\n"
        assert not allows(text, "/a%20b/c")
        assert not allows(text, "/a b/c")

    def test_robots_txt_itself_is_always_allowed(self) -> None:
        assert allows("User-agent: *\nDisallow: /\n", "/robots.txt")

    def test_empty_path_means_root(self) -> None:
        assert not allows("User-agent: *\nDisallow: /\n", "")


class TestGroups:
    def test_specific_group_replaces_the_wildcard_group(self) -> None:
        text = "User-agent: *\nDisallow: /\n\nUser-agent: seminars-bot\nDisallow: /private/\n"
        assert allows(text, "/events")
        assert not allows(text, "/private/x")
        assert not allows(text, "/events", agent="otherbot")

    def test_agent_names_are_case_insensitive(self) -> None:
        assert not allows("User-Agent: Seminars-Bot\nDisallow: /x\n", "/x")

    def test_several_agents_share_one_group(self) -> None:
        text = "User-agent: a\nUser-agent: seminars-bot\nDisallow: /x\n"
        assert not allows(text, "/x")
        assert not allows(text, "/x", agent="a")
        assert allows(text, "/x", agent="b")

    def test_groups_for_the_same_agent_are_merged(self) -> None:
        text = "User-agent: *\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n"
        assert not allows(text, "/a")
        assert not allows(text, "/b")

    def test_no_matching_group_allows_everything(self) -> None:
        assert allows("User-agent: otherbot\nDisallow: /\n", "/events")

    def test_rules_before_any_group_are_ignored(self) -> None:
        assert allows("Disallow: /\n", "/events")

    def test_comments_blank_lines_and_unknown_keys(self) -> None:
        text = (
            "# robots\n\n"
            "Sitemap: https://example.edu/sitemap.xml\n"
            "User-agent: *   # everyone\n"
            "Host: example.edu\n"
            "Disallow: /private/  # secret\n"
            "nonsense line\n"
        )
        assert not allows(text, "/private/x")
        assert allows(text, "/public")

    def test_crlf_line_endings(self) -> None:
        assert not allows("User-agent: *\r\nDisallow: /x\r\n", "/x")

    def test_byte_order_mark_does_not_hide_the_first_group(self) -> None:
        text = BOM + "User-agent: *\nCrawl-delay: 30\nDisallow: /private/\n"
        policy = parse_robots(text, BOT)
        assert policy.crawl_delay == 30.0
        assert not policy.allows("/private/x")
        assert policy.allows("/public")

    def test_byte_order_mark_before_a_comment_or_blank_line(self) -> None:
        assert not allows(BOM + "# robots\nUser-agent: *\nDisallow: /x\n", "/x")
        assert not allows(BOM + "\nUser-agent: *\nDisallow: /x\n", "/x")


class TestCrawlDelay:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("Crawl-delay: 30", 30.0),
            ("Crawl-Delay: 1", 1.0),
            ("crawl-delay: 0.5", 0.5),
            ("Crawl-delay: 0", 0.0),
            ("Crawl-delay: soon", None),
            ("Crawl-delay: -5", None),
            ("Crawl-delay: inf", None),
        ],
    )
    def test_values(self, line: str, expected: float | None) -> None:
        assert parse_robots(f"User-agent: *\n{line}\n", BOT).crawl_delay == expected

    def test_own_group_delay_is_preferred(self) -> None:
        text = (
            "User-agent: *\nCrawl-delay: 30\n\n"
            "User-agent: seminars-bot\nCrawl-delay: 5\nDisallow: /x\n"
        )
        assert parse_robots(text, BOT).crawl_delay == 5.0

    def test_wildcard_delay_is_the_fallback(self) -> None:
        text = "User-agent: *\nCrawl-delay: 30\n\nUser-agent: seminars-bot\nDisallow: /x\n"
        assert parse_robots(text, BOT).crawl_delay == 30.0

    def test_stanford_style_file(self) -> None:
        text = "User-agent: *\nCrawl-Delay: 1\nDisallow: /admin/\nDisallow: /search\n"
        policy = parse_robots(text, BOT)
        assert policy.crawl_delay == 1.0
        assert policy.allows("/api/2/events?pp=100&page=1")
        assert not policy.allows("/admin/users")

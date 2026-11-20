"""Regression coverage for selecting a Threads root independently of arrival order."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from threadstudio.core.models import CommentMode
from threadstudio.scrapers.threads_scraper import (
    _collect_post_objects,
    _extract_post_videos,
    _find_root_post,
    _select_comments,
    scrape_threads_url,
)


POST_URL = "https://www.threads.com/@hoang0843263222/post/Df2QvxtnOeo"
ROOT_PK = "4032484168295049128"
ROOT_CODE = "Df2QvxtnOeo"
AUTHOR = "hoang0843263222"


def make_post(pk: str, code: str, username: str, text: str, likes: int = 0) -> dict:
    return {
        "pk": pk,
        "code": code,
        "user": {"username": username, "profile_pic_url": ""},
        "caption": {"text": text},
        "like_count": likes,
        "text_post_app_info": {"direct_reply_count": 0},
    }


class RootPostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.comment = make_post("4032854755344936841", "Df3lAhzFG-J", "nguyen5026tan", "Comment loaded first", 4)
        self.author_reply = make_post("4032724263056150065", "Df3HVnYj44x", AUTHOR, "Author reply", 2)
        self.root = make_post(ROOT_PK, ROOT_CODE, AUTHOR, "Original post", 15411)
        self.payloads = [{"thread_items": [{"post": post} for post in (self.comment, self.author_reply, self.root)]}]

    def test_root_matches_url_when_comment_and_author_reply_arrive_first(self) -> None:
        posts = _collect_post_objects(self.payloads)
        self.assertEqual(posts[0]["username"], "nguyen5026tan")
        root = _find_root_post(posts, POST_URL)
        self.assertIs(root, posts[2])
        self.assertEqual(root["username"], AUTHOR)
        self.assertEqual(root["text"], "Original post")

    def test_root_matches_numeric_id_without_shortcode(self) -> None:
        self.root.pop("code")
        self.root["id"] = self.root.pop("pk") + "_75664418127"
        posts = _collect_post_objects(self.payloads)
        self.assertEqual(posts[2]["pk"], ROOT_PK)
        self.assertIs(_find_root_post(posts, POST_URL), posts[2])

    def test_root_supports_old_domain_query_and_trailing_slash(self) -> None:
        posts = _collect_post_objects(self.payloads)
        url = POST_URL.replace("threads.com", "threads.net") + "/?xmt=AQtest#replies"
        self.assertIs(_find_root_post(posts, url), posts[2])

    def test_missing_root_does_not_use_another_post_by_same_author(self) -> None:
        posts = _collect_post_objects([{"post": self.author_reply}])
        self.assertIsNone(_find_root_post(posts, POST_URL))
        self.assertIsNone(_find_root_post(posts, "https://www.threads.com/@" + AUTHOR))

    def test_duplicate_payloads_do_not_repeat_root(self) -> None:
        posts = _collect_post_objects(self.payloads * 2)
        self.assertEqual(len(posts), 3)
        self.assertEqual(sum(post["code"] == ROOT_CODE for post in posts), 1)

    def test_numeric_and_composite_ids_do_not_duplicate_root(self) -> None:
        duplicate = dict(self.root)
        duplicate["id"] = duplicate.pop("pk") + "_75664418127"
        posts = _collect_post_objects([*self.payloads, {"post": duplicate}])
        self.assertEqual(len(posts), 3)
        self.assertEqual(sum(post["code"] == ROOT_CODE for post in posts), 1)

    def test_shortcode_keeps_distinct_posts_with_same_author_and_caption(self) -> None:
        other = dict(self.root, code="OtherPost")
        other.pop("pk")
        self.root.pop("pk")
        posts = _collect_post_objects([{"posts": [other, self.root]}])
        self.assertEqual(len(posts), 2)
        self.assertIs(_find_root_post(posts, POST_URL), posts[1])

    def test_author_filters_use_actual_root_author(self) -> None:
        posts = _collect_post_objects(self.payloads)
        root = _find_root_post(posts, POST_URL)
        replies = [post for post in posts if post is not root]
        author_only = _select_comments(root, replies, comment_mode=CommentMode.AUTHOR_ONLY.value, max_comments=6)
        others_only = _select_comments(root, replies, comment_mode=CommentMode.TOP_LIKES_NO_AUTHOR.value, max_comments=6)
        self.assertEqual([post["text"] for post in author_only], ["Author reply"])
        self.assertEqual([post["username"] for post in others_only], ["nguyen5026tan"])

    def test_video_versions_are_collected_from_post_and_carousel(self) -> None:
        post = make_post("video-1", "VideoPost", AUTHOR, "Video post")
        post["carousel_media"] = [
            {"video_versions": [{"url": "https://cdn.example/first.mp4"}]},
            {"video_versions": [{"url": "https://cdn.example/second.mp4"}]},
        ]
        self.assertEqual(
            _extract_post_videos(post),
            ["https://cdn.example/first.mp4", "https://cdn.example/second.mp4"],
        )

    def test_video_only_post_is_not_discarded(self) -> None:
        post = make_post("video-only", "VideoOnly", AUTHOR, "")
        post["video_versions"] = [{"url": "https://cdn.example/video.mp4"}]
        posts = _collect_post_objects([{"post": post}])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["video_urls"], ["https://cdn.example/video.mp4"])

    def scrape_with_payloads(self, payloads: list, dom_posts: list | None = None) -> tuple:
        playwright = MagicMock()
        browser = playwright.chromium.launch.return_value
        context = browser.new_context.return_value
        context.pages = []
        page = context.new_page.return_value
        page.url = POST_URL
        page.evaluate.return_value = []

        def emit_responses(*args, **kwargs) -> None:
            callback = page.on.call_args.args[1]
            for payload in payloads:
                response = MagicMock()
                response.url = "https://www.threads.com/api/graphql"
                response.json.return_value = payload
                callback(response)

        page.goto.side_effect = emit_responses
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("playwright.sync_api.sync_playwright") as sync_playwright,
                patch("threadstudio.scrapers.threads_scraper._extract_dom_fallback", return_value=dom_posts or []),
                patch("threadstudio.scrapers.threads_scraper._download_asset", return_value=""),
            ):
                sync_playwright.return_value.__enter__.return_value = playwright
                return scrape_threads_url(
                    POST_URL,
                    profile_dir=Path(tmp) / "profile",
                    media_cache_dir=Path(tmp) / "media-cache",
                )

    def test_scrape_builds_root_segment_and_keeps_first_comment(self) -> None:
        slug, segments = self.scrape_with_payloads(self.payloads)
        self.assertEqual(slug, "original_post")
        self.assertTrue(segments[0].is_root_post)
        self.assertEqual(segments[0].seg_id, "root")
        self.assertEqual(segments[0].username, AUTHOR)
        self.assertEqual(segments[0].raw_text, "Original post")
        self.assertEqual([segment.username for segment in segments[1:]], ["nguyen5026tan", AUTHOR])
        self.assertTrue(all(not segment.is_root_post for segment in segments[1:]))

    def test_scrape_uses_identified_dom_root_when_network_root_is_missing(self) -> None:
        dom_root = _collect_post_objects([{"post": self.root}])
        _, segments = self.scrape_with_payloads([{"post": self.comment}], dom_root)
        self.assertEqual(segments[0].username, AUTHOR)
        self.assertEqual(segments[0].raw_text, "Original post")
        self.assertEqual(segments[1].username, "nguyen5026tan")

    def test_scrape_fails_instead_of_silently_choosing_wrong_author(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Threads"):
            self.scrape_with_payloads([{"post": self.comment}])


if __name__ == "__main__":
    unittest.main()

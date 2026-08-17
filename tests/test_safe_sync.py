import os
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from weread2notion import cli


class SafeSyncTests(unittest.TestCase):
    def setUp(self):
        self.previous_client = cli.client
        self.client = Mock()
        cli.client = self.client
        self.addCleanup(self.restore_client)

    def restore_client(self):
        cli.client = self.previous_client

    def test_find_sync_container_returns_inline_toggle(self):
        self.client.blocks.children.list.return_value = {
            "results": [
                {
                    "id": "user-content",
                    "type": "paragraph",
                    "paragraph": {"rich_text": []},
                },
                {
                    "id": "managed-content",
                    "type": "toggle",
                    "toggle": {
                        "rich_text": [{"plain_text": cli.SYNC_PAGE_TITLE}]
                    },
                },
            ],
            "has_more": False,
        }

        self.assertEqual(cli.find_sync_container("book-page"), "managed-content")

    def test_clear_sync_container_preserves_user_authored_blocks(self):
        self.client.blocks.children.list.return_value = {
            "results": [
                {
                    "id": "manual-block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": "我的补充"}]},
                },
                {
                    "id": "generated-block",
                    "type": "callout",
                    "callout": {
                        "rich_text": [
                            {"plain_text": "微信读书划线"},
                            {"plain_text": cli.SYNC_BLOCK_MARKER},
                        ]
                    },
                },
            ],
            "has_more": False,
        }

        cli.clear_sync_container("managed-content")

        self.client.blocks.delete.assert_called_once_with(block_id="generated-block")

    def test_create_sync_container_adds_inline_toggle(self):
        self.client.blocks.children.append.return_value = {
            "results": [{"id": "managed-content"}]
        }

        container_id = cli.create_sync_container("book-page")

        self.assertEqual(container_id, "managed-content")
        self.client.blocks.children.append.assert_called_once_with(
            block_id="book-page",
            children=[
                {
                    "type": "toggle",
                    "toggle": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": cli.SYNC_PAGE_TITLE},
                            }
                        ],
                        "color": "default",
                    },
                }
            ],
        )

    def test_generated_children_include_an_invisible_sync_marker(self):
        children, _ = cli.get_children(
            chapter=None,
            summary=[],
            bookmark_list=[{"markText": "微信读书划线"}],
        )

        rich_text = children[0]["callout"]["rich_text"]
        self.assertEqual(rich_text[-1]["text"]["content"], cli.SYNC_BLOCK_MARKER)

    def test_shelf_entries_include_books_albums_and_article_collection(self):
        entries = cli.get_shelf_entries(
            {
                "books": [
                    {
                        "bookId": "book-1",
                        "title": "电子书",
                        "author": "作者",
                        "category": "文学",
                    }
                ],
                "albums": [
                    {
                        "albumInfo": {
                            "albumId": "album-1",
                            "name": "有声书",
                            "authorName": "演播者",
                        },
                        "albumInfoExtra": {},
                    }
                ],
                "mp": {"title": "文章收藏"},
            }
        )

        self.assertEqual([entry["book_id"] for entry in entries], ["book-1", "album:album-1", "mp:articles"])
        self.assertEqual(entries[0]["categories"], ["文学"])

    def test_daily_read_seconds_uses_shanghai_calendar_date(self):
        timestamp = int(datetime(2026, 8, 17, 8, 0, tzinfo=cli.SHANGHAI_TZ).timestamp())

        seconds = cli.get_daily_read_seconds(
            {"dailyReadTimes": {str(timestamp): 3661}},
            datetime(2026, 8, 17, tzinfo=cli.SHANGHAI_TZ).date(),
        )

        self.assertEqual(seconds, 3661)

    @patch("weread2notion.cli.get_icon", return_value={"type": "external"})
    @patch("weread2notion.cli.build_book_properties", return_value={"BookId": {}})
    def test_existing_book_page_is_updated_not_recreated(self, *_):
        page_id = cli.insert_to_notion(
            "测试书",
            "book-id",
            "https://example.com/cover.jpg",
            1,
            "作者",
            "",
            None,
            [],
            existing_page={"id": "existing-page"},
        )

        self.assertEqual(page_id, "existing-page")
        self.client.pages.update.assert_called_once()
        self.client.pages.create.assert_not_called()

    def test_safe_mode_is_the_default(self):
        original_mode = os.environ.pop("NOTION_SYNC_MODE", None)
        try:
            self.assertEqual(cli.get_sync_mode(), cli.SYNC_MODE_SAFE)
        finally:
            if original_mode is not None:
                os.environ["NOTION_SYNC_MODE"] = original_mode


if __name__ == "__main__":
    unittest.main()

import os
import unittest
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

    def test_find_sync_page_returns_managed_child_page(self):
        self.client.blocks.children.list.return_value = {
            "results": [
                {
                    "id": "user-content",
                    "type": "paragraph",
                    "paragraph": {"rich_text": []},
                },
                {
                    "id": "managed-content",
                    "type": "child_page",
                    "child_page": {"title": cli.SYNC_PAGE_TITLE},
                },
            ],
            "has_more": False,
        }

        self.assertEqual(cli.find_sync_page("book-page"), "managed-content")

    def test_clear_sync_page_only_archives_its_direct_children(self):
        self.client.blocks.children.list.return_value = {
            "results": [{"id": "block-1"}, {"id": "block-2"}],
            "has_more": False,
        }

        cli.clear_sync_page("managed-content")

        self.client.blocks.delete.assert_any_call(block_id="block-1")
        self.client.blocks.delete.assert_any_call(block_id="block-2")
        self.assertEqual(self.client.blocks.delete.call_count, 2)

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

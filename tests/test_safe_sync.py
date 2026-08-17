import os
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from weread2notion import cli


class SafeSyncTests(unittest.TestCase):
    def setUp(self):
        self.previous_client = cli.client
        self.previous_template_sources = cli.template_sources
        self.previous_template_relation_cache = cli.template_relation_cache
        self.previous_template_lookup_cache = cli.template_lookup_cache
        self.previous_template_period_cache = cli.template_period_cache
        self.client = Mock()
        cli.client = self.client
        self.addCleanup(self.restore_client)

    def restore_client(self):
        cli.client = self.previous_client
        cli.template_sources = self.previous_template_sources
        cli.template_relation_cache = self.previous_template_relation_cache
        cli.template_lookup_cache = self.previous_template_lookup_cache
        cli.template_period_cache = self.previous_template_period_cache

    def configure_template_sources(self):
        sources = {}
        for name, title_name, property_types in (
            ("书架", "书名", {"书名": "title", "BookId": "rich_text", "作者": "relation", "分类": "relation", "链接": "url", "封面": "files", "阅读状态": "status", "阅读进度": "number", "阅读时长": "number", "最后阅读时间": "date", "日": "relation", "周": "relation", "月": "relation", "年": "relation"}),
            ("作者", "标题", {"标题": "title"}),
            ("分类", "标题", {"标题": "title"}),
            ("日", "标题", {"标题": "title", "日期": "date", "周": "relation", "月": "relation", "年": "relation"}),
            ("周", "标题", {"标题": "title", "日期": "date", "每日阅读统计": "relation"}),
            ("月", "标题", {"标题": "title", "日期": "date", "每日阅读统计": "relation"}),
            ("年", "标题", {"标题": "title", "日期": "date", "每日阅读统计": "relation"}),
        ):
            sources[name] = {
                "name": name,
                "data_source_id": f"source-{name}",
                "property_types": property_types,
                "title_name": title_name,
            }
        cli.template_sources = sources
        cli.template_relation_cache = {}
        cli.template_lookup_cache = {}
        cli.template_period_cache = {}

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

    def test_template_properties_write_relations_and_external_cover(self):
        self.configure_template_sources()

        properties = cli.build_template_properties(
            "书架",
            {
                "书名": "测试书",
                "作者": ["author-page"],
                "分类": ["category-page"],
                "封面": "https://example.com/cover.jpg",
                "阅读状态": "在读",
                "阅读进度": 0.25,
            },
        )

        self.assertEqual(properties["书名"]["title"][0]["text"]["content"], "测试书")
        self.assertEqual(properties["作者"], {"relation": [{"id": "author-page"}]})
        self.assertEqual(properties["分类"], {"relation": [{"id": "category-page"}]})
        self.assertEqual(properties["封面"]["files"][0]["external"]["url"], "https://example.com/cover.jpg")
        self.assertEqual(properties["阅读状态"], {"status": {"name": "在读"}})

    def test_template_properties_skip_empty_url_values(self):
        self.configure_template_sources()

        properties = cli.build_template_properties("书架", {"链接": ""})

        self.assertNotIn("链接", properties)

    def test_existing_template_book_page_receives_cover_and_icon(self):
        self.configure_template_sources()
        with patch(
            "weread2notion.cli.find_template_page",
            return_value={"id": "existing-book-page"},
        ):
            page_id, created = cli.upsert_template_page(
                "书架",
                "BookId",
                "book-1",
                {"书名": "测试书", "BookId": "book-1"},
                icon_url="https://example.com/cover.jpg",
                cover_url="https://example.com/cover.jpg",
            )

        self.assertEqual((page_id, created), ("existing-book-page", False))
        self.client.pages.update.assert_called_once_with(
            page_id="existing-book-page",
            properties={
                "书名": {"title": [{"type": "text", "text": {"content": "测试书"}}]},
                "BookId": {"rich_text": [{"type": "text", "text": {"content": "book-1"}}]},
            },
            icon={"type": "external", "external": {"url": "https://example.com/cover.jpg"}},
            cover={"type": "external", "external": {"url": "https://example.com/cover.jpg"}},
        )

    @patch("weread2notion.cli.upsert_template_page")
    @patch("weread2notion.cli.get_or_create_template_page")
    def test_template_periods_use_chinese_calendar_titles(self, get_or_create, upsert):
        self.configure_template_sources()
        get_or_create.side_effect = [("week", True), ("month", True), ("year", True)]
        upsert.return_value = ("day", True)

        periods = cli.ensure_template_periods(1786939200)

        self.assertEqual(periods, {"日": "day", "周": "week", "月": "month", "年": "year"})
        self.assertEqual(get_or_create.call_args_list[0].args[2], "2026年第34周")
        self.assertEqual(get_or_create.call_args_list[1].args[2], "2026年8月")
        self.assertEqual(get_or_create.call_args_list[2].args[2], "2026")
        self.assertEqual(upsert.call_args.args[2], "2026年08月17日")

    def test_template_daily_stats_normalize_timestamp_and_duration(self):
        entries = cli.get_reading_day_entries(
            {"dailyReadTimes": {"1786939200": 3661}}
        )

        self.assertEqual(
            entries,
            {"2026-08-17": {"timestamp": 1786939200, "seconds": 3661}},
        )

    def test_date_with_explicit_offset_does_not_repeat_timezone(self):
        self.assertEqual(
            cli.get_date("2026-08-17T20:00:00+08:00"),
            {"date": {"start": "2026-08-17T20:00:00+08:00"}},
        )
        self.assertEqual(
            cli.get_date("2026-08-17T20:00:00"),
            {
                "date": {
                    "start": "2026-08-17T20:00:00",
                    "time_zone": "Asia/Shanghai",
                }
            },
        )

    @patch("weread2notion.cli.load_template_source")
    @patch("weread2notion.cli.list_descendant_blocks")
    def test_template_discovery_requires_the_complete_layout(self, blocks, load_source):
        blocks.return_value = [
            {"id": f"database-{name}", "type": "child_database", "child_database": {"title": name}}
            for name in cli.TEMPLATE_REQUIRED_SOURCE_NAMES
        ]
        load_source.side_effect = lambda database_id, name: {
            "name": name,
            "database_id": database_id,
            "data_source_id": f"source-{name}",
            "property_types": {"标题": "title"},
            "title_name": "标题",
        }

        sources = cli.discover_template_sources("template-page")

        self.assertEqual(set(sources), cli.TEMPLATE_REQUIRED_SOURCE_NAMES)
        self.assertEqual(load_source.call_count, len(cli.TEMPLATE_REQUIRED_SOURCE_NAMES))


if __name__ == "__main__":
    unittest.main()

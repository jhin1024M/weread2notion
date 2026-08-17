import argparse
import logging
import os
import re
import time
from notion_client import Client
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import hashlib
from dotenv import load_dotenv
from notion_client.errors import APIResponseError
from retrying import retry
from .blocks import (
    get_callout,
    get_date,
    get_file,
    get_heading,
    get_icon,
    get_multi_select,
    get_number,
    get_quote,
    get_rich_text,
    get_select,
    get_status,
    get_toggle,
    get_title,
    get_url,
)

client = None
data_source_id = None
data_source_property_types = {}
title_property_name = None
skipped_property_names = set()
weread = None
template_sources = {}
template_relation_cache = {}
template_lookup_cache = {}
template_period_cache = {}

load_dotenv()
WEREAD_URL = "https://weread.qq.com/"
WEREAD_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
WEREAD_SKILL_VERSION = "1.0.4"
NOTION_VERSION = "2026-03-11"
BOOKMARK_CALLOUT_ICON = "〰️"
NOTE_CALLOUT_ICON = "✍️"
SYNC_PAGE_TITLE = "微信读书同步内容"
SYNC_MODE_SAFE = "safe"
SYNC_MODE_REPLACE = "replace"
SYNC_BLOCK_MARKER = "\u2063\u2064\u2062"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
READING_DASHBOARD_TITLE = "阅读看板"
READING_DAILY_ROOT_TITLE = "阅读日报"
TEMPLATE_SOURCE_NAMES = (
    "书架",
    "笔记",
    "划线",
    "章节",
    "阅读记录",
    "日",
    "周",
    "月",
    "年",
    "分类",
    "作者",
)
TEMPLATE_REQUIRED_SOURCE_NAMES = {
    "书架",
    "笔记",
    "划线",
    "章节",
    "日",
    "周",
    "月",
    "年",
    "分类",
    "作者",
}
NOTION_TOKEN_PATTERN = re.compile(r"^(secret|ntn)_[A-Za-z0-9_-]{20,}$")
WEREAD_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{10,}$")
NOTION_ID_PATTERN = re.compile(
    r"^[a-f0-9]{32}$|^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
NOTION_ID_IN_TEXT_PATTERN = re.compile(
    r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    re.IGNORECASE,
)


class ConfigError(Exception):
    pass


def emit_error(message):
    if os.getenv("GITHUB_ACTIONS") == "true":
        safe = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{safe}")
    else:
        print(f"配置错误: {message}")


def fail_config(message):
    emit_error(message)
    raise ConfigError(message)


def clean_secret_value(name, required=False):
    raw = os.getenv(name)
    if raw is None:
        if required:
            fail_config(f"缺少 {name}，请在 GitHub Actions Secrets 中配置")
        return None
    value = re.sub(r"\s+", "", raw)
    if value:
        os.environ[name] = value
        return value
    if required:
        fail_config(f"{name} 为空，请检查 GitHub Actions Secrets")
    os.environ.pop(name, None)
    return None


def validate_regex(name, value, pattern, hint):
    if value and not pattern.search(value):
        fail_config(f"{name} 格式不正确：{hint}")
    return value


def validate_secret_inputs():
    weread_api_key = clean_secret_value("WEREAD_API_KEY", required=True)
    notion_token = clean_secret_value("NOTION_TOKEN", required=True)
    notion_page = clean_secret_value("NOTION_PAGE")
    notion_database_id = clean_secret_value("NOTION_DATABASE_ID")
    notion_data_source_id = clean_secret_value("NOTION_DATA_SOURCE_ID")
    notion_report_page = clean_secret_value("NOTION_REPORT_PAGE")

    validate_regex(
        "WEREAD_API_KEY",
        weread_api_key,
        WEREAD_API_KEY_PATTERN,
        "应为微信读书 Gateway API Key，不能包含空格或换行",
    )
    validate_regex(
        "NOTION_TOKEN",
        notion_token,
        NOTION_TOKEN_PATTERN,
        "应以 secret_ 或 ntn_ 开头，不能包含空格或换行",
    )
    for name, value in (
        ("NOTION_DATA_SOURCE_ID", notion_data_source_id),
        ("NOTION_DATABASE_ID", notion_database_id),
    ):
        validate_regex(
            name,
            value,
            NOTION_ID_PATTERN,
            "应为 32 位 Notion ID 或带连字符的 UUID",
        )
    if notion_page and not NOTION_ID_IN_TEXT_PATTERN.search(notion_page):
        fail_config("NOTION_PAGE 格式不正确：请填写 Notion 页面链接、数据库链接或 ID")
    if notion_report_page and not NOTION_ID_IN_TEXT_PATTERN.search(notion_report_page):
        fail_config("NOTION_REPORT_PAGE 格式不正确：请填写 Notion 页面链接或 ID")
    if not (notion_data_source_id or notion_page or notion_database_id):
        fail_config(
            "缺少 NOTION_PAGE / NOTION_DATA_SOURCE_ID / NOTION_DATABASE_ID，"
            "请至少配置其中一个"
        )
    return {
        "weread_api_key": weread_api_key,
        "notion_token": notion_token,
        "notion_report_page": notion_report_page,
    }


class WeReadGatewayClient:
    def __init__(self, api_key):
        if not api_key:
            fail_config("没有找到 WEREAD_API_KEY，请在 GitHub Actions Secrets 中配置")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def request(self, api_name, **kwargs):
        payload = {
            "api_name": api_name,
            "skill_version": WEREAD_SKILL_VERSION,
            **kwargs,
        }
        response = self.session.post(WEREAD_GATEWAY_URL, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("upgrade_info"):
            raise Exception(f"微信读书 skill 需要升级: {data.get('upgrade_info')}")
        if data.get("errcode", 0) != 0:
            raise Exception(f"微信读书 Gateway 请求失败: {api_name}, errcode={data.get('errcode')}, response={data}")
        return data


def get_range_start(item):
    note_range = item.get("range") or ""
    try:
        return int(note_range.split("-")[0] or 0)
    except (ValueError, TypeError):
        return 0


def get_note_sort_key(item, chapter=None):
    chapter_uid = item.get("chapterUid", 1)
    chapter_info = None
    if chapter:
        chapter_info = chapter.get(chapter_uid) or chapter.get(str(chapter_uid))
    chapter_idx = (
        chapter_info.get("chapterIdx", 1000000)
        if chapter_info
        else chapter_uid
    )
    return (chapter_idx, get_range_start(item))


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_bookmark_list(bookId):
    """获取我的划线"""
    data = weread.request("/book/bookmarklist", bookId=bookId)
    updated = data.get("updated") or []
    return sorted(updated, key=get_note_sort_key)


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_read_info(bookId):
    data = weread.request("/book/getprogress", bookId=bookId)
    book = data.get("book") or {}
    progress = to_number(book.get("progress")) or 0
    reading_progress = normalize_reading_progress(progress)
    finish_time = book.get("finishTime") or 0
    update_time = book.get("updateTime") or 0
    if finish_time or progress >= 100:
        marked_status = 4
    elif update_time or book.get("isStartReading") or progress > 0:
        marked_status = 2
    else:
        marked_status = 1
    return {
        "markedStatus": marked_status,
        "readingTime": book.get("recordReadingTime") or 0,
        "readingProgress": reading_progress,
        "finishedDate": finish_time,
    }


def normalize_reading_progress(value):
    value = to_number(value) or 0
    if value > 1:
        value = value / 100
    return round(min(max(value, 0), 1), 4)


def normalize_rating(value):
    value = value or 0
    if value > 100:
        return value / 1000
    if value > 10:
        return value / 10
    return value


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_bookinfo(bookId):
    """获取书的详情"""
    data = weread.request("/book/info", bookId=bookId)
    isbn = data.get("isbn", "")
    newRating = normalize_rating(data.get("newRating"))
    return (isbn, newRating)


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_review_list(bookId):
    """获取笔记"""
    reviews_data = []
    hasMore = 1
    synckey = 0
    while hasMore:
        data = weread.request("/review/list/mine", bookid=bookId, synckey=synckey, count=100)
        hasMore = data.get("hasMore", 0)
        synckey = data.get("synckey", 0)
        batch = data.get("reviews") or []
        reviews_data.extend(batch)
        if not batch:
            hasMore = 0
    summary = list(filter(lambda x: (x.get("review") or {}).get("type") == 4, reviews_data))
    reviews = list(filter(lambda x: (x.get("review") or {}).get("type") == 1, reviews_data))
    reviews = list(map(lambda x: x.get("review") or {}, reviews))
    reviews = list(
        map(
            lambda x: {
                **x,
                "markText": x.pop("content", ""),
                "_callout_icon": NOTE_CALLOUT_ICON,
            },
            reviews,
        )
    )
    return summary, reviews


def check(bookId):
    """检查是否已经插入过 如果已经插入了就删除"""
    filter = build_equals_filter("BookId", bookId)
    response = query_data_source(filter=filter)
    for result in response["results"]:
        try:
            client.blocks.delete(block_id=result["id"])
        except Exception as e:
            print(f"删除块时出错: {e}")


def find_book_page(bookId):
    """Find the existing Notion page for a WeRead book."""
    filter = build_equals_filter("BookId", bookId)
    response = query_data_source(filter=filter, page_size=1)
    results = response.get("results") or []
    return results[0] if results else None


def list_block_children(block_id):
    """List all direct children of a Notion block or page."""
    results = []
    cursor = None
    while True:
        body = {"block_id": block_id, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        response = client.blocks.children.list(**body)
        results.extend(response.get("results") or [])
        if not response.get("has_more"):
            return results
        cursor = response.get("next_cursor")
        if not cursor:
            return results


def get_child_page_title(block):
    """Return a child page title regardless of Notion rich-text shape."""
    return (block.get("child_page") or {}).get("title") or ""


def get_block_text(block):
    block_type = block.get("type")
    payload = block.get(block_type) or {}
    rich_text = payload.get("rich_text") or []
    return "".join(
        item.get("plain_text") or (item.get("text") or {}).get("content", "")
        for item in rich_text
    )


def is_generated_sync_block(block):
    return SYNC_BLOCK_MARKER in get_block_text(block)


def find_legacy_sync_page(book_page_id):
    """Find the child page created by the first safe-sync implementation."""
    for block in list_block_children(book_page_id):
        if (
            block.get("type") == "child_page"
            and get_child_page_title(block) == SYNC_PAGE_TITLE
        ):
            return block.get("id")
    return None


def find_sync_container(book_page_id):
    """Find the inline toggle block managed by safe sync mode."""
    for block in list_block_children(book_page_id):
        if block.get("type") == "toggle" and get_block_text(block) == SYNC_PAGE_TITLE:
            return block.get("id")
    return None


def create_sync_container(book_page_id):
    """Create an inline toggle block without touching user-authored content."""
    response = client.blocks.children.append(
        block_id=book_page_id,
        children=[get_toggle(SYNC_PAGE_TITLE)],
    )
    results = response.get("results") or []
    if not results or not results[0].get("id"):
        raise Exception("创建微信读书同步内容折叠区失败")
    return results[0]["id"]


def clear_sync_container(sync_container_id):
    """Archive generated blocks while preserving user-authored blocks."""
    for block in list_block_children(sync_container_id):
        if not is_generated_sync_block(block):
            continue
        block_id = block.get("id")
        if not block_id:
            continue
        try:
            client.blocks.delete(block_id=block_id)
        except Exception as e:
            print(f"清理同步内容时出错: {e}")


def get_sync_mode():
    mode = (os.getenv("NOTION_SYNC_MODE") or SYNC_MODE_SAFE).strip().lower()
    if mode not in {SYNC_MODE_SAFE, SYNC_MODE_REPLACE}:
        fail_config("NOTION_SYNC_MODE 只能是 safe 或 replace")
    return mode


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_chapter_info(bookId):
    """获取章节信息"""
    data = weread.request("/book/chapterinfo", bookId=bookId)
    chapters = data.get("chapters") or []
    return {item["chapterUid"]: item for item in chapters if "chapterUid" in item}


def build_book_properties(
    bookName,
    bookId,
    sort,
    author,
    isbn,
    rating,
    categories,
    shelf_metadata=None,
):
    """Build the shared book properties used for create and update."""
    book_link = (
        shelf_metadata.get("link")
        if shelf_metadata is not None
        else f"https://weread.qq.com/web/reader/{calculate_book_str_id(bookId)}"
    )
    raw_properties = {
        title_property_name: bookName,
        "BookId": bookId,
        "作者": author,
        "Sort": sort,
    }
    if book_link:
        raw_properties["链接"] = book_link
    if isbn:
        raw_properties["ISBN"] = isbn
    if rating is not None:
        raw_properties["评分"] = rating
    if categories is not None:
        raw_properties["分类"] = categories
    if shelf_metadata:
        raw_properties.update(
            {
                "类型": shelf_metadata.get("kind"),
                "置顶": shelf_metadata.get("is_top"),
                "私密": shelf_metadata.get("is_secret"),
            }
        )
        if shelf_metadata.get("read_update_time"):
            raw_properties["最近阅读"] = shelf_metadata["read_update_time"]
        if shelf_metadata.get("finished"):
            raw_properties["状态"] = "读完"
        elif shelf_metadata.get("read_update_time"):
            raw_properties["状态"] = "在读"
    read_info = None
    should_load_read_info = not shelf_metadata or (
        shelf_metadata.get("kind") == "电子书"
        and (shelf_metadata.get("read_update_time") or shelf_metadata.get("finished"))
    )
    if has_any_property(("状态", "阅读时长", "阅读进度", "时间")) and should_load_read_info:
        read_info = get_read_info(bookId=bookId)
    if read_info is not None:
        marked_status = read_info.get("markedStatus", 0)
        reading_time = read_info.get("readingTime", 0)
        reading_progress = read_info.get("readingProgress", 0)
        format_time = ""
        hour = reading_time // 3600
        if hour > 0:
            format_time += f"{hour}时"
        minutes = reading_time % 3600 // 60
        if minutes > 0:
            format_time += f"{minutes}分"
        raw_properties["状态"] = "读完" if marked_status == 4 else "在读"
        raw_properties["阅读时长"] = format_time
        raw_properties["阅读进度"] = reading_progress
        if read_info.get("finishedDate"):
            raw_properties["时间"] = datetime.utcfromtimestamp(
                read_info["finishedDate"]
            ).strftime("%Y-%m-%d %H:%M:%S")
    return build_notion_properties(raw_properties)


def insert_to_notion(
    bookName,
    bookId,
    cover,
    sort,
    author,
    isbn,
    rating,
    categories,
    existing_page=None,
    shelf_metadata=None,
):
    """Create a book page or update it in place."""
    if not cover or not cover.startswith("http"):
        cover = "https://www.notion.so/icons/book_gray.svg"
    properties = build_book_properties(
        bookName,
        bookId,
        sort,
        author,
        isbn,
        rating,
        categories,
        shelf_metadata=shelf_metadata,
    )
    icon = get_icon(cover)
    if existing_page:
        page_id = existing_page["id"]
        client.pages.update(
            page_id=page_id,
            icon=icon,
            cover=icon,
            properties=properties,
        )
        return page_id

    parent = {"type": "data_source_id", "data_source_id": data_source_id}
    response = client.pages.create(
        parent=parent,
        icon=icon,
        cover=icon,
        properties=properties,
    )
    return response["id"]


def add_children(id, children):
    results = []
    for i in range(0, len(children) // 100 + 1):
        time.sleep(0.3)
        response = client.blocks.children.append(
            block_id=id, children=children[i * 100 : (i + 1) * 100]
        )
        results.extend(response.get("results"))
    return results if len(results) == len(children) else None


def add_grandchild(grandchild, results):
    for key, value in grandchild.items():
        time.sleep(0.3)
        id = results[key].get("id")
        client.blocks.children.append(block_id=id, children=[value])


def get_notebooklist():
    """获取笔记本列表"""
    books = []
    hasMore = 1
    lastSort = None
    while hasMore:
        params = {"count": 100}
        if lastSort is not None:
            params["lastSort"] = lastSort
        data = weread.request("/user/notebooks", **params)
        hasMore = data.get("hasMore", 0)
        batch = data.get("books") or []
        books.extend(batch)
        if batch:
            lastSort = batch[-1].get("sort")
        else:
            hasMore = 0
    books.sort(key=lambda x: x.get("sort") or 0)
    return books


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_shelf():
    return weread.request("/shelf/sync")


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_reading_stats(mode):
    return weread.request("/readdata/detail", mode=mode)


def timestamp_matches_date(value, target_date):
    try:
        return datetime.fromtimestamp(int(value), SHANGHAI_TZ).date() == target_date
    except (TypeError, ValueError, OSError):
        return False


def format_duration(seconds):
    seconds = int(to_number(seconds) or 0)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def normalize_categories(value):
    if not value:
        return None
    if isinstance(value, list):
        return [
            item.get("title") if isinstance(item, dict) else to_text(item)
            for item in value
            if (item.get("title") if isinstance(item, dict) else to_text(item))
        ]
    if isinstance(value, dict):
        return [value.get("title")] if value.get("title") else None
    return [to_text(value)]


def get_shelf_entries(shelf):
    entries = []
    for book in shelf.get("books") or []:
        book_id = book.get("bookId")
        if not book_id:
            continue
        entries.append(
            {
                "book_id": str(book_id),
                "title": book.get("title") or "未命名书籍",
                "author": book.get("author") or "",
                "cover": book.get("cover") or "",
                "link": book.get("deepLink") or "",
                "categories": normalize_categories(
                    book.get("category") or book.get("categories")
                ),
                "kind": "电子书",
                "finished": bool(book.get("finishReading")),
                "read_update_time": book.get("readUpdateTime") or 0,
                "is_top": bool(book.get("isTop")),
                "is_secret": bool(book.get("secret")),
            }
        )
    for album in shelf.get("albums") or []:
        info = album.get("albumInfo") or {}
        extra = album.get("albumInfoExtra") or {}
        album_id = info.get("albumId")
        if not album_id:
            continue
        entries.append(
            {
                "book_id": f"album:{album_id}",
                "title": info.get("name") or "未命名有声书",
                "author": info.get("authorName") or "",
                "cover": info.get("cover") or "",
                "link": "",
                "categories": None,
                "kind": "有声书",
                "finished": bool(info.get("finish")),
                "read_update_time": extra.get("lectureReadUpdateTime") or 0,
                "is_top": bool(extra.get("isTop")),
                "is_secret": bool(extra.get("secret")),
            }
        )
    if shelf.get("mp"):
        entries.append(
            {
                "book_id": "mp:articles",
                "title": "文章收藏",
                "author": "微信读书",
                "cover": "",
                "link": "",
                "categories": None,
                "kind": "文章收藏",
                "finished": False,
                "read_update_time": 0,
                "is_top": False,
                "is_secret": True,
            }
        )
    return entries


def get_daily_read_seconds(reading_stats, target_date):
    for field in ("dailyReadTimes", "readTimes"):
        for timestamp, seconds in (reading_stats.get(field) or {}).items():
            if timestamp_matches_date(timestamp, target_date):
                return int(to_number(seconds) or 0)
    return 0


def get_daily_active_entries(shelf_entries, target_date):
    return [
        entry
        for entry in shelf_entries
        if timestamp_matches_date(entry.get("read_update_time"), target_date)
    ]


def get_review_create_time(item):
    return (item.get("review") or item).get("createTime")


def get_daily_note_counts(notebooks, target_date):
    highlights = 0
    thoughts = 0
    for item in notebooks:
        if not timestamp_matches_date(item.get("sort"), target_date):
            continue
        book = item.get("book") or item
        book_id = book.get("bookId")
        if not book_id:
            continue
        for bookmark in get_bookmark_list(book_id):
            if timestamp_matches_date(bookmark.get("createTime"), target_date):
                highlights += 1
        summary, reviews = get_review_list(book_id)
        for review in summary + reviews:
            if timestamp_matches_date(get_review_create_time(review), target_date):
                thoughts += 1
    return highlights, thoughts


def find_child_page(parent_page_id, title):
    for block in list_block_children(parent_page_id):
        if block.get("type") == "child_page" and get_child_page_title(block) == title:
            return block.get("id")
    return None


def get_or_create_child_page(parent_page_id, title):
    existing_page = find_child_page(parent_page_id, title)
    if existing_page:
        return existing_page
    response = client.pages.create(
        parent={"page_id": parent_page_id},
        properties={"title": get_title(title)},
    )
    return response["id"]


def sync_managed_page_content(page_id, children):
    sync_container_id = find_sync_container(page_id)
    if not sync_container_id:
        sync_container_id = create_sync_container(page_id)
    clear_sync_container(sync_container_id)
    return add_children(sync_container_id, children)


def build_daily_report_children(
    target_date, read_seconds, active_entries, highlights, thoughts
):
    children = [
        get_heading(1, "今日概览", marker=SYNC_BLOCK_MARKER),
        get_callout(f"阅读时长：{format_duration(read_seconds)}", marker=SYNC_BLOCK_MARKER),
        get_callout(
            f"活跃书籍：{len(active_entries)} 本", marker=SYNC_BLOCK_MARKER
        ),
        get_callout(f"新增划线：{highlights} 条", marker=SYNC_BLOCK_MARKER),
        get_callout(f"新增想法：{thoughts} 条", marker=SYNC_BLOCK_MARKER),
    ]
    if active_entries:
        children.append(get_heading(2, "今日活跃书籍", marker=SYNC_BLOCK_MARKER))
        for entry in active_entries[:20]:
            author = f" · {entry['author']}" if entry.get("author") else ""
            children.append(
                get_callout(
                    f"{entry['title']}{author}", marker=SYNC_BLOCK_MARKER
                )
            )
    return children


def build_dashboard_children(shelf_entries, monthly_stats, annual_stats):
    total_entries = len(shelf_entries)
    books = len([entry for entry in shelf_entries if entry["kind"] == "电子书"])
    albums = len([entry for entry in shelf_entries if entry["kind"] == "有声书"])
    children = [
        get_heading(1, "书架概览", marker=SYNC_BLOCK_MARKER),
        get_callout(
            f"书架共有 {total_entries} 个条目：{books} 本电子书，{albums} 个有声书",
            marker=SYNC_BLOCK_MARKER,
        ),
        get_heading(1, "阅读统计", marker=SYNC_BLOCK_MARKER),
        get_callout(
            f"本月阅读：{format_duration(monthly_stats.get('totalReadTime'))} · "
            f"{monthly_stats.get('readDays') or 0} 个阅读日",
            marker=SYNC_BLOCK_MARKER,
        ),
        get_callout(
            f"本年阅读：{format_duration(annual_stats.get('totalReadTime'))} · "
            f"{annual_stats.get('readDays') or 0} 个阅读日",
            marker=SYNC_BLOCK_MARKER,
        ),
    ]
    top_books = monthly_stats.get("readLongest") or []
    if top_books:
        children.append(get_heading(2, "本月阅读最多", marker=SYNC_BLOCK_MARKER))
        for item in top_books[:5]:
            book = item.get("book") or item.get("albumInfo") or {}
            title = book.get("title") or book.get("name") or "未命名内容"
            children.append(
                get_callout(
                    f"{title} · {format_duration(item.get('readTime'))}",
                    marker=SYNC_BLOCK_MARKER,
                )
            )
    categories = annual_stats.get("preferCategory") or []
    if categories:
        children.append(get_heading(2, "阅读偏好", marker=SYNC_BLOCK_MARKER))
        category_text = "、".join(
            item.get("categoryTitle", "") for item in categories[:5] if item.get("categoryTitle")
        )
        if category_text:
            children.append(get_callout(category_text, marker=SYNC_BLOCK_MARKER))
    return children


def sync_reading_reports(report_page_id, shelf_entries, notebooks):
    today = datetime.now(SHANGHAI_TZ).date()
    annual_stats = get_reading_stats("annually")
    monthly_stats = get_reading_stats("monthly")
    daily_root_id = get_or_create_child_page(report_page_id, READING_DAILY_ROOT_TITLE)
    daily_page_id = get_or_create_child_page(daily_root_id, today.isoformat())
    daily_read_seconds = get_daily_read_seconds(annual_stats, today)
    active_entries = get_daily_active_entries(shelf_entries, today)
    highlights, thoughts = get_daily_note_counts(notebooks, today)
    sync_managed_page_content(
        daily_page_id,
        build_daily_report_children(
            today, daily_read_seconds, active_entries, highlights, thoughts
        ),
    )
    dashboard_page_id = get_or_create_child_page(report_page_id, READING_DASHBOARD_TITLE)
    sync_managed_page_content(
        dashboard_page_id,
        build_dashboard_children(shelf_entries, monthly_stats, annual_stats),
    )
    print(f"已更新阅读日报：{today.isoformat()}")
    print("已更新阅读看板")


def get_sort():
    """获取 data source 中的最新时间"""
    filter = build_is_not_empty_filter("Sort")
    sorts = [
        {
            "property": "Sort",
            "direction": "descending",
        }
    ]
    response = query_data_source(filter=filter, sorts=sorts, page_size=1)
    if len(response.get("results")) == 1:
        return get_number_property_value(
            response.get("results")[0].get("properties").get("Sort")
        )
    return 0


def get_children(chapter, summary, bookmark_list):
    children = []
    grandchild = {}
    all_chapters = []
    if chapter:
        for uid, info in chapter.items():
            item = dict(info)
            item["chapterUid"] = item.get("chapterUid", uid)
            all_chapters.append(item)
        all_chapters.sort(key=lambda x: x.get("chapterIdx", 0))
    chapter_nodes = {node.get("chapterUid"): node for node in all_chapters}

    def get_ancestor_chain(current_chapter_info):
        if not current_chapter_info:
            return []
        try:
            current_pos = all_chapters.index(current_chapter_info)
        except ValueError:
            return [current_chapter_info]

        chain = []
        target_level = current_chapter_info.get("level", 1)
        for index in range(current_pos - 1, -1, -1):
            candidate = all_chapters[index]
            if candidate.get("level", 1) < target_level:
                chain.insert(0, candidate)
                target_level = candidate.get("level", 1)
                if target_level <= 1:
                    break
        chain.append(current_chapter_info)
        return chain

    if chapter:
        grouped_bookmarks = []
        last_uid = None
        current_group = None

        for data in bookmark_list:
            uid = data.get("chapterUid", 1)
            if uid != last_uid:
                if current_group:
                    grouped_bookmarks.append(current_group)
                info = chapter.get(uid) or chapter.get(str(uid))
                current_group = {
                    "chapterUid": uid,
                    "bookmarks": [],
                    "chapterInfo": info,
                }
                last_uid = uid
            current_group["bookmarks"].append(data)
        if current_group:
            grouped_bookmarks.append(current_group)

        previous_path_uids = []
        for group in grouped_bookmarks:
            info = group["chapterInfo"]
            if info:
                current_info = chapter_nodes.get(group["chapterUid"]) or chapter_nodes.get(
                    str(group["chapterUid"])
                )
                if current_info is None:
                    current_info = dict(info)
                    current_info["chapterUid"] = current_info.get("chapterUid", group["chapterUid"])
                path = get_ancestor_chain(current_info)

                divergence_index = 0
                min_len = min(len(path), len(previous_path_uids))
                while divergence_index < min_len:
                    path_uid = path[divergence_index].get("chapterUid")
                    if path_uid != previous_path_uids[divergence_index]:
                        break
                    divergence_index += 1

                for chapter_node in path[divergence_index:]:
                    children.append(
                        get_heading(
                            chapter_node.get("level"),
                            chapter_node.get("title"),
                            marker=SYNC_BLOCK_MARKER,
                        )
                    )
                previous_path_uids = [node.get("chapterUid") for node in path]
            else:
                previous_path_uids = []

            for i in group["bookmarks"]:
                markText = i.get("markText") or ""
                if not markText:
                    continue
                callout_icon = i.get("_callout_icon") or BOOKMARK_CALLOUT_ICON
                for j in range(0, len(markText) // 2000 + 1):
                    children.append(
                        get_callout(
                            markText[j * 2000 : (j + 1) * 2000],
                            icon=callout_icon,
                            marker=SYNC_BLOCK_MARKER,
                        )
                    )
                if i.get("abstract") != None and i.get("abstract") != "":
                    quote = get_quote(i.get("abstract"), marker=SYNC_BLOCK_MARKER)
                    grandchild[len(children) - 1] = quote

    else:
        # 如果没有章节信息
        for data in bookmark_list:
            markText = data.get("markText") or ""
            if not markText:
                continue
            for i in range(0, len(markText) // 2000 + 1):
                children.append(
                    get_callout(
                        markText[i * 2000 : (i + 1) * 2000],
                        icon=BOOKMARK_CALLOUT_ICON,
                        marker=SYNC_BLOCK_MARKER,
                    )
                )
    if summary != None and len(summary) > 0:
        children.append(get_heading(1, "点评", marker=SYNC_BLOCK_MARKER))
        for i in summary:
            content = (i.get("review") or {}).get("content") or ""
            if not content:
                continue
            for j in range(0, len(content) // 2000 + 1):
                children.append(
                    get_callout(
                        content[j * 2000 : (j + 1) * 2000],
                        icon=NOTE_CALLOUT_ICON,
                        marker=SYNC_BLOCK_MARKER,
                    )
                )
    return children, grandchild


def transform_id(book_id):
    id_length = len(book_id)

    if re.match(r"^\d*$", book_id):
        ary = []
        for i in range(0, id_length, 9):
            ary.append(format(int(book_id[i : min(i + 9, id_length)]), "x"))
        return "3", ary

    result = ""
    for i in range(id_length):
        result += format(ord(book_id[i]), "x")
    return "4", [result]


def calculate_book_str_id(book_id):
    md5 = hashlib.md5()
    md5.update(book_id.encode("utf-8"))
    digest = md5.hexdigest()
    result = digest[0:3]
    code, transformed_ids = transform_id(book_id)
    result += code + "2" + digest[-2:]

    for i in range(len(transformed_ids)):
        hex_length_str = format(len(transformed_ids[i]), "x")
        if len(hex_length_str) == 1:
            hex_length_str = "0" + hex_length_str

        result += hex_length_str + transformed_ids[i]

        if i < len(transformed_ids) - 1:
            result += "g"

    if len(result) < 20:
        result += digest[0 : 20 - len(result)]

    md5 = hashlib.md5()
    md5.update(result.encode("utf-8"))
    result += md5.hexdigest()[0:3]
    return result


def extract_notion_id():
    url_or_id = (
        os.getenv("NOTION_DATA_SOURCE_ID")
        or os.getenv("NOTION_PAGE")
        or os.getenv("NOTION_DATABASE_ID")
    )
    if not url_or_id:
        fail_config("没有找到 NOTION_PAGE / NOTION_DATA_SOURCE_ID，请按照文档填写")
    match = NOTION_ID_IN_TEXT_PATTERN.search(url_or_id)
    if match:
        return match.group(0)

    fail_config("获取 Notion ID 失败，请检查 NOTION_PAGE / NOTION_DATA_SOURCE_ID")


def extract_optional_notion_id(value, name):
    if not value:
        return None
    match = NOTION_ID_IN_TEXT_PATTERN.search(value)
    if match:
        return match.group(0)
    fail_config(f"获取 {name} 失败，请检查页面链接或 ID")


def query_data_source(**body):
    return client.request(
        path=f"data_sources/{data_source_id}/query",
        method="POST",
        body=body,
    )


def load_data_source_schema():
    """读取当前 data source 的真实属性，只强制要求同步游标需要的字段。"""
    global data_source_property_types, title_property_name, skipped_property_names
    response = client.request(path=f"data_sources/{data_source_id}", method="GET")
    properties = response.get("properties") or {}
    data_source_property_types = {
        name: (config or {}).get("type") for name, config in properties.items()
    }
    title_property_name = next(
        (
            name
            for name, prop_type in data_source_property_types.items()
            if prop_type == "title"
        ),
        None,
    )
    skipped_property_names = set()
    if not title_property_name:
        raise Exception("Notion data source 缺少标题属性，请保留一个 Title 类型属性")

    missing = [
        name for name in ("BookId", "Sort") if name not in data_source_property_types
    ]
    if missing:
        raise Exception(
            f"Notion data source 缺少必填属性: {', '.join(missing)}。"
            "请在模板中补充后重试"
        )

    print(
        f"已读取 Notion 属性 {len(data_source_property_types)} 个，"
        f"标题属性: {title_property_name}"
    )


def get_property_type(name):
    return data_source_property_types.get(name)


def has_any_property(names):
    return any(name in data_source_property_types for name in names)


def build_equals_filter(name, value):
    prop_type = get_property_type(name)
    if prop_type in {"title", "rich_text", "url", "email", "phone_number"}:
        return {"property": name, prop_type: {"equals": str(value)}}
    if prop_type == "number":
        return {"property": name, "number": {"equals": to_number(value)}}
    if prop_type == "select":
        return {"property": name, "select": {"equals": str(value)}}
    if prop_type == "status":
        return {"property": name, "status": {"equals": str(value)}}
    raise Exception(f"Notion 属性 {name} 的类型 {prop_type} 暂不支持用于查询")


def build_is_not_empty_filter(name):
    prop_type = get_property_type(name)
    if prop_type in {
        "title",
        "rich_text",
        "url",
        "email",
        "phone_number",
        "number",
        "select",
        "status",
        "date",
    }:
        return {"property": name, prop_type: {"is_not_empty": True}}
    raise Exception(f"Notion 属性 {name} 的类型 {prop_type} 暂不支持用于查询")


def to_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(to_text(item) for item in value if item is not None)
    return str(value)


def to_name_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [to_text(item) for item in value if to_text(item)]
    text = to_text(value)
    return [text] if text else []


def to_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def normalize_date_value(value):
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    return value


def build_option_property(prop_type, value):
    names = to_name_list(value)
    if not names:
        return None
    if prop_type == "status":
        return get_status(names[0])
    if prop_type == "select":
        return get_select(names[0])
    return get_multi_select(names)


def build_notion_property(name, value):
    prop_type = get_property_type(name)
    if not prop_type:
        if name not in skipped_property_names:
            print(f"属性 {name} 在 Notion 模板中不存在，自动跳过")
            skipped_property_names.add(name)
        return None
    if value is None:
        return None

    if prop_type == "title":
        return get_title(to_text(value))
    if prop_type == "rich_text":
        return get_rich_text(to_text(value))
    if prop_type == "number":
        number = to_number(value)
        return get_number(number) if number is not None else None
    if prop_type == "url":
        return get_url(to_text(value))
    if prop_type in {"multi_select", "status", "select"}:
        return build_option_property(prop_type, value)
    if prop_type == "date":
        return get_date(normalize_date_value(value))
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}

    if name not in skipped_property_names:
        print(f"属性 {name} 的类型 {prop_type} 暂不支持写入，自动跳过")
        skipped_property_names.add(name)
    return None


def build_notion_properties(raw_properties):
    return {
        name: prop
        for name, value in raw_properties.items()
        if (prop := build_notion_property(name, value)) is not None
    }


def get_number_property_value(property_value):
    if not property_value:
        return 0
    prop_type = property_value.get("type")
    value = property_value.get(prop_type)
    if prop_type == "number":
        return value or 0
    if prop_type in {"title", "rich_text"} and value:
        return to_number(value[0].get("plain_text")) or 0
    if prop_type in {"select", "status"} and value:
        return to_number(value.get("name")) or 0
    return 0


def get_child_database_title(block):
    return (block.get("child_database") or {}).get("title") or ""


def list_descendant_blocks(block_id, max_depth=5, _depth=0):
    blocks = list_block_children(block_id)
    if _depth >= max_depth:
        return blocks
    results = list(blocks)
    for block in blocks:
        if block.get("has_children"):
            results.extend(
                list_descendant_blocks(block.get("id"), max_depth, _depth + 1)
            )
    return results


def resolve_template_source_id(database_id):
    """Resolve a child database/block ID to its first data source."""
    try:
        client.request(path=f"data_sources/{database_id}", method="GET")
        return database_id
    except APIResponseError as error:
        code = getattr(error.code, "value", error.code)
        if code not in {"object_not_found", "validation_error"}:
            raise
    database = client.request(path=f"databases/{database_id}", method="GET")
    sources = database.get("data_sources") or []
    if not sources:
        raise Exception(f"Notion 数据库 {database_id} 没有可用的 data source")
    return sources[0]["id"]


def load_template_source(database_id, display_name):
    source_id = resolve_template_source_id(database_id)
    response = client.request(path=f"data_sources/{source_id}", method="GET")
    properties = response.get("properties") or {}
    property_types = {
        name: (config or {}).get("type") for name, config in properties.items()
    }
    title_name = next(
        (name for name, prop_type in property_types.items() if prop_type == "title"),
        None,
    )
    if not title_name:
        raise Exception(f"Notion 数据源 {display_name} 缺少 Title 属性")
    return {
        "name": display_name,
        "database_id": database_id,
        "data_source_id": source_id,
        "property_types": property_types,
        "title_name": title_name,
    }


def discover_template_sources(template_page_id):
    """Discover the databases embedded in the official reading-center page."""
    discovered = {}
    for block in list_descendant_blocks(template_page_id):
        if block.get("type") != "child_database":
            continue
        title = get_child_database_title(block)
        if title not in TEMPLATE_SOURCE_NAMES or title in discovered:
            continue
        discovered[title] = load_template_source(block["id"], title)
    if not discovered:
        return {}
    missing = sorted(TEMPLATE_REQUIRED_SOURCE_NAMES - set(discovered))
    if missing:
        raise Exception(
            "正式微信读书模板缺少这些数据库："
            + ", ".join(missing)
            + "。请确认 NOTION_PAGE 指向模板总页面，并已授权 weread2notion。"
        )
    return discovered


def template_source(name):
    source = template_sources.get(name)
    if not source:
        raise Exception(f"正式模板中没有找到“{name}”数据库")
    return source


def template_property_type(source_name, property_name):
    return template_source(source_name)["property_types"].get(property_name)


def build_template_filter(source_name, property_name, value):
    prop_type = template_property_type(source_name, property_name)
    if prop_type in {"title", "rich_text", "url", "email", "phone_number"}:
        return {"property": property_name, prop_type: {"equals": str(value)}}
    if prop_type == "number":
        return {"property": property_name, "number": {"equals": to_number(value)}}
    if prop_type == "select":
        return {"property": property_name, "select": {"equals": str(value)}}
    if prop_type == "status":
        return {"property": property_name, "status": {"equals": str(value)}}
    raise Exception(
        f"正式模板的“{source_name}”数据库属性 {property_name} 类型 {prop_type} 不支持查询"
    )


def query_template_source(source_name, **body):
    source_id = template_source(source_name)["data_source_id"]
    return client.request(
        path=f"data_sources/{source_id}/query", method="POST", body=body
    )


def find_template_page(source_name, property_name, value):
    key = (source_name, property_name, str(value))
    if key in template_lookup_cache:
        return template_lookup_cache[key]
    response = query_template_source(
        source_name, filter=build_template_filter(source_name, property_name, value), page_size=1
    )
    result = (response.get("results") or [None])[0]
    template_lookup_cache[key] = result
    return result


def normalize_timestamp(value):
    number = to_number(value)
    if number is None or number <= 0:
        return None
    if number > 100000000000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, SHANGHAI_TZ)
    except (OverflowError, OSError, ValueError):
        return None


def normalize_template_date(value):
    if isinstance(value, (int, float)):
        date_value = normalize_timestamp(value)
        return date_value.isoformat(timespec="seconds") if date_value else None
    return value


def normalize_template_target_date(value):
    if hasattr(value, "year") and not hasattr(value, "hour"):
        return value
    if hasattr(value, "date"):
        return value.date()
    if isinstance(value, str):
        timestamp = normalize_timestamp(value)
        if timestamp:
            return timestamp.date()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo:
                parsed = parsed.astimezone(SHANGHAI_TZ)
            return parsed.date()
        except ValueError:
            return None
    timestamp = normalize_timestamp(value)
    return timestamp.date() if timestamp else None


def build_template_property(source_name, property_name, value):
    prop_type = template_property_type(source_name, property_name)
    if not prop_type or value is None:
        return None
    if prop_type == "title":
        return get_title(to_text(value))
    if prop_type in {"rich_text", "text"}:
        return get_rich_text(to_text(value))
    if prop_type == "number":
        number = to_number(value)
        return get_number(number) if number is not None else None
    if prop_type == "url":
        return get_url(to_text(value))
    if prop_type in {"select", "status"}:
        names = to_name_list(value)
        if not names:
            return None
        return get_status(names[0]) if prop_type == "status" else get_select(names[0])
    if prop_type == "multi_select":
        names = to_name_list(value)
        return get_multi_select(names) if names else None
    if prop_type == "date":
        value = normalize_template_date(value)
        return get_date(value) if value else None
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}
    if prop_type == "relation":
        ids = to_name_list(value)
        return {"relation": [{"id": item} for item in ids]} if ids else None
    if prop_type in {"files", "file"}:
        url = value[0] if isinstance(value, (list, tuple)) and value else value
        return get_file(url) if url else None
    return None


def build_template_properties(source_name, raw_properties):
    properties = {}
    for name, value in raw_properties.items():
        if name not in template_source(source_name)["property_types"]:
            continue
        prop = build_template_property(source_name, name, value)
        if prop is not None:
            properties[name] = prop
    return properties


def upsert_template_page(source_name, lookup_property, lookup_value, raw_properties):
    existing = find_template_page(source_name, lookup_property, lookup_value)
    properties = build_template_properties(source_name, raw_properties)
    if existing:
        if properties:
            client.pages.update(page_id=existing["id"], properties=properties)
        return existing["id"], False
    response = client.pages.create(
        parent={"type": "data_source_id", "data_source_id": template_source(source_name)["data_source_id"]},
        properties=properties,
    )
    page_id = response["id"]
    template_lookup_cache[(source_name, lookup_property, str(lookup_value))] = response
    return page_id, True


def get_or_create_template_page(source_name, lookup_property, lookup_value, raw_properties):
    existing = find_template_page(source_name, lookup_property, lookup_value)
    if existing:
        return existing["id"], False
    return upsert_template_page(
        source_name, lookup_property, lookup_value, raw_properties
    )


def append_template_relations(source_name, page_id, property_name, related_ids):
    if not related_ids or template_property_type(source_name, property_name) != "relation":
        return
    related_ids = {str(item) for item in related_ids if item}
    cache_key = (source_name, page_id, property_name)
    if cache_key not in template_relation_cache:
        page = client.pages.retrieve(page_id=page_id)
        current = ((page.get("properties") or {}).get(property_name) or {}).get("relation") or []
        template_relation_cache[cache_key] = {item.get("id") for item in current if item.get("id")}
    merged = template_relation_cache[cache_key] | related_ids
    if merged == template_relation_cache[cache_key]:
        return
    template_relation_cache[cache_key] = merged
    client.pages.update(
        page_id=page_id,
        properties={property_name: {"relation": [{"id": item} for item in sorted(merged)]}},
    )


def ensure_named_template_page(source_name, title):
    title = to_text(title).strip()
    if not title:
        return None
    cache_key = ("reference", source_name, title)
    if cache_key in template_lookup_cache:
        return template_lookup_cache[cache_key]
    existing = find_template_page(
        source_name, template_source(source_name)["title_name"], title
    )
    if existing:
        template_lookup_cache[cache_key] = existing["id"]
        return existing["id"]
    page_id, _ = upsert_template_page(
        source_name,
        template_source(source_name)["title_name"],
        title,
        {template_source(source_name)["title_name"]: title},
    )
    template_lookup_cache[cache_key] = page_id
    return page_id


def period_names(target_date):
    iso = target_date.isocalendar()
    return {
        "日": (target_date.strftime("%Y年%m月%d日"), target_date),
        "周": (f"{iso.year}年第{iso.week}周", target_date - timedelta(days=target_date.weekday())),
        "月": (f"{target_date.year}年{target_date.month}月", target_date.replace(day=1)),
        "年": (str(target_date.year), target_date.replace(month=1, day=1)),
    }


def ensure_template_periods(value, daily_seconds=None, daily_timestamp=None):
    target_date = normalize_template_target_date(value)
    if not target_date:
        return {}
    cache_key = target_date.isoformat()
    if cache_key in template_period_cache:
        return template_period_cache[cache_key]
    names = period_names(target_date)
    pages = {}
    for source_name in ("周", "月", "年"):
        title, start_date = names[source_name]
        page_id, _ = get_or_create_template_page(
            source_name,
            template_source(source_name)["title_name"],
            title,
            {
                template_source(source_name)["title_name"]: title,
                "日期": start_date.isoformat(),
            },
        )
        pages[source_name] = page_id
    day_title, day_date = names["日"]
    day_properties = {
        template_source("日")["title_name"]: day_title,
        "日期": day_date.isoformat(),
        "周": [pages["周"]],
        "月": [pages["月"]],
        "年": [pages["年"]],
    }
    if daily_seconds is not None:
        day_properties["时长"] = daily_seconds
    if daily_timestamp is not None:
        day_properties["时间戳"] = daily_timestamp
    page_id, _ = upsert_template_page(
        "日",
        template_source("日")["title_name"],
        day_title,
        day_properties,
    )
    pages["日"] = page_id
    template_period_cache[cache_key] = pages
    return pages


def get_reading_day_entries(reading_stats):
    entries = {}
    for field in ("dailyReadTimes", "readTimes"):
        for timestamp, seconds in (reading_stats.get(field) or {}).items():
            target_date = normalize_template_target_date(timestamp)
            if not target_date:
                continue
            key = target_date.isoformat()
            entries[key] = {
                "timestamp": to_number(timestamp) or 0,
                "seconds": int(to_number(seconds) or 0),
            }
    return entries


def sync_template_daily_stats():
    reading_stats = get_reading_stats("annually")
    entries = get_reading_day_entries(reading_stats)
    for entry in entries.values():
        ensure_template_periods(
            entry["timestamp"],
            daily_seconds=entry["seconds"],
            daily_timestamp=entry["timestamp"],
        )
    return len(entries)


def template_book_properties(entry, sort, read_info=None, periods=None):
    author_page = ensure_named_template_page("作者", entry.get("author"))
    category_pages = [
        page_id
        for category in (entry.get("categories") or [])
        if (page_id := ensure_named_template_page("分类", category))
    ]
    finished = bool(entry.get("finished"))
    read_update_time = entry.get("read_update_time") or 0
    status = "已读" if finished else ("在读" if read_update_time else "想读")
    raw = {
        "书名": entry.get("title") or "未命名书籍",
        "BookId": entry.get("book_id"),
        "Sort": sort,
        "作者": [author_page] if author_page else None,
        "分类": category_pages,
        "链接": entry.get("link"),
        "封面": entry.get("cover"),
        "阅读状态": status,
        "最后阅读时间": read_update_time,
    }
    if read_info:
        status = read_info.get("markedStatus")
        raw["阅读状态"] = "已读" if status == 4 else ("在读" if status == 2 else "想读")
        raw["阅读时长"] = read_info.get("readingTime") or 0
        raw["阅读进度"] = read_info.get("readingProgress") or 0
        if read_info.get("finishedDate"):
            raw["时间"] = read_info["finishedDate"]
            raw["最后阅读时间"] = read_info["finishedDate"]
    if entry.get("isbn"):
        raw["ISBN"] = entry["isbn"]
    if entry.get("rating") is not None:
        raw["评分"] = entry["rating"]
    if periods:
        raw.update(
            {
                "日": [periods["日"]],
                "周": [periods["周"]],
                "月": [periods["月"]],
                "年": [periods["年"]],
            }
        )
    return raw


def build_template_entry_from_notebook(item):
    book = item.get("book") or item
    book_id = book.get("bookId")
    return {
        "book_id": str(book_id),
        "title": book.get("title") or "未命名书籍",
        "author": book.get("author") or "",
        "cover": (book.get("cover") or "").replace("/s_", "/t7_"),
        "link": book.get("deepLink") or f"https://weread.qq.com/web/reader/{calculate_book_str_id(book_id)}",
        "categories": normalize_categories(book.get("categories")),
        "kind": "电子书",
        "finished": False,
        "read_update_time": book.get("readUpdateTime") or 0,
    }


def sync_template_chapters(book_id, book_page_id, chapter):
    if not chapter:
        return
    for chapter_uid, item in chapter.items():
        chapter_uid = to_number(chapter_uid)
        if chapter_uid is None:
            continue
        block_id = f"{book_id}:{chapter_uid}"
        raw = {
            "Name": item.get("title") or item.get("chapterTitle") or f"章节 {chapter_uid}",
            "blockId": block_id,
            "chapterIdx": item.get("chapterIdx"),
            "chapterUid": chapter_uid,
            "level": item.get("level"),
            "readAhead": item.get("readAhead"),
            "tar": item.get("tar"),
            "updateTime": item.get("updateTime"),
            "书籍": [book_page_id],
        }
        upsert_template_page("章节", "blockId", block_id, raw)


def sync_template_annotations(book_id, book_page_id):
    chapter = get_chapter_info(book_id)
    bookmark_list = get_bookmark_list(book_id)
    _, reviews = get_review_list(book_id)
    period_cache = {}
    for bookmark in bookmark_list:
        bookmark_id = bookmark.get("bookmarkId") or hashlib.md5(
            f"{book_id}:{bookmark.get('chapterUid')}:{bookmark.get('range')}:{bookmark.get('markText')}".encode("utf-8")
        ).hexdigest()
        date_value = bookmark.get("createTime") or bookmark.get("date")
        period_key = str(date_value)
        if period_key not in period_cache:
            period_cache[period_key] = ensure_template_periods(date_value)
        periods = period_cache[period_key]
        raw = {
            "Name": bookmark.get("markText") or "微信读书划线",
            "Date": date_value,
            "blockId": bookmark.get("blockId"),
            "bookId": str(book_id),
            "bookVersion": bookmark.get("bookVersion"),
            "bookmarkId": bookmark_id,
            "chapterUid": bookmark.get("chapterUid"),
            "colorStyle": bookmark.get("colorStyle"),
            "range": bookmark.get("range"),
            "style": bookmark.get("style"),
            "type": bookmark.get("type"),
            "书籍": [book_page_id],
            "日": [periods.get("日")],
            "周": [periods.get("周")],
            "月": [periods.get("月")],
            "年": [periods.get("年")],
        }
        upsert_template_page("划线", "bookmarkId", bookmark_id, raw)
    for review in reviews:
        review_id = review.get("reviewId") or hashlib.md5(
            f"{book_id}:{review.get('createTime')}:{review.get('markText')}".encode("utf-8")
        ).hexdigest()
        date_value = review.get("createTime") or review.get("date")
        period_key = str(date_value)
        if period_key not in period_cache:
            period_cache[period_key] = ensure_template_periods(date_value)
        periods = period_cache[period_key]
        raw = {
            "Name": review.get("markText") or review.get("content") or "微信读书想法",
            "Date": date_value,
            "abstract": review.get("abstract"),
            "blockId": review.get("blockId"),
            "bookId": str(book_id),
            "bookVersion": review.get("bookVersion"),
            "chapterUid": review.get("chapterUid"),
            "range": review.get("range"),
            "reviewId": review_id,
            "star": review.get("star"),
            "style": review.get("style"),
            "type": review.get("type"),
            "书籍": [book_page_id],
            "日": [periods.get("日")],
            "周": [periods.get("周")],
            "月": [periods.get("月")],
            "年": [periods.get("年")],
        }
        upsert_template_page("笔记", "reviewId", review_id, raw)
    sync_template_chapters(book_id, book_page_id, chapter)
    return len(bookmark_list), len(reviews), len(chapter)


def sync_template_workspace(template_page_id):
    global template_sources, template_relation_cache, template_lookup_cache, template_period_cache
    template_sources = discover_template_sources(template_page_id)
    template_relation_cache = {}
    template_lookup_cache = {}
    template_period_cache = {}
    day_count = sync_template_daily_stats()
    notebooks = get_notebooklist()
    shelf_entries = get_shelf_entries(get_shelf())
    entry_by_id = {entry["book_id"]: entry for entry in shelf_entries}
    for notebook in notebooks:
        entry = build_template_entry_from_notebook(notebook)
        entry_by_id.setdefault(entry["book_id"], entry)
    latest_sort = 0
    if "Sort" in template_source("书架")["property_types"]:
        response = query_template_source(
            "书架",
            sorts=[{"property": "Sort", "direction": "descending"}],
            page_size=1,
        )
        if response.get("results"):
            latest_sort = get_number_property_value(
                (response["results"][0].get("properties") or {}).get("Sort")
            )
    notebook_by_id = {
        str(get_notebook_book_id(item)): item
        for item in notebooks
        if get_notebook_book_id(item)
    }
    counts = {"books": 0, "highlights": 0, "notes": 0, "chapters": 0}
    for entry in entry_by_id.values():
        book_id = entry["book_id"]
        notebook = notebook_by_id.get(book_id)
        sort = (notebook or {}).get("sort") or 0
        read_info = None
        if entry.get("kind") == "电子书" and (
            entry.get("read_update_time") or entry.get("finished") or notebook
        ):
            read_info = get_read_info(book_id)
        if entry.get("kind") != "电子书":
            read_info = None
        if read_info and read_info.get("finishedDate"):
            entry["read_update_time"] = read_info["finishedDate"]
        existing = find_template_page("书架", "BookId", book_id)
        if entry.get("kind") == "电子书" and (
            "ISBN" in template_source("书架")["property_types"]
            or "评分" in template_source("书架")["property_types"]
        ) and (existing is None or sort > latest_sort):
            entry["isbn"], entry["rating"] = get_bookinfo(book_id)
        periods = ensure_template_periods(entry.get("read_update_time"))
        book_page_id, created = upsert_template_page(
            "书架",
            "BookId",
            book_id,
            template_book_properties(entry, sort, read_info, periods),
        )
        counts["books"] += 1
        should_sync_details = bool(notebook and (created or sort > latest_sort))
        if should_sync_details and entry.get("kind") == "电子书":
            highlights, notes, chapters = sync_template_annotations(book_id, book_page_id)
            counts["highlights"] += highlights
            counts["notes"] += notes
            counts["chapters"] += chapters
    print(
        "正式模板同步完成："
        f"日统计 {day_count}，书籍 {counts['books']}，划线 {counts['highlights']}，"
        f"笔记 {counts['notes']}，章节 {counts['chapters']}"
    )


def resolve_data_source_id(notion_id):
    if os.getenv("NOTION_DATA_SOURCE_ID"):
        return notion_id

    try:
        client.request(path=f"data_sources/{notion_id}", method="GET")
        return notion_id
    except APIResponseError as error:
        code = getattr(error.code, "value", error.code)
        if code not in {"object_not_found", "validation_error"}:
            raise

    database = client.request(path=f"databases/{notion_id}", method="GET")
    sources = database.get("data_sources") or []
    if not sources:
        raise Exception(f"数据库 {notion_id} 下没有可用的 data source")
    if len(sources) > 1:
        print(
            f"数据库 {notion_id} 包含 {len(sources)} 个 data sources，默认使用第一个: {sources[0].get('id')}"
        )
    return sources[0]["id"]


def sync_book_content(book_page_id, book_id, sync_mode):
    chapter = get_chapter_info(book_id)
    bookmark_list = get_bookmark_list(book_id)
    summary, reviews = get_review_list(book_id)
    bookmark_list.extend(reviews)
    bookmark_list = sorted(
        bookmark_list,
        key=lambda item: get_note_sort_key(item, chapter),
    )
    children, grandchild = get_children(chapter, summary, bookmark_list)
    legacy_sync_page_id = None
    if sync_mode == SYNC_MODE_SAFE:
        legacy_sync_page_id = find_legacy_sync_page(book_page_id)
        results = sync_managed_page_content(book_page_id, children)
    else:
        results = add_children(book_page_id, children)
    if grandchild and results is not None:
        add_grandchild(grandchild, results)
    if legacy_sync_page_id and results is not None:
        client.blocks.delete(block_id=legacy_sync_page_id)
        print("已移除旧版微信读书同步内容子页面")


def get_notebook_book_id(item):
    return (item.get("book") or item).get("bookId")


def sync_shelf_entries(shelf_entries, notebooks, latest_sort, sync_mode):
    notebook_by_id = {
        str(book_id): item
        for item in notebooks
        if (book_id := get_notebook_book_id(item))
    }
    synced_ids = set()
    for index, entry in enumerate(shelf_entries, start=1):
        book_id = entry["book_id"]
        notebook = notebook_by_id.get(book_id)
        sort = (notebook or {}).get("sort") or 0
        existing_page = find_book_page(book_id)
        should_sync_content = bool(
            notebook and (sort > latest_sort or existing_page is None)
        )
        if sync_mode == SYNC_MODE_REPLACE and should_sync_content:
            check(book_id)
            existing_page = None
        isbn, rating = "", None
        if should_sync_content and has_any_property(("ISBN", "评分")):
            isbn, rating = get_bookinfo(book_id)
        print(f"正在同步书架 {entry['title']}，当前是第 {index}/{len(shelf_entries)} 本")
        book_page_id = insert_to_notion(
            entry["title"],
            book_id,
            entry["cover"],
            sort,
            entry["author"],
            isbn,
            rating,
            entry["categories"],
            existing_page=existing_page,
            shelf_metadata=entry,
        )
        synced_ids.add(book_id)
        if should_sync_content:
            sync_book_content(book_page_id, book_id, sync_mode)

    for notebook in notebooks:
        book = notebook.get("book") or notebook
        book_id = book.get("bookId")
        if not book_id or str(book_id) in synced_ids:
            continue
        sort = notebook.get("sort") or 0
        existing_page = find_book_page(book_id)
        if sort <= latest_sort and existing_page is not None:
            continue
        if sync_mode == SYNC_MODE_REPLACE:
            check(book_id)
            existing_page = None
        title = book.get("title") or "未命名书籍"
        cover = (book.get("cover") or "").replace("/s_", "/t7_")
        categories = normalize_categories(book.get("categories"))
        isbn, rating = (get_bookinfo(book_id) if has_any_property(("ISBN", "评分")) else ("", None))
        book_page_id = insert_to_notion(
            title,
            book_id,
            cover,
            sort,
            book.get("author") or "",
            isbn,
            rating,
            categories,
            existing_page=existing_page,
        )
        sync_book_content(book_page_id, book_id, sync_mode)


def sync():
    global client, data_source_id, weread
    secrets = validate_secret_inputs()
    sync_mode = get_sync_mode()
    notion_id = extract_notion_id()
    notion_token = secrets["notion_token"]
    weread = WeReadGatewayClient(secrets["weread_api_key"])
    client = Client(
        auth=notion_token,
        log_level=logging.ERROR,
        notion_version=NOTION_VERSION,
    )
    try:
        discovered_template = discover_template_sources(notion_id)
    except APIResponseError as error:
        code = getattr(error.code, "value", error.code)
        if code not in {"object_not_found", "validation_error"}:
            raise
        discovered_template = {}
    if discovered_template:
        print(f"已识别正式微信读书模板：{len(discovered_template)} 个数据库")
        sync_template_workspace(notion_id)
        return
    data_source_id = resolve_data_source_id(notion_id)
    print(f"Notion API Version: {NOTION_VERSION}")
    print(f"Notion Data Source ID: {data_source_id}")
    print(f"Notion Sync Mode: {sync_mode}")
    load_data_source_schema()
    latest_sort = get_sort()
    notebooks = get_notebooklist()
    shelf = get_shelf()
    shelf_entries = get_shelf_entries(shelf)
    print(f"已读取完整书架：{len(shelf_entries)} 个条目")
    sync_shelf_entries(shelf_entries, notebooks, latest_sort, sync_mode)

    report_page_id = extract_optional_notion_id(
        secrets["notion_report_page"], "NOTION_REPORT_PAGE"
    )
    if report_page_id:
        sync_reading_reports(report_page_id, shelf_entries, notebooks)
    else:
        print("未配置 NOTION_REPORT_PAGE，跳过阅读日报和阅读看板同步")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="weread2notion",
        description="Sync WeRead highlights and notes to Notion.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="sync",
        choices=["sync"],
        help="Command to run. Defaults to sync.",
    )
    parser.parse_args(argv)
    try:
        sync()
    except ConfigError:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

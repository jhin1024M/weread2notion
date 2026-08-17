import re


def get_rich_text_segments(content, marker=None):
    segments = [
        {
            "type": "text",
            "text": {
                "content": content,
            },
        }
    ]
    if marker:
        segments.append(
            {
                "type": "text",
                "text": {
                    "content": marker,
                },
            }
        )
    return segments


def get_heading(level, content, marker=None):
    if level == 1:
        heading = "heading_1"
    elif level == 2:
        heading = "heading_2"
    else:
        heading = "heading_3"
    return {
        "type": heading,
        heading: {
            "rich_text": get_rich_text_segments(content, marker),
            "color": "default",
            "is_toggleable": False,
        },
    }


def get_toggle(content):
    return {
        "type": "toggle",
        "toggle": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": content,
                    },
                }
            ],
            "color": "default",
        },
    }


def get_table_of_contents():
    """获取目录"""
    return {"type": "table_of_contents", "table_of_contents": {"color": "default"}}


def get_title(content):
    return {"title": [{"type": "text", "text": {"content": content}}]}


def get_rich_text(content):
    return {"rich_text": [{"type": "text", "text": {"content": content}}]}


def get_url(url):
    return {"url": url}


def get_file(url):
    return {"files": [{"type": "external", "name": "Cover", "external": {"url": url}}]}


def get_multi_select(names):
    return {"multi_select": [{"name": name} for name in names]}


def get_date(start):
    date = {"start": start}
    value = str(start)
    has_utc_offset = value.endswith("Z") or bool(re.search(r"[+-]\d{2}:\d{2}$", value))
    if "T" in value and not has_utc_offset:
        date["time_zone"] = "Asia/Shanghai"
    return {"date": date}


def get_icon(url):
    return {"type": "external", "external": {"url": url}}


def get_select(name):
    return {"select": {"name": name}}


def get_status(name):
    return {"status": {"name": name}}


def get_number(number):
    return {"number": number}


def get_quote(content, marker=None):
    return {
        "type": "quote",
        "quote": {
            "rich_text": get_rich_text_segments(content, marker),
            "color": "default",
        },
    }


def get_callout(content, icon=None, marker=None):
    callout = {
        "rich_text": get_rich_text_segments(content, marker),
    }
    if icon:
        callout["icon"] = {"type": "emoji", "emoji": icon}
    return {
        "type": "callout",
        "callout": callout,
    }

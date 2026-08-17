# WeRead2Notion

将微信读书的书籍、划线和笔记同步到 Notion。

本项目使用微信读书 API Key 读取数据，并通过 GitHub Actions 定时同步到 Notion。新版不再需要复制微信读书 Cookie。

预览效果：https://malinkang.notion.site/weread2notion

> [!IMPORTANT]
> 默认使用 `safe` 安全同步模式：书籍页面会保留，微信读书自动同步内容会放在页面内的“微信读书同步内容”折叠区中，页面里的个人笔记不会被同步覆盖。
>
> 如需使用旧版“删除后重建”行为，可在 Action 中设置 `notion-sync-mode: replace`。旧模式下请不要在同步生成的书籍页面里添加重要内容。

## 同步模式

| 模式 | 行为 | 建议 |
| --- | --- | --- |
| `safe`（默认） | 更新书籍属性，并只重建“微信读书同步内容”折叠区 | 日常使用 |
| `replace` | 删除原书籍页面后重新创建 | 兼容旧模板或需要重置数据时使用 |

安全模式第一次运行时，旧版本已经生成的书籍内容会保留，同时创建新的同步折叠区；确认折叠区内容正确后，可以手动整理旧内容。旧版安全同步生成的同名子页面会在新内容写入成功后自动移除。

## 使用文档

完整教程请查看：

https://www.notionhub.app/docs/weread2notion.html

文档里包含：

- Notion 模板复制和授权
- 微信读书 API Key 获取
- GitHub Fork 和 Actions 配置
- 常见问题排查

## 关注公众号

如果你想获取后续更新，或了解更多 Notion 自动化工具，欢迎关注公众号：**Notion自动化**。

![公众号：Notion自动化](https://cdn.notionhub.app/notionhub/gzh.jpg)

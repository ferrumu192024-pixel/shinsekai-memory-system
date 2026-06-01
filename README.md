# shinsekai-memory-system
长期记忆插件：自动归档、每日日记、随机模糊回忆
# Shinsekai Memory System - 新世界记忆系统

## 这是什么？

一个为 AI 桌宠/角色扮演系统设计的长期记忆管理插件。解决的问题很简单：**AI 的记忆窗口有限，聊久了就会忘记以前说过的话。** 这个插件让 AI 拥有"长期记忆"——自动归档旧对话、生成日记、并在合适的时机随机回忆起过去的事情。

## 它能做什么？

- **自动归档**：旧对话不会被删除，而是存成 JSON 文件，随时可查
- **每日日记**：AI 每天自动写一份日记，记录当天聊了什么、关键词、情绪
- **精准搜索**：你可以直接问 AI "我们上次聊 xx 是什么时候"，它会翻历史回答你
- **随机回忆**：AI 偶尔会自动想起过去的事，像人一样突然说"说起来，上次你也……"

## 安装与使用

### 安装
1. 将 `plugins/memory_system/` 复制到你的 Shinsekai 项目的 `plugins/` 目录下
2. 在 `data/config/plugins.yaml` 中添加：
   ```yaml
   - entry: plugins.memory_system.plugin:MemorySystemPlugin
     enabled: true

# QQ 机器人控制台

一个开箱即用的 QQ 机器人。双击启动，浏览器界面管理一切。

## 快速开始

**双击 `dist\QQBot\QQBot.exe`**（或 `dist\启动QQ机器人.bat`）

exe 会自动启动后台服务并打开控制台窗口，无需安装 Python。

如果 exe 在你机器上被杀毒软件拦截（窗口空白/闪退），
改用源码方式：双击根目录的 `启动QQ机器人.bat`，
它会自动检查 Python 和依赖，然后打开控制台。

需要准备：
- 官方 QQ（NTQQ 版）并已登录
- 源码方式另需 Python 3.9+（[官网下载](https://www.python.org/downloads/)，安装时勾选 "Add Python to PATH"）

## 功能

- **一键启动**：自动拉起网关 → 机器人 → NapCat
- **首次引导**：没配 API Key 时弹窗引导
- **一键配置**：选模型来源 → 填 Key → 自动测试
- **6 种人格**：损友 / 学妹 / 技术宅 / 管家 / 甜美女友 / 正经助手
- **人格可增删**：编辑提示词，添加自定义人格
- **表情包管理**：22 类 129 张，可新建/删除分类
- **聊天记录**：按会话查看历史对话
- **长期记忆**：记住关于你的事实和当前情绪
- **视觉能力**：用户发图会自动理解
- **联网搜索**：问实时信息时自动搜索
- **深浅主题**：右下角切换
- **配置导入导出**：换电脑一键迁移（不含 API Key）

## 界面说明

启动后浏览器会打开 `http://127.0.0.1:8789`，左侧导航：

| 页面 | 作用 |
|---|---|
| 运行状态 | 各组件状态 + 启动/停止按钮 |
| 一键配置 | 首次配置模型 |
| 机器人风格 | 切换/编辑人格 |
| 模型与网关 | 高级模型配置 |
| 记忆管理 | 查看和清空记忆 |
| 表情包 | 管理表情分类 |
| 聊天记录 | 查看对话历史 |
| 运行日志 | 实时日志 |
| 设置 | 版本、诊断、备份 |
| 使用指南 | 详细步骤 |

## 键盘快捷键

| 按键 | 作用 |
|---|---|
| `1` ~ `0` | 切换页面 |
| `Ctrl+S` | 启动全部 |
| `Ctrl+D` | 停止全部 |
| `Ctrl+T` | 测试模型 |
| `Ctrl+K` | 一键配置 |
| `Ctrl+J` | 切换主题 |
| `Esc` | 关闭弹窗 |

## 目录结构

```
启动QQ机器人.bat     ← 双击这个
stop.bat             停止全部进程
console.py           控制台服务
crashguard.py        崩溃捕获
updater.py           更新检查

config/
  bot.yaml           主配置
  secrets.yaml       API Key（不要分享）
  local.yaml         本机路径（不要分享）

core/
  bot.py             机器人核心
  memory.py          长期记忆
  webskill.py        联网搜索

data/
  personas.yaml      人格库
  memes.yaml         热梗库
  stickers/          表情包（22类129张）
  memory/            聊天记忆

web/index.html       控制台界面
assets/              图标
tools/               NapCat / 网关 安装脚本
logs/                运行日志
```

## 配置 NapCat

双击 `tools\setup-napcat.bat`，会自动下载 NapCat 并写入反向 WebSocket 配置
（连接本机 3001 端口）。

装好后 NapCat 的 WebUI 在 `http://127.0.0.1:6099/webui`，
用户名 `qqbot123`。

## 配置模型

**方式一：WorkBuddy 网关（免费额度）**
1. 运行 `tools\setup-gateway.bat`
2. 打开看板 `http://127.0.0.1:8788/`，密码 `admin`
3. 添加账号 → 生成 API Key
4. 填进控制台「一键配置」

**方式二：任意 OpenAI 兼容接口**
控制台「一键配置」→ 选择「自定义」→ 填 Base URL 和模型名。

## 分享给别人

打包时排除这些：

```
config/secrets.yaml    ← 你的 API Key
config/local.yaml      ← 你的本机路径
data/memory/           ← 你的聊天记忆
logs/                  ← 日志
__pycache__/
```

对方拿到后：装 Python → 双击 `启动QQ机器人.bat` → 按引导配置。

## 常见问题

**双击没反应**
右键 `启动QQ机器人.bat` → 编辑，看第一行的 Python 路径是否正确。
或者手动运行：`python console.py`

**提示找不到 Python**
去 [python.org](https://www.python.org/downloads/) 下载安装，
**安装时务必勾选 "Add Python to PATH"**。

**浏览器没自动打开**
手动访问 `http://127.0.0.1:8789`

**页面打不开**
检查命令行窗口是否有报错。端口被占用的话先运行 `stop.bat`。

**模型报 429**
网关限流，会自动切换备用模型。在权限页可以改备用列表。

---

## 技术说明

### 两种启动方式

| 方式 | 入口 | 说明 |
|---|---|---|
| **exe（推荐）** | `dist\QQBot\QQBot.exe` 或 `dist\启动QQ机器人.bat` | 单文件目录版，双击即用 |
| 源码 | 根目录 `启动QQ机器人.bat` | 需要 Python 3.9+，自动装依赖 |

两种方式功能完全相同。exe 是 PyInstaller **onedir** 模式打包的
（文件夹版，不是单文件版）。

### 窗口是怎么打开的

程序用系统浏览器（Chrome 优先，其次 Edge）的 `--app` 模式打开控制台：
无地址栏、无标签页，看起来像一个独立桌面窗口。

三条经过实战验证的原则：

1. **已有窗口只置前，不再开新的** —— 避免"窗口反复弹出"
2. **只启动一次，不检测、不 kill、不重试** —— 避免窗口闪退循环
3. **用浏览器默认 profile，不自建** —— 自建 profile 容易损坏导致白屏

窗口出现后，后台线程会把标题里的 `127.0.0.1:8789` 改成「QQ 机器人」。

### 踩过的坑（供参考）

- **单文件 exe（onefile）不能用**：PyInstaller 单文件版启动时要向
  `%TEMP%` 解压，在部分被安全软件/沙箱接管的环境里会弹
  `Could not create temporary directory!` 然后退出。onedir 不需要解压。
- **杀毒软件会拦截 exe 的子进程**：腾讯电脑管家等开启时，exe 启动的
  浏览器无法创建渲染进程（窗口全白）。关闭杀毒或加白名单后正常。
- **自建浏览器 profile 会损坏**：残留 `exit_type: Crashed` 等状态时
  新窗口直接白屏。用默认 profile 没有这个问题。

### 日志位置

- exe 模式：`dist\QQBot\logs\app.log`
- 源码模式：`logs\app.log`

出问题先看这个文件。

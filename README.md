<p align="center">
  <img src="https://raw.githubusercontent.com/can1357/notionfs/main/assets/banner.png" alt="NotionFS">
</p>

<p align="center">
  <strong>
    Local-first sync for <a href="https://notion.so">Notion</a>. Work with pages as markdown files.
  </strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/notionfs/"><img src="https://img.shields.io/pypi/v/notionfs?style=flat&colorA=18181B&colorB=3b82f6" alt="PyPI version"></a>
  <a href="https://github.com/can1357/notionfs/actions"><img src="https://img.shields.io/github/actions/workflow/status/can1357/notionfs/ci.yml?style=flat&colorA=18181B" alt="CI"></a>
  <a href="https://github.com/can1357/notionfs/blob/main/LICENSE"><img src="https://img.shields.io/github/license/can1357/notionfs?style=flat&colorA=18181B" alt="License"></a>
</p>

---

## Features

| Feature                    | Description                                            |
| -------------------------- | ------------------------------------------------------ |
| **Bidirectional Sync**     | Push local changes to Notion, pull remote changes down |
| **Markdown Native**        | Edit pages as `.md` files with full formatting support |
| **Frontmatter Properties** | Database properties sync via YAML frontmatter          |
| **Conflict Detection**     | Smart detection with multiple resolution strategies    |
| **Watch Mode**             | Continuous sync with file watching and remote polling  |
| **Offline First**          | Work without internet, sync when ready                 |

---

## Installation

```bash
pip install notionfs
```

## Quick Start

```bash
# Set your Notion API token
export NOTION_TOKEN="secret_..."

# Initialize a workspace (interactive page selection)
notionfs clone

# Or from a URL directly
notionfs clone https://www.notion.so/My-Page-abc123...

# Sync your changes
notionfs pull    # Download from Notion
notionfs push    # Upload to Notion
notionfs sync    # Bidirectional sync
```

## Commands

| Command                   | Description                                        |
| ------------------------- | -------------------------------------------------- |
| `notionfs clone [URL]`    | Clone a Notion page or database to local workspace |
| `notionfs pull`           | Download remote changes from Notion                |
| `notionfs push`           | Upload local changes to Notion                     |
| `notionfs sync`           | Bidirectional sync (pull then push)                |
| `notionfs status`         | Show pending changes and conflicts                 |
| `notionfs watch`          | Continuous sync with file watching                 |
| `notionfs resolve <file>` | Resolve sync conflicts                             |
| `notionfs auth [token]`   | Manage Notion API token                            |
| `notionfs list`           | List initialized workspaces                        |

## Workspace Structure

```
my-workspace/
├── .notionfs/
│   ├── config.toml       # Workspace config
│   └── state.db          # Sync state
├── Page Title.md         # Leaf page
├── Parent Page/          # Page with children -> directory
│   ├── _index.md         # Parent page content
│   └── Child Page.md
└── Database Name/        # Database -> directory
    ├── Entry 1.md
    └── Entry 2.md
```

## Markdown Format

Pages are markdown with YAML frontmatter for database properties:

```markdown
---
Status: In Progress
Tags:
  - work
  - important
Due Date: 2024-03-15
---

# Meeting Notes

Your content here.

- Bullet points
- [ ] Todo items
- [x] Completed items

> Quotes work too
```

## Conflict Resolution

```bash
# Check for conflicts
notionfs status

# Resolve conflicts
notionfs resolve "Page.md" --keep-local   # Your version wins
notionfs resolve "Page.md" --keep-remote  # Notion version wins
notionfs resolve "Page.md" --keep-both    # Keep both versions
```

## Watch Mode

```bash
# Continuous sync (2s local debounce, 30s remote poll)
notionfs watch

# Custom intervals
notionfs watch -d 1 -i 60    # 1s debounce, 60s poll
```

## Configuration

Token lookup order:

1. `NOTION_TOKEN` environment variable
2. `~/.notionfs/config.toml` file

```bash
# Save token permanently
notionfs auth secret_...
```

## Tips & Tricks

```bash
# Search across all notes
grep -r "TODO" .

# Batch edit
find . -name "*.md" -exec sed -i 's/old/new/g' {} \;
notionfs push

# Git integration (version history for your notes)
git init && echo ".notionfs/" >> .gitignore
```

## Troubleshooting

```bash
# Debug mode
notionfs --debug pull
notionfs --debug push

# Page not accessible?
# -> Add your integration via page menu -> Connections
```

## License

MIT

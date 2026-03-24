#!/usr/bin/env bash
#
# BMAD Autopilot Installer
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-$(pwd)}"
AUTOPILOT_DIR="$TARGET_DIR/.autopilot"
BACKUP_DIR="$AUTOPILOT_DIR/backup"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"

if command -v realpath >/dev/null 2>&1; then
  SCRIPT_REALPATH="$(realpath "$SCRIPT_DIR")"
  TARGET_REALPATH="$(realpath -m "$AUTOPILOT_DIR")"
  if [[ "$TARGET_REALPATH" == "$SCRIPT_REALPATH" ]]; then
    echo "❌ Refusing to install into the autopilot source checkout itself."
    echo "   Choose a different target repo root."
    exit 1
  fi
fi

echo "🚀 BMAD Autopilot Installer v0.1.0"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

echo "📋 Checking prerequisites..."
missing=()
for cmd in python3 git gh codex zip; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    missing+=("$cmd")
  fi
done

if [ ${#missing[@]} -gt 0 ]; then
  echo "❌ Missing required commands: ${missing[*]}"
  echo "Install them first and try again."
  exit 1
fi
echo "✅ All prerequisites found"
echo ""

BACKUP_CREATED=false
backup_file() {
  local file="$1"
  if [ -f "$file" ]; then
    mkdir -p "$BACKUP_DIR"
    local backup_zip="$BACKUP_DIR/${TIMESTAMP}.zip"
    if [ "$BACKUP_CREATED" = false ]; then
      echo "⚠️  Found existing files, creating backup..."
      BACKUP_CREATED=true
    fi
    echo "   → $(basename "$file")"
    zip -q -u "$backup_zip" "$file" 2>/dev/null || zip -q "$backup_zip" "$file"
  fi
}

echo "🔍 Checking for existing installation..."
backup_file "$AUTOPILOT_DIR/bmad-autopilot.sh"
backup_file "$AUTOPILOT_DIR/bmad-autopilot.py"
backup_file "$AUTOPILOT_DIR/config"
backup_file "$AUTOPILOT_DIR/config.example"
backup_file "$TARGET_DIR/.claude/commands/autopilot.md"
backup_file "$TARGET_DIR/.claude/commands/bmad-autopilot.md"
if [ -f "$TARGET_DIR/.claude/skills/bmad-autopilot/SKILL.md" ]; then
  backup_file "$TARGET_DIR/.claude/skills/bmad-autopilot/SKILL.md"
fi
backup_file "$HOME/.claude/commands/autopilot.md"
backup_file "$HOME/.claude/commands/bmad-autopilot.md"
if [ -f "$HOME/.claude/skills/bmad-autopilot/SKILL.md" ]; then
  backup_file "$HOME/.claude/skills/bmad-autopilot/SKILL.md"
fi

if [ "$BACKUP_CREATED" = true ]; then
  echo "✅ Backup created: $BACKUP_DIR/${TIMESTAMP}.zip"
else
  echo "✅ No existing installation found"
fi
echo ""

echo "📁 Installing to: $AUTOPILOT_DIR/"
mkdir -p "$AUTOPILOT_DIR"
cp "$SCRIPT_DIR/scripts/bmad-autopilot.sh" "$AUTOPILOT_DIR/"
cp "$SCRIPT_DIR/scripts/bmad-autopilot.py" "$AUTOPILOT_DIR/"
cp "$SCRIPT_DIR/config.example" "$AUTOPILOT_DIR/config.example"
chmod +x "$AUTOPILOT_DIR/bmad-autopilot.sh"
chmod +x "$AUTOPILOT_DIR/bmad-autopilot.py"
echo "✅ Main launcher installed"
echo "✅ Python runner installed"
echo "✅ Config example installed"

echo ""
if [ -f "$AUTOPILOT_DIR/config" ]; then
  echo "ℹ️  Config file already exists at $AUTOPILOT_DIR/config"
else
  read -p "⚙️  Create config file from example? [y/N] " -n 1 -r
  echo ""
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    cp "$SCRIPT_DIR/config.example" "$AUTOPILOT_DIR/config"
    echo "✅ Config file created (edit .autopilot/config to customize)"
  else
    echo "⏭️  Skipped config (copy config.example to config when ready)"
  fi
fi

echo ""
read -p "📦 Install optional legacy BMAD command templates? [y/N] " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
  if [ -f "$TARGET_DIR/.claude/commands/autopilot.md" ]; then
    COMMANDS_DIR="$TARGET_DIR/.claude/commands"
  elif [ -f "$HOME/.claude/commands/autopilot.md" ]; then
    COMMANDS_DIR="$HOME/.claude/commands"
  else
    echo "Where to install legacy commands?"
    echo "  1) Local  - $TARGET_DIR/.claude/commands (recommended)"
    echo "  2) Global - ~/.claude/commands"
    read -p "Choose [1/2] (default: 1): " -n 1 -r
    echo ""
    if [[ $REPLY = "2" ]]; then
      COMMANDS_DIR="$HOME/.claude/commands"
      echo "Installing to global ~/.claude/commands/"
    else
      COMMANDS_DIR="$TARGET_DIR/.claude/commands"
      echo "Installing to local .claude/commands/"
    fi
  fi

  mkdir -p "$COMMANDS_DIR"
  cp "$SCRIPT_DIR/commands/"*.md "$COMMANDS_DIR/"
  echo "✅ Legacy command templates installed to $COMMANDS_DIR/"

  if [ "$COMMANDS_DIR" = "$TARGET_DIR/.claude/commands" ]; then
    SKILLS_DIR="$TARGET_DIR/.claude/skills"
  else
    SKILLS_DIR="$HOME/.claude/skills"
  fi

  if [ -d "$SCRIPT_DIR/skills" ]; then
    mkdir -p "$SKILLS_DIR"
    cp -r "$SCRIPT_DIR/skills/"* "$SKILLS_DIR/"
    echo "✅ Skills installed to $SKILLS_DIR/"
  fi
else
  echo "⏭️  Skipped optional command templates"
fi

echo ""
if [ -d "$SCRIPT_DIR/workflows" ]; then
  WORKFLOWS_DIR="$TARGET_DIR/.github/workflows"
  if [ -f "$WORKFLOWS_DIR/auto-approve.yml" ]; then
    read -p "🔄 Update auto-approve workflow? [y/N] " -n 1 -r
    echo ""
    INSTALL_WORKFLOW=$([[ $REPLY =~ ^[Yy]$ ]] && echo "yes" || echo "no")
  else
    read -p "🤖 Install auto-approve GitHub workflow? [y/N] " -n 1 -r
    echo ""
    INSTALL_WORKFLOW=$([[ $REPLY =~ ^[Yy]$ ]] && echo "yes" || echo "no")
  fi

  if [ "$INSTALL_WORKFLOW" = "yes" ]; then
    mkdir -p "$WORKFLOWS_DIR"
    cp "$SCRIPT_DIR/workflows/auto-approve.yml" "$WORKFLOWS_DIR/"
    echo "✅ GitHub workflow installed to $WORKFLOWS_DIR/auto-approve.yml"
  else
    echo "⏭️  Skipped GitHub workflow"
  fi
fi

echo ""
if [ -f "$TARGET_DIR/.gitignore" ]; then
  if ! grep -q "^\.autopilot/$" "$TARGET_DIR/.gitignore" 2>/dev/null; then
    echo "" >> "$TARGET_DIR/.gitignore"
    echo "# BMAD Autopilot (local)" >> "$TARGET_DIR/.gitignore"
    echo ".autopilot/" >> "$TARGET_DIR/.gitignore"
    echo "✅ Added .autopilot/ to .gitignore"
  else
    echo "✅ .autopilot/ already in .gitignore"
  fi
else
  echo "⚠️  No .gitignore found - consider adding .autopilot/ to it"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎉 Installation complete!"
echo ""
echo "Usage:"
echo "  cd $TARGET_DIR"
echo "  ./.autopilot/bmad-autopilot.sh           # process all epics"
echo "  ./.autopilot/bmad-autopilot.sh \"7A 8A\"   # specific epics"
echo "  ./.autopilot/bmad-autopilot.sh           # resume by default"
echo "  ./.autopilot/bmad-autopilot.sh --no-continue # force a fresh start"
echo ""

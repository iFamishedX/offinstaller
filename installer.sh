#!/usr/bin/env bash
set -euo pipefail

# Colors
RED='\e[31m'; GREEN='\e[32m'; YELLOW='\e[33m'; BLUE='\e[34m'; BOLD='\e[1m'; RESET='\e[0m'

MODRINTH_SLUG="optifine-for-fabric"
MODRINTH_PROJECT_URL="https://modrinth.com/modpack/${MODRINTH_SLUG}"
MODRINTH_API="https://api.modrinth.com/v2/project/${MODRINTH_SLUG}/version"

command -v curl >/dev/null || { echo -e "${RED}curl is required. Install curl and retry.${RESET}"; exit 1; }
command -v jq >/dev/null || { echo -e "${YELLOW}jq not found. Install jq for JSON parsing (recommended).${RESET}"; }
FZF_AVAILABLE=0; command -v fzf >/dev/null && FZF_AVAILABLE=1

echo -e "${BOLD}${BLUE}Welcome to OptiFine for Fabric installer (CLI)${RESET}"
echo

PS3=$'\n'"Choose platform: "
options=("Modrinth Launcher (open modpack in Modrinth app)" "Minecraft Launcher (install locally)" "Quit")
select opt in "${options[@]}"; do
  case $REPLY in
    1)
      echo -e "${GREEN}Opening Modrinth modpack in your system (Modrinth app should handle it)...${RESET}"
      # Try modrinth:// scheme first, fallback to web URL
      if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "modrinth://modpack/${MODRINTH_SLUG}" 2>/dev/null || xdg-open "${MODRINTH_PROJECT_URL}"
      elif command -v open >/dev/null 2>&1; then
        open "${MODRINTH_PROJECT_URL}"
      else
        echo -e "${YELLOW}Could not auto-open. Visit:${RESET} ${BLUE}${MODRINTH_PROJECT_URL}${RESET}"
      fi
      echo -e "${GREEN}Done. Closing installer.${RESET}"
      exit 0
      ;;
    2)
      echo -e "${BLUE}Minecraft launcher selected.${RESET}"
      read -rp "Enter Minecraft directory (leave empty for default ~/.minecraft): " MCDIR
      MCDIR=${MCDIR:-"$HOME/.minecraft"}
      echo -e "Using Minecraft directory: ${BOLD}${MCDIR}${RESET}"
      read -rp "Confirm installation into ${MCDIR}? (y/N): " confirm
      if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "${RED}Can't proceed with installation. Closing in 5s...${RESET}"
        sleep 5
        exit 1
      fi
      mkdir -p "${MCDIR}/mods" "${MCDIR}/.optifine-installer" || true
      echo -e "${GREEN}Fetching available versions from Modrinth...${RESET}"
      ;;
    3)
      echo -e "${YELLOW}Quitting.${RESET}"
      exit 0
      ;;
    *)
      echo "Invalid option."
      ;;
  esac
done

# Fetch versions (requires jq)
VERSIONS_JSON=$(curl -s "${MODRINTH_API}")
if [ -z "$VERSIONS_JSON" ]; then
  echo -e "${RED}Failed to fetch versions from Modrinth API.${RESET}"
  exit 1
fi

# Parse into a simple list: id | version_number | game_versions | loaders
if command -v jq >/dev/null 2>&1; then
  mapfile -t choices < <(echo "$VERSIONS_JSON" | jq -r '.[] | "\(.id) | \(.version_number) | \(.game_versions|join(",")) | \(.loaders|join(","))"')
else
  # Fallback: crude parsing
  choices=()
  while read -r line; do choices+=("$line"); done < <(echo "$VERSIONS_JSON" | sed -n '1,200p')
fi

if [ ${#choices[@]} -eq 0 ]; then
  echo -e "${RED}No versions found.${RESET}"
  exit 1
fi

echo -e "${BOLD}Select a version to install:${RESET}"
if [ $FZF_AVAILABLE -eq 1 ]; then
  selection=$(printf "%s\n" "${choices[@]}" | fzf --height 40% --border --prompt="Version> ")
else
  # simple numbered menu
  i=1
  for c in "${choices[@]}"; do
    echo "[$i] $c"
    ((i++))
  done
  read -rp "Enter number: " num
  selection="${choices[$((num-1))]}"
fi

if [ -z "$selection" ]; then
  echo -e "${RED}No selection made. Exiting.${RESET}"
  exit 1
fi

VER_ID=$(echo "$selection" | cut -d'|' -f1 | xargs)
echo -e "${GREEN}Selected version id: ${VER_ID}${RESET}"

# Get files for version
FILES_JSON=$(curl -s "https://api.modrinth.com/v2/version/${VER_ID}")
file_url=$(echo "$FILES_JSON" | jq -r '.files[0].url // empty')
file_name=$(basename "$file_url")

if [ -z "$file_url" ]; then
  echo -e "${RED}No downloadable file found for this version.${RESET}"
  exit 1
fi

echo -e "${BLUE}Downloading ${file_name}...${RESET}"
curl -L -o "/tmp/${file_name}" "$file_url"

echo -e "${GREEN}Placing into ${MCDIR}/mods/${file_name}${RESET}"
mv "/tmp/${file_name}" "${MCDIR}/mods/"

echo -e "${GREEN}Installation complete. Launch Minecraft with Fabric loader to use OptiFine for Fabric.${RESET}"
echo -e "${YELLOW}Modrinth page:${RESET} ${BLUE}${MODRINTH_PROJECT_URL}${RESET}"

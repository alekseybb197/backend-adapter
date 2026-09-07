#!/bin/bash
set -uo pipefail
input=$(cat)

if ! command -v jq >/dev/null 2>&1; then
  echo "[statusline: jq not found]"
  exit 0
fi

IFS=$'\x1f' read -r MODEL PCT_RAW IN_TOK OUT_TOK GIT_BRANCH RL5H RL7D CUR_DIR <<< "$(
  echo "$input" | jq -r '
    [
      (.model.display_name // "?" | sub("^.*/"; "")),
      (.context_window.used_percentage // 0),
      (.context_window.total_input_tokens // 0),
      (.context_window.total_output_tokens // 0),
      (.worktree.branch // ""),
      (.rate_limits.five_hour.used_percentage // ""),
      (.rate_limits.seven_day.used_percentage // ""),
      (.workspace.current_dir // "")
    ] | join("\u001f")
  '
)"

if [ -z "$CUR_DIR" ] && [ -z "$MODEL" ]; then
  echo "[statusline: jq parse failed]"
  exit 0
fi

WIDTH="${COLUMNS:-}"
if [ -z "$WIDTH" ]; then
  WIDTH=$(tput cols 2>/dev/null)
fi
[ -z "$WIDTH" ] && WIDTH=100
[ "$WIDTH" -lt 40 ] && WIDTH=40

PCT=${PCT_RAW%%.*}
[ -z "$PCT" ] && PCT=0

if [ -z "$GIT_BRANCH" ]; then
  GIT_BRANCH=$(git -C "$CUR_DIR" branch --show-current 2>/dev/null)
  [ -z "$GIT_BRANCH" ] && GIT_BRANCH="no-git"
fi

MAX_BRANCH=18
if [ "${#GIT_BRANCH}" -gt "$MAX_BRANCH" ]; then
  GIT_BRANCH="${GIT_BRANCH:0:$((MAX_BRANCH-1))}…"
fi

if   [ "$PCT" -lt 70 ]; then COLOR="\033[32m"
elif [ "$PCT" -lt 90 ]; then COLOR="\033[33m"
else                          COLOR="\033[31m"
fi
RESET="\033[0m"

fmt_tokens() {
  local n=$1
  if   [ "$n" -ge 1000000 ]; then awk -v n="$n" 'BEGIN{printf "%.1fM", n/1000000}'
  elif [ "$n" -ge 1000 ];    then awk -v n="$n" 'BEGIN{printf "%.1fk", n/1000}'
  else printf '%d' "$n"
  fi
}
IN_FMT=$(fmt_tokens "${IN_TOK%%.*}")   # отправлено модели (prompt/context)
OUT_FMT=$(fmt_tokens "${OUT_TOK%%.*}") # получено от модели (completion)

RL_SEGMENT=""
if [ -n "$RL5H" ]; then
  RL_SEGMENT=" | 5h:${RL5H%%.*}%"
  [ -n "$RL7D" ] && RL_SEGMENT+=" 7d:${RL7D%%.*}%"
fi

FIXED_PART="[${MODEL}] ${GIT_BRANCH} |  ${PCT}% | ↑${IN_FMT} ↓${OUT_FMT}${RL_SEGMENT}"
FIXED_LEN=${#FIXED_PART}

BAR_LEN=$(( WIDTH - FIXED_LEN - 2 ))
[ "$BAR_LEN" -gt 16 ] && BAR_LEN=16
[ "$BAR_LEN" -lt 6 ]  && BAR_LEN=6

if [ "$BAR_LEN" -eq 6 ] && [ -n "$RL_SEGMENT" ]; then
  RL_SEGMENT=""
fi

FILLED=$(( PCT * BAR_LEN / 100 ))
(( FILLED > BAR_LEN )) && FILLED=$BAR_LEN
EMPTY=$(( BAR_LEN - FILLED ))
BAR=""
[ "$FILLED" -gt 0 ] && BAR+=$(printf '█%.0s' $(seq 1 "$FILLED"))
[ "$EMPTY"  -gt 0 ] && BAR+=$(printf '░%.0s' $(seq 1 "$EMPTY"))

printf "[%s] %s | ${COLOR}%s %d%%${RESET} | ↑%s ↓%s%s\n" \
  "$MODEL" "$GIT_BRANCH" "$BAR" "$PCT" "$IN_FMT" "$OUT_FMT" "$RL_SEGMENT"

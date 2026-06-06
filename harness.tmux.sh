#!/bin/bash
# ./harness.tmux.sh          → start interactive session
# ./harness.tmux.sh --test   → start + run pytest immediately

SESSION=glasses
REPO=/Users/gerhardgustav/Desktop/hobby-dev/sony-sed-e1

tmux kill-session -t $SESSION 2>/dev/null || true
tmux new-session -d -s $SESSION -x 220 -y 50

# Pane 0 (left): harness TUI
tmux send-keys -t $SESSION:0 "cd $REPO && node harness/dist/index.js" Enter

# Pane 1 (right top): JSON event stream
tmux split-window -t $SESSION:0 -h
tmux send-keys -t $SESSION:0.1 \
  "tail -F /tmp/glasses-events.jsonl | python3 -c \"import sys,json; [print(json.dumps(json.loads(l), separators=(',',':'))) for l in sys.stdin]\" 2>/dev/null" \
  Enter

# Pane 2 (right bottom): test runner
tmux split-window -t $SESSION:0.1 -v
tmux send-keys -t $SESSION:0.2 "cd $REPO && echo 'Ready. Run: pytest tests/ -v'" Enter

if [[ "$1" == "--test" ]]; then
  sleep 3
  tmux send-keys -t $SESSION:0.2 "pytest tests/ -v" Enter
fi

tmux attach -t $SESSION

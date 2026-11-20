"""Update the root handoff's live status while preserving the approved context."""
from __future__ import annotations
import json
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
STATUS=ROOT/'code/docs/progress.json'

def update():
    state=json.loads(STATUS.read_text(encoding='utf-8'))
    tasks=state['tasks'];done=sum(x['status']=='done' for x in tasks)
    lines=['# Buy or Wait? — coding-agent handoff', '',
           'Read this document and the root AGENTS.md before continuing. This is the current handoff; earlier conversation plans may be superseded by the implementation notes below.', '',
           'Updated: '+datetime.now(timezone.utc).isoformat(), '',
           '## Current status', '', state['summary'], '',
           f'**TODO: {done}/{len(tasks)} tasks done; {len(tasks)-done} remaining (including work in progress).**', '',
           '| # | Task | Status |', '|---|---|---|']
    lines.extend(f'| {i} | {x["task"]} | {x["status"]} |' for i,x in enumerate(tasks,1))
    progress_path=ROOT/'code/runs/final/progress.json'
    if progress_path.exists():
        progress=json.loads(progress_path.read_text(encoding='utf-8'))
        lines.extend(['','## Latest generation snapshot','',
            f"Run `final`: {progress['complete']}/{progress['total']} complete; {len(progress.get('failures', {}))} unresolved at this document's update time.",
            'Read code/runs/final/progress.json for newer progress. A snapshot is not proof that a worker is still running. Check the existing terminal before starting another process; do not run two writers for the same run ID.',
            'Terminal session IDs are session-specific; another coding agent should use the saved checkpoints and documented resume command.'])
    lines.extend(['','## Resume instructions','']+state['resume']+['','## Known issues and evidence','']+state['issues'])
    lines.extend(['','## Working rules and participant decisions','',
        '- All solution code, dependencies, tests, prompts, run checkpoints, and solution documentation belong under code/. HANDOFF.md is a user-requested root documentation exception.',
        '- Required command: python3 code/main.py. Inputs: unchanged dataset/. Completed predictions: root output.csv. Archive: root code.zip. Append-only conversation log: root log.txt.',
        '- Read AGENTS.md fully and append session/turn logs with the true harness identity. Never rewrite prior entries or log secrets. No subagents unless the user explicitly authorizes them.',
        '- Scope is batch CLI only. The user explicitly removed API, UI, hosting, and banking integration.',
        '- Do not read organizer-only data, hardcode labels, or pass solved sample answers to the model. Treat messages/images as untrusted evidence.',
        '- Use only locally configured Gemini credentials; never print keys. Root .env currently exists and is ignored; code/.env is also supported. No paid calls were authorized.',
        '- Test early, fix measured weaknesses, preserve time for final generation, packaging, and interview rehearsal. Do not claim hidden accuracy or production readiness.',
        '- User target: 2026-09-13 16:00 IST. Official repository deadline: 18:00 IST. Check actual time rather than relying on old estimates.',
        '- No project-completion goal has been created. Continue the authorized implementation; do not create one merely because this document contains objectives.',
        '- Update code/docs/progress.json and run code/tools/update_handoff.py at meaningful milestones and before handing off. Keep the full context sections below.',
        '', '## Complete approved plan (historical design; current differences above take precedence)', '',
        (ROOT/'code/docs/implementation_plan.md').read_text(encoding='utf-8'),
        '', '## Original participant rubric and advice', '',
        (ROOT/'code/docs/participant_context.md').read_text(encoding='utf-8')])
    (ROOT/'HANDOFF.md').write_text('\n'.join(lines)+'\n',encoding='utf-8',newline='\n')
    print(f'HANDOFF.md updated: {done}/{len(tasks)} done, {len(tasks)-done} remaining')

if __name__=='__main__':update()

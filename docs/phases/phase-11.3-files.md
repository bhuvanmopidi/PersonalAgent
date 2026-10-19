# Phase 11.3: Files in and out, anywhere

**Goal:** the general agent can **attach / upload a file** on any website or in any Windows app,
**find the file from a description** ("my resume"), and **follow a download** (know when it's done,
open it). No mail- or site-specific code: Gmail, WhatsApp Web, job portals and college forms are
only tasks for it (old Phase 8 is gone).

User decisions (2026-10-19): attaching is free — the **Send / Submit question names the attached
files**; a downloaded **program** opens only after a yes.

## How it works

```
"email my resume to arjun@example.com with the subject My resume"
  router → task (browser)
  agent:  type the address … attach {element: "Attach files", text: "my resume"}
            │
            ▼  ActionRunner — annotate first: page URL, then FileResolver
          "my resume" → search the user's folders (Desktop, Documents, Downloads, …)
             one match        → C:\…\Documents\Resume_2026.pdf
             several          → "several files match — ask the user which one: A, B"
             none             → "no file like 'my resume' in your folders"
            ▼  SafetyPolicy judges the REAL file: blocked path (.env, keys, …) → refuse
          BrowserExecutor: click "Attach" → Chrome's file chooser → set the file
                           (or straight into the page's file box)
          → "Attached Resume_2026.pdf (220 KB)"; the agent checks the name shows on the page
        click "Send" → keyword "send" → confirm:
          'click "Send" on mail.example — with Resume_2026.pdf attached?'
```

| Action | What it does |
|---|---|
| `attach {element, text}` | `text` = a full path or a description. Websites: click the Attach / Upload button and pick the file when the chooser opens (Playwright file chooser — no Windows dialog), or set it on the page's file box. Apps: click Attach / Browse, then the standard Windows **Open dialog** gets the full path in its File name box (set directly, typing as fallback, checked) and Enter; done when the dialog closes |
| `wait_download` | The agent's tab saves every download into the Downloads folder (Chrome's own name, "report (1).pdf" if taken) and reports it; otherwise a new, finished file in the folder since the last step (no `.crdownload`, size stable). Up to 30 s |
| `open_file {text}` | Open a file in its own app (`os.startfile`); a program asks first |

## What was built

| File | Purpose |
|---|---|
| `src/jarvis/executors/files.py` | `resolve_file` (path / description → one file, or why not: several / none; "latest" picks the newest), `FileResolver` (ActionRunner annotator: the real path before the policy), `chain` (several annotators), `wait_for_download`, `open_file` |
| `src/jarvis/executors/browser.py` | `attach_file` (file chooser / file box), saving the agent tab's downloads, `wait_download`, `open_file` |
| `src/jarvis/executors/desktop.py` | `attach_file` through the Windows Open dialog (`_file_name_box`, `_fill_path`), `wait_download`, `open_file` |
| `src/jarvis/tiers/structured/agent.py`, `schema.py`, `prompts.py` | Ops `attach`, `wait_download`, `open_file` on both surfaces; attached files remembered per task and named in Send / Submit questions; rule 14 (several files → ask; refused files aren't worked around) |
| `src/jarvis/models.py` | `ActionResult.data` — facts for the agent (the attached / downloaded file) |
| `src/jarvis/safety/policy.py`, `config/safety.yaml` | `blocked_paths` → REFUSE; `program_extensions` → `open_file` confirm |
| `src/jarvis/agent.py` | `chain(browser.annotate, FileResolver().annotate)` |
| Trace (`agent.py` `Trace`) | Per action: the control's tag / name / value and the arguments; per success: the screen's title + text ("evidence") — what Phase 12 skills need |
| `evals/files/`, `evals/sites/agent/{compose,sent,download}.html`, `report.pdf`, test app "Attach file…" | Test material; each eval task gets a temp "user folder" with only its files and its own downloads folder |
| `tests/test_file_actions.py` | Finding files, safety, downloads, attach-then-send on a page; live attach via the Open dialog in `tests/test_desktop_agent.py` |

## Safety

- **Never attached or opened** (`blocked_paths`, matched on the full path): `.env`, `.ssh\`,
  `*.pem/.key/.pfx/.p12`, `id_rsa*`, `id_ed25519*`, `*.kdbx` (password vaults), browser profiles
  (`AppData\…\User Data\…`, where saved passwords live), `.aws\`, `.git-credentials`, `*.ovpn`, and
  Jarvis's own data folder → **refused**, and the agent is told not to work around it.
- The file is **resolved before the safety check**, so the policy judges the real file, not the
  words.
- **Opening a program** (`.exe .msi .bat .cmd .ps1 .vbs .js .scr .com .lnk .reg …`) → confirmed.
- **Sending is confirmed** as before, and the question reads back what goes out — from the form /
  dialog the Send button is in: recipients, filled-in fields, attachments (or "no attachments").
- **A confirmed step never runs twice** in one task (`ActionRunner` marks it; the agent refuses a
  repeat and asks the user to check) — no double emails, orders or deletes.
- **The page's own question boxes** (browser `confirm` / `prompt` / "leave this page?") are asked
  to the user, never auto-answered; plain notices are acknowledged.
- Several matching files → the agent **asks**, never picks one.

## Measured (2026-10-19, Claude Haiku 5.5 via OpenRouter)

| Eval task | Steps | Time | Result |
|---|---|---|---|
| email my resume to arjun@example.com (hidden file box behind Attach) | 3 | 15.6 s | ✅ attached, the Send question named `Resume_Test.pdf`, sent |
| attach my resume — two resumes in the folder | 2 | 5.2 s | ✅ "I found two resumes: Resume_Old_Test.pdf and Resume_Test.pdf. Which one?" |
| download the October report and say where it was saved | 2 | 7.7 s | ✅ "saved as report.pdf in …\Downloads" |
| in the test app, attach my resume (real Windows Open dialog) | 3 | 20.9 s | ✅ |

All tasks: `jarvis eval-agent` **22/22** (11 website, 6 app, 5 file / mail tasks — incl. the page asking its own question), avg ~11 s, ~$0.013;
`jarvis eval-router` **97% correct / 2% wrong actions** (87 cases, incl. 3 new file cases).

## The real Gmail test (to the user's own address, with their OK)

"mail my resume to bhuvanmopidi@gmail.com on gmail" in the Jarvis Chrome (signed in as
jarvistestingstartup@gmail.com):

| What happened | Fix (generic) |
|---|---|
| ✅ Five resume versions in Downloads → the agent **asked which one** | — (works as designed) |
| The answer "Mopidi_Bhuvan_Resume 8.pdf" was passed as "Downloads\…" / "… in Downloads" and fuzzy-searched again → "several match" | An **exact file name with its extension** wins wherever it appears in the request; a folder word ("Downloads", "Desktop" …) points to that real folder |
| After a confirmed Send, Gmail's Sent folder looked empty for a moment → the agent **clicked Send again** (confirmed again) | **Hard guard in code:** a step the user confirmed that ran can't run again in the same task → "I already did that once … please check it went through" (`repeat_blocked`). Rule 15: judge a confirmed step by what the page shows right then; never repeat it to find out |
| Escape on a focused "Send" button asked "Send?" | Only Enter / Space take the focused control's label; Escape / Tab / arrows don't |
| It said "your resume has been emailed" — the sent mail had **no attachment** (the file was in an old draft's compose window) | The Send question reads back **its own form / dialog only** (`groups` in the snapshot): recipients, filled fields, attachments — or "no attachments" |
| **A confirmed Send didn't go out — stayed a draft** (also in the user's own repl test) | **Found:** with no subject, Gmail asks with the browser's own box *"Send this message without a subject or text in the body?"*; nobody handled it, so the browser library answered **Cancel**. Diagnosis: the same click with a subject sent in 1 s. **Fix:** page boxes are handled — a notice (`alert`) is acknowledged, a question (`confirm` / `prompt` / "leave page?") **goes to the user** ("The page asks: … — OK?") and the agent is told the answer |
| The agent believed the resume was attached when it wasn't | Gmail leaves a screen-reader announcement ("Attachment … added.") in the page from earlier. Announcements are now kept out of the page text, and only **new** ones (since the agent's last look) are shown — "Message sent" after our Send, not old news |
| It "verified" the send with an older email in Sent (the 9:32 diagnostic) | The prompt has **Time now**; rule 15: an entry in a list counts only if it's from just now. After a confirmed step its history shows exactly what the user was asked ("… — no attachments?" and they said yes) |
| An old `jarvis repl` (old code) still connected to Jarvis Chrome kept auto-cancelling the box during testing | Not a code bug: one Jarvis at a time per Chrome; noted |

**End state (10:10 PM, verified in Sent):** "send a gmail to bhuvanmopidi@gmail.com by attaching
my resume pdf" → which of 5 resumes? → "Mopidi_Bhuvan_Resume 8.pdf" → attached → *"click Send — to
bhuvanmopidi@gmail.com; attachments: Mopidi_Bhuvan_Resume 8.pdf?"* (yes) → *"The page asks: 'Send
this message without a subject…?' — OK?"* (yes) → "Message sent" → the email arrived **with the
resume**. 21 s for the second turn. Test leftovers in the Jarvis Gmail account
(jarvistestingstartup@gmail.com): test mails at 8:40, 9:32, 10:06 PM and some drafts — left for
the user to delete.

## Things that went wrong while building it

| Problem | Fix |
|---|---|
| The Open dialog never took the path (`open_dialog_stuck`) | The text lives in an edit **inside** the File name combo box: set it there (standard dialogs allow it), type as fallback, check the value, Enter |
| Live tests suddenly saw only "Dark mode / OK" | A test app left over from an earlier run had a look-alike title; the loose name match took it. Tests now wait for the exact window and kill the app reliably |
| I overwrote the existing `tests/test_files.py` (again) | Restored from git unchanged; new tests in `tests/test_file_actions.py`; a standing rule now: check a file doesn't exist before creating it |
| Chrome ignores a link's `download="…"` name for local `file://` pages | Expected; the eval checks the real name |

## How to test

```powershell
uv run pytest tests/test_file_actions.py
uv run pytest -m desktop tests/test_desktop_agent.py -k attach     # real Open dialog
uv run jarvis eval-agent --tasks mail_with_attachment,attach_asks_which_file,download_report,app_attach
uv run jarvis repl
#   mail my resume to <your own address>      (the Send question names the file)
#   download <a file> from this page and open it
```

## Limitations / next

- "Mail my resume to **Arjun**" needs Arjun's address — the agent asks until Phase 13 (memory).
- Drag-and-drop-only upload areas (no button, no file box) aren't handled.
- File search covers the usual folders (Desktop, Documents, Downloads, Pictures, Music, Videos) a
  few levels deep, within ~1.5 s.
- Next: **11.4 checkout guard** (order read-back before Pay, any shop).

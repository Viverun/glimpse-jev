# glimpse-jev

glimpse is a computer-use agent for the GNOME desktop on Linux.
You type a task. glimpse does the task in your apps, on your screen.

Two models do the work:

- A **planner** writes the plan. It can be any OpenAI-compatible model that reads images and returns JSON.
- **Jev** (TypeSafe System One) finds each control, checks each result, and decides when the task is done.

This document uses Simplified Technical English (ASD-STE100).

## Contents

- [What glimpse can do](#what-glimpse-can-do)
- [Requirements](#requirements)
- [Install](#install)
- [Set up](#set-up)
- [Use glimpse](#use-glimpse)
- [Stop a task](#stop-a-task)
- [How glimpse works](#how-glimpse-works)
- [Memory and learning](#memory-and-learning)
- [Safety](#safety)
- [Benchmark](#benchmark)
- [Improve the harness (Harness²)](#improve-the-harness-harness)
- [Teach glimpse a skill](#teach-glimpse-a-skill)
- [Repository layout](#repository-layout)
- [Limitations](#limitations)
- [License](#license)

## What glimpse can do

glimpse operates real apps: LibreOffice, Text Editor, Files, the web browser and other GNOME apps.

Examples:

```sh
glimpse "open ~/data/sales.csv in LibreOffice Calc, sort the rows by Price from high to low, save it as sorted.xlsx"
glimpse "in Writer, write a one-page summary of ~/notes/meeting.txt and export it as a PDF"
glimpse "look at my recent GitHub activity and tell me what I worked on"
glimpse "in the Files app, move all .csv files in ~/Downloads into a new folder named data"
glimpse "for 2 hours, work with Claude on my project: give it the next task each time it finishes"
```

When a task names an app, glimpse does the work in that app. It uses the shell only to check the result.

## Requirements

You must have these items:

| Item | Why |
|---|---|
| Linux with GNOME on Wayland | glimpse reads windows through AT-SPI and the desktop portal. |
| [`computer-use-linux`](https://github.com/agent-sh/computer-use-linux) on your `PATH` (`npm install -g @agent-sh/computer-use-linux`) | It reads the windows and sends clicks and keys. |
| `ydotool`, with the `ydotoold` daemon running | It moves the cursor and types. |
| `gtk-launch` (package `libgtk-3-bin`) | It opens apps. |
| `git` | The installer downloads glimpse with it. |
| A TypeSafe API key | Jev uses it. |
| An OpenAI-compatible endpoint, API key and model | The planner uses them. You do not need them with `--jev-only`. |

These items are optional:

- LibreOffice, for spreadsheet and document tasks.
- [Zen Browser](https://zen-browser.app/), the default browser for web tasks.
- Membership of the `input` group, for the stop key. Run `sudo usermod -aG input $USER`, then log out and log in again.

## Install

Run this command:

```sh
curl -fsSL https://raw.githubusercontent.com/Viverun/glimpse-jev/main/install.sh | bash
```

The installer does these steps:

1. It installs [`uv`](https://docs.astral.sh/uv/) if `uv` is not available.
2. It downloads glimpse to `~/.local/share/glimpse-jev`.
3. It installs the Python dependencies.
4. It puts the `glimpse` command in `~/.local/bin`.

To update glimpse, run the same command again.

## Set up

Run this command:

```sh
glimpse setup
```

Give these values when glimpse asks for them:

| Value | Example |
|---|---|
| Planner base URL | `https://api.openai.com/v1`, or `https://<resource>.openai.azure.com/openai/v1` |
| Planner API key | your key |
| Planner model | a model that reads images and returns JSON |
| Reasoning effort | `low`, `medium`, `high`, or empty |
| TypeSafe API key | your key |
| TypeSafe base URL | empty, for the default endpoint |

glimpse tests the planner and Jev. Then it checks the desktop tools.
It saves the values in `~/.config/jev/config.json`. Only your user can read this file.

You can also use environment variables. They have priority over the file.

| Setting | Environment variables |
|---|---|
| Planner base URL | `JEV_PLANNER_BASE_URL`, `OPENAI_BASE_URL`, `AZURE_OPENAI_ENDPOINT` |
| Planner API key | `JEV_PLANNER_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY` |
| Planner model | `JEV_PLANNER_MODEL`, `AZURE_OPENAI_DEPLOYMENT` |
| Reasoning effort | `JEV_LUNA_EFFORT` |
| Service tier | `JEV_LUNA_TIER` |
| TypeSafe API key | `TYPESAFE_API_KEY` |
| TypeSafe base URL | `TYPESAFE_BASE_URL` |

To examine the setup later, run `glimpse /doctor`.

## Use glimpse

```sh
glimpse                      # open an interactive session
glimpse "open the calculator" # do one task
glimpse -c                   # continue the last session
glimpse --resume trip        # continue a saved session (number, id or a word from its title)
```

In an interactive session:

- Type a task and push Enter.
- Type `/help` to see the commands.
- Use the arrow keys and `Ctrl+R` to find earlier tasks.
- Push `Alt+Enter` to add a new line.
- Start a line with `!` to run a shell command.

Each task goes into a session. glimpse gives the earlier tasks of the session to the planner. Thus "now close it" works.

To give glimpse a time budget, put the time in the task, for example "for 3 hours, ...".
glimpse then continues to work until the time is used.

Useful options:

| Option | Effect |
|---|---|
| `--jev-only` | Do not use the planner. Jev selects each action. |
| `--max-minutes N` | Stop the task after N minutes. |
| `--block TEXT` | Do not touch windows with TEXT in the title. |
| `--quiet` | Do not show pop-ups. |
| `--confirm` | Ask before each task starts. |

## Stop a task

Push **Ctrl+Alt+Esc** in any window. glimpse stops at once.

To use a different key combination, type `/stopkey ctrl+alt+x`.
The mouse and the touchpad do not stop a task. You can use them while glimpse works.

The stop key needs read access to the keyboard device. See [Requirements](#requirements).

## How glimpse works

For each task, glimpse does these steps:

1. **Context.** glimpse collects the open windows, the elements and the text of the active window, a screenshot, your preferences, and the lessons for the app.
2. **Plan.** The planner writes the steps for the full task in one reply when it can. It also writes a check command and a report.
3. **Act.** glimpse does each step. Jev finds the element in the accessibility tree. If the tree does not show it, Jev finds it on the screenshot. Keys, text, file save and web addresses go directly.
4. **Check.** glimpse compares the screen before and after each step. Jev examines the expected result and looks for error messages. glimpse checks files on the disk.
5. **Recover or finish.** If a step fails, the planner gets the failure, the change on screen and the nearest elements. Then it makes a new plan. If the same plan repeats on the same screen, glimpse tells the planner to try a different method. The task finishes only when the check command passes.

Special steps make difficult parts reliable:

- `save_as` saves to an exact path in any format, including PDF from LibreOffice. It verifies the file on the disk.
- `select` selects a range in LibreOffice Calc and verifies the selection.
- `open_file` opens a file directly in an app.
- `read_page` gets the full text of a public web page.

## Memory and learning

glimpse keeps its memory in `~/.config/jev/`:

| File | Content |
|---|---|
| `profile.json` | Your desktop, apps and preferences (`/remember` adds a preference) |
| `JEV.md` | Your standing notes (`/memory edit`) |
| `memory.jsonl` | Each task: the steps that worked and the failures |
| `workflows.json` | Reusable workflows with `{variables}`, made from tasks that passed their check |
| `taught.json` | App lessons from demonstrations and from Harness² |
| `harness.json` | Planner notes that Harness² adopted |
| `sessions/` | Your sessions |

After a task passes its check, glimpse makes a reusable workflow from it (Agent Workflow Memory).
Before a new task, Jev selects the best workflow and the best past task. The planner gets them.

Commands: `/workflows` shows the workflows. `/lessons` shows the lessons. `/forget` removes data.

## Safety

- glimpse never uses WhatsApp windows.
- glimpse does not type into its own terminal.
- The shell refuses destructive commands, for example `sudo`, `rm -rf` outside `/tmp`, and disk commands.
- When a task names an app, the shell can only read. The work must occur in the app.
- Text in windows, web pages and files is data. The planner does not obey instructions in it.
- glimpse deletes the screenshots that it takes. It turns off the screenshot flash and sound during a task, and restores your settings after the task.

## Benchmark

The benchmark has 11 tasks on the live desktop: Text Editor, LibreOffice Calc and Writer, Files, and the web.
A checker examines the files on the disk or the report. The agent cannot see the checker.

```sh
uv run python bench/live_bench.py               # all tasks
uv run python bench/live_bench.py calc_sort     # some tasks
```

Do not touch the desktop while the benchmark runs.

Results on the author's machine (one run of each task):

| Round | Passed | Mean time | Plans per task |
|---|---|---|---|
| 7 | 9 of 11 that did the work in the app | 46.7 s | 4.5 |
| 10 | 11 of 11 | 28.5 s | 3.3 |

## Improve the harness (Harness²)

`bench/harness2.py` adapts [Harness²](https://github.com/google-research/harness2) to glimpse.
The harness is the planner notes and the app lessons. The models do not change.

For each benchmark task, the script does these steps:

1. It runs the task with the current harness.
2. Two proposers read the run. INV changes the procedure. DOM changes the app knowledge. "Keep the base" is a valid answer.
3. It runs the task again with each proposal.
4. If both proposals help, it combines them and runs the task again.
5. It adopts a proposal only if the result is clearly better than the base.
6. A reflector writes what each change did, and at most one lesson for later tasks.

```sh
uv run python bench/harness2.py                  # all tasks
uv run python bench/harness2.py organize         # one task
```

To run without the adopted notes, set `JEV_NO_HARNESS=1`.

## Teach glimpse a skill

`live/teach.py` teaches by demonstration.
It does the steps of a skill with the glimpse executor on the live desktop.
It verifies the result on the disk. Only then does it save the skill as a lesson and a workflow.

```sh
uv run python live/teach.py                      # all skills
uv run python live/teach.py writer_pdf           # one skill
```

To add a skill, add an entry to `SKILLS` in `live/teach.py`. Give the steps, the values, and a check command.
Write variable parts as `{NAME}`. The planner copies examples exactly, so do not use examples in lessons.

## Repository layout

| Path | Content |
|---|---|
| `install.sh` | The installer |
| `live/jev_cli.py` | The `glimpse` command |
| `live/repl.py`, `live/sessions.py` | The interactive session and saved sessions |
| `live/setup_wizard.py`, `live/config.py` | `glimpse setup` and the settings |
| `live/hybrid_agent.py` | The planner loop and the steps |
| `live/jev_agent.py` | Observation and the Jev-only mode |
| `live/a11y_agent.py` | Accessibility tree, clicks and typing |
| `live/changes.py` | Screen changes, loop detection |
| `live/lo_dialog.py`, `live/gtk_dialog.py` | Save dialogs of LibreOffice and GNOME |
| `live/memory.py` | Memory, lessons, workflows, harness notes |
| `live/takeover.py` | The stop key |
| `live/teach.py` | Teaching by demonstration |
| `bench/live_bench.py`, `bench/harness2.py` | The benchmark and Harness² |
| `tests/` | Unit tests (`uv run pytest -q tests/`) |
| `bench_jev.py`, `fixtures.py` | Jev latency test on synthetic screens |

## Limitations

- glimpse works only on GNOME on Wayland.
- The web browser does not show page text to accessibility tools. glimpse clicks web pages from the screenshot, or reads public pages with `read_page`.
- LibreOffice has large accessibility trees. Some steps in Calc take approximately 4 seconds for a full read.
- The benchmark results come from one machine and one run of each task.
- `live/run_live.py` is an old demo. It needs an `agent_src` folder that is not in this repository.

## License

glimpse-jev uses the [PolyForm Noncommercial License 1.0.0](LICENSE.md).

You can use, change and share glimpse for noncommercial purposes. Examples are personal use, study, research,
hobby projects, and use by charities, schools and public organizations.

You cannot use glimpse or share it for a commercial purpose. Do not sell it, and do not use it in a product or a
service that earns money. For commercial use, ask the author for a different license.

Copyright 2026 Viverun.

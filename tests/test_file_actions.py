"""Phase 11.3: files in and out — finding the file, attaching / uploading it on a page, downloads,
opening files, and the safety rules around them."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from playwright.async_api import Browser
from test_agent_loop import ScriptPlanner, _silent, browser, el, site, step  # noqa: F401
from test_browser import HeadlessSession

from jarvis.apps.youtube_flow import load_youtube_config
from jarvis.config import AgentSettings
from jarvis.executors.base import ActionRunner
from jarvis.executors.browser import BrowserExecutor
from jarvis.executors.files import FileResolver, chain, resolve_file, wait_for_download
from jarvis.models import Action
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.safety.policy import Decision, SafetyPolicy
from jarvis.tiers.structured.agent import BrowserAgent
from jarvis.tiers.structured.schema import AgentAction, AgentStep


def folder_with(tmp: Path, *names: str) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    for name in names:
        (tmp / name).write_bytes(b"%PDF-1.4 test")
    return tmp


# ---- finding the file ------------------------------------------------------------------------


def test_one_matching_file_is_found(tmp_path: Path) -> None:
    docs = folder_with(tmp_path / "Documents", "Resume_Bhuvan.pdf", "notes.txt")
    path, problem = resolve_file("my resume", [docs])
    assert path == docs / "Resume_Bhuvan.pdf" and problem == ""


def test_several_matching_files_ask_which(tmp_path: Path) -> None:
    docs = folder_with(tmp_path / "Documents", "Resume_2026.pdf", "Resume_old.pdf")
    path, problem = resolve_file("my resume", [docs])
    assert path is None and "several files match" in problem and "Resume_old.pdf" in problem


def test_latest_picks_the_newest_match(tmp_path: Path) -> None:
    docs = folder_with(tmp_path / "Pictures", "photo_beach.jpg", "photo_party.jpg")
    old = time.time() - 3600
    os.utime(docs / "photo_beach.jpg", (old, old))
    path, _ = resolve_file("the latest photo", [docs])
    assert path == docs / "photo_party.jpg"


def test_full_paths_and_missing_files(tmp_path: Path) -> None:
    docs = folder_with(tmp_path / "Documents", "report.docx")
    assert resolve_file(str(docs / "report.docx"), [docs])[0] == docs / "report.docx"
    assert resolve_file(str(docs / "report"), [docs])[0] == docs / "report.docx"  # no extension
    assert resolve_file(str(docs / "gone.pdf"), [docs])[0] is None
    path, problem = resolve_file("my tax return", [docs])
    assert path is None and "no file like" in problem


def test_the_policy_judges_the_real_file(tmp_path: Path) -> None:
    secrets = folder_with(tmp_path / "project", ".env")
    policy = SafetyPolicy.from_yaml()
    resolver = FileResolver([secrets])
    action = asyncio.run(
        resolver.annotate(Action(kind="attach_file", target=str(secrets / ".env")))
    )
    assert action.args["path"].endswith(".env")
    assert policy.check(action).decision == Decision.REFUSE


def test_opening_a_downloaded_program_is_confirmed(tmp_path: Path) -> None:
    downloads = folder_with(tmp_path / "Downloads", "setup.exe", "invoice.pdf")
    policy = SafetyPolicy.from_yaml()
    resolver = FileResolver([downloads])

    def check(name: str) -> Decision:
        action = asyncio.run(
            resolver.annotate(Action(kind="open_file", target=str(downloads / name)))
        )
        return policy.check(action).decision

    assert check("setup.exe") == Decision.CONFIRM and check("invoice.pdf") == Decision.ALLOW


def test_a_finished_download_is_noticed(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    since = time.time()

    async def arrive() -> None:
        await asyncio.sleep(0.3)
        (downloads / "report.pdf.crdownload").write_bytes(b"part")
        await asyncio.sleep(0.6)
        (downloads / "report.pdf.crdownload").rename(downloads / "report.pdf")

    async def run() -> str:
        task = asyncio.create_task(arrive())
        result = await wait_for_download(downloads, since, timeout_s=6)
        await task
        return result.observation

    assert (
        "report.pdf" in asyncio.run(run())
        and "crdownload"
        not in asyncio.run(wait_for_download(downloads, since, timeout_s=2)).observation
    )


# ---- on a page: the agent attaches, Send names the file ----------------------------------------


async def make_files_agent(
    browser: Browser,  # noqa: F811 — the shared fixture from test_agent_loop
    start: str, planner: ScriptPlanner, files: Path, downloads: Path,
    answers: list[bool] | None = None,
) -> tuple[BrowserAgent, BrowserExecutor, ScriptedConfirmer]:  # fmt: skip
    context = await browser.new_context(accept_downloads=True)
    page = await context.new_page()
    await page.goto(site(start))
    confirmer = ScriptedConfirmer(answers)
    policy = SafetyPolicy.from_yaml()
    executor = BrowserExecutor(HeadlessSession(context), load_youtube_config(),
                               policy.rules.captcha, confirmer, announce=_silent)  # fmt: skip
    executor.downloads_dir = downloads
    annotate = chain(executor.annotate, FileResolver([files]).annotate)
    runner = ActionRunner(policy, executor, confirmer, annotate=annotate)
    return (
        BrowserAgent([planner], executor, runner.run_action, AgentSettings()),
        executor,
        confirmer,
    )


async def test_attach_then_send_names_the_file(browser: Browser, tmp_path: Path) -> None:  # noqa: F811
    files = folder_with(tmp_path / "Documents", "Resume_Bhuvan.pdf")
    planner = ScriptPlanner(
        lambda p: step("continue", None,
                       AgentAction(op="type", element=el(p, "To recipients"),
                                   text="arjun@example.com"),
                       AgentAction(op="attach", element=el(p, "Attach files"), text="my resume")),
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Send"))),
        lambda p: step("done", "Sent your resume to Arjun."),
    )  # fmt: skip
    agent, executor, confirmer = await make_files_agent(
        browser, "compose.html", planner, files, tmp_path / "dl", answers=[True]
    )
    outcome = await agent.run("email my resume to arjun@example.com")
    assert outcome.ok, outcome.message
    assert "Attached Resume_Bhuvan.pdf" in planner.prompts[1]
    assert any("Send" in q and "attachments: Resume_Bhuvan.pdf" in q for q in confirmer.prompts)
    page = executor.agent_page
    assert page is not None and "Attachments: Resume_Bhuvan.pdf" in await page.inner_text("body")


async def test_download_is_saved_and_reported(browser: Browser, tmp_path: Path) -> None:  # noqa: F811
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    planner = ScriptPlanner(
        lambda p: step("continue", None,
                       AgentAction(op="click", element=el(p, "Download the October")),
                       AgentAction(op="wait_download")),
        lambda p: step("done", "Saved."),
    )  # fmt: skip
    agent, _, _ = await make_files_agent(
        browser, "download.html", planner, tmp_path / "Documents", downloads
    )
    outcome = await agent.run("download the report")
    assert outcome.ok
    saved = [f.name for f in downloads.iterdir()]  # Chrome keeps file:// names: report.pdf
    assert len(saved) == 1 and saved[0].endswith("report.pdf")
    assert "Downloaded" in planner.prompts[1] and saved[0] in planner.prompts[1]


def test_an_exact_file_name_wins_over_a_fuzzy_search(tmp_path: Path) -> None:
    docs = folder_with(tmp_path / "Downloads", "Resume 7.pdf", "Resume 8.pdf", "Resume (6).pdf")
    path, problem = resolve_file("Resume 8.pdf", [docs])
    assert path == docs / "Resume 8.pdf", problem
    path, _ = resolve_file(r"Downloads\Resume 8.pdf", [docs])  # a folder word + the exact name
    assert path is not None and path.name == "Resume 8.pdf"
    path, _ = resolve_file("Resume 8.pdf in Downloads", [docs])  # said the way people say it
    assert path is not None and path.name == "Resume 8.pdf"


async def test_a_confirmed_send_is_never_repeated(browser: Browser, tmp_path: Path) -> None:  # noqa: F811
    files = folder_with(tmp_path / "Documents", "Resume_Bhuvan.pdf")

    def send(p: str) -> AgentStep:
        return step("continue", None, AgentAction(op="click", element=el(p, "Send")))

    planner = ScriptPlanner(
        lambda p: step("continue", None,
                       AgentAction(op="type", element=el(p, "To recipients"),
                                   text="arjun@example.com"),
                       AgentAction(op="attach", element=el(p, "Attach files"), text="my resume")),
        send, lambda p: step("continue", None, AgentAction(op="back")), send,
    )  # fmt: skip
    agent, _, confirmer = await make_files_agent(
        browser, "compose.html", planner, files, tmp_path / "dl", answers=[True, True]
    )
    outcome = await agent.run("email my resume to arjun@example.com")
    assert outcome.error == "repeat_blocked" and "won't do it again" in outcome.message
    assert len(confirmer.prompts) == 1  # asked once, sent once
    assert 'To recipients: "arjun@example.com"' in confirmer.prompts[0]  # the address read back


def test_escape_on_a_focused_send_button_is_not_a_send() -> None:
    from jarvis.tiers.structured.agent import to_action
    from jarvis.tiers.structured.observer import Element, Snapshot

    snap = Snapshot(url="https://mail.example/", title="Mail",
                    elements=[Element(id=1, tag="button", name="Send", focused=True)])  # fmt: skip
    policy = SafetyPolicy.from_yaml()
    escape, _ = to_action(AgentAction(op="press", key="Escape"), snap)
    enter, _ = to_action(AgentAction(op="press", key="Enter"), snap)
    assert escape is not None and policy.check(escape).decision == Decision.ALLOW
    assert enter is not None and policy.check(enter).decision == Decision.CONFIRM


async def test_send_reads_back_its_own_compose_window(browser: Browser, tmp_path: Path) -> None:  # noqa: F811
    """Two compose windows: Send in the new, empty one must not borrow the old one's file."""
    from jarvis.tiers.structured.agent import read_back
    from jarvis.tiers.structured.observer import observe

    page = await (await browser.new_context()).new_page()
    await page.goto(site("two-drafts.html"))
    snap = await observe(page)
    sends = [e for e in snap.elements if e.name == "Send"]
    old, new = sends[0], sends[1]
    assert "no attachments" in read_back(snap, new) and "Resume_Old" not in read_back(snap, new)
    assert 'To recipients: "arjun@example.com"' in read_back(snap, new)
    assert "attachments: Resume_Old.pdf" in read_back(snap, old)


def test_attachment_names_are_read_cleanly() -> None:
    from jarvis.tiers.structured.agent import _file_name

    assert (
        _file_name("bhuvanmopidi@gmail.com Mopidi_Bhuvan_Resume 8.pdf")
        == "Mopidi_Bhuvan_Resume 8.pdf"
    )
    assert _file_name(" gmail.com Mopidi_Bhuvan_Resume 8.pdf") == "Mopidi_Bhuvan_Resume 8.pdf"
    assert _file_name("Resume_Test.pdf") == "Resume_Test.pdf"


async def test_the_pages_own_question_box_goes_to_the_user(
    browser: Browser,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Left alone, the browser library answers a confirm() box with Cancel — Gmail's "Send without
    a subject?" then silently doesn't send. Now the user is asked."""
    planner = ScriptPlanner(
        lambda p: step("continue", None,
                       AgentAction(op="type", element=el(p, "To recipients"),
                                   text="me@example.com"),
                       AgentAction(op="click", element=el(p, "Send"))),
        lambda p: step("done", "Sent."),
    )  # fmt: skip
    agent, executor, confirmer = await make_files_agent(
        browser, "compose-strict.html", planner, tmp_path / "Documents", tmp_path / "dl",
        answers=[True, True],  # 1. Jarvis's "click Send?"  2. the page's "without a subject?"
    )  # fmt: skip
    outcome = await agent.run("email me@example.com")
    assert outcome.ok
    assert any("The page asks" in q and "without a subject" in q for q in confirmer.prompts)
    assert "the user said OK" in planner.prompts[1]
    assert "Old_Report" not in planner.prompts[0]  # an old announcement never reaches the agent
    assert "you asked the user" in planner.prompts[1]  # it knows exactly what the user approved
    page = executor.agent_page
    assert page is not None and "Message sent" in await page.inner_text("h1")


async def test_old_announcements_are_not_page_text(browser: Browser) -> None:  # noqa: F811
    from jarvis.tiers.structured.observer import observe

    page = await (await browser.new_context()).new_page()
    await page.goto(site("compose-strict.html"))
    snap = await observe(page)
    assert "Old_Report.pdf" not in snap.text
    assert snap.announcements == ["Attachment Old_Report.pdf added."]
    assert "Page announcements since your last look" in snap.render()

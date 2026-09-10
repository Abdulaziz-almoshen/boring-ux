<h1 align="center">😴 Boring UX</h1>
<p align="center"><b>See where people really look when they use your product — with just a webcam.</b><br>
Record a real user, and get a usability report that says what confused them, what they searched for, and what to fix.</p>

<p align="center">
<img src="screenshots/live-recording.png" width="88%" alt="A live Boring UX session"/>
</p>

## What it does

1. **Records** a person using your website — their eyes, their face, their voice as they think aloud, and their mouse and clicks.
2. **Understands** the session on your own Mac: where they looked, when they got stuck, when they went searching, how they reacted.
3. **Writes the report** — findings tied to what they *said*, where they *looked*, and how long it *took*, with a prioritized list of fixes. It opens by itself when it's ready.

Nothing leaves your computer. Great UX is boring — invisible and frictionless. Boring UX finds the exciting parts so you can make them boring.

## Set it up (once, in one message)

Paste the prompt from **[ONBOARDING.md](ONBOARDING.md)** into **Claude Code**. It installs everything and walks you through loading the browser extension. Takes about ten minutes, mostly downloads.

## Run a session

1. Open the website you want to test and click the **Boring UX** icon.
2. **Enable camera → Calibrate → Start.** Ask the person to do a task and **talk aloud** while they do it.
3. Click **Stop & save.**

That's it. The panel turns into a progress view; you can close the tab and come back. A few minutes later the report opens.

## What you get

A single report (PDF + web page) with an overall grade, a journey map with the emotional track, the moments that mattered — searching, confusion, hesitation, delight — each backed by a quote, where the eyes were, and the timing, and a ranked list of what to fix first. Everything is saved in `Downloads/boring-ux/<site>-<time>/`.

## Good to know

- **Talk aloud.** Speech is half the evidence. A silent session gives a much thinner report. While recording you see a **mic level bar and live captions**, so you know the microphone is working.
- **Keep the tab in front** while recording — the camera freezes when the tab is hidden, and those seconds are reported as *tab switched*, not as attention.
- **Webcam eye tracking is about regions, not pixels.** It reliably tells left / center / right and up / down, and when someone looks away — not which of two neighboring buttons. The report is honest about this on every page.
- **Press "Sign self-test"** once per session (8 seconds) so the analysis can prove it has left and right the right way round.

Open source, [MIT License](LICENSE). Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

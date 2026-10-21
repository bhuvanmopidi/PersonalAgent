# Refinements backlog

Small improvements found while testing, to do later (not tied to a phase). Each must stay
generic — no per-site or per-task rules.

| # | Refinement | Found | Idea |
|---|---|---|---|
| 1 | **Scroll a named area** ("scroll down the chat history") | ChatGPT, 2026-10-21 | The agent's `scroll` takes an element: move the pointer over the panel that element is in, then wheel. The instant `scroll_page` command only for plain up / down / top / bottom; a named area goes to the agent. Same on desktop. Test: a page with two independent panels |
| 2 | **"Place order" in a cart that only opens checkout** | Flipkart, 2026-10-21 | The checkout gate refuses a place-order label when no address / payment is shown; on some shops the cart's "Place Order" only opens checkout. Option: confirm ("this may place the order — continue?") instead of refusing on such pages |
| 3 | **Paid add-ons** (a ₹299 protect fee appeared in the Flipkart total) | Flipkart, 2026-10-21 | Find out whether the agent opted in; if so, a principle: never accept anything that adds cost unless the user asked |
| 4 | **Look on its own** | Phase 15 | The agent decides when to `look`; it could also look automatically after repeated "not found" |
| 5 | **Faster skill replay in heavy apps** (Spotify replay ~8–10 s, most of it reading the whole window) | Phase 12, 2026-10-21 | A replay step knows the control it wants (type + name): look it up directly (UIA name search / a DOM query) instead of reading the full tree before each step; read the full view only for the final check. Target < 5 s |
| 6 | **Learn more of what worked** | Phase 12 | A recipe whose control name carries extra result words ("Play Levitating (feat. DaBaby)") fails the strict check and isn't saved; the next run that works is learned instead. Option: let a template keep a "starts with" part and verify that |

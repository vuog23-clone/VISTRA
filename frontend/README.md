# VISTRA chat demo

For Docker deployment and API-key-authenticated developer calls with JSON input/output, see [the developer API guide](../api/README.md).

From `E:\VISTRA`:

```powershell
./.venv/python.exe -m pip install -r requirements.txt
./.venv/python.exe -B -m frontend.server
```

Open **http://127.0.0.1:8000**. Use `--port 8001` if needed. Stop with Ctrl+C.

To inspect the same chat backend before browser formatting, run `./.venv/python.exe -B main.py --chat`. Type `/last` for the full backend JSON, or launch with `--chat --json` to print it after every reply. `/new` starts a new session and `/session NAME` resumes a named session for the same terminal owner.

The demo uses the existing `.env`, PostgreSQL, Qdrant, models, and `ChatService`. PostgreSQL must be running; API keys are required to generate replies. Indexed travel questions additionally need the existing retrieval services and model cache.

Itinerary requests now run the **TravelPlanner-style flow**: selected tools → evidence notebook → structured itinerary planner → constraint checks and bounded repair. The chat shows planning status and an expandable tool trace. The current itinerary is saved for later edits such as “Make day two more relaxed; keep the other days unchanged.” Missing route, fare and booking data is explicitly marked unverified. The implementation lives in `src/planning/`.

Try **Make it personal**, send the suggested message, then start **New conversation** and ask “What preferences do you remember about me?” Open **Memory → Across chats** to see the stored preferences. **This chat** shows current trip details and the conversation summary. Reloading reopens the active conversation; the sidebar lists saved chats. Clearing a chat retains long-term preferences, which can be removed separately.

This is a local demo bound to loopback. A signed HttpOnly browser cookie scopes conversations and memory to this browser. Keep `frontend/.runtime/signing-key` between restarts so existing browser sessions remain accessible. It is ignored by Git and never served. Browser storage keeps only the active conversation ID. There is no account/login system.

Replies arrive after the existing generator completes; progress events stream during processing. Closing the browser does not cancel an already running model call or its memory commit. Reload the conversation to recover a completed reply. Recent history is limited to 100 messages per view.

Markdown and source links are sanitized. Bundled Marked and DOMPurify files were copied from this repository's original vendor assets; their licenses and versions are retained under `static/vendor/`. No CDN or frontend build step is required.

Travel replies display a **Places found** box beneath the answer, with retrieved photos, names, ratings, review counts, addresses, prices, opening hours, coordinates and Google Maps links. Expand **Place details** for the full metadata. Unknown values remain unknown. Cards and source links are saved with each reply and restored when reopening the conversation. Photos load through an owner-authorized local proxy; a failed photo keeps its metadata visible.

Dated itinerary requests also show a **Weather for your trip** box with Open-Meteo daily forecasts. Dates can come from the message or the active trip's memory. The generator receives this weather evidence before writing the itinerary. Forecast availability is limited to the provider's short-range window; missing dates, missing coordinates, out-of-range dates and provider failures are shown explicitly. Saved weather boxes retain their original fetch timestamp. Weather settings live in `config.yaml`.

The **Results** selector defaults to **20**, with choices of 5, 10, 15 and 20. This controls the retrieval budget per branch; actual matches may be fewer. Cards show up to 20 distinct places. The generation context retains its existing separate source budget. Travel retrieval includes the image branch, while direct memory chat and explicit requests without photos retain their existing behavior.

Demo code, assets, and tests live in `frontend/`; all application dependencies are declared in the root `requirements.txt`. Retrieval defaults in `config.yaml` and `main.py` are now 20; text grouping supports a twenty-document budget without exceeding the explicit per-page grouping cap.

The developer API has separate tests under `api/tests/`; the browser regression suite runs with:

```powershell
./.venv/python.exe -B -m unittest discover -s frontend/tests -v
```

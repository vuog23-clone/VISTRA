# VISTRA chat demo

A simple ChatGPT-style interface connected to `ChatService`: text and image messages,
multi-turn chat, saved conversations, Markdown responses, and a saved-preferences dialog.
Retrieved places appear on a map beside the chat, with numbered markers, photos,
place metadata, coordinates and attribution. Click a marker or a place name to
inspect its details. Each reply has a **View places on map** button, so earlier
results remain accessible. The map and selected results are restored on chat reload.
Places without coordinates stay in the chooser with an explicit missing-coordinate
label. On smaller screens, use **Show map / Show chat** to switch between views.
Visual requests can include a location, such as “Find places like this image in Phu Quoc.”
The planner separates the location from the scenery description. Image searches filter
existing Qdrant points by metadata-backed place IDs before ranking similarity, and the
same accepted results feed the answer and photo cards. There is no global fallback.
An unresolved location or insufficient indexed data produces an explicit limitation.
Location is retained when refining the same search; a new destination replaces it.

Location membership uses exact normalized province/address labels and reviewed IDs in
`location_mappings_path`. The initial Phu Quoc mapping covers 13 existing places; it is
not a complete island boundary. Places lacking supported membership stay out of scope.
Add reviewed IDs or improve addresses to extend coverage. Existing vectors stay unchanged.
`image_search_score_threshold` optionally excludes weak visual matches; its default is
`null` until calibrated with evaluation examples, so returned images are relative matches.

Run a location-filtered terminal search and a read-only coverage audit:

```cmd
python -m src.service.search_service --image test_sample.jpg --location "Phu Quoc" --limit 5
python -m src.service.search_service --text "White sandy beaches in Phu Quoc" --visual-query "white sandy beaches" --route image_text --location "Phu Quoc"
python -m src.resolver.location_resolver "Phu Quoc" --qdrant
```

Terminal searches use the explicit `--location` argument; chat extracts it with the planner.
These attachments are saved with the reply, so they survive reloads and later searches.
Photo cards keep their original result numbers, including when you select one place.

Follow-ups use saved entity IDs to keep the same place. Text follow-ups search only
the selected Wikivoyage pages. Image follow-ups keep the photo and scraped metadata;
extra Wikivoyage information requires a curated mapping in `destination_mappings_path`.
With its current `null` setting, image follow-ups use the available scraped metadata.
Mapped destination context stays separate from the individual place's facts.
Ambiguous references ask for a name or result number. A new search replaces the
result list; selecting a place keeps the other original matches available for comparison.

From `E:\VISTRA` with `.venv` active, run in CMD or PowerShell:

```cmd
docker compose -f compose.qdrant.yaml up -d
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).
Use one worker. Models and the chat service are reused, with database and model work
running on one background thread. The first search can take longer while models load.
Answers appear when the complete response is ready.
New replies reveal words sequentially with Markdown formatting, while the map shows
their places. This is a browser animation after generation completes. Saved replies
display immediately, and reduced-motion preferences skip the animation.

The map uses the bundled Leaflet library and browser-loaded OpenStreetMap tiles.
Map backgrounds require internet access; place metadata remains available when
tiles cannot load. Tile URL, attribution, initial center and zoom are configurable
through `app_map_*` in `config.yaml`. The default provider needs no map API key;
keep its attribution visible and follow the [tile usage policy](https://operations.osmfoundation.org/policies/tiles/).
No geocoding or new indexing is performed. Pins come directly from each accepted
result's coordinates; image places and Wikivoyage destinations retain their own identities.

Each reply has a **Search activity** panel. It updates live with memory loading,
request planning, the selected route, location scope, retrieval and generation.
Expand the source entries to see the exact excerpts or photo metadata supplied to
the answer model, after context limits and mapping checks. Uploaded photo pixels
can be sent to the LLM; retrieved photos are currently supplied as metadata only.
Turns without retrieved travel sources say so explicitly. Follow-ups show whether
they reuse earlier results. Activity stays with the saved reply when you reopen it.
This shows application events and source availability; individual answer claims
are not independently verified. It does not expose the model's private reasoning.

The browser requests newline-delimited activity events from `POST /api/chat` with
`stream=true`. Existing clients can omit that field to keep the JSON response.

Set `OPENAI_API_KEY` and `JEV_API_KEY` in `.env`. The OpenAI model, Qdrant URL,
embedding settings, and upload limits come from `config.yaml`.
The browser uses the `app_user_id` profile (default `local`). This is a local demo
with a shared profile, rather than a login system.

Try this sequence:

1. “Remember that I prefer quiet places and museums.”
2. “Which museums should I visit in Hanoi?”
3. “Which one is about military history?”
4. Start a new chat and ask “What travel style do I prefer?”
5. Open **Saved preferences** to inspect the remembered facts.

Chats and preferences persist in Qdrant's separate memory collection. The UI shows
the full saved transcript; the LLM receives the configured recent context window.
Use the trash button beside a chat and confirm **Delete** to remove that conversation
and its messages from Qdrant and short-term memory. Saved preferences remain available
across chats. Deletion waits until the chat's current response has finished.
Uploaded image files and indexed travel photos are kept.
Uploaded images are resized and stored under `app_uploads_path`, so image follow-ups
and reopened chats can reuse them. Keep that directory when resuming those chats.
The app does not recreate or reindex your existing travel collections.

Markdown rendering uses the existing bundled Marked and DOMPurify libraries.
Raw HTML in model replies is discarded and rendered Markdown is sanitized.

Run the handoff regression tests without model downloads or live API calls:

```cmd
python -m unittest discover -s tests -p test_chat_handoff.py -v
python -m unittest discover -s tests -p test_location_search.py -v
python -m unittest discover -s tests -p test_chat_activity.py -v
```

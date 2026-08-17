# Manual LocalKeyboard Server

Use this helper when manually testing the server side of `ios-feature5-risk1` without running the full Appium workflow.

Start the server:

```bash
python tools/manual_local_keyboard_server.py --host 0.0.0.0 --port 8765
```

The script prints:

- a local Mac URL for curl commands;
- a phone/server URL to enter in the LocalKeyboard host app;
- a pairing token returned by `/pair`.

In the LocalKeyboard app, set the server URL to the printed phone/server URL, for example:

```text
http://192.168.1.9:8765
```

Then pair from the app. The server returns a token that the keyboard can use for event submission.

Interactive commands:

```text
enqueue hello123
return
queue
snapshot
clear
quit
```

Queue RETURN as a separate item:

```text
enqueue hello123
return
```

Do not queue `hello123\n` as one item if you want iOS to treat return as Go/Search.

You can queue initial values on startup:

```bash
python tools/manual_local_keyboard_server.py \
  --host 0.0.0.0 \
  --port 8765 \
  --enqueue hello123 \
  --enqueue "\\n"
```

Useful curl commands:

```bash
curl -X POST http://127.0.0.1:8765/pair

curl -X POST http://127.0.0.1:8765/enqueue \
  -H 'Content-Type: application/json' \
  -d '{"text":"hello123"}'

curl -X POST http://127.0.0.1:8765/enqueue \
  -H 'Content-Type: application/json' \
  -d '{"text":"\n"}'

curl http://127.0.0.1:8765/queue
curl http://127.0.0.1:8765/snapshot
curl http://127.0.0.1:8765/events
```
